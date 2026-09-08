# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/db/test_protocol_config_migration.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the PR2 ``protocol_config`` JSON column migration.
"""

# Standard
import importlib
import inspect as pyinspect

# Third-Party
from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

MODULE_NAME = "mcpgateway.alembic.versions.9f3e2d1c4b5a_add_protocol_config_to_tools"
REVISION = "9f3e2d1c4b5a"
DOWN_REVISION = "3c9d5e7a1b2f"
TABLE_NAME = "tools"
COLUMN_NAME = "protocol_config"


def _run_migration(conn, operation: str) -> None:
    """Run one migration entrypoint against a live connection."""
    context = MigrationContext.configure(conn, opts={"as_sql": False})
    with Operations.context(context):
        migration = importlib.import_module(MODULE_NAME)
        getattr(migration, operation)()


def _create_pre_migration_schema(conn) -> None:
    """Create the minimal tools table required by the migration."""
    conn.execute(sa.text("CREATE TABLE tools (id VARCHAR(36) PRIMARY KEY, original_name VARCHAR(255) NOT NULL)"))
    conn.execute(sa.text("INSERT INTO tools (id, original_name) VALUES ('tool-existing', 'demo.tool')"))
    conn.commit()


def _column_names(conn) -> set[str]:
    """Return reflected tool columns."""
    return {column["name"] for column in sa.inspect(conn).get_columns(TABLE_NAME)}


class TestProtocolConfigMigrationStructure:
    """Verify migration metadata."""

    def test_migration_metadata(self):
        """Revision metadata points to the current head."""
        migration = importlib.import_module(MODULE_NAME)

        assert migration.revision == REVISION
        assert migration.down_revision == DOWN_REVISION
        assert len(pyinspect.signature(migration.upgrade).parameters) == 0
        assert len(pyinspect.signature(migration.downgrade).parameters) == 0


class TestProtocolConfigMigrationSqlite:
    """Exercise the idempotent migration on SQLite."""

    def test_upgrade_adds_nullable_column_and_preserves_rows(self):
        """Upgrade adds a nullable column without disturbing existing tools."""
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                _create_pre_migration_schema(conn)

                _run_migration(conn, "upgrade")
                _run_migration(conn, "upgrade")

                columns = {column["name"]: column for column in sa.inspect(conn).get_columns(TABLE_NAME)}
                assert columns[COLUMN_NAME]["nullable"] is True
                assert conn.execute(sa.text("SELECT original_name FROM tools WHERE id = 'tool-existing'")).scalar_one() == "demo.tool"

                # A JSON value round-trips through the column (no backfill).
                conn.execute(sa.text("UPDATE tools SET protocol_config = '{\"version\": 1}' WHERE id = 'tool-existing'"))
                assert conn.execute(sa.text("SELECT protocol_config FROM tools WHERE id = 'tool-existing'")).scalar_one() == '{"version": 1}'
        finally:
            engine.dispose()

    def test_downgrade_is_idempotent(self):
        """Downgrade removes the column on repeated runs without data loss."""
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                _create_pre_migration_schema(conn)
                _run_migration(conn, "upgrade")

                _run_migration(conn, "downgrade")
                _run_migration(conn, "downgrade")

                assert COLUMN_NAME not in _column_names(conn)
                assert conn.execute(sa.text("SELECT original_name FROM tools WHERE id = 'tool-existing'")).scalar_one() == "demo.tool"
        finally:
            engine.dispose()

    def test_upgrade_skips_when_tools_table_is_missing(self):
        """A schema without the tools table is left untouched."""
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("CREATE TABLE other (id INTEGER PRIMARY KEY)"))
                conn.commit()

                _run_migration(conn, "upgrade")

                assert "tools" not in set(sa.inspect(conn).get_table_names())
        finally:
            engine.dispose()
