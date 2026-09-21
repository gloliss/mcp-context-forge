# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/adapters/database/test_sql_policy.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the SQL execution policy (OB-04).

Covers the required cases: statement classification (SELECT / WITH SELECT /
WITH DELETE / INSERT / UPDATE / DELETE / MERGE / CREATE / ALTER / DROP /
TRUNCATE / EXPLAIN / SHOW / DESCRIBE / GRANT / REVOKE / comments), multi
statement rejection, read-only enforcement (writes denied before any database
access), parameter binding (no string concatenation), row limits with the
``min(request, policy)`` cap, and structured connection/query timeouts.
"""

# Standard
import time

# Third-Party
import pytest
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

# First-Party
from mcpgateway.adapters.database import (
    DB_CONNECTION_TIMEOUT,
    DB_MULTI_STATEMENT_DENIED,
    DB_QUERY_TIMEOUT,
    DB_STATEMENT_DENIED,
    ConnectionPool,
    DatabaseConnectionTimeoutError,
    DatabaseMultiStatementError,
    DatabaseQueryTimeoutError,
    DatabaseStatementDeniedError,
    PoolConfig,
    PoolIdentity,
)
from mcpgateway.adapters.database.adapters import MySQLAdapter
from mcpgateway.adapters.database.sql_policy import SqlPolicy, SqlStatementClassifier
from mcpgateway.db import DatabaseSource


# --------------------------------------------------------------------------
# Fakes: a scripted engine that records calls and can simulate timeouts.
# --------------------------------------------------------------------------
class RecordingResult:
    """A minimal ``CursorResult`` stand-in returning canned rows."""

    def __init__(self, columns, rows):
        self._columns = list(columns)
        self._rows = list(rows)

    def keys(self):
        """Return the column names."""
        return self._columns

    def fetchmany(self, size):
        """Return up to ``size`` rows."""
        return list(self._rows[:size])


class RecordingConnection:
    """A connection that records the SQL/params and optionally sleeps."""

    def __init__(self, engine, delay=0.0):
        self._engine = engine
        self._delay = delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        """Record the bound SQL and return the engine's canned result."""
        if self._delay:
            time.sleep(self._delay)
        self._engine.calls.append((str(stmt), params))
        return RecordingResult(self._engine.columns, self._engine.rows)


class FakeEngine:
    """A fake engine tracking connect calls and simulating failure modes."""

    def __init__(self, columns=("n",), rows=(), delay=0.0, connect_error=None):
        self.columns = list(columns)
        self.rows = list(rows)
        self.delay = delay
        self.connect_error = connect_error
        self.calls = []
        self.connect_calls = 0
        self.disposed = False

    def connect(self):
        """Return a recording connection, or raise ``connect_error``."""
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        return RecordingConnection(self, delay=self.delay)

    def dispose(self):
        """Mark the engine disposed."""
        self.disposed = True


def _adapter(policy_config=None, **engine_kwargs):
    """Build a MySQL adapter over a scripted fake engine."""
    data = {
        "name": "test-source",
        "slug": "test-source",
        "engine": "mysql",
        "host": "127.0.0.1",
        "port": 3306,
        "database_name": "testdb",
        "username": "user",
        "password": "secret",
    }
    if policy_config is not None:
        data["policy_config"] = policy_config
    source = DatabaseSource(**data)
    engine = FakeEngine(**engine_kwargs)
    pool = ConnectionPool(identity=PoolIdentity.from_source(source), engine=engine, pool_config=PoolConfig())
    return MySQLAdapter(source, pool=pool), engine


# --------------------------------------------------------------------------
# Statement classification (the SQL parser is not ``startswith``)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", "select"),
        ("select * from t", "select"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "select"),
        ("with x as (select 1) delete from t", "delete"),
        ("INSERT INTO t VALUES (1)", "insert"),
        ("UPDATE t SET a=1", "update"),
        ("DELETE FROM t", "delete"),
        ("MERGE INTO t USING s ON (t.id=s.id) WHEN MATCHED THEN UPDATE SET a=s.a", "merge"),
        ("CREATE TABLE t (id int)", "create"),
        ("ALTER TABLE t ADD COLUMN c int", "alter"),
        ("DROP TABLE t", "drop"),
        ("TRUNCATE TABLE t", "truncate"),
        ("EXPLAIN SELECT * FROM t", "explain"),
        ("SHOW TABLES", "show"),
        ("DESCRIBE t", "describe"),
        ("GRANT SELECT ON t TO u", "grant"),
        ("REVOKE SELECT ON t FROM u", "revoke"),
        ("-- leading comment\nSELECT 1", "select"),
        ("/* block */ SELECT 1", "select"),
    ],
)
def test_classify_single_statement_type(sql, expected):
    """The classifier maps each statement to its normalized lowercase type."""
    classification = SqlStatementClassifier().classify(sql)
    assert classification.statement_types == (expected,)
    assert classification.statement_count == 1
    assert classification.is_multi_statement is False


