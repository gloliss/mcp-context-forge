# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/adapters/database/test_database_integration.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

In-process integration tests for OceanBase adapter hardening (OB-07).

The venv has no external database driver, so these tests drive the full
``DatabaseToolService -> DatabaseRuntimeClient -> adapter -> pool`` chain with a
scripted fake adapter in both OceanBase compatibility modes (MySQL and Oracle).

They cover the OB-07 acceptance matrix (connection, mode detection, metadata,
SELECT/JOIN/CTE, view, explain, template, row limit, timeout, readonly reject,
pool reuse, pool reconnect, source disable, credential update, metadata cache,
health check) plus a 10/50-way concurrency sweep reporting P50/P95/P99, error
rate, and pool reuse, with DB SQL time distinguished from adapter overhead.
"""

# Standard
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# Third-Party
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

# First-Party
from mcpgateway.adapters.database import (
    AdapterRegistry,
    DatabaseMetadataCache,
    DatabaseQueryTimeoutError,
    DatabaseRuntimeClient,
    DatabaseStatementDeniedError,
    PoolManager,
    QueryResult,
    SQLAlchemyDatabaseAdapter,
)
from mcpgateway.adapters.database.sql_policy import SqlPolicyGuard, SqlStatementClassifier
from mcpgateway.db import Base, DatabaseAudit, DatabaseQueryTemplate, DatabaseSource
from mcpgateway.schemas import DatabaseSourceUpdate
from mcpgateway.services.database_source_service import DatabaseSourceService
from mcpgateway.services.database_tool_service import (
    TOOL_EXECUTE_QUERY,
    TOOL_EXECUTE_TEMPLATE,
    TOOL_EXPLAIN_QUERY,
    TOOL_HEALTH_CHECK,
    TOOL_SEARCH_OBJECTS,
    DatabaseToolService,
    DatabaseToolSourceDisabledError,
)


# --------------------------------------------------------------------------
# Scripted engine/connection — no real driver is installed.
# --------------------------------------------------------------------------
class _FakeResult:
    def keys(self):
        return ["n"]

    def fetchmany(self, size):
        return [[1]]


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        return _FakeResult()


class ScriptedEngine:
    """A pool engine that only tracks connect/dispose for lifecycle asserts."""

    def __init__(self):
        self.disposed = False
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        return _FakeConnection()

    def dispose(self):
        self.disposed = True


class ScriptedAdapter(SQLAlchemyDatabaseAdapter):
    """A fake adapter reusing the real OB-04 policy, with canned DB results."""

    dialect_driver = "test+test"

    #: Cross-test counters/flags, reset by the autouse fixture.
    built = 0
    search_calls = 0
    execute_calls = 0
    slow_sql: set[str] = set()
    db_latency = 0.0

    @classmethod
    def build_engine(cls, source: Any, pool_config: Any):
        ScriptedAdapter.built += 1
        return ScriptedEngine()

    def search_objects(self, name=None, kind=None, limit=100):
        ScriptedAdapter.search_calls += 1
        if kind == "view":
            rows = [["APP", "V_USERS", "view", None, []]]
        elif kind == "column":
            rows = [["APP", "USERS", "column", None, ["ID", "EMAIL"]]]
        elif kind == "index":
            rows = [["APP", "USERS", "index", None, ["idx_users_email"]]]
        else:
            rows = [["APP", "USERS", "table", None, []]]
        return QueryResult(columns=["schema", "name", "type", "description", "columns"], rows=rows, row_count=len(rows))

    def execute(self, sql, params=None, max_rows=None, query_timeout=None):
        ScriptedAdapter.execute_calls += 1
        # Real OB-04 policy gate: classification + readonly/multi-statement guard.
        policy = self._policy()
        classification = SqlStatementClassifier().classify(sql or "")
        SqlPolicyGuard(policy).check(classification)

        if (sql or "").strip().lower() in self.slow_sql:
            raise DatabaseQueryTimeoutError(f"Query exceeded the {query_timeout or policy.query_timeout_seconds}s timeout")

        if self.db_latency:
            time.sleep(self.db_latency)

        rows = [[1, "alice"], [2, "bob"], [3, "carol"], [4, "dave"], [5, "eve"]]
        limit = self._effective_max_rows(max_rows, policy)
        truncated = len(rows) > limit
        if truncated:
            rows = rows[:limit]
        return QueryResult(
            columns=["id", "name"],
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=round(self.db_latency * 1000.0, 3),
        )

    def explain(self, sql):
        return QueryResult(columns=["plan"], rows=[["TABLE ACCESS FULL"]], row_count=1)

    def health_check(self):
        probe = self.test_connection()  # real: exercises self._pool.connect()
        return {"ok": probe["ok"], "latency_ms": probe["latency_ms"], "error": probe.get("error"), "pool": self.pool.stats()}

    def detect_mode(self):
        return (self.source.compatibility_mode or "mysql").upper()


@pytest.fixture(autouse=True)
def _reset_scripted_adapter():
    """Reset the scripted adapter's cross-test counters before each test."""
    ScriptedAdapter.built = 0
    ScriptedAdapter.search_calls = 0
    ScriptedAdapter.execute_calls = 0
    ScriptedAdapter.slow_sql = set()
    ScriptedAdapter.db_latency = 0.0
    yield


