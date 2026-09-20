# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/database_source_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unified database source management (OB-01).

CRUD for the ``database_sources`` domain model that underpins the
OceanBase/Oracle/MySQL/PostgreSQL adapters.  Passwords are encrypted at rest
by the ``EncryptedText`` column type (reusing the existing Encryption
Service), never returned by the read model, and preserved when an update does
not supply a new credential.
"""

# Standard
from datetime import datetime, timezone
from typing import Optional

# Third-Party
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import DatabaseSource
from mcpgateway.schemas import DatabaseSourceCreate, DatabaseSourceRead, DatabaseSourceUpdate
from mcpgateway.services.logging_service import LoggingService
from mcpgateway.utils.create_slug import slugify

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

_VALID_ENGINES = {"oceanbase", "oracle", "mysql", "postgresql"}
_VALID_COMPAT_MODES = {"mysql", "oracle"}


class DatabaseSourceError(ValueError):
    """Base error for database source operations."""


class DatabaseSourceNotFoundError(DatabaseSourceError):
    """Raised when a requested database source is not found."""


class DatabaseSourceNameConflictError(DatabaseSourceError):
    """Raised when a database source name or slug conflicts with an existing one."""

    def __init__(self, name: str, slug: str):
        """Initialize the conflict error.

        Args:
            name: The conflicting display name.
            slug: The conflicting slug.
        """
        self.name = name
        self.slug = slug
        super().__init__(f"Database source with name '{name}' or slug '{slug}' already exists")


def _validate_engine_compat(engine: str, compatibility_mode: Optional[str]) -> None:
    """Enforce the engine/compatibility-mode modeling rule on a merged state.

    OceanBase is a single ``oceanbase`` engine whose tenant mode is captured
    in ``compatibility_mode`` (``mysql`` or ``oracle``).  Other engines must
    not carry a compatibility mode.

    Args:
        engine: The effective engine value.
        compatibility_mode: The effective compatibility mode value.

    Raises:
        DatabaseSourceError: If the combination is invalid.
    """
    if engine not in _VALID_ENGINES:
        raise DatabaseSourceError(f"Unsupported engine: {engine!r}")
    if engine == "oceanbase":
        if compatibility_mode not in _VALID_COMPAT_MODES:
            raise DatabaseSourceError("compatibility_mode must be 'mysql' or 'oracle' for engine 'oceanbase'")
    elif compatibility_mode is not None:
        raise DatabaseSourceError("compatibility_mode is only valid for engine 'oceanbase'")


class DatabaseSourceService:
    """Manage unified database source records."""

    @staticmethod
    def _conflicting_source(db: Session, name: str, slug: str, exclude_id: Optional[str] = None) -> Optional[DatabaseSource]:
        """Return a source whose name or slug collides with the given pair.

        Args:
            db: Database session.
            name: Candidate display name.
            slug: Candidate slug.
            exclude_id: Optional ID to exclude (for updates).

        Returns:
            The conflicting source, or ``None`` when no collision exists.
        """
        clause = or_(DatabaseSource.name == name, DatabaseSource.slug == slug)
        if exclude_id is not None:
            clause = and_(clause, DatabaseSource.id != exclude_id)
        return db.execute(select(DatabaseSource).where(clause)).scalar_one_or_none()

    @classmethod
    def create_source(cls, db: Session, data: DatabaseSourceCreate, user_email: Optional[str] = None) -> DatabaseSourceRead:
        """Persist a new validated source with an encrypted credential.

        Args:
            db: Database session (transaction owned by the caller).
            data: Validated creation payload.
            user_email: Email of the creating user.

        Returns:
            DatabaseSourceRead: The credential-free read model.

        Raises:
            DatabaseSourceNameConflictError: If the name or slug already exists.
        """
        slug = slugify(data.name)
        existing = cls._conflicting_source(db, data.name, slug)
        if existing is not None:
            raise DatabaseSourceNameConflictError(data.name, slug)

        password_value = data.password.get_secret_value() if data.password else None
        source = DatabaseSource(
            name=data.name,
            slug=slug,
            description=data.description,
            engine=data.engine,
            compatibility_mode=data.compatibility_mode,
            host=data.host,
            port=data.port,
            cluster_name=data.cluster_name,
            tenant_name=data.tenant_name,
            database_name=data.database_name,
            schema_name=data.schema_name,
            username=data.username,
            password=password_value,
            credential_encrypted=bool(password_value),
            ssl_mode=data.ssl_mode,
            charset=data.charset,
            timezone=data.timezone,
            connection_config=data.connection_config or {},
            pool_config=data.pool_config or {},
            policy_config=data.policy_config or {},
            tool_config=data.tool_config or {},
            enabled=data.enabled,
            created_by=user_email,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(source)
        db.commit()
        db.refresh(source)
        logger.info("Created database source %s (engine=%s)", source.name, source.engine)
        return DatabaseSourceRead.model_validate(source)

    @classmethod
    def list_sources(cls, db: Session, include_inactive: bool = False) -> list[DatabaseSourceRead]:
        """List sources, hiding disabled ones unless ``include_inactive``.

        Args:
            db: Database session.
            include_inactive: When true, also return disabled sources.

        Returns:
            list[DatabaseSourceRead]: The credential-free read models.
        """
        query = select(DatabaseSource).order_by(desc(DatabaseSource.created_at), desc(DatabaseSource.id))
        if not include_inactive:
            query = query.where(DatabaseSource.enabled.is_(True))
        rows = db.execute(query).scalars().all()
        return [DatabaseSourceRead.model_validate(row) for row in rows]

    @classmethod
    def get_source(cls, db: Session, source_id: str) -> DatabaseSourceRead:
        """Fetch one source by ID.

        Args:
            db: Database session.
            source_id: Source ID.

        Returns:
            DatabaseSourceRead: The credential-free read model.

        Raises:
            DatabaseSourceNotFoundError: If no such source exists.
        """
        source = db.get(DatabaseSource, source_id)
        if source is None:
            raise DatabaseSourceNotFoundError(f"Database source with ID '{source_id}' not found")
        return DatabaseSourceRead.model_validate(source)

    @classmethod
    def update_source(
        cls,
        db: Session,
        source_id: str,
        data: DatabaseSourceUpdate,
        user_email: Optional[str] = None,
    ) -> DatabaseSourceRead:
        """Update a source, preserving the credential when none is supplied.

        Args:
            db: Database session.
            source_id: Source ID.
            data: Update payload.
            user_email: Email of the updating user.

        Returns:
            DatabaseSourceRead: The credential-free read model.

        Raises:
            DatabaseSourceNotFoundError: If no such source exists.
            DatabaseSourceNameConflictError: If the new name/slug collides.
            DatabaseSourceError: If the merged engine/compatibility-mode state
                is invalid.
        """
        source = db.get(DatabaseSource, source_id)
        if source is None:
            raise DatabaseSourceNotFoundError(f"Database source with ID '{source_id}' not found")

        new_password = data.password
        values = data.model_dump(exclude_unset=True, exclude={"password"})

        if "name" in values and values["name"] != source.name:
            new_slug = slugify(values["name"])
            existing = cls._conflicting_source(db, values["name"], new_slug, exclude_id=source_id)
            if existing is not None:
                raise DatabaseSourceNameConflictError(values["name"], new_slug)

        if new_password is not None:
            plaintext = new_password.get_secret_value()
            if plaintext and plaintext != settings.masked_auth_value:
                source.password = plaintext
                source.credential_encrypted = True

        for field, value in values.items():
            setattr(source, field, value)

        if "name" in values:
            source.slug = slugify(source.name)

        _validate_engine_compat(source.engine, source.compatibility_mode)

        source.updated_at = datetime.now(timezone.utc)
        if user_email is not None:
            source.modified_by = user_email
        source.version += 1

        db.commit()
        db.refresh(source)
        logger.info("Updated database source %s", source.name)
        return DatabaseSourceRead.model_validate(source)

    @classmethod
    def delete_source(cls, db: Session, source_id: str) -> None:
        """Delete a source.

        Args:
            db: Database session.
            source_id: Source ID.

        Raises:
            DatabaseSourceNotFoundError: If no such source exists.
        """
        source = db.get(DatabaseSource, source_id)
        if source is None:
            raise DatabaseSourceNotFoundError(f"Database source with ID '{source_id}' not found")
        db.delete(source)
        db.commit()
        logger.info("Deleted database source %s", source.name)