def test_classify_multi_statement():
    """Two meaningful statements are reported as multi-statement."""
    classification = SqlStatementClassifier().classify("SELECT 1; DROP TABLE t")
    assert classification.statement_types == ("select", "drop")
    assert classification.statement_count == 2
    assert classification.is_multi_statement is True


def test_classify_trailing_semicolon_is_single():
    """A trailing semicolon is not a second statement."""
    classification = SqlStatementClassifier().classify("SELECT 1;")
    assert classification.statement_types == ("select",)
    assert classification.is_multi_statement is False


def test_classify_comment_only_is_empty():
    """Comment-only input yields no meaningful statement."""
    classification = SqlStatementClassifier().classify("-- just a comment")
    assert classification.statement_count == 0
    assert classification.is_multi_statement is False


# --------------------------------------------------------------------------
# Policy defaults and coercion
# --------------------------------------------------------------------------
def test_policy_defaults():
    """OB-04 defaults: read-only, no multi-statement, 1000 rows, 15s."""
    policy = SqlPolicy()
    assert policy.readonly is True
    assert policy.allow_multi_statement is False
    assert policy.allowed_statement_types == frozenset({"select", "show", "describe", "explain"})
    assert policy.max_rows == 1000
    assert policy.query_timeout_seconds == 15.0


def test_policy_from_source_overrides():
    """Source ``policy_config`` overrides every default."""
    source = DatabaseSource(
        name="s",
        slug="s",
        engine="mysql",
        host="h",
        port=3306,
        policy_config={
            "readonly": False,
            "allow_multi_statement": True,
            "max_rows": 50,
            "query_timeout_seconds": 2,
        },
    )
    policy = SqlPolicy.from_source(source)
    assert policy.readonly is False
    assert policy.allow_multi_statement is True
    assert policy.max_rows == 50
    assert policy.query_timeout_seconds == 2.0


def test_policy_from_source_defaults_when_empty():
    """An absent ``policy_config`` yields the secure defaults."""
    source = DatabaseSource(name="s", slug="s", engine="mysql", host="h", port=3306)
    policy = SqlPolicy.from_source(source)
    assert policy.readonly is True
    assert policy.max_rows == 1000
    assert policy.query_timeout_seconds == 15.0


# --------------------------------------------------------------------------
# Read-only enforcement: writes are rejected before touching the database
# --------------------------------------------------------------------------
def test_readonly_denies_delete_without_touching_db():
    """A write is denied before any connection is acquired."""
    adapter, engine = _adapter()
    with pytest.raises(DatabaseStatementDeniedError) as exc_info:
        adapter.execute("DELETE FROM t")
    assert exc_info.value.code == DB_STATEMENT_DENIED
    assert exc_info.value.statement_type == "delete"
    assert engine.connect_calls == 0


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("INSERT INTO t VALUES (1)", "insert"),
        ("UPDATE t SET a=1", "update"),
        ("DELETE FROM t", "delete"),
        ("MERGE INTO t USING s ON (t.id=s.id) WHEN MATCHED THEN UPDATE SET a=s.a", "merge"),
        ("CREATE TABLE t (id int)", "create"),
        ("ALTER TABLE t ADD COLUMN c int", "alter"),
        ("DROP TABLE t", "drop"),
        ("TRUNCATE TABLE t", "truncate"),
        ("GRANT SELECT ON t TO u", "grant"),
        ("REVOKE SELECT ON t FROM u", "revoke"),
    ],
)
def test_default_readonly_denies_write_types(sql, kind):
    """The default read-only policy denies every write statement type."""
    adapter, engine = _adapter()
    with pytest.raises(DatabaseStatementDeniedError) as exc_info:
        adapter.execute(sql)
    assert exc_info.value.statement_type == kind
    assert engine.connect_calls == 0


def test_cte_cannot_bypass_readonly():
    """A CTE-prefixed DELETE is still classified as a write and denied."""
    adapter, engine = _adapter()
    with pytest.raises(DatabaseStatementDeniedError) as exc_info:
        adapter.execute("WITH x AS (SELECT 1) DELETE FROM t")
    assert exc_info.value.statement_type == "delete"
    assert engine.connect_calls == 0


