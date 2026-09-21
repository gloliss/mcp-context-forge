# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/base.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

The :class:`DatabaseAdapter` interface and its SQLAlchemy base (OB-02).

``DatabaseAdapter`` is the contract every engine implements, so the tool layer
never branches on ``engine == ...``.  :class:`SQLAlchemyDatabaseAdapter` is a
shared, driver-agnostic implementation: concrete adapters only supply their
dialect driver and a handful of dialect-specific SQL fragments.
"""

# Standard
from abc import ABC, abstractmethod
from time import monotonic
from typing import Any, Optional
import threading

# Third-Party
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import OperationalError, SQLAlchemyError, TimeoutError as SQLAlchemyTimeoutError

# First-Party
from mcpgateway.adapters.database.exceptions import (
    AdapterNotAvailableError,
    ConnectionFailureError,
    DatabaseAdapterError,
    DatabaseConnectionTimeoutError,
    DatabaseQueryTimeoutError,
    QueryError,
)
from mcpgateway.adapters.database.pool import ConnectionPool, PoolConfig, PoolIdentity
from mcpgateway.adapters.database.sql_policy import SqlPolicy, SqlPolicyGuard, SqlStatementClassifier
from mcpgateway.adapters.database.types import QueryResult


class DatabaseAdapter(ABC):
    """Protocol decoupling database engines from the ContextForge tool layer.

    Implementations are looked up via the adapter registry keyed on
    ``(engine, compatibility_mode)``; business code never branches on engine.
    """

    @abstractmethod
    def test_connection(self) -> dict[str, Any]:
        """Probe connectivity and return ``{"ok": ..., "latency_ms": ...}``."""

    @abstractmethod
    def detect_mode(self) -> Optional[str]:
        """Best-effort detection of the wire mode, or ``None`` when unknown."""

    @abstractmethod
    def execute(self, sql: str, params: Optional[dict] = None, max_rows: Optional[int] = None) -> QueryResult:
        """Run a single SQL statement and return the unified result contract."""

    @abstractmethod
    def search_objects(self, name: Optional[str] = None, kind: Optional[str] = None, limit: int = 100) -> QueryResult:
        """List schema objects (tables/views) visible to the source."""

    @abstractmethod
    def explain(self, sql: str) -> QueryResult:
        """Return the engine's execution plan for ``sql``."""

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Return health plus pool state without raising."""

    @abstractmethod
    def close(self) -> None:
        """Release the adapter's underlying pool."""


