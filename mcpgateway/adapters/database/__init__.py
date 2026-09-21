# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/__init__.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Database Runtime Core (OB-02).

A generic database runtime that decouples engine capabilities from the
ContextForge tool layer:

- :mod:`types`          — shared contracts (adapter keys, pool identity, result).
- :mod:`exceptions`     — the error hierarchy.
- :mod:`error_codes`    — the unified error contract (OB-07).
- :mod:`metadata_cache` — TTL metadata cache (OB-07).
- :mod:`base`           — the :class:`DatabaseAdapter` interface.
- :mod:`adapters`       — built-in engine implementations.
- :mod:`oceanbase`      — the unified OceanBase adapter (OB-03).
- :mod:`registry`       — engine/mode -> adapter resolution.
- :mod:`pool`           — one connection pool per database source.
- :mod:`runtime_client` — the single entry point for callers.
"""

from mcpgateway.adapters.database.base import DatabaseAdapter, SQLAlchemyDatabaseAdapter
from mcpgateway.adapters.database.error_codes import (
    DB_AUTH_FAILED,
    DB_COMPATIBILITY_MODE_MISMATCH,
    DB_CONNECTION_FAILED,
    DB_CONNECTION_TIMEOUT,
    DB_DATABASE_NOT_FOUND,
    DB_DRIVER_ERROR,
    DB_INTERNAL_ERROR,
    DB_MULTI_STATEMENT_DENIED,
    DB_QUERY_DENIED,
    DB_QUERY_TIMEOUT,
    DB_SCHEMA_NOT_FOUND,
    DB_SOURCE_DISABLED,
    DB_SOURCE_NOT_FOUND,
    DB_STATEMENT_DENIED,
    DB_TEMPLATE_ARGUMENT_INVALID,
    DB_TEMPLATE_NOT_FOUND,
    SUPPORTED_CODES,
    code_for,
    error_contract,
)
from mcpgateway.adapters.database.exceptions import (
    AdapterNotAvailableError,
    ConnectionFailureError,
    DatabaseAdapterError,
    DatabaseCompatModeMismatchError,
    DatabaseConnectionTimeoutError,
    DatabaseMultiStatementError,
    DatabaseQueryTimeoutError,
    DatabaseStatementDeniedError,
    DBAuthFailedError,
    DBDatabaseNotFoundError,
    DBSchemaNotFoundError,
    PoolClosedError,
    PoolError,
    QueryError,
    QueryTimeoutError,
    UnknownCompatibilityModeError,
    UnknownEngineError,
)
from mcpgateway.adapters.database.metadata_cache import DEFAULT_TTL_SECONDS, DatabaseMetadataCache
from mcpgateway.adapters.database.oceanbase import OceanBaseAdapter, OceanBaseModeDetector
from mcpgateway.adapters.database.pool import ConnectionPool, PoolManager
from mcpgateway.adapters.database.registry import AdapterRegistry, default_registry, get_default_registry
from mcpgateway.adapters.database.runtime_client import DatabaseRuntimeClient
from mcpgateway.adapters.database.sql_policy import SqlPolicy, SqlPolicyGuard, SqlStatementClassifier, StatementClassification
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
    "DB_AUTH_FAILED",
    "DB_COMPATIBILITY_MODE_MISMATCH",
    "DB_CONNECTION_FAILED",
    "DB_CONNECTION_TIMEOUT",
    "DB_DATABASE_NOT_FOUND",
    "DB_DRIVER_ERROR",
    "DB_INTERNAL_ERROR",
    "DB_MULTI_STATEMENT_DENIED",
    "DB_QUERY_DENIED",
    "DB_QUERY_TIMEOUT",
    "DB_SCHEMA_NOT_FOUND",
    "DB_SOURCE_DISABLED",
    "DB_SOURCE_NOT_FOUND",
    "DB_STATEMENT_DENIED",
    "DB_TEMPLATE_ARGUMENT_INVALID",
    "DB_TEMPLATE_NOT_FOUND",
    "DBAuthFailedError",
    "DBDatabaseNotFoundError",
    "DBSchemaNotFoundError",
    "DEFAULT_TTL_SECONDS",
    "DatabaseAdapter",
    "DatabaseAdapterError",
    "DatabaseCompatModeMismatchError",
    "DatabaseConnectionTimeoutError",
    "DatabaseMetadataCache",
    "DatabaseMultiStatementError",
    "DatabaseQueryTimeoutError",
    "DatabaseRuntimeClient",
    "DatabaseStatementDeniedError",
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
    "SUPPORTED_CODES",
    "SqlPolicy",
    "SqlPolicyGuard",
    "SqlStatementClassifier",
    "StatementClassification",
    "UnknownCompatibilityModeError",
    "UnknownEngineError",
    "code_for",
    "default_registry",
    "error_contract",
    "get_default_registry",
]
