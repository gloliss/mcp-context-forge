# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/db/test_grpc_runtime_config_migration.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the PR6 ``runtime_config`` JSON column migration on grpc_services.
"""

# Standard
import importlib
import inspect as pyinspect

# Third-Party
from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

MODULE_NAME = "mcpgateway.alembic.versions.5c6d7e8f9a0b_add_grpc_runtime_config"
REVISION = "5c6d7e8f9a0b"
DOWN_REVISION = "a4b5c6d7e8f9"
TABLE_NAME = "grpc_services"
COLUMN_NAME = "runtime_config"


def _run_migration(conn, operation: str) -> None:
    """Run one migration entrypoint against a live connection."""
    context = MigrationContext.configure(conn, opts={"as_sql": False})
    with Operations.context(context):
        migration = importlib.import_module(MODULE_NAME)
        getattr(migration, operation)()


def _create_pre_migration_schema(conn) -> None:
    """Create the minimal grpc_services table required by the migration."""
    conn.execute(sa.text("CREATE TABLE grpc_services (id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL)"))
    conn.execute(sa.text("INSERT INTO grpc_services (id, name) VALUES ('svc-existing', 'demo.svc')"))
    conn.commit()


def _column_names(conn) -> set[str]:
    """Return reflected grpc_services columns."""
    return {column["name"] for column in sa.inspect(conn).get_columns(TABLE_NAME)}


class TestGrpcRuntimeConfigMigrationStructure:
    """Verify migration metadata."""

    def test_migration_metadata(self):
        """Revision metadata points to the current head."""
        migration = importlib.import_module(MODULE_NAME)

        assert migration.revision == REVISION
        assert migration.down_revision == DOWN_REVISION
        assert len(pyinspect.signature(migration.upgrade).parameters) == 0
        assert len(pyinspect.signature(migration.downgrade).parameters) == 0


class TestGrpcRuntimeConfigMigrationSqlite:
    """Exercise the idempotent migration on SQLite."""

    def test_upgrade_adds_nullable_column(self, tmp_path):
        """The column is added and existing rows keep their data."""
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'migrate.db'}")
        try:
            with engine.begin() as conn:
                _create_pre_migration_schema(conn)
            with engine.begin() as conn:
                _run_migration(conn, "upgrade")

            assert COLUMN_NAME in _column_names(engine.connect())
            row = engine.connect().execute(sa.text("SELECT name FROM grpc_services WHERE id = 'svc-existing'")).fetchone()
            assert row[0] == "demo.svc"
        finally:
            engine.dispose()

    def test_upgrade_is_idempotent(self, tmp_path):
        """Running upgrade twice does not error or duplicate the column."""
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'idempotent.db'}")
        try:
            with engine.begin() as conn:
                _create_pre_migration_schema(conn)
            with engine.begin() as conn:
                _run_migration(conn, "upgrade")
            with engine.begin() as conn:
                _run_migration(conn, "upgrade")

            columns = _column_names(engine.connect())
            assert COLUMN_NAME in columns
        finally:
            engine.dispose()

    def test_downgrade_removes_column(self, tmp_path):
        """downgrade removes the column cleanly."""
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'downgrade.db'}")
        try:
            with engine.begin() as conn:
                _create_pre_migration_schema(conn)
            with engine.begin() as conn:
                _run_migration(conn, "upgrade")
            with engine.begin() as conn:
                _run_migration(conn, "downgrade")

            assert COLUMN_NAME not in _column_names(engine.connect())
        finally:
            engine.dispose()

    def test_upgrade_skips_missing_table(self, tmp_path):
        """A database without grpc_services is untouched."""
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
        try:
            with engine.begin() as conn:
                _run_migration(conn, "upgrade")
            tables = {row[0] for row in engine.connect().execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
            assert "grpc_services" not in tables
        finally:
            engine.dispose()
