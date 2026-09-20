# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/runtime_client.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Database runtime client (OB-02).

The single entry point the tool layer uses to obtain an adapter for a database
source.  It wires the adapter registry and the pool manager together so that
one source maps to one pooled adapter, and exposes pool lifecycle operations.
"""

# Standard
from typing import Any, Optional

# First-Party
from mcpgateway.adapters.database.base import DatabaseAdapter
from mcpgateway.adapters.database.pool import PoolManager
from mcpgateway.adapters.database.registry import AdapterRegistry, default_registry


class DatabaseRuntimeClient:
    """Facade binding the adapter registry to per-source connection pools."""

    def __init__(self, registry: Optional[AdapterRegistry] = None, pool_manager: Optional[PoolManager] = None):
        """Initialize a client with the default registry and a fresh pool manager."""
        self._registry = registry if registry is not None else default_registry
        self._pool_manager = pool_manager if pool_manager is not None else PoolManager()

    @property
    def registry(self) -> AdapterRegistry:
        """Return the bound adapter registry."""
        return self._registry

    @property
    def pool_manager(self) -> PoolManager:
        """Return the bound pool manager."""
        return self._pool_manager

    def adapter_for(self, source: Any) -> DatabaseAdapter:
        """Return a pooled adapter for ``source``, reusing its pool.

        Args:
            source: A ``DatabaseSource`` (or compatible object).

        Returns:
            DatabaseAdapter: An adapter bound to the source's current pool.
        """
        adapter_cls = self._registry.lookup(getattr(source, "engine", None), getattr(source, "compatibility_mode", None))
        pool = self._pool_manager.acquire(source, engine_factory=adapter_cls.build_engine)
        return adapter_cls(source, pool=pool)

    def invalidate(self, source_id: str) -> None:
        """Invalidate and dispose the pool for ``source_id``."""
        self._pool_manager.invalidate(source_id)

    def close_source(self, source_id: str) -> None:
        """Close the pool for ``source_id``."""
        self._pool_manager.close(source_id)

    def close_all(self) -> None:
        """Close every pool managed by this runtime client."""
        self._pool_manager.close_all()
