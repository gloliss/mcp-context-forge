# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_token_blocklist_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for Token Blocklist Service.

Tests cover:
- Token revocation
- Revocation checking
- Idle timeout enforcement
- Activity tracking
- Automatic cleanup
- Statistics gathering
"""

# Standard
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

# First-Party
from mcpgateway.db import Base, TokenRevocation, EmailUser, utc_now
from mcpgateway.services.token_blocklist_service import TokenBlocklistService, get_token_blocklist_service


@pytest.fixture
def test_db():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    # Create test user
    test_user = EmailUser(email="test@example.com", password_hash="test_hash", full_name="Test User", is_admin=False, is_active=True, auth_provider="local")
    db.add(test_user)
    db.commit()

    yield db

    db.close()
    engine.dispose()


@pytest.fixture
def blocklist_service(test_db):
    """Create blocklist service with test database."""
    return TokenBlocklistService(db=test_db)


class TestTokenRevocation:
    """Tests for token revocation functionality."""

    def test_revoke_token_success(self, blocklist_service, test_db):
        """Test successful token revocation."""
        jti = str(uuid.uuid4())
        # Use utc_now() to ensure consistent timezone handling
        token_expiry = utc_now() + timedelta(minutes=20)

        result = blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout", token_expiry=token_expiry)

        assert result is True

        # Verify token is in database
        revocation = test_db.execute(select(TokenRevocation).where(TokenRevocation.jti == jti)).scalar_one_or_none()

        assert revocation is not None
        assert revocation.jti == jti
        assert revocation.revoked_by == "test@example.com"
        assert revocation.reason == "logout"

        # SQLite doesn't preserve timezone info, so we need to handle both cases
        if revocation.token_expiry.tzinfo is None:
            # Make token_expiry naive for comparison
            token_expiry_naive = token_expiry.replace(tzinfo=None)
            assert abs((revocation.token_expiry - token_expiry_naive).total_seconds()) < 1
        else:
            # Both are timezone-aware
            assert abs((revocation.token_expiry - token_expiry).total_seconds()) < 1

    def test_revoke_token_duplicate(self, blocklist_service, test_db):
        """Test revoking an already revoked token."""
        jti = str(uuid.uuid4())

        # Revoke once
        blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout")

        # Revoke again - should succeed (idempotent)
        result = blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout")

        assert result is True

    def test_revoke_token_fail_if_already_revoked(self, blocklist_service, test_db):
        """Single-use semantics: an already-revoked jti returns False with fail_if_already_revoked."""
        jti = str(uuid.uuid4())

        # First revocation wins
        assert blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="token_refresh", fail_if_already_revoked=True) is True

        # Second attempt loses the compare-and-set
        assert blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="token_refresh", fail_if_already_revoked=True) is False

        # Default behavior stays idempotent
        assert blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout") is True

    def test_revoke_token_duplicate_without_db_session(self, test_db):
        """Test revoking an already revoked token without db session."""
        # Create service without db to test fresh_db_session path
        service = TokenBlocklistService(db=None)
        jti = str(uuid.uuid4())

        # Mock fresh_db_session to use test_db
        with patch("mcpgateway.services.token_blocklist_service.fresh_db_session") as mock_session:
            mock_session.return_value.__enter__.return_value = test_db
            mock_session.return_value.__exit__.return_value = None

            # Revoke once
            service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout")

            # Revoke again - should succeed (idempotent) and hit the duplicate check path
            result = service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout")

            assert result is True

    def test_revoke_token_with_last_activity(self, blocklist_service, test_db):
        """Test token revocation with last activity timestamp."""
        jti = str(uuid.uuid4())
        last_activity = utc_now() - timedelta(minutes=30)

        result = blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="idle_timeout", last_activity=last_activity)

        assert result is True

        revocation = test_db.execute(select(TokenRevocation).where(TokenRevocation.jti == jti)).scalar_one_or_none()

        assert revocation.last_activity is not None
        assert revocation.reason == "idle_timeout"


class TestRevocationCheck:
    """Tests for checking if tokens are revoked."""

    def test_is_token_revoked_true(self, blocklist_service, test_db):
        """Test checking a revoked token."""
        jti = str(uuid.uuid4())

        # Revoke token
        blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout")

        # Check if revoked
        assert blocklist_service.is_token_revoked(jti) is True

    def test_is_token_revoked_false(self, blocklist_service):
        """Test checking a non-revoked token."""
        jti = str(uuid.uuid4())

        assert blocklist_service.is_token_revoked(jti) is False

    @patch("mcpgateway.services.token_blocklist_service.TokenBlocklistService._get_redis_client")
    def test_is_token_revoked_with_redis_cache(self, mock_redis, blocklist_service, test_db):
        """Test revocation check with Redis caching."""
        jti = str(uuid.uuid4())

        # Mock Redis client
        redis_mock = MagicMock()
        redis_mock.exists.return_value = True
        mock_redis.return_value = redis_mock

        # Revoke token (should cache in Redis)
        blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout")

        # Check revocation (should hit Redis cache)
        assert blocklist_service.is_token_revoked(jti) is True

    @patch("mcpgateway.services.token_blocklist_service.TokenBlocklistService._get_redis_client")
    def test_revoke_token_redis_cache_error(self, mock_redis, blocklist_service, test_db):
        """Test token revocation when Redis caching fails."""
        jti = str(uuid.uuid4())

        # Mock Redis client to raise error on setex
        redis_mock = MagicMock()
        redis_mock.setex.side_effect = Exception("Redis connection failed")
        mock_redis.return_value = redis_mock

        # Should still succeed even if Redis caching fails
        result = blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout", token_expiry=utc_now() + timedelta(hours=1))

        assert result is True

        # Verify token is still in database
        revocation = test_db.execute(select(TokenRevocation).where(TokenRevocation.jti == jti)).scalar_one_or_none()
        assert revocation is not None

    def test_is_token_revoked_without_db_session(self):
        """Test checking revocation without db session."""
        service = TokenBlocklistService(db=None)
        jti = str(uuid.uuid4())

        # Revoke token first
        service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout")

        # Check if revoked (should use fresh_db_session)
        assert service.is_token_revoked(jti) is True


class TestIdleTimeout:
    """Tests for idle timeout functionality."""

    def test_check_idle_timeout_exceeded(self, blocklist_service):
        """Test idle timeout detection when exceeded."""
        jti = str(uuid.uuid4())
        last_activity = utc_now() - timedelta(minutes=90)  # 90 minutes ago

        # Default idle timeout is 60 minutes
        with patch("mcpgateway.services.token_blocklist_service.settings") as mock_settings:
            mock_settings.token_idle_timeout = 60

            result = blocklist_service.check_idle_timeout(jti=jti, last_activity=last_activity)

            assert result is True

    def test_check_idle_timeout_not_exceeded(self, blocklist_service):
        """Test idle timeout detection when not exceeded."""
        jti = str(uuid.uuid4())
        last_activity = utc_now() - timedelta(minutes=30)  # 30 minutes ago

        with patch("mcpgateway.services.token_blocklist_service.settings") as mock_settings:
            mock_settings.token_idle_timeout = 60

            result = blocklist_service.check_idle_timeout(jti=jti, last_activity=last_activity)

            assert result is False

    def test_check_idle_timeout_with_custom_time(self, blocklist_service):
        """Test idle timeout with custom current time."""
        jti = str(uuid.uuid4())
        last_activity = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        current_time = datetime(2026, 1, 1, 13, 30, 0, tzinfo=timezone.utc)  # 90 minutes later

        with patch("mcpgateway.services.token_blocklist_service.settings") as mock_settings:
            mock_settings.token_idle_timeout = 60

            result = blocklist_service.check_idle_timeout(jti=jti, last_activity=last_activity, current_time=current_time)

            assert result is True


class TestActivityTracking:
    """Tests for activity tracking functionality."""

    @patch("mcpgateway.services.token_blocklist_service.TokenBlocklistService._get_redis_client")
    def test_update_activity_success(self, mock_redis, blocklist_service):
        """Test updating token activity timestamp."""
        jti = str(uuid.uuid4())

        # Mock Redis client
        redis_mock = MagicMock()
        mock_redis.return_value = redis_mock

        result = blocklist_service.update_activity(jti)

        assert result is True
        redis_mock.setex.assert_called_once()

    @patch("mcpgateway.services.token_blocklist_service.TokenBlocklistService._get_redis_client")
    def test_get_last_activity_from_redis(self, mock_redis, blocklist_service):
        """Test retrieving last activity from Redis."""
        jti = str(uuid.uuid4())
        activity_time = utc_now()

        # Mock Redis client
        redis_mock = MagicMock()
        redis_mock.get.return_value = activity_time.isoformat()
        mock_redis.return_value = redis_mock

        result = blocklist_service.get_last_activity(jti)

        assert result is not None
        assert isinstance(result, datetime)

    @patch("mcpgateway.services.token_blocklist_service.TokenBlocklistService._get_redis_client")
    def test_get_last_activity_not_found(self, mock_redis, blocklist_service):
        """Test retrieving last activity when not found."""
        jti = str(uuid.uuid4())

        # Mock Redis client
        redis_mock = MagicMock()
        redis_mock.get.return_value = None
        mock_redis.return_value = redis_mock

        result = blocklist_service.get_last_activity(jti)

        assert result is None


class TestCleanup:
    """Tests for automatic cleanup functionality."""

    def test_cleanup_expired_tokens(self, blocklist_service, test_db):
        """Test cleanup of expired tokens."""
        # Create expired token (expired 48 hours ago)
        expired_jti = str(uuid.uuid4())
        expired_time = utc_now() - timedelta(hours=48)

        revocation = TokenRevocation(jti=expired_jti, revoked_by="test@example.com", reason="logout", token_expiry=expired_time)
        test_db.add(revocation)

        # Create recent token (expires in future)
        recent_jti = str(uuid.uuid4())
        future_time = utc_now() + timedelta(hours=1)

        revocation2 = TokenRevocation(jti=recent_jti, revoked_by="test@example.com", reason="logout", token_expiry=future_time)
        test_db.add(revocation2)
        test_db.commit()

        # Cleanup with 24-hour retention
        deleted_count = blocklist_service.cleanup_expired_tokens(hours_retention=24)

        assert deleted_count == 1

        # Verify expired token is gone
        expired = test_db.execute(select(TokenRevocation).where(TokenRevocation.jti == expired_jti)).scalar_one_or_none()
        assert expired is None

        # Verify recent token remains
        recent = test_db.execute(select(TokenRevocation).where(TokenRevocation.jti == recent_jti)).scalar_one_or_none()
        assert recent is not None

    def test_cleanup_no_expired_tokens(self, blocklist_service, test_db):
        """Test cleanup when no tokens are expired."""
        # Create recent token
        jti = str(uuid.uuid4())
        future_time = utc_now() + timedelta(hours=1)

        revocation = TokenRevocation(jti=jti, revoked_by="test@example.com", reason="logout", token_expiry=future_time)
        test_db.add(revocation)
        test_db.commit()

        deleted_count = blocklist_service.cleanup_expired_tokens(hours_retention=24)

        assert deleted_count == 0

    def test_cleanup_expired_tokens_without_db_session(self, test_db):
        """Test cleanup of expired tokens without db session."""
        service = TokenBlocklistService(db=None)

        # Create expired token
        expired_jti = str(uuid.uuid4())
        expired_time = utc_now() - timedelta(hours=48)

        revocation = TokenRevocation(jti=expired_jti, revoked_by="test@example.com", reason="logout", token_expiry=expired_time)
        test_db.add(revocation)
        test_db.commit()

        # Mock fresh_db_session to use test_db
        with patch("mcpgateway.services.token_blocklist_service.fresh_db_session") as mock_session:
            mock_session.return_value.__enter__.return_value = test_db
            mock_session.return_value.__exit__.return_value = None

            # Cleanup with 24-hour retention (should use fresh_db_session)
            deleted_count = service.cleanup_expired_tokens(hours_retention=24)

            assert deleted_count == 1

        # Verify token is gone
        expired = test_db.execute(select(TokenRevocation).where(TokenRevocation.jti == expired_jti)).scalar_one_or_none()
        assert expired is None

    def test_cleanup_expired_tokens_with_logging(self, blocklist_service, test_db):
        """Test cleanup logs when tokens are deleted."""
        # Create expired token
        expired_jti = str(uuid.uuid4())
        expired_time = utc_now() - timedelta(hours=48)

        revocation = TokenRevocation(jti=expired_jti, revoked_by="test@example.com", reason="logout", token_expiry=expired_time)
        test_db.add(revocation)
        test_db.commit()

        # Cleanup should log the deletion
        with patch("mcpgateway.services.token_blocklist_service.logger") as mock_logger:
            deleted_count = blocklist_service.cleanup_expired_tokens(hours_retention=24)

            assert deleted_count == 1
            # Verify info log was called
            mock_logger.info.assert_called()


class TestStatistics:
    """Tests for revocation statistics."""

    def test_get_revocation_stats(self, test_db):
        """Test getting revocation statistics."""
        # Create revocations directly in the database
        reasons = ["logout", "logout", "idle_timeout", "security"]

        for reason in reasons:
            jti = str(uuid.uuid4())
            revocation = TokenRevocation(jti=jti, revoked_by="test@example.com", reason=reason)
            test_db.add(revocation)

        test_db.commit()
        test_db.flush()

        # Verify data was actually inserted
        count = test_db.execute(select(func.count()).select_from(TokenRevocation)).scalar()
        assert count == 4, f"Expected 4 revocations in DB, found {count}"

        # Create a service that uses the test database
        service = TokenBlocklistService(db=test_db)

        # Get stats using the same database session
        stats = service.get_revocation_stats()

        assert stats["total_revoked"] == 4
        assert stats["by_reason"]["logout"] == 2
        assert stats["by_reason"]["idle_timeout"] == 1
        assert stats["by_reason"]["security"] == 1

    def test_get_revocation_stats_empty(self, blocklist_service):
        """Test getting statistics when no revocations exist."""
        stats = blocklist_service.get_revocation_stats()

        assert stats["total_revoked"] == 0
        assert stats["by_reason"] == {}

    def test_get_revocation_stats_without_db_session(self, test_db):
        """Test getting revocation statistics without db session."""
        # Create revocations
        reasons = ["logout", "idle_timeout"]

        for reason in reasons:
            jti = str(uuid.uuid4())
            revocation = TokenRevocation(jti=jti, revoked_by="test@example.com", reason=reason)
            test_db.add(revocation)
        test_db.commit()

        # Mock fresh_db_session to use test_db
        service = TokenBlocklistService(db=None)

        with patch("mcpgateway.services.token_blocklist_service.fresh_db_session") as mock_session:
            mock_session.return_value.__enter__.return_value = test_db
            mock_session.return_value.__exit__.return_value = None

            # Get stats without db session (should use fresh_db_session)
            stats = service.get_revocation_stats()

            assert stats["total_revoked"] >= 2
            assert "logout" in stats["by_reason"]
            assert "idle_timeout" in stats["by_reason"]


class TestSingletonService:
    """Tests for singleton service instance."""

    def test_get_token_blocklist_service_singleton(self):
        """Test that service returns singleton instance."""
        service1 = get_token_blocklist_service()
        service2 = get_token_blocklist_service()

        assert service1 is service2

    def test_get_token_blocklist_service_with_db(self, test_db):
        """Test that service creates new instance when db is provided."""
        service1 = get_token_blocklist_service(db=test_db)
        service2 = get_token_blocklist_service(db=test_db)

        # Should be different instances when db is provided
        assert service1 is not service2
        assert service1.db is test_db
        assert service2.db is test_db


class TestErrorHandling:
    """Tests for error handling."""

    def test_revoke_token_database_error(self):
        """Test token revocation with database error."""
        # Create a service with no database (will use fresh_db_session)
        service = TokenBlocklistService(db=None)

        # Mock fresh_db_session to raise an error
        with patch("mcpgateway.services.token_blocklist_service.fresh_db_session") as mock_session:
            mock_session.side_effect = Exception("Database connection failed")

            result = service.revoke_token(jti=str(uuid.uuid4()), revoked_by="test@example.com", reason="logout")

            assert result is False

    def test_is_token_revoked_database_error(self):
        """Test revocation check with database error."""
        # Create a service with no database (will use fresh_db_session)
        service = TokenBlocklistService(db=None)

        # Mock fresh_db_session to raise an error
        with patch("mcpgateway.services.token_blocklist_service.fresh_db_session") as mock_session:
            mock_session.side_effect = Exception("Database connection failed")

            # Should fail closed (treat as revoked on error)
            result = service.is_token_revoked(str(uuid.uuid4()))

    @patch("mcpgateway.services.token_blocklist_service.TokenBlocklistService._get_redis_client")
    def test_is_token_revoked_redis_error_fallback_to_db(self, mock_redis, blocklist_service, test_db):
        """Test revocation check falls back to DB when Redis fails."""
        jti = str(uuid.uuid4())

        # Revoke token in database
        blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout")

        # Mock Redis to raise an error
        redis_mock = MagicMock()
        redis_mock.exists.side_effect = Exception("Redis connection failed")
        mock_redis.return_value = redis_mock

        # Should fall back to database and find the token
        result = blocklist_service.is_token_revoked(jti)

        assert result is True

    def test_is_token_revoked_closes_session(self):
        """Test that is_token_revoked properly closes its own session."""
        service = TokenBlocklistService(db=None)
        jti = str(uuid.uuid4())

        # This should create and close its own session
        result = service.is_token_revoked(jti)

        # Should return True (fail-closed) when database error occurs
        # because fresh_db_session() will fail without proper database setup
        assert result is True

    def test_check_idle_timeout_naive_datetime(self, blocklist_service):
        """Test idle timeout with naive datetime (no timezone)."""
        jti = str(uuid.uuid4())
        # Create naive datetime (no timezone info)
        last_activity = datetime(2026, 1, 1, 12, 0, 0)  # No tzinfo

        with patch("mcpgateway.services.token_blocklist_service.settings") as mock_settings:
            mock_settings.token_idle_timeout = 60

            # Should handle naive datetime by adding UTC timezone
            result = blocklist_service.check_idle_timeout(jti=jti, last_activity=last_activity)

            # Result depends on current time, but should not raise an error
            assert isinstance(result, bool)

    @patch("mcpgateway.services.token_blocklist_service.TokenBlocklistService._get_redis_client")
    def test_update_activity_redis_error(self, mock_redis, blocklist_service):
        """Test update_activity handles Redis errors gracefully."""
        jti = str(uuid.uuid4())

        # Mock Redis to raise an error
        redis_mock = MagicMock()
        redis_mock.setex.side_effect = Exception("Redis connection failed")
        mock_redis.return_value = redis_mock

        # Should handle error and return True (best effort)
        result = blocklist_service.update_activity(jti)

        assert result is True

    def test_update_activity_general_error(self, blocklist_service):
        """Test update_activity handles general errors."""
        jti = str(uuid.uuid4())

        with patch("mcpgateway.services.token_blocklist_service.TokenBlocklistService._get_redis_client") as mock_redis:
            mock_redis.side_effect = Exception("Unexpected error")

            # Should handle error and return False
            result = blocklist_service.update_activity(jti)

            assert result is False

    @patch("mcpgateway.services.token_blocklist_service.TokenBlocklistService._get_redis_client")
    def test_get_last_activity_redis_error(self, mock_redis, blocklist_service):
        """Test get_last_activity handles Redis errors gracefully."""
        jti = str(uuid.uuid4())

        # Mock Redis to raise an error
        redis_mock = MagicMock()
        redis_mock.get.side_effect = Exception("Redis connection failed")
        mock_redis.return_value = redis_mock

        # Should handle error and return None
        result = blocklist_service.get_last_activity(jti)

        assert result is None

    def test_get_last_activity_general_error(self, blocklist_service):
        """Test get_last_activity handles general errors."""
        jti = str(uuid.uuid4())

        with patch("mcpgateway.services.token_blocklist_service.TokenBlocklistService._get_redis_client") as mock_redis:
            mock_redis.side_effect = Exception("Unexpected error")

            # Should handle error and return None
            result = blocklist_service.get_last_activity(jti)

            assert result is None

    def test_cleanup_expired_tokens_error(self, blocklist_service):
        """Test cleanup handles database errors gracefully."""
        with patch.object(blocklist_service.db, "execute") as mock_execute:
            mock_execute.side_effect = Exception("Database error")

            # Should handle error and return 0
            result = blocklist_service.cleanup_expired_tokens()
            assert result == 0

    def test_revoke_user_tokens_placeholder(self, blocklist_service):
        """Test revoke_user_tokens placeholder method."""
        # This is a placeholder method that logs a warning
        result = blocklist_service.revoke_user_tokens(user_email="test@example.com", revoked_by="admin@example.com", reason="security")

        # Should return 0 (not implemented yet)
        assert result == 0

    def test_get_revocation_stats_error(self, blocklist_service):
        """Test get_revocation_stats handles database errors gracefully."""
        with patch.object(blocklist_service.db, "execute") as mock_execute:
            mock_execute.side_effect = Exception("Database error")

            # Should handle error and return empty stats
            result = blocklist_service.get_revocation_stats()
            assert result == {"total_revoked": 0, "by_reason": {}}


class TestRedisClientLifecycle:
    """Cover the Redis-success path of ``_get_redis_client`` (line 76)."""

    def test_get_redis_client_success_logs_connection(self):
        """Successful ping → client cached, success-path debug log fires."""
        mock_redis_module = MagicMock()
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_redis_module.from_url.return_value = mock_client

        service = TokenBlocklistService(db=None)

        with patch.dict(sys.modules, {"redis": mock_redis_module}), patch("mcpgateway.services.token_blocklist_service.settings") as mock_settings:
            mock_settings.redis_url = "redis://localhost:6379/0"
            client = service._get_redis_client()

        assert client is mock_client
        mock_client.ping.assert_called_once()
        mock_redis_module.from_url.assert_called_once()
        assert service._redis_client is mock_client

    def test_get_redis_client_caches_after_first_call(self):
        """Second call returns the cached client without re-importing redis."""
        mock_redis_module = MagicMock()
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_redis_module.from_url.return_value = mock_client

        service = TokenBlocklistService(db=None)

        with patch.dict(sys.modules, {"redis": mock_redis_module}), patch("mcpgateway.services.token_blocklist_service.settings") as mock_settings:
            mock_settings.redis_url = "redis://localhost:6379/0"
            first = service._get_redis_client()
            second = service._get_redis_client()

        assert first is second
        mock_redis_module.from_url.assert_called_once()


class TestIsTokenRevokedFreshSession:
    """Cover ``is_token_revoked`` ``fresh_db_session`` return path (line 189)."""

    def test_returns_false_when_no_revocation_via_fresh_session(self):
        service = TokenBlocklistService(db=None)
        service._redis_client = False  # short-circuit Redis path

        @contextmanager
        def fake_fresh_session():
            session = MagicMock()
            session.execute.return_value.scalar_one_or_none.return_value = None
            yield session

        with patch(
            "mcpgateway.services.token_blocklist_service.fresh_db_session",
            side_effect=fake_fresh_session,
        ):
            result = service.is_token_revoked(str(uuid.uuid4()))

        assert result is False

    def test_returns_true_when_revocation_present_via_fresh_session(self):
        service = TokenBlocklistService(db=None)
        service._redis_client = False

        @contextmanager
        def fake_fresh_session():
            session = MagicMock()
            session.execute.return_value.scalar_one_or_none.return_value = MagicMock(spec=TokenRevocation)
            yield session

        with patch(
            "mcpgateway.services.token_blocklist_service.fresh_db_session",
            side_effect=fake_fresh_session,
        ):
            result = service.is_token_revoked(str(uuid.uuid4()))

        assert result is True


class _FakeRedis:
    """Sync-write / async-read Redis stub shared between two simulated workers.

    ``revoke_token`` publishes the cross-worker marker through the blocklist's
    *synchronous* client, while ``AuthCache.get_auth_context`` reads it through
    its own *asynchronous* client, so the stub exposes both surfaces over one
    keyspace.
    """

    def __init__(self):
        self.kv: dict = {}
        self.sets: dict = {}

    # --- synchronous surface (AuthCache.mark_revoked_sync) ---
    def setex(self, key, ttl, value):
        self.kv[key] = value

    def sadd(self, name, value):
        self.sets.setdefault(name, set()).add(value)

    # --- asynchronous surface (AuthCache.get_auth_context) ---
    async def exists(self, key):
        return key in self.kv


@pytest.fixture
def primed_cache():
    """An ``AuthCache`` holding a stale "this token is fine" entry."""
    # First-Party
    from mcpgateway.cache.auth_cache import AuthCache, CachedAuthContext, CacheEntry

    cache = AuthCache(enabled=True)
    email, jti = "test@example.com", str(uuid.uuid4())
    cache._context_cache[f"{email}:{jti}"] = CacheEntry(value=CachedAuthContext(user={"email": email}, is_token_revoked=False), expiry=time.time() + 30)
    cache._revocation_cache[jti] = CacheEntry(value=False, expiry=time.time() + 30)
    return cache, email, jti


class TestRevocationInvalidatesAuthCache:
    """A revoked token must stop being accepted immediately.

    Regression for the ~30s window in which ``AuthCache`` kept serving a
    ``is_token_revoked=False`` entry cached by a prior authenticated request, so
    a logged-out session token still authorised the Admin UI until the cache TTL
    (``auth_cache_revocation_ttl``, 30s) expired.

    These tests run without a running event loop, which also covers the
    worker-thread call path (session rotation uses ``asyncio.to_thread``): the
    invalidation must not depend on a loop being available.
    """

    def test_revoke_token_evicts_cached_not_revoked_context(self, blocklist_service, primed_cache):
        """The cached negative entry and context must be gone after revoke."""
        cache, email, jti = primed_cache

        with patch("mcpgateway.cache.auth_cache.get_auth_cache", return_value=cache):
            assert blocklist_service.revoke_token(jti=jti, revoked_by=email, reason="admin_logout") is True

        assert jti in cache._revoked_jtis
        assert f"{email}:{jti}" not in cache._context_cache
        assert jti not in cache._revocation_cache

    def test_get_auth_context_flips_to_revoked_immediately(self, blocklist_service, primed_cache):
        """Deny-path: the stale 'valid' context must flip to revoked right away."""
        cache, email, jti = primed_cache

        # Before revocation the cached negative entry is served (the bug's origin).
        before = asyncio.run(cache.get_auth_context(email, jti))
        assert before is not None
        assert before.is_token_revoked is False

        with patch("mcpgateway.cache.auth_cache.get_auth_cache", return_value=cache):
            blocklist_service.revoke_token(jti=jti, revoked_by=email, reason="admin_logout")

        # After revocation the very next lookup must report the token as revoked,
        # without waiting for the ~30s TTL.
        after = asyncio.run(cache.get_auth_context(email, jti))
        assert after is not None
        assert after.is_token_revoked is True

    def test_cross_worker_marker_reaches_a_second_cache(self, blocklist_service, primed_cache):
        """A *different* worker's cache must learn about the revocation.

        This is the contract the local-only L1 eviction cannot satisfy: worker B
        still holds a stale "not revoked" context and only consults the shared
        Redis marker. It also covers the worker-thread path — the revoke below
        runs with no event loop, which is exactly the session-rotation case.
        """
        worker_b, email, jti = primed_cache
        shared_redis = _FakeRedis()

        # Sanity: with no marker published, worker B happily serves the stale entry.
        with patch.object(worker_b, "_get_redis_client", new=AsyncMock(return_value=shared_redis)):
            stale = asyncio.run(worker_b.get_auth_context(email, jti))
            assert stale is not None
            assert stale.is_token_revoked is False

        # Worker A revokes. No running loop here => the sync-only path.
        # First-Party
        from mcpgateway.cache.auth_cache import AuthCache

        worker_a = AuthCache(enabled=True)
        with (
            patch("mcpgateway.cache.auth_cache.get_auth_cache", return_value=worker_a),
            patch.object(blocklist_service, "_get_redis_client", return_value=shared_redis),
        ):
            assert blocklist_service.revoke_token(jti=jti, revoked_by=email, reason="token_refresh") is True

        # The marker must have been published through the sync client...
        assert shared_redis.kv.get(worker_a._get_redis_key("revoke", jti)) == "1"

        # ...and worker B must now reject the token despite its stale L1 entry.
        with patch.object(worker_b, "_get_redis_client", new=AsyncMock(return_value=shared_redis)):
            after = asyncio.run(worker_b.get_auth_context(email, jti))
            assert after is not None
            assert after.is_token_revoked is True

    def test_marker_written_when_revoked_from_a_worker_thread(self, primed_cache):
        """Session rotation calls revoke_token inside ``asyncio.to_thread``.

        That thread has no running loop, so the cross-worker marker must be
        published synchronously rather than scheduled onto a loop. The DB session
        is stubbed out: the production concern here is the loop/thread context,
        not the persistence (covered by the tests above).
        """
        cache, email, jti = primed_cache
        shared_redis = _FakeRedis()
        service = TokenBlocklistService(db=None)

        @contextmanager
        def fake_fresh_session():
            session = MagicMock()
            session.execute.return_value.scalar_one_or_none.return_value = None
            yield session

        async def _revoke_in_worker_thread():
            with (
                patch("mcpgateway.cache.auth_cache.get_auth_cache", return_value=cache),
                patch.object(service, "_get_redis_client", return_value=shared_redis),
                patch("mcpgateway.services.token_blocklist_service.fresh_db_session", side_effect=fake_fresh_session),
            ):
                return await asyncio.to_thread(service.revoke_token, jti=jti, revoked_by=email, reason="token_refresh")

        assert asyncio.run(_revoke_in_worker_thread()) is True
        assert shared_redis.kv.get(cache._get_redis_key("revoke", jti)) == "1"
        assert jti in shared_redis.sets.get("mcpgw:auth:revoked_tokens", set())

    def test_idempotent_rerevocation_still_evicts(self, blocklist_service, primed_cache):
        """An already-revoked jti must still evict this worker's stale context."""
        cache, email, jti = primed_cache

        # First revocation records the jti in the DB.
        with patch("mcpgateway.cache.auth_cache.get_auth_cache", return_value=cache):
            assert blocklist_service.revoke_token(jti=jti, revoked_by=email, reason="admin_logout") is True

        # Re-prime a stale entry, then revoke the same jti again: the second call
        # takes the "already revoked" early return and used to skip invalidation.
        # First-Party
        from mcpgateway.cache.auth_cache import CachedAuthContext, CacheEntry

        cache._context_cache[f"{email}:{jti}"] = CacheEntry(value=CachedAuthContext(user={"email": email}, is_token_revoked=False), expiry=time.time() + 30)
        cache._revoked_jtis.discard(jti)

        with patch("mcpgateway.cache.auth_cache.get_auth_cache", return_value=cache):
            assert blocklist_service.revoke_token(jti=jti, revoked_by=email, reason="idle_timeout") is True

        assert jti in cache._revoked_jtis
        assert f"{email}:{jti}" not in cache._context_cache

    def test_invalidate_failure_does_not_break_revocation(self, blocklist_service):
        """Cache invalidation is best-effort; revocation itself must still succeed."""
        jti = str(uuid.uuid4())

        with patch("mcpgateway.cache.auth_cache.get_auth_cache", side_effect=RuntimeError("cache unavailable")):
            assert blocklist_service.revoke_token(jti=jti, revoked_by="test@example.com", reason="logout") is True
