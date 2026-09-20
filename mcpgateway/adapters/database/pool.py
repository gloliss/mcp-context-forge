# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/pool.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Per-source connection pooling (OB-02).

One database source maps to exactly one :class:`ConnectionPool`, never to a
process-global connection.  :class:`PoolManager` keeps the source -> pool
mapping and invalidates a pool when the source's connection identity changes.
"""

# Standard
import threading
from typing import Any, Callable, Optional

# First-Party
from mcpgateway.adapters.database.exceptions import PoolClosedError
from mcpgateway.adapters.database.types import PoolConfig, PoolIdentity

#: An engine factory builds a fresh SQLAlchemy engine for a source.
EngineFactory = Callable[[Any, PoolConfig], Any]


class ConnectionPool:
    """A single database source's pooled engine with lifecycle management."""

    def __init__(self, identity: PoolIdentity, engine: Any, pool_config: PoolConfig):
        """Initialize a pool with its identity, engine, and configuration."""
        self.identity = identity
        self.engine = engine
        self.pool_config = pool_config
        self._closed = False
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        """Return whether the pool has been disposed."""
        return self._closed

    def connect(self):
        """Return a connection context manager for the pool's engine.

        Raises:
            PoolClosedError: If the pool has been disposed.
        """
        if self._closed:
            raise PoolClosedError("Connection pool is closed")
        return self.engine.connect()

    def stats(self) -> dict[str, Any]:
        """Return configured pool stats (closed flag plus sizing)."""
        return {
            "closed": self._closed,
            "pool_size": self.pool_config.pool_size,
            "max_overflow": self.pool_config.max_overflow,
        }

    def dispose(self) -> None:
        """Dispose the pool idempotently and release the underlying engine."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        dispose = getattr(self.engine, "dispose", None)
        if callable(dispose):
            dispose()

    def __repr__(self) -> str:
        """Render a short, credential-safe pool summary."""
        return f"<ConnectionPool identity={self.identity!r} closed={self._closed}>"


class PoolManager:
    """One :class:`ConnectionPool` per source id, invalidated on identity change.

    Tool calls acquire the source's pool instead of creating a connection each
    time; the engine factory only runs once per distinct identity.
    """

    def __init__(self):
        """Initialize an empty source -> pool mapping."""
        self._pools: dict[str, ConnectionPool] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _source_id(source: Any) -> str:
        """Return a stable key for a persisted or unpersisted source."""
        source_id = getattr(source, "id", None)
        if source_id is not None:
            return str(source_id)
        return f"<unpersisted:{id(source)}>"

    def acquire(self, source: Any, engine_factory: EngineFactory) -> ConnectionPool:
        """Return the source's pool, creating or invalidating it as needed.

        Args:
            source: The database source.
            engine_factory: Builds a fresh engine when a new pool is required.

        Returns:
            ConnectionPool: The current pool for the source's identity.
        """
        source_id = self._source_id(source)
        identity = PoolIdentity.from_source(source)
        with self._lock:
            pool = self._pools.get(source_id)
            if pool is not None and pool.identity == identity and not pool.closed:
                return pool
            if pool is not None:
                pool.dispose()  # stale config -> invalidate old pool
            pool_config = PoolConfig.from_source(source)
            engine = engine_factory(source, pool_config)
            pool = ConnectionPool(identity=identity, engine=engine, pool_config=pool_config)
            self._pools[source_id] = pool
            return pool

    def get(self, source_id: str) -> Optional[ConnectionPool]:
        """Return the pool for ``source_id`` without creating one."""
        with self._lock:
            return self._pools.get(str(source_id))

    def invalidate(self, source_id: str) -> None:
        """Drop and dispose the pool for ``source_id`` if present."""
        with self._lock:
            pool = self._pools.pop(str(source_id), None)
        if pool is not None:
            pool.dispose()

    def close(self, source_id: str) -> None:
        """Alias of :meth:`invalidate` for parity with the runtime client."""
        self.invalidate(source_id)

    def close_all(self) -> None:
        """Dispose every pool and clear the mapping."""
        with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
        for pool in pools:
            pool.dispose()

    def __len__(self) -> int:
        """Return the number of live pools."""
        with self._lock:
            return len(self._pools)
