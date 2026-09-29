# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/adapters/database/test_database_runtime.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the database runtime core (OB-02).

Covers the required cases: registry registration, adapter creation, unknown
engine, unknown mode, pool create, pool reuse, pool invalidate, pool close, and
connection failure — plus the unified result contract and truncation.
"""

# Standard
import ssl
from types import SimpleNamespace

# Third-Party
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

# First-Party
from mcpgateway.adapters.database import (
    AdapterRegistry,
    ConnectionFailureError,
    ConnectionPool,
    DatabaseAdapterError,
    DatabaseRuntimeClient,
    METADATA_TYPE_SCHEMA,
    PoolConfig,
    PoolIdentity,
    PoolManager,
    QueryResult,
    SQLAlchemyDatabaseAdapter,
    UnknownCompatibilityModeError,
    UnknownEngineError,
    get_default_registry,
)
from mcpgateway.adapters.database.adapters import (
    MySQLAdapter,
    OracleAdapter,
    PostgreSQLAdapter,
)
from mcpgateway.adapters.database.oceanbase import OceanBaseAdapter
from mcpgateway.db import DatabaseSource


# --------------------------------------------------------------------------
# Fakes: no external driver is installed, so engines/connections are stubbed.
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


class FakeConnection:
    """A minimal SQLAlchemy ``Connection`` stand-in."""

    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        return FakeResult(self._columns, self._rows)

    def close(self):
        pass


class FakeEngine:
    """A minimal SQLAlchemy ``Engine`` stand-in tracking disposal/connects."""

    def __init__(self, fail_connect=False, columns=("col",), rows=((1,),)):
        self.fail_connect = fail_connect
        self.disposed = False
        self.connect_calls = 0
        self._columns = columns
        self._rows = rows

    def connect(self):
        self.connect_calls += 1
        if self.fail_connect:
            raise OperationalError("SELECT 1", {}, ConnectionError("connection refused"))
        return FakeConnection(self._columns, self._rows)

    def dispose(self):
        self.disposed = True


class CountingFactory:
    """Engine factory that records how many times it was invoked."""

    def __init__(self, engine=None):
        self.engine = engine or FakeEngine()
        self.calls = 0

    def __call__(self, source, pool_config):
        self.calls += 1
        return self.engine


def make_source(**overrides) -> DatabaseSource:
    """Build an unpersisted ``DatabaseSource`` with sensible defaults."""
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
    data.update(overrides)
    return DatabaseSource(**data)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
def test_registry_registration():
    """A registered adapter is returned by lookup for its engine/mode key."""
    registry = AdapterRegistry()
    registry.register("oceanbase", "mysql", OceanBaseAdapter)
    assert registry.lookup("oceanbase", "mysql") is OceanBaseAdapter


def test_adapter_creation_from_source():
    """Every supported engine/mode resolves to the correct adapter class."""
    registry = get_default_registry()
    cases = [
        ({"engine": "oceanbase", "compatibility_mode": "mysql"}, OceanBaseAdapter),
        ({"engine": "oceanbase", "compatibility_mode": "oracle"}, OceanBaseAdapter),
        ({"engine": "oracle", "compatibility_mode": None}, OracleAdapter),
        ({"engine": "mysql", "compatibility_mode": None}, MySQLAdapter),
        ({"engine": "postgresql", "compatibility_mode": None}, PostgreSQLAdapter),
    ]
    for overrides, expected_cls in cases:
        source = make_source(**overrides)
        pool = ConnectionPool(identity=PoolIdentity.from_source(source), engine=FakeEngine(), pool_config=PoolConfig())
        adapter = registry.create_adapter(source, pool=pool)
        assert isinstance(adapter, expected_cls)
        assert adapter.source is source
        assert adapter.pool is pool


def test_unknown_engine():
    """An engine with no registration raises UnknownEngineError."""
    with pytest.raises(UnknownEngineError):
        get_default_registry().lookup("mongodb", None)


def test_unknown_mode():
    """A known engine with an unknown mode raises UnknownCompatibilityModeError."""
    with pytest.raises(UnknownCompatibilityModeError):
        get_default_registry().lookup("oceanbase", "postgresql")


# --------------------------------------------------------------------------
# Pool lifecycle
# --------------------------------------------------------------------------
def test_pool_create():
    """Acquiring a source builds exactly one pool."""
    manager = PoolManager()
    factory = CountingFactory()
    pool = manager.acquire(make_source(), engine_factory=factory)

    assert isinstance(pool, ConnectionPool)
    assert factory.calls == 1
    assert len(manager) == 1


def test_pool_reuse():
    """A second acquire for the same source reuses the same pool/engine."""
    manager = PoolManager()
    factory = CountingFactory()
    source = make_source()

    first = manager.acquire(source, engine_factory=factory)
    second = manager.acquire(source, engine_factory=factory)

    assert first is second
    assert factory.calls == 1


def test_pool_invalidate_on_host_change():
    """Changing an identity field invalidates the old pool and builds a new one."""
    manager = PoolManager()
    factory = CountingFactory()
    source = make_source()

    old_pool = manager.acquire(source, engine_factory=factory)
    source.host = "10.0.0.2"
    new_pool = manager.acquire(source, engine_factory=factory)

    assert new_pool is not old_pool
    assert factory.calls == 2
    assert old_pool.closed is True
    assert new_pool.closed is False


def test_pool_invalidate_on_password_change():
    """A password change (captured as a digest) also invalidates the pool."""
    manager = PoolManager()
    factory = CountingFactory()
    source = make_source(password="first")

    old_pool = manager.acquire(source, engine_factory=factory)
    source.password = "second"
    new_pool = manager.acquire(source, engine_factory=factory)

    assert new_pool is not old_pool
    assert factory.calls == 2


def test_pool_close_disposes_engine():
    """Disposing a pool is idempotent and releases the engine."""
    engine = FakeEngine()
    pool = ConnectionPool(identity=PoolIdentity.from_source(make_source()), engine=engine, pool_config=PoolConfig())

    assert pool.closed is False
    pool.dispose()
    assert pool.closed is True
    assert engine.disposed is True
    pool.dispose()
    assert engine.disposed is True


def test_pool_manager_close():
    """Closing a source through the manager disposes its pool and engine."""
    manager = PoolManager()
    engine = FakeEngine()
    source = make_source(id="src-1")

    manager.acquire(source, engine_factory=CountingFactory(engine))
    assert len(manager) == 1

    manager.close("src-1")
    assert len(manager) == 0
    assert engine.disposed is True


# --------------------------------------------------------------------------
# Connection failure + result contract
# --------------------------------------------------------------------------
def test_connection_failure_raises():
    """An unreachable database raises ConnectionFailureError, never a raw DBAPI."""
    source = make_source()
    engine = FakeEngine(fail_connect=True)
    pool = ConnectionPool(identity=PoolIdentity.from_source(source), engine=engine, pool_config=PoolConfig())
    adapter = MySQLAdapter(source, pool=pool)

    with pytest.raises(ConnectionFailureError):
        adapter.test_connection()
    with pytest.raises(ConnectionFailureError):
        adapter.execute("SELECT 1")

    health = adapter.health_check()
    assert health["ok"] is False
    assert "error" in health


def test_execute_truncates_rows():
    """Execute returns the unified contract and truncates at the row cap."""
    engine = FakeEngine(columns=("n",), rows=tuple((i,) for i in range(5)))
    source = make_source(policy_config={"max_rows": 2})
    pool = ConnectionPool(identity=PoolIdentity.from_source(source), engine=engine, pool_config=PoolConfig())
    adapter = MySQLAdapter(source, pool=pool)

    result = adapter.execute("SELECT n FROM t")

    assert result.columns == ["n"]
    assert result.rows == [[0], [1]]
    assert result.row_count == 2
    assert result.truncated is True
    assert result.warnings


def test_result_contract_shape():
    """The serialized result contract exposes exactly the six stable keys."""
    assert QueryResult().to_dict() == {
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "elapsed_ms": 0.0,
        "warnings": [],
    }
    filled = QueryResult(columns=["a"], rows=[[1]], row_count=1)
    assert filled.to_dict()["columns"] == ["a"]
    assert filled.to_dict()["row_count"] == 1


# --------------------------------------------------------------------------
# Runtime client composition
# --------------------------------------------------------------------------
class _TestAdapter(SQLAlchemyDatabaseAdapter):
    """Adapter stub whose engine factory never touches a real driver."""

    dialect_driver = "test+test"

    @classmethod
    def build_engine(cls, source, pool_config):
        return FakeEngine()


def test_runtime_client_reuses_pool():
    """The runtime client returns a pooled adapter and reuses its pool."""
    registry = AdapterRegistry()
    registry.register("mysql", None, _TestAdapter)
    client = DatabaseRuntimeClient(registry=registry, pool_manager=PoolManager())

    source = make_source()
    first = client.adapter_for(source)
    second = client.adapter_for(source)

    assert isinstance(first, _TestAdapter)
    assert first.pool is second.pool


# --------------------------------------------------------------------------
# TLS connect args: ssl_mode must reach the driver
# --------------------------------------------------------------------------
def _ca_bundle(monkeypatch, tmp_path) -> str:
    """Point the system CA lookup at an existing file and return its path."""
    bundle = tmp_path / "ca.pem"
    bundle.write_text("")
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: SimpleNamespace(cafile=str(bundle)))
    return str(bundle)


@pytest.mark.parametrize(
    "adapter_cls,ssl_mode,expected",
    [
        (MySQLAdapter, None, {}),
        (MySQLAdapter, "preferred", {}),
        (MySQLAdapter, "disabled", {"ssl_disabled": True}),
        (MySQLAdapter, "required", {"ssl_verify_cert": False}),
        (PostgreSQLAdapter, None, {}),
        (PostgreSQLAdapter, "disabled", {"sslmode": "disable"}),
        (PostgreSQLAdapter, "preferred", {"sslmode": "prefer"}),
        (PostgreSQLAdapter, "required", {"sslmode": "require"}),
        (OracleAdapter, None, {}),
        (OracleAdapter, "disabled", {"protocol": "tcp"}),
        (OracleAdapter, "preferred", {"protocol": "tcps"}),
        (OracleAdapter, "required", {"protocol": "tcps"}),
    ],
)
def test_ssl_mode_reaches_the_driver(adapter_cls, ssl_mode, expected):
    """Every mappable mode becomes driver parameters that enforce it."""
    assert adapter_cls.build_connect_args(make_source(ssl_mode=ssl_mode)) == expected


@pytest.mark.parametrize("adapter_cls", [MySQLAdapter, PostgreSQLAdapter])
@pytest.mark.parametrize("ssl_mode", ["verify_ca", "verify_full"])
def test_verifying_ssl_modes_pin_the_system_ca_bundle(adapter_cls, ssl_mode, monkeypatch, tmp_path):
    """The verifying modes name the trust store rather than leaving it implicit."""
    bundle = _ca_bundle(monkeypatch, tmp_path)
    args = adapter_cls.build_connect_args(make_source(ssl_mode=ssl_mode))

    if adapter_cls is MySQLAdapter:
        assert args["ssl_ca"] == bundle
        assert args["ssl_verify_cert"] is True
        # PyMySQL only verifies the hostname when it was given a CA to verify with.
        assert bool(args.get("ssl_verify_identity")) == (ssl_mode == "verify_full")
    else:
        assert args["sslrootcert"] == bundle
        assert args["sslmode"] == ssl_mode.replace("_", "-")


def test_missing_ca_bundle_fails_closed(monkeypatch):
    """A verifying mode with no discoverable trust store refuses to connect."""
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: SimpleNamespace(cafile=None))

    with pytest.raises(DatabaseAdapterError):
        MySQLAdapter.build_connect_args(make_source(ssl_mode="verify_ca"))


def test_postgres_connect_args_keep_the_search_path():
    """The TLS mapping composes with the schema search path."""
    args = PostgreSQLAdapter.build_connect_args(make_source(ssl_mode="required", schema_name="reporting"))

    assert args == {"options": "-csearch_path=reporting", "sslmode": "require"}


def test_unknown_ssl_mode_is_rejected():
    """An unrecognized mode is an error, never a silent default."""
    with pytest.raises(DatabaseAdapterError):
        MySQLAdapter.build_connect_args(make_source(ssl_mode="sometimes"))


@pytest.mark.parametrize("ssl_mode", ["verify_ca", "verify_full"])
def test_oracle_refuses_unenforceable_ssl_modes(ssl_mode):
    """Oracle verification needs a wallet, so it is refused, not downgraded."""
    with pytest.raises(DatabaseAdapterError):
        OracleAdapter.build_connect_args(make_source(engine="oracle", ssl_mode=ssl_mode))


# --------------------------------------------------------------------------
# Metadata reflection shared by the non-OceanBase adapters
# --------------------------------------------------------------------------
class _ReflectionAdapter(SQLAlchemyDatabaseAdapter):
    """Adapter bound to an in-memory SQLite engine, for reflection tests.

    The generic metadata path reflects through whatever engine the pool holds,
    so SQLite exercises it without an external server.
    """

    dialect_driver = "sqlite+pysqlite"


def test_generic_adapter_lists_schemas():
    """The shared adapter answers the schema contract for a non-OceanBase engine."""
    source = make_source()
    pool = ConnectionPool(
        identity=PoolIdentity.from_source(source),
        engine=create_engine("sqlite+pysqlite:///:memory:"),
        pool_config=PoolConfig(),
    )
    adapter = _ReflectionAdapter(source, pool=pool)

    schemas = adapter.list_schemas()

    assert [obj.name for obj in schemas] == ["main"]
    assert schemas[0].type == METADATA_TYPE_SCHEMA
    assert schemas[0].schema is None