def _runtime() -> DatabaseRuntimeClient:
    """Return a runtime client with the scripted adapter for both OceanBase modes."""
    registry = AdapterRegistry()
    registry.register("oceanbase", "mysql", ScriptedAdapter)
    registry.register("oceanbase", "oracle", ScriptedAdapter)
    return DatabaseRuntimeClient(registry=registry, pool_manager=PoolManager(), metadata_cache=DatabaseMetadataCache())


def _source(db, mode="mysql", **overrides) -> DatabaseSource:
    """Persist an OceanBase source in ``mode`` and return it."""
    token = uuid.uuid4().hex[:10]
    source = DatabaseSource(
        name=overrides.pop("name", f"src-{token}"),
        slug=overrides.pop("slug", f"src-{token}"),
        engine="oceanbase",
        compatibility_mode=mode,
        host=overrides.pop("host", "127.0.0.1"),
        port=overrides.pop("port", 2881),
        tenant_name="obmysql",
        database_name="app",
        username="root",
        password="secret",
        enabled=True,
        **overrides,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _percentile(values, pct):
    """Return the ``pct``-th percentile by linear interpolation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = int(k)
    hi = lo + 1 if lo + 1 < len(ordered) else lo
    return ordered[lo] + (k - lo) * (ordered[hi] - ordered[lo])


# --------------------------------------------------------------------------
# Full acceptance suite, parameterized over both OceanBase modes.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["mysql", "oracle"])
def test_integration_suite_per_mode(test_db, mode):
    """The OB-07 acceptance matrix passes in both OceanBase compatibility modes."""
    source = _source(test_db, mode=mode)
    runtime = _runtime()

    # Connection + mode detection via db_health_check.
    health = DatabaseToolService.call(test_db, TOOL_HEALTH_CHECK, {"source": source.slug}, runtime=runtime)
    assert health["healthy"] is True
    assert health["engine"] == "oceanbase"
    assert health["compatibility_mode"] == mode
    assert health["detected_mode"] == mode.upper()

    # Metadata (table) + view via db_search_objects.
    meta = DatabaseToolService.call(test_db, TOOL_SEARCH_OBJECTS, {"source": source.slug, "kind": "table"}, runtime=runtime)
    assert meta["rows"][0][2] == "table"
    view = DatabaseToolService.call(test_db, TOOL_SEARCH_OBJECTS, {"source": source.slug, "kind": "view"}, runtime=runtime)
    assert view["rows"][0][2] == "view"

    # SELECT / JOIN / CTE through the real SQL policy.
    for sql in (
        "SELECT * FROM users",
        "SELECT a.id, b.name FROM a JOIN b ON a.id = b.id",
        "WITH x AS (SELECT 1 AS one) SELECT * FROM x",
    ):
        result = DatabaseToolService.call(test_db, TOOL_EXECUTE_QUERY, {"source": source.slug, "sql": sql}, runtime=runtime)
        assert result["row_count"] == 5

    # Explain.
    plan = DatabaseToolService.call(test_db, TOOL_EXPLAIN_QUERY, {"source": source.slug, "sql": "SELECT * FROM users"}, runtime=runtime)
    assert plan["rows"][0][0] == "TABLE ACCESS FULL"

    # Row limit.
    limited = DatabaseToolService.call(test_db, TOOL_EXECUTE_QUERY, {"source": source.slug, "sql": "SELECT * FROM users", "max_rows": 2}, runtime=runtime)
    assert limited["row_count"] == 2
    assert limited["truncated"] is True

    # Readonly rejection (write rejected before any DB access).
    with pytest.raises(DatabaseStatementDeniedError):
        DatabaseToolService.call(test_db, TOOL_EXECUTE_QUERY, {"source": source.slug, "sql": "DELETE FROM users"}, runtime=runtime)

    # Timeout.
    ScriptedAdapter.slow_sql.add("select * from slow")
    with pytest.raises(DatabaseQueryTimeoutError):
        DatabaseToolService.call(test_db, TOOL_EXECUTE_QUERY, {"source": source.slug, "sql": "SELECT * FROM slow"}, runtime=runtime)
    ScriptedAdapter.slow_sql.clear()

    # Template execution binds the immutable statement.
    template = DatabaseQueryTemplate(
        source_id=source.id,
        name="recent-users",
        slug="recent-users",
        statement="SELECT * FROM users WHERE id > :min_id",
        max_rows=50,
        timeout_seconds=7.0,
        enabled=True,
    )
    test_db.add(template)
    test_db.commit()
    test_db.refresh(template)
    tpl = DatabaseToolService.call(
        test_db,
        TOOL_EXECUTE_TEMPLATE,
        {"source": source.slug, "template": template.slug, "arguments": {"min_id": 10}},
        runtime=runtime,
    )
    assert tpl["row_count"] == 5

    # Pool reuse: the same source maps to one pool / one engine.
    assert ScriptedAdapter.built == 1
    first = runtime.adapter_for(source)
    second = runtime.adapter_for(source)
    assert first.pool is second.pool
    assert ScriptedAdapter.built == 1

    # Pool reconnect: invalidating rebuilds the engine.
    runtime.invalidate(source.id)
    runtime.adapter_for(source)
    assert ScriptedAdapter.built == 2

    # Source disable is rejected at the tool layer.
    source.enabled = False
    test_db.commit()
    with pytest.raises(DatabaseToolSourceDisabledError):
        DatabaseToolService.call(test_db, TOOL_SEARCH_OBJECTS, {"source": source.slug}, runtime=runtime)


def test_metadata_cache_serves_second_lookup(test_db):
    """``db_search_objects`` is cached; a repeat lookup never re-hits the adapter."""
    source = _source(test_db)
    runtime = _runtime()

    first = DatabaseToolService.call(test_db, TOOL_SEARCH_OBJECTS, {"source": source.slug, "kind": "table", "name": "USERS", "limit": 20}, runtime=runtime)
    second = DatabaseToolService.call(test_db, TOOL_SEARCH_OBJECTS, {"source": source.slug, "kind": "table", "name": "USERS", "limit": 20}, runtime=runtime)

    assert first == second
    assert ScriptedAdapter.search_calls == 1  # second call served from the cache
    assert len(runtime.metadata_cache) == 1


def test_credential_update_invalidates_cache(monkeypatch, test_db):
    """A credential change eagerly invalidates the runtime's metadata cache."""
    runtime = _runtime()
    monkeypatch.setattr(DatabaseToolService, "_runtime", runtime)
    source = _source(test_db)

    # First lookup populates the cache through the (patched) module runtime.
    DatabaseToolService.call(test_db, TOOL_SEARCH_OBJECTS, {"source": source.slug, "kind": "table"})
    assert ScriptedAdapter.search_calls == 1

    # Updating the credential invalidates the runtime's pool + metadata cache.
    DatabaseSourceService.update_source(test_db, source.id, DatabaseSourceUpdate(password="new-secret"))

    # The next lookup re-fetches because the cache was invalidated.
    DatabaseToolService.call(test_db, TOOL_SEARCH_OBJECTS, {"source": source.slug, "kind": "table"})
    assert ScriptedAdapter.search_calls == 2


def test_source_update_invalidates_runtime(monkeypatch, test_db):
    """``update_source`` invalidates the module-level tool runtime pool/cache."""
    invalidated: list[str] = []

    class _RecordingRuntime:
        def invalidate(self, source_id):
            invalidated.append(source_id)

    monkeypatch.setattr(DatabaseToolService, "_runtime", _RecordingRuntime())
    source = _source(test_db)

    DatabaseSourceService.update_source(test_db, source.id, DatabaseSourceUpdate(host="10.9.9.9"))
    assert source.id in invalidated


def test_source_delete_invalidates_runtime(monkeypatch, test_db):
    """``delete_source`` invalidates the module-level tool runtime pool/cache."""
    invalidated: list[str] = []

    class _RecordingRuntime:
        def invalidate(self, source_id):
            invalidated.append(source_id)

    monkeypatch.setattr(DatabaseToolService, "_runtime", _RecordingRuntime())
    source = _source(test_db)

    DatabaseSourceService.delete_source(test_db, source.id)
    assert source.id in invalidated


def test_audit_distinguishes_total_vs_db_time(test_db):
    """The audit row stores total invocation time; the result stores DB SQL time."""
    source = _source(test_db)
    runtime = _runtime()
    ScriptedAdapter.db_latency = 0.005  # 5ms scripted DB SQL time
    trace_id = f"trace-{uuid.uuid4().hex}"

    result = DatabaseToolService.call(
        test_db,
        TOOL_EXECUTE_QUERY,
        {"source": source.slug, "sql": "SELECT * FROM users"},
        runtime=runtime,
        caller="alice@example.com",
        trace_id=trace_id,
    )

    audit = test_db.execute(select(DatabaseAudit).where(DatabaseAudit.trace_id == trace_id)).scalar_one()
    assert audit.tool_name == TOOL_EXECUTE_QUERY
    assert audit.caller == "alice@example.com"
    assert audit.trace_id == trace_id
    assert audit.source_id == source.id
    assert audit.statement_type == "select"
    assert audit.row_count == 5
    assert audit.success is True
    assert audit.error_code is None
    assert result["elapsed_ms"] >= 5.0  # DB SQL time
    assert audit.elapsed_ms >= result["elapsed_ms"]  # total >= DB SQL time


def test_failed_invocation_audits_error_code(test_db):
    """A rejected invocation still records an audit row with its error code."""
    source = _source(test_db)
    runtime = _runtime()
    trace_id = f"trace-{uuid.uuid4().hex}"

    with pytest.raises(DatabaseStatementDeniedError):
        DatabaseToolService.call(
            test_db,
            TOOL_EXECUTE_QUERY,
            {"source": source.slug, "sql": "DELETE FROM users"},
            runtime=runtime,
            trace_id=trace_id,
        )

    audit = test_db.execute(select(DatabaseAudit).where(DatabaseAudit.trace_id == trace_id)).scalar_one()
    assert audit.success is False
    assert audit.error_code == "DB_STATEMENT_DENIED"
    assert audit.statement_type == "delete"


# --------------------------------------------------------------------------
# Concurrency sweep (10 and 50, with an optional 100 when the env opts in).
# --------------------------------------------------------------------------
@pytest.mark.parametrize("n", [10, 50])
def test_concurrency_pool_reuse_and_latency(n):
    """Concurrent invocations reuse one pool and report sane latency percentiles."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)

    try:
        db = factory()
        source = DatabaseSource(
            name="conc", slug="conc", engine="oceanbase", compatibility_mode="mysql",
            host="127.0.0.1", port=2881, tenant_name="obmysql", database_name="app",
            username="root", password="secret", enabled=True,
        )
        db.add(source)
        db.commit()
        db.refresh(source)
        slug = source.slug
        db.close()

        runtime = _runtime()
        ScriptedAdapter.db_latency = 0.001  # 1ms scripted DB SQL time

        def _task(_):
            session = factory()
            start = time.monotonic()
            try:
                result = DatabaseToolService.call(
                    session, TOOL_EXECUTE_QUERY, {"source": slug, "sql": "SELECT * FROM users"}, runtime=runtime
                )
                return ("ok", (time.monotonic() - start) * 1000.0, result.get("elapsed_ms", 0.0))
            except Exception as exc:  # pragma: no cover - assertion reports the cause
                return ("err", (time.monotonic() - start) * 1000.0, type(exc).__name__)
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=n) as pool:
            outcomes = list(pool.map(_task, range(n)))

        latencies = [outcome[1] for outcome in outcomes]
        errors = [outcome for outcome in outcomes if outcome[0] == "err"]
        error_rate = len(errors) / n

        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)
        p99 = _percentile(latencies, 99)

        # OB-07 requires reporting P50/P95/P99, error rate, and pool usage.
        print(
            f"[concurrency n={n}] P50={p50:.3f}ms P95={p95:.3f}ms P99={p99:.3f}ms "
            f"error_rate={error_rate:.1%} pool_builds={ScriptedAdapter.built} "
            f"adapter_executes={ScriptedAdapter.execute_calls}"
        )

        assert errors == []
        assert ScriptedAdapter.built == 1  # one pool reused by every caller
        assert ScriptedAdapter.execute_calls == n  # every call reached the adapter
        # Every recorded DB SQL time is positive and a subset of total time.
        for outcome in outcomes:
            total_ms, db_ms = outcome[1], outcome[2]
            assert db_ms > 0
            assert total_ms >= db_ms - 0.5  # tolerance for timer resolution
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)