class SQLAlchemyDatabaseAdapter(DatabaseAdapter):
    """Shared SQLAlchemy implementation; subclasses define the dialect only."""

    #: SQLAlchemy dialect+driver, e.g. ``mysql+pymysql``.
    dialect_driver: str = ""

    def __init__(self, source: Any, pool: Optional[ConnectionPool] = None, pool_config: Optional[PoolConfig] = None):
        """Bind an adapter to a source, reusing ``pool`` when supplied.

        Args:
            source: A ``DatabaseSource`` (or compatible object).
            pool: Optional pre-built pool.  When omitted, a pool is built with
                :meth:`build_engine`, which requires the engine driver.
            pool_config: Optional pool tuning; defaults to the source's config.
        """
        self._source = source
        self._pool_config = pool_config or PoolConfig.from_source(source)
        if pool is not None:
            self._pool = pool
        else:
            identity = PoolIdentity.from_source(source)
            self._pool = ConnectionPool(
                identity=identity,
                engine=self.build_engine(source, self._pool_config),
                pool_config=self._pool_config,
            )

    @property
    def source(self) -> Any:
        """Return the bound database source."""
        return self._source

    @property
    def pool(self) -> ConnectionPool:
        """Return the adapter's connection pool."""
        return self._pool

    # ------------------------------------------------------------------
    # Engine construction (overridable per adapter)
    # ------------------------------------------------------------------
    @classmethod
    def build_engine(cls, source: Any, pool_config: PoolConfig) -> Engine:
        """Build a pooled SQLAlchemy engine for ``source``.

        Never reuses a global connection: each call returns a fresh engine
        whose connection pool is owned by the enclosing :class:`ConnectionPool`.
        """
        try:
            return create_engine(
                cls.build_url(source),
                connect_args=cls.build_connect_args(source),
                pool_pre_ping=bool(pool_config.pre_ping),
                pool_size=int(pool_config.pool_size),
                max_overflow=int(pool_config.max_overflow),
                pool_timeout=float(pool_config.acquire_timeout_seconds),
                pool_recycle=int(pool_config.recycle_seconds),
            )
        except ImportError as exc:  # includes ModuleNotFoundError
            raise AdapterNotAvailableError(f"Database driver for {cls.dialect_driver!r} is not installed: {exc}") from exc

    @classmethod
    def build_url(cls, source: Any) -> URL:
        """Render the SQLAlchemy connection URL from the source."""
        return URL.create(
            cls.dialect_driver,
            username=getattr(source, "username", None),
            password=getattr(source, "password", None),
            host=getattr(source, "host", None) or "localhost",
            port=getattr(source, "port", None),
            database=cls._url_database(source),
        )

    @classmethod
    def _url_database(cls, source: Any) -> Optional[str]:
        """Return the URL database component (schema/service) for ``source``."""
        return getattr(source, "database_name", None)

    @classmethod
    def build_connect_args(cls, source: Any) -> dict[str, Any]:
        """Return driver ``connect_args``; ssl/charset/timezone mapping is deferred."""
        return {}

    # ------------------------------------------------------------------
    # Interface implementation
    # ------------------------------------------------------------------
    def test_connection(self) -> dict[str, Any]:
        """Probe connectivity with a trivial round-trip query."""
        start = monotonic()
        try:
            with self._pool.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise ConnectionFailureError(str(exc)) from exc
        return {"ok": True, "latency_ms": round((monotonic() - start) * 1000.0, 3), "error": None}

    def detect_mode(self) -> Optional[str]:
        """Probe the version string and infer the wire mode."""
        try:
            result = self.execute(self._version_sql(), max_rows=1)
        except DatabaseAdapterError:
            return None
        version = " ".join(str(value) for row in result.rows for value in row)
        return self._infer_mode(version)

    def execute(self, sql: str, params: Optional[dict] = None, max_rows: Optional[int] = None, query_timeout: Optional[float] = None) -> QueryResult:
        """Run ``sql`` through the SQL policy and return the unified result.

        The statement is parsed and classified before any database access, so a
        write or multi-statement submission is rejected without touching the pool.
        """
        policy = self._policy()
        classification = SqlStatementClassifier().classify(sql)
        SqlPolicyGuard(policy).check(classification)

        limit = self._effective_max_rows(max_rows, policy)
        timeout = float(query_timeout) if query_timeout is not None else policy.query_timeout_seconds
        bound = dict(params or {})
        start = monotonic()
        try:
            columns, fetched = self._execute_bound(sql, bound, limit, timeout)
        except SQLAlchemyTimeoutError as exc:
            raise DatabaseConnectionTimeoutError(str(exc)) from exc
        except OperationalError as exc:
            raise ConnectionFailureError(str(exc)) from exc
        except SQLAlchemyError as exc:
            raise QueryError(str(exc)) from exc

        truncated = len(fetched) > limit
        if truncated:
            fetched = fetched[:limit]
        warnings = [f"Result truncated to {limit} rows"] if truncated else []

        return QueryResult(
            columns=columns,
            rows=fetched,
            row_count=len(fetched),
            truncated=truncated,
            elapsed_ms=round((monotonic() - start) * 1000.0, 3),
            warnings=warnings,
        )

    def _execute_bound(self, sql: str, bound: dict, limit: int, timeout: float) -> tuple[list[str], list[list[Any]]]:
        """Execute a bound statement in a daemon worker bounded by ``timeout``.

        The worker is a daemon so a timed-out query cannot block process
        shutdown; the abandoned in-flight statement is a documented trade-off
        whose production mitigation is a driver-side statement timeout.
        """
        outcome: dict[str, Any] = {}

        def _run() -> None:
            try:
                with self._pool.connect() as conn:
                    result = conn.execute(text(sql), bound)
                    outcome["columns"] = [str(column) for column in result.keys()]
                    outcome["rows"] = [list(row) for row in result.fetchmany(limit + 1)]
            except Exception as exc:  # transport the DB error to the caller thread
                outcome["error"] = exc

        worker = threading.Thread(target=_run, daemon=True, name="cf-db-query")
        worker.start()
        worker.join(timeout=timeout)
        if worker.is_alive():
            raise DatabaseQueryTimeoutError(f"Query exceeded the {timeout}s timeout")
        error = outcome.get("error")
        if error is not None:
            raise error
        return outcome["columns"], outcome["rows"]

    def search_objects(self, name: Optional[str] = None, kind: Optional[str] = None, limit: int = 100) -> QueryResult:
        """List visible schema objects via the dialect's metadata query."""
        sql, params = self._search_objects_query(name, kind, limit)
        return self.execute(sql, params, max_rows=int(limit))

    def explain(self, sql: str) -> QueryResult:
        """Return the engine's execution plan for ``sql``."""
        return self.execute(self._explain_sql(sql))

    def health_check(self) -> dict[str, Any]:
        """Return health plus pool state, never raising on a dead database."""
        pool_stats = self._pool.stats()
        try:
            probe = self.test_connection()
        except ConnectionFailureError as exc:
            return {"ok": False, "latency_ms": None, "error": str(exc), "pool": pool_stats}
        return {"ok": True, "latency_ms": probe["latency_ms"], "error": None, "pool": pool_stats}

    def close(self) -> None:
        """Dispose the adapter's connection pool."""
        self._pool.dispose()

    # ------------------------------------------------------------------
    # Dialect hooks (subclasses supply these fragments)
    # ------------------------------------------------------------------
    def _policy(self) -> SqlPolicy:
        """Resolve the per-source SQL execution policy from ``policy_config``."""
        return SqlPolicy.from_source(self._source)

    def _effective_max_rows(self, requested: Optional[int], policy: SqlPolicy) -> int:
        """Return ``min(requested, policy.max_rows)`` with sensible coercion."""
        cap = policy.max_rows
        if requested is None:
            return cap
        try:
            requested = int(requested)
        except (TypeError, ValueError):
            return cap
        requested = max(requested, 1)
        return min(requested, cap)

    def _version_sql(self) -> str:
        """Return the SQL that reports the server version."""
        raise NotImplementedError

    def _infer_mode(self, version: str) -> Optional[str]:
        """Map a version string to a wire mode, or ``None``."""
        return None

    def _search_objects_query(self, name: Optional[str], kind: Optional[str], limit: int) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing visible schema objects."""
        raise NotImplementedError

    def _explain_sql(self, sql: str) -> str:
        """Wrap ``sql`` in the engine's EXPLAIN statement."""
        raise NotImplementedError
