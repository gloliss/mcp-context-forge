# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_database_source_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the unified database source CRUD service (OB-01).

Covers create/get/list/update/delete, duplicate slug/name rejection,
engine/compatibility-mode validation, and the password-at-rest /
password-masking guarantees.
"""

# Standard
import uuid

# Third-Party
import pytest
from pydantic import ValidationError
from sqlalchemy import text

# First-Party
from mcpgateway.config import settings
from mcpgateway.schemas import DatabaseSourceCreate, DatabaseSourceUpdate
from mcpgateway.services.database_source_service import (
    DatabaseSourceNameConflictError,
    DatabaseSourceNotFoundError,
    DatabaseSourceService,
)
from mcpgateway.services.encryption_service import get_encryption_service

_ENGINE = "oceanbase"
_COMPAT = "mysql"


def _payload(name: str | None = None, **overrides) -> DatabaseSourceCreate:
    """Build a valid OceanBase/MySQL creation payload, overridable per-test."""
    data = {
        "name": name or f"db-source-{uuid.uuid4().hex[:12]}",
        "engine": _ENGINE,
        "compatibility_mode": _COMPAT,
        "host": "127.0.0.1",
        "port": 2881,
        "database_name": "test",
    }
    data.update(overrides)
    return DatabaseSourceCreate(**data)


def _stored_password(db, source_id: str) -> str:
    """Return the raw (unprocessed) password column value for a source."""
    return db.execute(text("SELECT password FROM database_sources WHERE id = :id"), {"id": source_id}).scalar()


def test_create_source(test_db):
    """Creating a source persists fields, slug, and encrypted-credential flag."""
    read = DatabaseSourceService.create_source(test_db, _payload(name="Prod DB", password="s3cr3t"), "owner@example.com")

    assert read.id
    assert read.name == "Prod DB"
    assert read.slug == "prod-db"
    assert read.engine == "oceanbase"
    assert read.compatibility_mode == "mysql"
    assert read.host == "127.0.0.1"
    assert read.port == 2881
    assert read.credential_encrypted is True
    assert read.enabled is True
    assert "password" not in read.model_dump()


def test_get_source(test_db):
    """Getting a source returns the same credential-free record."""
    created = DatabaseSourceService.create_source(test_db, _payload(), "owner@example.com")

    fetched = DatabaseSourceService.get_source(test_db, created.id)

    assert fetched.id == created.id
    assert fetched.name == created.name
    assert fetched.slug == created.slug
    assert fetched.engine == created.engine
    assert "password" not in fetched.model_dump()


def test_list_sources(test_db):
    """Listing hides disabled sources unless include_inactive is set."""
    enabled = DatabaseSourceService.create_source(test_db, _payload(name=f"en-{uuid.uuid4().hex[:8]}"))
    disabled = DatabaseSourceService.create_source(test_db, _payload(name=f"dis-{uuid.uuid4().hex[:8]}", enabled=False))

    active_names = {s.name for s in DatabaseSourceService.list_sources(test_db)}
    all_names = {s.name for s in DatabaseSourceService.list_sources(test_db, include_inactive=True)}

    assert enabled.name in active_names
    assert disabled.name not in active_names
    assert disabled.name in all_names


def test_update_source(test_db):
    """Updating refreshes the slug and preserves the credential when omitted."""
    created = DatabaseSourceService.create_source(test_db, _payload(name="Before", password="s3cr3t"), "owner@example.com")

    updated = DatabaseSourceService.update_source(
        test_db,
        created.id,
        DatabaseSourceUpdate(name="After Rename", description="updated"),
        "owner@example.com",
    )

    assert updated.name == "After Rename"
    assert updated.slug == "after-rename"
    assert updated.description == "updated"
    assert updated.credential_encrypted is True
    assert _stored_password(test_db, created.id) is not None
    assert get_encryption_service(settings.auth_encryption_secret).decrypt_secret(_stored_password(test_db, created.id)) == "s3cr3t"


def test_update_source_rotates_password(test_db):
    """Supplying a new password rotates the encrypted credential."""
    created = DatabaseSourceService.create_source(test_db, _payload(password="old-pass"))

    DatabaseSourceService.update_source(test_db, created.id, DatabaseSourceUpdate(password="new-pass"), "owner@example.com")

    assert get_encryption_service(settings.auth_encryption_secret).decrypt_secret(_stored_password(test_db, created.id)) == "new-pass"


def test_delete_source(test_db):
    """Deleting a source removes it so a subsequent get raises."""
    created = DatabaseSourceService.create_source(test_db, _payload())

    DatabaseSourceService.delete_source(test_db, created.id)

    with pytest.raises(DatabaseSourceNotFoundError):
        DatabaseSourceService.get_source(test_db, created.id)


def test_duplicate_slug(test_db):
    """A name colliding on slug or exact name is rejected."""
    token = uuid.uuid4().hex[:8]
    DatabaseSourceService.create_source(test_db, _payload(name=f"Prod {token}"))

    with pytest.raises(DatabaseSourceNameConflictError):
        DatabaseSourceService.create_source(test_db, _payload(name=f"prod-{token}"))

    with pytest.raises(DatabaseSourceNameConflictError):
        DatabaseSourceService.create_source(test_db, _payload(name=f"Prod {token}"))


def test_invalid_engine(test_db):
    """Composite OceanBase engine names and unknown engines are rejected."""
    with pytest.raises(ValidationError):
        _payload(engine="oceanbase_mysql")
    with pytest.raises(ValidationError):
        _payload(engine="mongodb")


def test_invalid_compatibility_mode(test_db):
    """Compatibility-mode is required for OceanBase and forbidden otherwise."""
    with pytest.raises(ValidationError):
        DatabaseSourceCreate(
            name="no-compat",
            engine="oceanbase",
            host="127.0.0.1",
            port=2881,
        )
    with pytest.raises(ValidationError):
        DatabaseSourceCreate(
            name="bad-compat",
            engine="oceanbase",
            compatibility_mode="postgresql",
            host="127.0.0.1",
            port=2881,
        )
    with pytest.raises(ValidationError):
        DatabaseSourceCreate(
            name="mysql-with-compat",
            engine="mysql",
            compatibility_mode="oracle",
            host="127.0.0.1",
            port=3306,
        )


def test_password_encryption(test_db):
    """The password is encrypted at rest and decryptable via the service."""
    read = DatabaseSourceService.create_source(test_db, _payload(password="s3cr3t-password"))

    stored = _stored_password(test_db, read.id)
    enc = get_encryption_service(settings.auth_encryption_secret)

    assert stored is not None
    assert stored != "s3cr3t-password"
    assert enc.is_encrypted(stored) is True
    assert enc.decrypt_secret(stored) == "s3cr3t-password"


def test_password_masking(test_db):
    """Passwords never appear in create/get/list/update responses."""
    created = DatabaseSourceService.create_source(test_db, _payload(password="s3cr3t"))

    assert "password" not in created.model_dump()
    assert "password" not in created.model_dump(mode="json")
    assert "password" not in DatabaseSourceService.get_source(test_db, created.id).model_dump()
    assert "password" not in DatabaseSourceService.update_source(
        test_db, created.id, DatabaseSourceUpdate(description="masked")
    ).model_dump()
