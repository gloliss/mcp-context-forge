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

# Third-Party
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import OperationalError, SQLAlchemyError

# First-Party
from mcpgateway.adapters.database.exceptions import AdapterNotAvailableError, ConnectionFailureError, DatabaseAdapterError, QueryError
from mcpgateway.adapters.database.pool import ConnectionPool, PoolConfig, PoolIdentity
from mcpgateway.adapters.database.types import QueryResult

#: Default maximum rows fetched by a single ``execute`` before truncation.
DEFAULT_MAX_ROWS = 1000


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

    def execute(self, sql: str, params: Optional[dict] = None, max_rows: Optional[int] = None) -> QueryResult:
        """Run ``sql`` and return the unified result contract."""
        limit = int(max_rows) if max_rows is not None else self._max_rows()
        bound = dict(params or {})
        warnings: list[str] = []
        start = monotonic()
        try:
            with self._pool.connect() as conn:
                result = conn.execute(text(sql), bound)
                columns = [str(column) for column in result.keys()]
                fetched = [list(row) for row in result.fetchmany(limit + 1)]
        except OperationalError as exc:
            raise ConnectionFailureError(str(exc)) from exc
        except SQLAlchemyError as exc:
            raise QueryError(str(exc)) from exc

        truncated = len(fetched) > limit
        if truncated:
            fetched = fetched[:limit]
            warnings.append(f"Result truncated to {limit} rows")

        return QueryResult(
            columns=columns,
            rows=fetched,
            row_count=len(fetched),
            truncated=truncated,
            elapsed_ms=round((monotonic() - start) * 1000.0, 3),
            warnings=warnings,
        )

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
    def _max_rows(self) -> int:
        """Resolve the per-source row cap from ``policy_config``."""
        policy = getattr(self._source, "policy_config", None) or {}
        raw = policy.get("max_rows", DEFAULT_MAX_ROWS)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_MAX_ROWS

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
