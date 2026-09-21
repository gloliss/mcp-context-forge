# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/adapters/database/test_oceanbase_adapter.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Contract tests for the unified OceanBase adapter (OB-03).

The same suite runs for the MySQL and Oracle compatibility modes.  Because the
development venv ships no external database driver, every test injects a fake
engine that returns canned rows; this exercises the adapter's detection,
validation, and metadata mapping without a live OceanBase tenant.
"""

# Third-Party
import pytest
from sqlalchemy.exc import OperationalError

# First-Party
from mcpgateway.adapters.database import (
    DB_COMPATIBILITY_MODE_MISMATCH,
    ConnectionPool,
    DatabaseCompatModeMismatchError,
    PoolConfig,
    PoolIdentity,
    get_default_registry,
)
from mcpgateway.adapters.database.oceanbase import (
    MODE_MYSQL,
    MODE_ORACLE,
    OceanBaseAdapter,
    OceanBaseMySQLDialect,
    OceanBaseOracleDialect,
)
from mcpgateway.adapters.database.types import (
    METADATA_TYPE_COLUMN,
    METADATA_TYPE_FUNCTION,
    METADATA_TYPE_INDEX,
    METADATA_TYPE_PROCEDURE,
    METADATA_TYPE_SCHEMA,
    METADATA_TYPE_TABLE,
    METADATA_TYPE_VIEW,
)
from mcpgateway.db import DatabaseSource


# --------------------------------------------------------------------------
# Fakes: a scripted engine that answers by SQL substring.
# --------------------------------------------------------------------------
class FakeResult:
    """A minimal SQLAlchemy ``CursorResult`` stand-in."""

    def __init__(self, columns, rows):
        self._columns = list(columns)
        self._rows = list(rows)

    def keys(self):
        return self._columns

    def fetchmany(self, size):
        return list(self._rows[:size])


class DictConnection:
    """A connection that answers the connectivity probe and scripted SQL."""

    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if sql.strip().lower() == "select 1":
            return FakeResult(["1"], [[1]])
        return self._engine.result_for(sql)


class DictEngine:
    """A fake engine that returns canned results keyed by SQL substring."""

    def __init__(self, responses, fail_connect=False):
        self.responses = list(responses)
        self.fail_connect = fail_connect
        self.disposed = False
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        if self.fail_connect:
            raise OperationalError("SELECT 1", {}, ConnectionError("connection refused"))
        return DictConnection(self)

    def dispose(self):
        self.disposed = True

    def result_for(self, sql):
        for substring, columns, rows in self.responses:
            if substring in sql:
                return FakeResult(columns, rows)
        raise AssertionError(f"No response registered for SQL: {sql!r}")


# Both modes return identical metadata values, only through different views, so
# the contract assertions below are byte-for-byte the same for each mode.
MYSQL_RESPONSES = [
    ("VERSION()", ["version"], [["8.0.30-OceanBase-v4.2.1"]]),
    ("SCHEMATA", ["schema", "name"], [("APP", "APP"), ("MYSQL", "MYSQL")]),
    ("'BASE TABLE'", ["schema", "name", "description"], [("APP", "USERS", "用户表")]),
    ("'VIEW'", ["schema", "name", "description"], [("APP", "V_USERS", None)]),
    ("COLUMNS", ["schema", "name", "description"], [("APP", "ID", None), ("APP", "NAME", None)]),
    ("STATISTICS", ["schema", "name", "column_name"], [("APP", "IDX_USERS_NAME", "NAME"), ("APP", "IDX_USERS_NAME", "ID")]),
    ("ROUTINES", ["schema", "name", "type"], [("APP", "SP_GET_USER", "PROCEDURE"), ("APP", "FN_COUNT", "FUNCTION")]),
]

ORACLE_RESPONSES = [
    ("V$VERSION", ["banner"], [["Oracle Database 19c Enterprise Edition ... OceanBase"]]),
    ("all_users", ["schema", "name"], [("APP", "APP"), ("SYS", "SYS")]),
    ("all_tables", ["schema", "name", "description"], [("APP", "USERS", "用户表")]),
    ("all_views", ["schema", "name", "description"], [("APP", "V_USERS", None)]),
    ("all_tab_columns", ["schema", "name", "description"], [("APP", "ID", None), ("APP", "NAME", None)]),
    ("all_indexes", ["schema", "name", "column_name"], [("APP", "IDX_USERS_NAME", "NAME"), ("APP", "IDX_USERS_NAME", "ID")]),
    ("all_procedures", ["schema", "name", "type"], [("APP", "SP_GET_USER", "PROCEDURE"), ("APP", "FN_COUNT", "FUNCTION")]),
]

RESPONSES = {"mysql": MYSQL_RESPONSES, "oracle": ORACLE_RESPONSES}


def _source(compat_mode):
    return DatabaseSource(
        name="ob",
        slug="ob",
        engine="oceanbase",
        compatibility_mode=compat_mode,
        host="127.0.0.1",
        port=2881,
        database_name="testdb",
        username="user",
        password="secret",
    )


def _build(compat_mode, responses=None):
    """Build an OceanBase adapter over a scripted fake engine."""
    source = _source(compat_mode)
    engine = DictEngine(responses if responses is not None else RESPONSES[compat_mode])
    pool = ConnectionPool(identity=PoolIdentity.from_source(source), engine=engine, pool_config=PoolConfig())
    return OceanBaseAdapter(source, pool=pool), engine


@pytest.mark.parametrize("compat_mode", ["mysql", "oracle"])
class TestOceanBaseContract:
    """The same contract suite, run identically for MySQL and Oracle mode."""

    def test_connection(self, compat_mode):
        adapter, _ = _build(compat_mode)
        probe = adapter.test_connection()
        assert probe["ok"] is True

    def test_detect_mode(self, compat_mode):
        adapter, _ = _build(compat_mode)
        assert adapter.detect_mode() == compat_mode.upper()

    def test_validate_mode_match(self, compat_mode):
        adapter, _ = _build(compat_mode)
        assert adapter.validate_mode() == compat_mode.upper()

    def test_schemas(self, compat_mode):
        adapter, _ = _build(compat_mode)
        schemas = adapter.list_schemas()
        assert all(obj.type == METADATA_TYPE_SCHEMA for obj in schemas)
        assert [obj.name for obj in schemas] == ["APP", "SYS" if compat_mode == "oracle" else "MYSQL"]

    def test_tables(self, compat_mode):
        adapter, _ = _build(compat_mode)
        tables = adapter.list_tables()
        assert len(tables) == 1
        assert tables[0].type == METADATA_TYPE_TABLE
        assert tables[0].schema == "APP"
        assert tables[0].name == "USERS"
        assert tables[0].description == "用户表"
        assert tables[0].columns == []

    def test_views(self, compat_mode):
        adapter, _ = _build(compat_mode)
        views = adapter.list_views()
        assert len(views) == 1
        assert views[0].type == METADATA_TYPE_VIEW
        assert views[0].name == "V_USERS"

    def test_columns(self, compat_mode):
        adapter, _ = _build(compat_mode)
        columns = adapter.list_columns("APP", "USERS")
        assert all(obj.type == METADATA_TYPE_COLUMN for obj in columns)
        assert [obj.name for obj in columns] == ["ID", "NAME"]

    def test_indexes(self, compat_mode):
        adapter, _ = _build(compat_mode)
        indexes = adapter.list_indexes("APP", "USERS")
        assert len(indexes) == 1
        assert indexes[0].type == METADATA_TYPE_INDEX
        assert indexes[0].name == "IDX_USERS_NAME"
        assert indexes[0].columns == ["NAME", "ID"]

    def test_procedures(self, compat_mode):
        adapter, _ = _build(compat_mode)
        procedures = adapter.list_procedures()
        assert {(obj.name, obj.type) for obj in procedures} == {
            ("SP_GET_USER", METADATA_TYPE_PROCEDURE),
            ("FN_COUNT", METADATA_TYPE_FUNCTION),
        }

    def test_health(self, compat_mode):
        adapter, _ = _build(compat_mode)
        health = adapter.health_check()
        assert health["ok"] is True

    def test_disconnect(self, compat_mode):
        adapter, engine = _build(compat_mode)
        adapter.close()
        assert engine.disposed is True
        assert adapter.pool.closed is True


def test_unified_metadata_contract():
    """Both modes emit the exact five-key metadata contract."""
    for compat_mode in ("mysql", "oracle"):
        adapter, _ = _build(compat_mode)
        obj = adapter.list_tables()[0]
        assert obj.to_dict() == {
            "schema": "APP",
            "name": "USERS",
            "type": "table",
            "description": "用户表",
            "columns": [],
        }
        assert set(obj.to_dict().keys()) == {"schema", "name", "type", "description", "columns"}


def test_mode_mismatch_raises():
    """A configured MySQL source detected as Oracle is rejected with the code."""
    engine = DictEngine([("VERSION()", ["version"], [["Oracle Database 19c ... OceanBase"]])])
    source = _source("mysql")
    pool = ConnectionPool(identity=PoolIdentity.from_source(source), engine=engine, pool_config=PoolConfig())
    adapter = OceanBaseAdapter(source, pool=pool)

    with pytest.raises(DatabaseCompatModeMismatchError) as exc_info:
        adapter.validate_mode()

    assert exc_info.value.code == DB_COMPATIBILITY_MODE_MISMATCH
    assert exc_info.value.configured == MODE_MYSQL
    assert exc_info.value.detected == MODE_ORACLE


def test_detect_mode_returns_none_on_failure():
    """A dead connection yields ``None`` from detection, not an exception."""
    engine = DictEngine([], fail_connect=True)
    source = _source("mysql")
    pool = ConnectionPool(identity=PoolIdentity.from_source(source), engine=engine, pool_config=PoolConfig())
    adapter = OceanBaseAdapter(source, pool=pool)

    assert adapter.detect_mode() is None


def test_single_adapter_class_for_both_modes():
    """Both modes resolve to one adapter class (no duplicated logic)."""
    registry = get_default_registry()
    assert registry.lookup("oceanbase", "mysql") is OceanBaseAdapter
    assert registry.lookup("oceanbase", "oracle") is OceanBaseAdapter
    assert registry.lookup("oceanbase", "mysql") is registry.lookup("oceanbase", "oracle")


def test_dialect_metadata_sources():
    """MySQL mode reads INFORMATION_SCHEMA; Oracle mode reads ALL_* views."""
    mysql = OceanBaseMySQLDialect()
    oracle = OceanBaseOracleDialect()

    assert "information_schema" in mysql.tables_sql(None)[0]
    assert "information_schema.STATISTICS" in mysql.indexes_sql("APP", "USERS")[0]
    assert "all_tables" in oracle.tables_sql(None)[0]
    assert "all_views" in oracle.views_sql(None)[0]
    assert "all_tab_columns" in oracle.columns_sql("APP", "USERS")[0]
    assert "all_indexes" in oracle.indexes_sql("APP", "USERS")[0]
    assert "all_procedures" in oracle.procedures_sql(None)[0]


@pytest.mark.parametrize("compat_mode", ["mysql", "oracle"])
class TestOceanBaseSearchObjects:
    """The five-kind ``search_objects`` contract, run for both modes (OB-05).

    ``db_search_objects`` exposes table/view/column/index/procedure with an
    identical tool contract in MySQL and Oracle mode; these tests pin that the
    aggregated ``search_objects`` result stays consistent across modes.
    """

    def test_search_tables(self, compat_mode):
        adapter, _ = _build(compat_mode)
        result = adapter.search_objects(kind="table")
        assert result.columns == ["schema", "name", "type", "description", "columns"]
        assert result.rows == [["APP", "USERS", "table", "用户表", []]]

    def test_search_views(self, compat_mode):
        adapter, _ = _build(compat_mode)
        result = adapter.search_objects(kind="view")
        assert result.rows == [["APP", "V_USERS", "view", None, []]]

    def test_search_columns(self, compat_mode):
        adapter, _ = _build(compat_mode)
        result = adapter.search_objects(kind="column", name="USERS")
        assert [row[1] for row in result.rows] == ["ID", "NAME"]
        assert all(row[2] == "column" for row in result.rows)

    def test_search_indexes(self, compat_mode):
        adapter, _ = _build(compat_mode)
        result = adapter.search_objects(kind="index", name="USERS")
        assert len(result.rows) == 1
        assert result.rows[0][1] == "IDX_USERS_NAME"
        assert result.rows[0][4] == ["NAME", "ID"]

    def test_search_procedures(self, compat_mode):
        adapter, _ = _build(compat_mode)
        result = adapter.search_objects(kind="procedure")
        assert {(row[1], row[2]) for row in result.rows} == {
            ("SP_GET_USER", "procedure"),
            ("FN_COUNT", "function"),
        }

    def test_search_default_is_table_and_view(self, compat_mode):
        adapter, _ = _build(compat_mode)
        result = adapter.search_objects()
        assert {(row[1], row[2]) for row in result.rows} == {("USERS", "table"), ("V_USERS", "view")}

    def test_search_name_filter(self, compat_mode):
        adapter, _ = _build(compat_mode)
        result = adapter.search_objects(kind="table", name="user")
        assert [row[1] for row in result.rows] == ["USERS"]

    def test_search_unknown_kind_is_empty(self, compat_mode):
        adapter, _ = _build(compat_mode)
        result = adapter.search_objects(kind="nonsense")
        assert result.rows == []

