# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_database_tool_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the Database MCP Tool layer (OB-05).

Covers the required acceptance cases: registry seeding (five tools, the
``db_execute_query`` default-disabled), source routing by slug/name/id,
disabled source/template rejection, template parameter binding with an
immutable statement, the SQL policy gate on free-form execution, and the
unified result contract.  Every database operation is scripted with a fake
engine/adapter — no external driver is required.
"""

# Standard
import uuid

# Third-Party
import pytest
from sqlalchemy import select

# First-Party
from mcpgateway.adapters.database import (
    ConnectionPool,
    DatabaseStatementDeniedError,
    PoolConfig,
    PoolIdentity,
    QueryResult,
)
from mcpgateway.adapters.database.adapters import MySQLAdapter
from mcpgateway.db import DatabaseQueryTemplate, DatabaseSource, Tool
from mcpgateway.services.database_tool_service import (
    TOOL_EXECUTE_QUERY,
    TOOL_EXECUTE_TEMPLATE,
    TOOL_EXPLAIN_QUERY,
    TOOL_HEALTH_CHECK,
    TOOL_SEARCH_OBJECTS,
    DatabaseToolService,
    DatabaseToolSourceDisabledError,
    DatabaseToolSourceNotFoundError,
    DatabaseToolTemplateDisabledError,
    DatabaseToolTemplateNotFoundError,
)


# --------------------------------------------------------------------------
# Fakes: a scripted adapter that records calls and returns canned results.
# --------------------------------------------------------------------------
class RecordingAdapter:
    """A duck-typed adapter that records every call and returns canned rows."""

    def __init__(self):
        self.calls = []
        self.search_result = QueryResult(
            columns=["schema", "name", "type", "description", "columns"],
            rows=[["APP", "USERS", "table", None, []]],
        )
        self.execute_result = QueryResult(columns=["n"], rows=[[1], [2]], row_count=2)
        self.explain_result = QueryResult(columns=["plan"], rows=[["TABLE ACCESS FULL"]], row_count=1)

    def search_objects(self, name=None, kind=None, limit=100):
        self.calls.append(("search_objects", name, kind, limit))
        return self.search_result

    def execute(self, sql, params=None, max_rows=None, query_timeout=None):
        self.calls.append(("execute", sql, params, max_rows, query_timeout))
        return self.execute_result

    def explain(self, sql):
        self.calls.append(("explain", sql))
        return self.explain_result

    def health_check(self):
        self.calls.append(("health_check",))
        return {"ok": True, "latency_ms": 1.5, "error": None, "pool": {"closed": False, "pool_size": 5, "max_overflow": 5}}

    def detect_mode(self):
        self.calls.append(("detect_mode",))
        return "MYSQL"


class FakeRuntime:
    """A runtime client that always returns the same scripted adapter."""

    def __init__(self, adapter):
        self._adapter = adapter
        self.requested_sources = []

    def adapter_for(self, source):
        self.requested_sources.append(source)
        return self._adapter


# A minimal engine for the SQL-policy test: it records connect calls so a
# rejected write is proven to never touch the database.
class _NoopResult:
    def __init__(self, columns, rows):
        self._columns = list(columns)
        self._rows = list(rows)

    def keys(self):
        return self._columns

    def fetchmany(self, size):
        return list(self._rows[:size])


class _NoopConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        return _NoopResult(["n"], [[1]])


class PolicyEngine:
    def __init__(self):
        self.connect_calls = 0
        self.disposed = False

    def connect(self):
        self.connect_calls += 1
        return _NoopConnection()

    def dispose(self):
        self.disposed = True


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _source(db, enabled=True, **overrides):
    """Persist a database source and return it."""
    token = uuid.uuid4().hex[:10]
    source = DatabaseSource(
        name=overrides.pop("name", f"src-{token}"),
        slug=overrides.pop("slug", f"src-{token}"),
        engine=overrides.pop("engine", "oceanbase"),
        compatibility_mode=overrides.pop("compatibility_mode", "mysql"),
        host=overrides.pop("host", "127.0.0.1"),
        port=overrides.pop("port", 2881),
        enabled=enabled,
        **overrides,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _runtime(adapter):
    return FakeRuntime(adapter)


# --------------------------------------------------------------------------
# Tool Registry seeding (tools/list + disabled tool)
# --------------------------------------------------------------------------
def test_ensure_registered_creates_five_tools(test_db):
    """Seeding registers exactly five source-agnostic Database tools."""
    tools = DatabaseToolService.ensure_registered(test_db)

    names = {tool.original_name for tool in tools}
    assert names == {
        TOOL_SEARCH_OBJECTS,
        TOOL_EXECUTE_QUERY,
        TOOL_EXPLAIN_QUERY,
        TOOL_HEALTH_CHECK,
        TOOL_EXECUTE_TEMPLATE,
    }
    for tool in tools:
        assert tool.integration_type == "DATABASE"
        assert tool.custom_name_slug == tool.original_name.replace("_", "-")
        assert tool.name == tool.custom_name_slug  # MCP-exposed slugified name


def test_ensure_registered_execute_query_default_disabled(test_db):
    """``db_execute_query`` is seeded disabled; the other four enabled."""
    DatabaseToolService.ensure_registered(test_db)

    rows = test_db.execute(select(Tool).where(Tool.integration_type == "DATABASE")).scalars().all()
    by_name = {tool.original_name: tool for tool in rows}

    assert by_name[TOOL_EXECUTE_QUERY].enabled is False
    for name in (TOOL_SEARCH_OBJECTS, TOOL_EXPLAIN_QUERY, TOOL_HEALTH_CHECK, TOOL_EXECUTE_TEMPLATE):
        assert by_name[name].enabled is True


def test_ensure_registered_is_idempotent(test_db):
    """Re-seeding does not duplicate rows and preserves an admin's enabled toggle."""
    DatabaseToolService.ensure_registered(test_db)
    first_count = len(test_db.execute(select(Tool).where(Tool.integration_type == "DATABASE")).scalars().all())

    # Simulate an admin enabling the (seed-disabled) execute_query tool.
    execute_tool = test_db.execute(
        select(Tool).where(Tool.integration_type == "DATABASE", Tool.original_name == TOOL_EXECUTE_QUERY)
    ).scalar_one()
    execute_tool.enabled = True
    test_db.commit()

    DatabaseToolService.ensure_registered(test_db)
    rows = test_db.execute(select(Tool).where(Tool.integration_type == "DATABASE")).scalars().all()
    assert len(rows) == first_count
    execute_tool = next(t for t in rows if t.original_name == TOOL_EXECUTE_QUERY)
    assert execute_tool.enabled is True  # not reverted by re-seeding