def test_with_select_is_allowed():
    """A CTE-prefixed SELECT is a read and executes normally."""
    adapter, engine = _adapter(rows=((1,),))
    result = adapter.execute("WITH x AS (SELECT 1) SELECT * FROM x")
    assert result.row_count == 1
    assert engine.connect_calls == 1


def test_select_is_allowed_by_default():
    """A plain SELECT executes under the default policy."""
    adapter, engine = _adapter(rows=((1,), (2,)))
    result = adapter.execute("SELECT n FROM t")
    assert result.columns == ["n"]
    assert result.rows == [[1], [2]]
    assert engine.connect_calls == 1


# --------------------------------------------------------------------------
# Multi-statement control
# --------------------------------------------------------------------------
def test_multi_statement_denied():
    """Multiple statements are rejected by default before any database access."""
    adapter, engine = _adapter()
    with pytest.raises(DatabaseMultiStatementError) as exc_info:
        adapter.execute("SELECT 1; DROP TABLE t")
    assert exc_info.value.code == DB_MULTI_STATEMENT_DENIED
    assert exc_info.value.statement_count == 2
    assert engine.connect_calls == 0


def test_trailing_semicolon_is_allowed():
    """A single statement with a trailing semicolon is not multi-statement."""
    adapter, engine = _adapter(rows=((1,),))
    result = adapter.execute("SELECT 1;")
    assert result.row_count == 1
    assert engine.connect_calls == 1


def test_multi_statement_allowed_when_configured():
    """Explicitly enabling multi-statement allows multiple reads."""
    adapter, engine = _adapter(policy_config={"allow_multi_statement": True}, rows=((1,),))
    result = adapter.execute("SELECT 1; SELECT 2")
    assert engine.connect_calls == 1
    assert result.row_count == 1


def test_empty_sql_denied():
    """Empty or comment-only SQL is rejected as having no statement."""
    adapter, engine = _adapter()
    with pytest.raises(DatabaseStatementDeniedError) as exc_info:
        adapter.execute("")
    assert exc_info.value.statement_type == "empty"
    assert engine.connect_calls == 0


# --------------------------------------------------------------------------
# Parameter binding, row limit, timeouts
# --------------------------------------------------------------------------
def test_parameters_bound_not_concatenated():
    """Parameters are passed as a bound dict, never spliced into the SQL."""
    adapter, engine = _adapter(rows=((1,),))
    sql = "SELECT :name FROM t WHERE x = :val"
    params = {"name": "alice", "val": "b'; DROP TABLE users;--"}
    adapter.execute(sql, params=params)
    recorded_sql, recorded_params = engine.calls[-1]
    assert recorded_sql == sql
    assert recorded_params == params


def test_max_rows_capped_by_policy():
    """A request larger than the policy cap is silently clamped to the cap."""
    adapter, engine = _adapter(policy_config={"max_rows": 3}, rows=tuple((i,) for i in range(5)))
    result = adapter.execute("SELECT n FROM t", max_rows=10)
    assert result.row_count == 3
    assert result.rows == [[0], [1], [2]]
    assert result.truncated is True


def test_max_rows_request_smaller_than_policy():
    """A request smaller than the policy cap wins."""
    adapter, engine = _adapter(policy_config={"max_rows": 3}, rows=tuple((i,) for i in range(5)))
    result = adapter.execute("SELECT n FROM t", max_rows=2)
    assert result.row_count == 2
    assert result.truncated is True


def test_max_rows_within_policy_not_truncated():
    """A result under the cap is not marked truncated."""
    adapter, engine = _adapter(rows=tuple((i,) for i in range(3)))
    result = adapter.execute("SELECT n FROM t")
    assert result.row_count == 3
    assert result.truncated is False


def test_query_timeout_raises_structured_error():
    """A slow statement raises ``DatabaseQueryTimeoutError`` with its code."""
    adapter, engine = _adapter(delay=0.3, rows=((1,),))
    with pytest.raises(DatabaseQueryTimeoutError) as exc_info:
        adapter.execute("SELECT 1", query_timeout=0.05)
    assert exc_info.value.code == DB_QUERY_TIMEOUT


def test_connection_timeout_raises_structured_error():
    """A pool/connect timeout raises ``DatabaseConnectionTimeoutError``."""
    adapter, engine = _adapter(connect_error=SQLAlchemyTimeoutError("QueuePool limit reached"))
    with pytest.raises(DatabaseConnectionTimeoutError) as exc_info:
        adapter.execute("SELECT 1")
    assert exc_info.value.code == DB_CONNECTION_TIMEOUT
