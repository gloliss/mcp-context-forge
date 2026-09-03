# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/db/test_grpc_tool_artifact_migration.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for gRPC tool schema-artifact provenance migration.
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
from mcpgateway.db import GrpcSchemaArtifact, Tool

MODULE_NAME = "mcpgateway.alembic.versions.t2b3c4d5e6f7_add_grpc_schema_artifact_id_to_tools"
REVISION = "t2b3c4d5e6f7"
DOWN_REVISION = "s1a2b3c4d5e6"
TABLE_NAME = "tools"
COLUMN_NAME = "grpc_schema_artifact_id"
INDEX_NAME = "ix_tools_grpc_schema_artifact_id"


def _run_migration(conn, operation: str) -> None:
    """Run one migration entrypoint against a live connection."""
    context = MigrationContext.configure(conn, opts={"as_sql": False})
    with Operations.context(context):
        migration = importlib.import_module(MODULE_NAME)
        getattr(migration, operation)()


def _create_pre_migration_schema(conn) -> None:
    """Create the minimal tables required by the migration."""
    conn.execute(sa.text("CREATE TABLE grpc_schema_artifacts (id VARCHAR(36) PRIMARY KEY)"))
    conn.execute(sa.text("CREATE TABLE tools (id VARCHAR(36) PRIMARY KEY, original_name VARCHAR(255) NOT NULL)"))
    conn.execute(sa.text("INSERT INTO tools (id, original_name) VALUES ('tool-existing', 'test.Service.Method')"))
    conn.commit()


def _column_names(conn) -> set[str]:
    """Return reflected tool columns."""
    return {column["name"] for column in sa.inspect(conn).get_columns(TABLE_NAME)}


def _index_names(conn) -> set[str]:
    """Return reflected tool indexes."""
    return {index["name"] for index in sa.inspect(conn).get_indexes(TABLE_NAME)}


def _artifact_foreign_keys(conn) -> list[dict[str, Any]]:
    """Return reflected foreign keys for the artifact provenance column."""
    return [foreign_key for foreign_key in sa.inspect(conn).get_foreign_keys(TABLE_NAME) if foreign_key.get("constrained_columns") == [COLUMN_NAME]]


class TestGrpcToolArtifactMigrationStructure:
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
        foreign_keys = Tool.__table__.c.grpc_schema_artifact_id.foreign_keys

        assert {foreign_key.target_fullname for foreign_key in foreign_keys} == {"grpc_schema_artifacts.id"}
        assert Tool.grpc_schema_artifact.property.back_populates == "tools"
        assert GrpcSchemaArtifact.tools.property.back_populates == "grpc_schema_artifact"


class TestGrpcToolArtifactMigrationSqlite:
    """Exercise the idempotent migration on SQLite."""

    def test_upgrade_adds_nullable_indexed_set_null_foreign_key_and_preserves_rows(self):
        """Upgrade adds provenance without disturbing existing tools."""
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("PRAGMA foreign_keys=ON"))
                _create_pre_migration_schema(conn)

                _run_migration(conn, "upgrade")
                _run_migration(conn, "upgrade")

                columns = {column["name"]: column for column in sa.inspect(conn).get_columns(TABLE_NAME)}
                assert columns[COLUMN_NAME]["nullable"] is True
                assert INDEX_NAME in _index_names(conn)
                foreign_keys = _artifact_foreign_keys(conn)
                assert len(foreign_keys) == 1
                assert foreign_keys[0]["referred_table"] == "grpc_schema_artifacts"
                assert foreign_keys[0]["referred_columns"] == ["id"]
                assert foreign_keys[0]["options"].get("ondelete") == "SET NULL"
                assert conn.execute(sa.text("SELECT original_name FROM tools WHERE id = 'tool-existing'")).scalar_one() == "test.Service.Method"

                conn.execute(sa.text("INSERT INTO grpc_schema_artifacts (id) VALUES ('artifact-1')"))
                conn.execute(sa.text("UPDATE tools SET grpc_schema_artifact_id = 'artifact-1' WHERE id = 'tool-existing'"))
                conn.execute(sa.text("DELETE FROM grpc_schema_artifacts WHERE id = 'artifact-1'"))
                assert conn.execute(sa.text("SELECT grpc_schema_artifact_id FROM tools WHERE id = 'tool-existing'")).scalar_one_or_none() is None
        finally:
            engine.dispose()

    def test_downgrade_is_idempotent(self):
        """Downgrade removes the index, foreign key, and column on repeated runs."""
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                _create_pre_migration_schema(conn)
                _run_migration(conn, "upgrade")

                _run_migration(conn, "downgrade")
                _run_migration(conn, "downgrade")

                assert COLUMN_NAME not in _column_names(conn)
                assert INDEX_NAME not in _index_names(conn)
                assert conn.execute(sa.text("SELECT original_name FROM tools WHERE id = 'tool-existing'")).scalar_one() == "test.Service.Method"
        finally:
            engine.dispose()

    def test_upgrade_skips_when_artifact_table_is_missing(self):
        """A partial or fresh schema without the referenced table is left untouched."""
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("CREATE TABLE tools (id VARCHAR(36) PRIMARY KEY)"))
                conn.commit()

                _run_migration(conn, "upgrade")

                assert COLUMN_NAME not in _column_names(conn)
        finally:
            engine.dispose()
