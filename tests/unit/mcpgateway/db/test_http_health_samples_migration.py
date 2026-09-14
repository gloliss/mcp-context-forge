# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/db/test_http_health_samples_migration.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the PR8 ``http_health_samples`` table migration (§57).
"""

# Standard
import importlib
import inspect as pyinspect

# Third-Party
from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

MODULE_NAME = "mcpgateway.alembic.versions.6d7e8f9a0b1c_add_http_health_samples"
REVISION = "6d7e8f9a0b1c"
DOWN_REVISION = "5c6d7e8f9a0b"
TABLE_NAME = "http_health_samples"


def _run_migration(conn, operation: str) -> None:
    """Run one migration entrypoint against a live connection."""
    context = MigrationContext.configure(conn, opts={"as_sql": False})
    with Operations.context(context):
        migration = importlib.import_module(MODULE_NAME)
        getattr(migration, operation)()


def _create_pre_migration_schema(conn) -> None:
    """Create the referenced http_services table required by the FK."""
    conn.execute(sa.text("CREATE TABLE http_services (id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL, slug VARCHAR(255) NOT NULL)"))
    conn.execute(sa.text("INSERT INTO http_services (id, name, slug) VALUES ('svc-1', 'demo', 'demo')"))
    conn.commit()


def _table_names(conn) -> set[str]:
    """Return reflected table names."""
    return {row[0] for row in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}


class TestHttpHealthSamplesMigrationStructure:
    """Verify migration metadata."""

    def test_migration_metadata(self):
        """Revision metadata points to the current head."""
        migration = importlib.import_module(MODULE_NAME)

        assert migration.revision == REVISION
        assert migration.down_revision == DOWN_REVISION
        assert len(pyinspect.signature(migration.upgrade).parameters) == 0
        assert len(pyinspect.signature(migration.downgrade).parameters) == 0


class TestHttpHealthSamplesMigrationSqlite:
    """Exercise the idempotent migration on SQLite."""

    def test_upgrade_creates_table(self, tmp_path):
        """The table is created with the expected columns."""
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
        try:
            with engine.begin() as conn:
                _create_pre_migration_schema(conn)
            with engine.begin() as conn:
                _run_migration(conn, "upgrade")

            assert TABLE_NAME in _table_names(engine.connect())
            columns = {column["name"] for column in sa.inspect(engine.connect()).get_columns(TABLE_NAME)}
            assert {"http_service_id", "timestamp", "healthy", "status_code", "latency_ms", "error_message"} <= columns
        finally:
            engine.dispose()

    def test_upgrade_is_idempotent(self, tmp_path):
        """Running upgrade twice does not error."""
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'idempotent.db'}")
        try:
            with engine.begin() as conn:
                _create_pre_migration_schema(conn)
            with engine.begin() as conn:
                _run_migration(conn, "upgrade")
            with engine.begin() as conn:
                _run_migration(conn, "upgrade")

            assert TABLE_NAME in _table_names(engine.connect())
        finally:
            engine.dispose()

    def test_downgrade_removes_table(self, tmp_path):
        """downgrade drops the table cleanly."""
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'downgrade.db'}")
        try:
            with engine.begin() as conn:
                _create_pre_migration_schema(conn)
            with engine.begin() as conn:
                _run_migration(conn, "upgrade")
            with engine.begin() as conn:
                _run_migration(conn, "downgrade")

            assert TABLE_NAME not in _table_names(engine.connect())
        finally:
            engine.dispose()

    def test_upgrade_skips_missing_parent_table(self, tmp_path):
        """Without http_services the migration still creates the table (SQLite FK lax)."""
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
        try:
            with engine.begin() as conn:
                _run_migration(conn, "upgrade")

            assert TABLE_NAME in _table_names(engine.connect())
        finally:
            engine.dispose()
