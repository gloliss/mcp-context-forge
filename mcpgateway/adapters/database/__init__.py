# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/__init__.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Database Runtime Core (OB-02).

A generic database runtime that decouples engine capabilities from the
ContextForge tool layer:

- :mod:`types`          — shared contracts (adapter keys, pool identity, result).
- :mod:`exceptions`     — the error hierarchy.
- :mod:`base`           — the :class:`DatabaseAdapter` interface.
- :mod:`adapters`       — built-in engine implementations.
- :mod:`registry`       — engine/mode -> adapter resolution.
- :mod:`pool`           — one connection pool per database source.
- :mod:`runtime_client` — the single entry point for callers.
"""

from mcpgateway.adapters.database.base import DatabaseAdapter, SQLAlchemyDatabaseAdapter
from mcpgateway.adapters.database.exceptions import (
    AdapterNotAvailableError,
    ConnectionFailureError,
    DatabaseAdapterError,
    PoolClosedError,
    PoolError,
    QueryError,
    QueryTimeoutError,
    UnknownCompatibilityModeError,
    UnknownEngineError,
)
from mcpgateway.adapters.database.pool import ConnectionPool, PoolManager
from mcpgateway.adapters.database.registry import AdapterRegistry, default_registry, get_default_registry
from mcpgateway.adapters.database.runtime_client import DatabaseRuntimeClient
from mcpgateway.adapters.database.types import (
    AdapterKey,
    PoolConfig,
    PoolIdentity,
    QueryResult,
)

__all__ = [
    "AdapterKey",
    "AdapterNotAvailableError",
    "AdapterRegistry",
    "ConnectionFailureError",
    "ConnectionPool",
    "DatabaseAdapter",
    "DatabaseAdapterError",
    "DatabaseRuntimeClient",
    "PoolClosedError",
    "PoolConfig",
    "PoolError",
    "PoolIdentity",
    "PoolManager",
    "QueryError",
    "QueryResult",
    "QueryTimeoutError",
    "SQLAlchemyDatabaseAdapter",
    "UnknownCompatibilityModeError",
    "UnknownEngineError",
    "default_registry",
    "get_default_registry",
]
