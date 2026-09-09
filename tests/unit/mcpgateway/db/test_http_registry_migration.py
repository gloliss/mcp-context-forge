# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/db/test_http_registry_migration.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the HTTP registry migration (PR3).
"""

# Standard
import importlib
import inspect as pyinspect
from typing import Any

# Third-Party
from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

# First-Party
from mcpgateway.db import HttpSchemaArtifact, HttpService, Tool

MODULE_NAME = "mcpgateway.alembic.versions.a4b5c6d7e8f9_add_http_registry"
REVISION = "a4b5c6d7e8f9"
DOWN_REVISION = "9f3e2d1c4b5a"
TOOLS_TABLE = "tools"
SERVICE_TABLE = "http_services"
ARTIFACT_TABLE = "http_schema_artifacts"


def _run_migration(conn, operation: str) -> None:
    """Run one migration entrypoint against a live connection."""
    context = MigrationContext.configure(conn, opts={"as_sql": False})
    with Operations.context(context):
        migration = importlib.import_module(MODULE_NAME)
        getattr(migration, operation)()


def _create_pre_migration_schema(conn) -> None:
    """Create the minimal tables required by the migration."""
    conn.execute(sa.text("CREATE TABLE email_teams (id VARCHAR(36) PRIMARY KEY)"))
    conn.execute(sa.text("CREATE TABLE tools (id VARCHAR(36) PRIMARY KEY, original_name VARCHAR(255) NOT NULL)"))
    conn.execute(sa.text("INSERT INTO tools (id, original_name) VALUES ('tool-existing', 'GET /ping')"))
    conn.commit()


def _column_names(conn, table_name: str) -> set[str]:
    """Return reflected columns for a table."""
    return {column["name"] for column in sa.inspect(conn).get_columns(table_name)}


def _index_names(conn, table_name: str) -> set[str]:
    """Return reflected indexes for a table."""
    return {index["name"] for index in sa.inspect(conn).get_indexes(table_name)}


def _tool_foreign_keys(conn) -> list[dict[str, Any]]:
    """Return reflected foreign keys for HTTP provenance columns."""
    return [
        foreign_key
        for foreign_key in sa.inspect(conn).get_foreign_keys(TOOLS_TABLE)
        if set(foreign_key.get("constrained_columns") or []).issubset({"http_service_id", "http_schema_artifact_id"})
    ]


def _insert_service_and_artifact(conn) -> None:
    """Insert a service/artifact pair for on-delete behaviour checks."""
    conn.execute(
        sa.text(
            "INSERT INTO http_services (id, name, slug, base_url, discovery_config, runtime_config, "
            "discovered_operations, tags, created_at, updated_at) "
            "VALUES ('service-1', 'n', 's', 'http://x', '{}', '{}', '{}', '[]', '2026-09-09', '2026-09-09')"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO http_schema_artifacts (id, http_service_id, version, source_type, artifact_format, "
            "content_hash, artifact_blob, source_info, is_active, created_at) "
            "VALUES ('artifact-1', 'service-1', 1, 'openapi', 'json', 'hash', x'00', '{}', 1, '2026-09-09')"
        )
    )


class TestHttpRegistryMigrationStructure:
    """Verify migration metadata and ORM relationships."""

    def test_migration_metadata(self):
        """Revision metadata points to the verified previous head."""
        migration = importlib.import_module(MODULE_NAME)

        assert migration.revision == REVISION
        assert migration.down_revision == DOWN_REVISION
        assert len(pyinspect.signature(migration.upgrade).parameters) == 0
        assert len(pyinspect.signature(migration.downgrade).parameters) == 0

    def test_orm_relationships_are_bidirectional(self):
        """Tool provenance exposes clear relationships from both ORM models."""
        service_foreign_keys = Tool.__table__.c.http_service_id.foreign_keys
        artifact_foreign_keys = Tool.__table__.c.http_schema_artifact_id.foreign_keys

        assert {foreign_key.target_fullname for foreign_key in service_foreign_keys} == {"http_services.id"}
        assert {foreign_key.target_fullname for foreign_key in artifact_foreign_keys} == {"http_schema_artifacts.id"}
        assert Tool.http_service.property.back_populates == "tools"
        assert Tool.http_schema_artifact.property.back_populates == "tools"
        assert HttpService.tools.property.back_populates == "http_service"
        assert HttpService.artifacts.property.back_populates == "http_service"
        assert HttpSchemaArtifact.http_service.property.back_populates == "artifacts"
        assert HttpSchemaArtifact.tools.property.back_populates == "http_schema_artifact"


class TestHttpRegistryMigrationSqlite:
    """Exercise the idempotent migration on SQLite."""

    def test_upgrade_creates_tables_and_provenance_and_preserves_rows(self):
        """Upgrade adds provenance without disturbing existing tools."""
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("PRAGMA foreign_keys=ON"))
                _create_pre_migration_schema(conn)

                _run_migration(conn, "upgrade")
                _run_migration(conn, "upgrade")

                tables = set(sa.inspect(conn).get_table_names())
                assert {SERVICE_TABLE, ARTIFACT_TABLE}.issubset(tables)

                columns = {column["name"]: column for column in sa.inspect(conn).get_columns(TOOLS_TABLE)}
                assert columns["http_service_id"]["nullable"] is True
                assert columns["http_schema_artifact_id"]["nullable"] is True
                assert "ix_tools_http_schema_artifact_id" in _index_names(conn, TOOLS_TABLE)
                assert "ix_http_schema_artifacts_service_active" in _index_names(conn, ARTIFACT_TABLE)

                foreign_keys = {tuple(foreign_key["constrained_columns"]): foreign_key for foreign_key in _tool_foreign_keys(conn)}
                assert set(foreign_keys) == {("http_service_id",), ("http_schema_artifact_id",)}
                assert foreign_keys[("http_service_id",)]["referred_table"] == SERVICE_TABLE
                assert foreign_keys[("http_service_id",)]["options"].get("ondelete") == "CASCADE"
                assert foreign_keys[("http_schema_artifact_id",)]["referred_table"] == ARTIFACT_TABLE
                assert foreign_keys[("http_schema_artifact_id",)]["options"].get("ondelete") == "SET NULL"
                assert conn.execute(sa.text("SELECT original_name FROM tools WHERE id = 'tool-existing'")).scalar_one() == "GET /ping"

                # Deleting an artifact nulls provenance; deleting a service cascades to its tools.
                _insert_service_and_artifact(conn)
                conn.execute(sa.text("UPDATE tools SET http_service_id = 'service-1', http_schema_artifact_id = 'artifact-1' WHERE id = 'tool-existing'"))
                conn.execute(sa.text("DELETE FROM http_schema_artifacts WHERE id = 'artifact-1'"))
                assert conn.execute(sa.text("SELECT http_schema_artifact_id FROM tools WHERE id = 'tool-existing'")).scalar_one_or_none() is None
                conn.execute(sa.text("DELETE FROM http_services WHERE id = 'service-1'"))
                assert conn.execute(sa.text("SELECT COUNT(*) FROM tools WHERE id = 'tool-existing'")).scalar_one() == 0
        finally:
            engine.dispose()

    def test_downgrade_is_idempotent(self):
        """Downgrade removes tool columns and both tables on repeated runs."""
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                _create_pre_migration_schema(conn)
                _run_migration(conn, "upgrade")

                _run_migration(conn, "downgrade")
                _run_migration(conn, "downgrade")

                tool_columns = _column_names(conn, TOOLS_TABLE)
                assert "http_service_id" not in tool_columns
                assert "http_schema_artifact_id" not in tool_columns
                assert "ix_tools_http_schema_artifact_id" not in _index_names(conn, TOOLS_TABLE)
                tables = set(sa.inspect(conn).get_table_names())
                assert SERVICE_TABLE not in tables
                assert ARTIFACT_TABLE not in tables
                assert conn.execute(sa.text("SELECT original_name FROM tools WHERE id = 'tool-existing'")).scalar_one() == "GET /ping"
        finally:
            engine.dispose()

    def test_upgrade_still_creates_http_tables_when_tools_table_is_missing(self):
        """A schema without the tools table still gains the HTTP tables."""
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("CREATE TABLE email_teams (id VARCHAR(36) PRIMARY KEY)"))
                conn.commit()

                _run_migration(conn, "upgrade")

                tables = set(sa.inspect(conn).get_table_names())
                assert SERVICE_TABLE in tables
                assert ARTIFACT_TABLE in tables
                assert TOOLS_TABLE not in tables
        finally:
            engine.dispose()
