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
- :mod:`oceanbase`      — the unified OceanBase adapter (OB-03).
- :mod:`registry`       — engine/mode -> adapter resolution.
- :mod:`pool`           — one connection pool per database source.
- :mod:`runtime_client` — the single entry point for callers.
"""

from mcpgateway.adapters.database.base import DatabaseAdapter, SQLAlchemyDatabaseAdapter
from mcpgateway.adapters.database.exceptions import (
    AdapterNotAvailableError,
    ConnectionFailureError,
    DB_COMPATIBILITY_MODE_MISMATCH,
    DatabaseAdapterError,
    DatabaseCompatModeMismatchError,
    PoolClosedError,
    PoolError,
    QueryError,
    QueryTimeoutError,
    UnknownCompatibilityModeError,
    UnknownEngineError,
)
from mcpgateway.adapters.database.oceanbase import OceanBaseAdapter, OceanBaseModeDetector
from mcpgateway.adapters.database.pool import ConnectionPool, PoolManager
from mcpgateway.adapters.database.registry import AdapterRegistry, default_registry, get_default_registry
from mcpgateway.adapters.database.runtime_client import DatabaseRuntimeClient
from mcpgateway.adapters.database.types import (
    AdapterKey,
    METADATA_TYPE_COLUMN,
    METADATA_TYPE_FUNCTION,
    METADATA_TYPE_INDEX,
    METADATA_TYPE_PROCEDURE,
    METADATA_TYPE_SCHEMA,
    METADATA_TYPE_TABLE,
    METADATA_TYPE_VIEW,
    MetadataObject,
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
    "DB_COMPATIBILITY_MODE_MISMATCH",
    "DatabaseAdapter",
    "DatabaseAdapterError",
    "DatabaseCompatModeMismatchError",
    "DatabaseRuntimeClient",
    "METADATA_TYPE_COLUMN",
    "METADATA_TYPE_FUNCTION",
    "METADATA_TYPE_INDEX",
    "METADATA_TYPE_PROCEDURE",
    "METADATA_TYPE_SCHEMA",
    "METADATA_TYPE_TABLE",
    "METADATA_TYPE_VIEW",
    "MetadataObject",
    "OceanBaseAdapter",
    "OceanBaseModeDetector",
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
