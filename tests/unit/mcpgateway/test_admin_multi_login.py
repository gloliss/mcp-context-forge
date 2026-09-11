# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_admin_multi_login.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Regression tests for the Admin UI multi-session guarantee.

The Admin UI issues stateless session JWTs: every successful login mints a
fresh ``jti`` and no login path revokes a previously issued session. These
tests lock in the requirement "一处登陆不会将另外一处的 admin 账号顶掉"
(issue #2) so that a future single-session change would fail loudly here.
"""

# Standard
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

# Third-Party
import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

# First-Party
from mcpgateway.admin import admin_login_handler, enforce_admin_csrf
from mcpgateway.config import settings


@pytest.fixture
def mock_db() -> MagicMock:
    """A minimal DB session stand-in; the login handler passes it to the patched
    ``EmailAuthService`` and never touches it directly on the happy path."""
    return MagicMock()


def _login_request(email: str = "admin@test.com") -> MagicMock:
    """Build a login-form request for ``admin_login_handler``."""
    request = MagicMock(spec=Request)
    request.scope = {"root_path": ""}
    request.form = AsyncMock(return_value={"email": email, "password": "secret123"})  # pragma: allowlist secret
    return request


def _configure_login_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the login handler at a local-password, non-SSO, non-enforcement path."""
    monkeypatch.setattr("mcpgateway.admin.settings.email_auth_enabled", True, raising=False)
    monkeypatch.setattr("mcpgateway.admin.settings.password_change_enforcement_enabled", False, raising=False)
    monkeypatch.setattr("mcpgateway.admin.settings.sso_enabled", False, raising=False)
    monkeypatch.setattr("mcpgateway.admin.settings.sso_preserve_admin_auth", True, raising=False)
    monkeypatch.setattr("mcpgateway.admin.settings.secure_cookies", False, raising=False)
    monkeypatch.setattr("mcpgateway.admin.settings.environment", "development", raising=False)
    monkeypatch.setattr("mcpgateway.admin._set_admin_csrf_cookie", lambda *a, **k: None, raising=False)


def _mock_authenticated_user(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Make ``EmailAuthService.authenticate_user`` return a non-forced-change admin."""
    user = MagicMock()
    user.password_change_required = False
    auth_service = MagicMock()
    auth_service.authenticate_user = AsyncMock(return_value=user)
    monkeypatch.setattr("mcpgateway.admin.EmailAuthService", lambda db: auth_service)
    return user


def _jwt_secret() -> str:
    """Resolve the configured JWT signing secret to a plain string."""
    secret = settings.jwt_secret_key
    return secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret


def _build_admin_jwt(expires_in_minutes: int = 20) -> tuple[str, dict]:
    """Build a signed admin session JWT and return ``(token, payload)``."""
    now = datetime.now(timezone.utc)
    payload = {
        "email": "admin@example.com",
        "exp": int((now + timedelta(minutes=expires_in_minutes)).timestamp()),
        "iat": int(now.timestamp()),
        "last_activity": int(now.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, _jwt_secret(), algorithm=settings.jwt_algorithm)
    return token, payload


class TestAdminMultiLogin:
    """A second login must mint an independent session and never revoke the first."""

    @pytest.mark.asyncio
    async def test_two_logins_mint_distinct_jti(self, monkeypatch: pytest.MonkeyPatch, mock_db: MagicMock) -> None:
        """Each login produces its own fresh ``jti``, so sessions are independent."""
        _configure_login_settings(monkeypatch)
        _mock_authenticated_user(monkeypatch)
        monkeypatch.setattr("mcpgateway.admin.set_auth_cookie", lambda resp, token, remember_me=False: None)

        create_token = AsyncMock(return_value=("fake-token", None))
        monkeypatch.setattr("mcpgateway.admin.create_access_token", create_token)

        first = await admin_login_handler(_login_request(), mock_db)
        second = await admin_login_handler(_login_request(), mock_db)

        assert isinstance(first, RedirectResponse) and first.status_code == 303
        assert isinstance(second, RedirectResponse) and second.status_code == 303
        assert create_token.call_count == 2

        jtis = [call.kwargs.get("jti") for call in create_token.call_args_list]
        assert all(isinstance(j, str) and j for j in jtis)
        # Each login mints a fresh UUID session id.
        assert all(str(uuid.UUID(j)) == j for j in jtis)
        # The two sessions are distinct — a second login does not reuse or overwrite
        # the first session's identity.
        assert jtis[0] != jtis[1]

    @pytest.mark.asyncio
    async def test_login_path_never_revokes_other_sessions(self, monkeypatch: pytest.MonkeyPatch, mock_db: MagicMock) -> None:
        """The login handler must never call the blocklist — a new login cannot kick
        out an already-active session elsewhere."""
        _configure_login_settings(monkeypatch)
        _mock_authenticated_user(monkeypatch)
        monkeypatch.setattr("mcpgateway.admin.set_auth_cookie", lambda resp, token, remember_me=False: None)
        monkeypatch.setattr("mcpgateway.admin.create_access_token", AsyncMock(return_value=("fake-token", None)))

        with patch("mcpgateway.services.token_blocklist_service.get_token_blocklist_service") as mock_get_service:
            mock_blocklist = MagicMock()
            mock_blocklist.revoke_token.return_value = True
            mock_get_service.return_value = mock_blocklist

            result = await admin_login_handler(_login_request(), mock_db)

        assert isinstance(result, RedirectResponse) and result.status_code == 303
        # No revocation happens on the login path: only logout / idle-timeout / explicit
        # admin actions revoke a specific ``jti``.
        mock_blocklist.revoke_token.assert_not_called()


class TestAdminMultiSessionLogoutIsolation:
    """AC-2: logging out one session must not revoke the account's other sessions."""

    @pytest.fixture
    def client(self, main_app_with_admin_api: FastAPI) -> TestClient:
        return TestClient(main_app_with_admin_api, follow_redirects=False)

    @pytest.fixture
    def disable_admin_csrf(self, main_app_with_admin_api: FastAPI) -> Generator[None, None, None]:
        """Bypass admin CSRF for this happy-path route test."""

        async def _noop():
            return None

        main_app_with_admin_api.dependency_overrides[enforce_admin_csrf] = _noop
        try:
            yield
        finally:
            main_app_with_admin_api.dependency_overrides.pop(enforce_admin_csrf, None)

    def test_logout_revokes_only_the_current_session(self, client: TestClient, disable_admin_csrf: None) -> None:
        """POST /admin/logout must revoke exactly the calling session's ``jti`` and
        leave the account's other live session untouched."""
        session_a, payload_a = _build_admin_jwt()
        session_b, payload_b = _build_admin_jwt()
        assert payload_a["jti"] != payload_b["jti"]

        with (
            patch("mcpgateway.admin.verify_jwt_token_cached", new_callable=AsyncMock) as mock_verify,
            patch("mcpgateway.services.token_blocklist_service.get_token_blocklist_service") as mock_get_service,
        ):
            # The request carries session A.
            mock_verify.return_value = payload_a
            mock_blocklist = MagicMock()
            mock_blocklist.revoke_token.return_value = True
            mock_get_service.return_value = mock_blocklist

            client.cookies.set("jwt_token", session_a)
            try:
                response = client.post("/admin/logout")
            finally:
                del client.cookies["jwt_token"]

            assert response.status_code in (302, 303, 307, 200)
            mock_blocklist.revoke_token.assert_called_once()
            revoked_jti = mock_blocklist.revoke_token.call_args.kwargs["jti"]
            # Session A is revoked...
            assert revoked_jti == payload_a["jti"]
            # ...while the account's other session (B) is never touched.
            assert revoked_jti != payload_b["jti"]