# --------------------------------------------------------------------------
# Source routing
# --------------------------------------------------------------------------
def test_resolve_source_by_slug_name_and_id(test_db):
    """A source resolves by slug, name, and id alike."""
    source = _source(test_db, name="Central Mars OB", slug="central-mars-ob")

    assert DatabaseToolService.resolve_source(test_db, "central-mars-ob").id == source.id
    assert DatabaseToolService.resolve_source(test_db, "Central Mars OB").id == source.id
    assert DatabaseToolService.resolve_source(test_db, source.id).id == source.id


def test_resolve_source_missing(test_db):
    """An unknown source raises a structured error."""
    with pytest.raises(DatabaseToolSourceNotFoundError):
        DatabaseToolService.resolve_source(test_db, "does-not-exist")


def test_resolve_source_disabled(test_db):
    """A disabled source is rejected."""
    source = _source(test_db, enabled=False)

    with pytest.raises(DatabaseToolSourceDisabledError):
        DatabaseToolService.resolve_source(test_db, source.slug)


# --------------------------------------------------------------------------
# tools/call dispatch + result contract
# --------------------------------------------------------------------------
def test_call_search_objects_result_contract(test_db):
    """``db_search_objects`` returns the unified result contract."""
    source = _source(test_db)
    adapter = RecordingAdapter()
    result = DatabaseToolService.call(
        test_db, TOOL_SEARCH_OBJECTS, {"source": source.slug, "kind": "table", "name": "USERS", "limit": 20}, _runtime(adapter)
    )

    assert set(result.keys()) == {"columns", "rows", "row_count", "truncated", "elapsed_ms", "warnings"}
    assert result["columns"] == ["schema", "name", "type", "description", "columns"]
    assert result["rows"] == [["APP", "USERS", "table", None, []]]
    assert adapter.calls[0] == ("search_objects", "USERS", "table", 20)


