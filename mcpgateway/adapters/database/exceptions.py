# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/exceptions.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Error hierarchy for the database runtime (OB-02).

Callers translate these into stable HTTP/MCP error codes without ever relying
on a specific engine's exception type — that is the decoupling boundary.
"""


class DatabaseAdapterError(Exception):
    """Base error for all database adapter operations."""


class UnknownEngineError(DatabaseAdapterError):
    """Raised when no adapter is registered for an engine."""


class UnknownCompatibilityModeError(DatabaseAdapterError):
    """Raised when the engine is known but the compatibility mode is not."""


class AdapterNotAvailableError(DatabaseAdapterError):
    """Raised when an adapter cannot be built, e.g. its driver is missing."""


class ConnectionFailureError(DatabaseAdapterError):
    """Raised when a database cannot be reached or a connection fails."""


class QueryError(DatabaseAdapterError):
    """Raised when a statement fails on an otherwise-reachable database."""


class QueryTimeoutError(QueryError):
    """Raised when a statement exceeds its deadline."""


class PoolError(DatabaseAdapterError):
    """Base error for connection-pool operations."""


class PoolClosedError(PoolError):
    """Raised when using a pool that has been disposed or closed."""