def test_call_health_check_no_credentials(test_db):
    """``db_health_check`` returns health fields and never credentials."""
    source = _source(test_db)
    result = DatabaseToolService.call(test_db, TOOL_HEALTH_CHECK, {"source": source.slug}, _runtime(RecordingAdapter()))

    assert set(result.keys()) >= {"healthy", "engine", "compatibility_mode", "detected_mode", "latency", "pool"}
    assert result["healthy"] is True
    assert result["engine"] == "oceanbase"
    assert result["compatibility_mode"] == "mysql"
    assert result["detected_mode"] == "MYSQL"
    assert "password" not in result
    assert "credential" not in result
    assert "dsn" not in result


def test_call_unknown_tool(test_db):
    """An unknown database tool name is rejected."""
    from mcpgateway.services.database_tool_service import DatabaseToolNotFoundError

    with pytest.raises(DatabaseToolNotFoundError):
        DatabaseToolService.call(test_db, "db_unknown_tool", {}, _runtime(RecordingAdapter()))


# --------------------------------------------------------------------------
# Template parameters (db_execute_template)
# --------------------------------------------------------------------------
def _template(db, source, **overrides):
    token = uuid.uuid4().hex[:10]
    template = DatabaseQueryTemplate(
        source_id=source.id,
        name=overrides.pop("name", f"tpl-{token}"),
        slug=overrides.pop("slug", f"tpl-{token}"),
        statement=overrides.pop("statement", "SELECT * FROM users WHERE id = :id"),
        max_rows=overrides.pop("max_rows", 50),
        timeout_seconds=overrides.pop("timeout_seconds", 7.0),
        **overrides,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def test_execute_template_binds_params_and_keeps_statement(test_db):
    """``db_execute_template`` passes the statement verbatim and binds arguments."""
    source = _source(test_db)
    template = _template(test_db, source)
    adapter = RecordingAdapter()

    result = DatabaseToolService.call(
        test_db,
        TOOL_EXECUTE_TEMPLATE,
        {"source": source.slug, "template": template.slug, "arguments": {"id": 42}},
        _runtime(adapter),
    )

    op, sql, params, max_rows, timeout = adapter.calls[0]
    assert op == "execute"
    assert sql == "SELECT * FROM users WHERE id = :id"  # statement immutable
    assert params == {"id": 42}
    assert max_rows == 50
    assert timeout == 7.0
    assert result["row_count"] == 2


def test_execute_template_missing(test_db):
    """An unknown template raises a structured error."""
    source = _source(test_db)
    with pytest.raises(DatabaseToolTemplateNotFoundError):
        DatabaseToolService.call(test_db, TOOL_EXECUTE_TEMPLATE, {"source": source.slug, "template": "nope"}, _runtime(RecordingAdapter()))


def test_execute_template_disabled(test_db):
    """A disabled template is rejected."""
    source = _source(test_db)
    template = _template(test_db, source, enabled=False)
    with pytest.raises(DatabaseToolTemplateDisabledError):
        DatabaseToolService.call(test_db, TOOL_EXECUTE_TEMPLATE, {"source": source.slug, "template": template.slug}, _runtime(RecordingAdapter()))


# --------------------------------------------------------------------------
# Query policy (db_execute_query)
# --------------------------------------------------------------------------
def test_execute_query_write_denied_before_db(test_db):
    """Free-form execution still passes through the OB-04 SQL policy."""
    source = _source(test_db, engine="mysql", compatibility_mode=None)
    engine = PolicyEngine()
    pool = ConnectionPool(identity=PoolIdentity.from_source(source), engine=engine, pool_config=PoolConfig())
    adapter = MySQLAdapter(source, pool=pool)

    with pytest.raises(DatabaseStatementDeniedError):
        DatabaseToolService.call(
            test_db,
            TOOL_EXECUTE_QUERY,
            {"source": source.slug, "sql": "DELETE FROM t"},
            _runtime(adapter),
        )

    assert engine.connect_calls == 0  # rejected before any database access
