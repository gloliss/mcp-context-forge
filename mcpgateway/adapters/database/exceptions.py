# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/exceptions.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Error hierarchy for the database runtime (OB-02).

Callers translate these into stable HTTP/MCP error codes without ever relying
on a specific engine's exception type — that is the decoupling boundary.
"""

# Standard
from typing import Optional


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


#: Stable error code for a configured/detected compatibility-mode mismatch.
DB_COMPATIBILITY_MODE_MISMATCH = "DB_COMPATIBILITY_MODE_MISMATCH"


class DatabaseCompatModeMismatchError(DatabaseAdapterError):
    """Raised when a tenant's detected mode differs from its configured mode."""

    code = DB_COMPATIBILITY_MODE_MISMATCH

    def __init__(self, configured: Optional[str], detected: Optional[str]):
        """Initialize the mismatch error.

        Args:
            configured: The source's configured compatibility mode.
            detected: The tenant's actually-detected compatibility mode.
        """
        self.configured = configured
        self.detected = detected
        super().__init__(f"Compatibility mode mismatch: configured={configured!r}, detected={detected!r}")


#: Stable error code for a connection that cannot be established in time.
DB_CONNECTION_TIMEOUT = "DB_CONNECTION_TIMEOUT"

#: Stable error code for a statement that exceeds its query deadline.
DB_QUERY_TIMEOUT = "DB_QUERY_TIMEOUT"

#: Stable error code for a statement rejected by the SQL execution policy.
DB_STATEMENT_DENIED = "DB_STATEMENT_DENIED"

#: Stable error code for a multi-statement submission while disallowed.
DB_MULTI_STATEMENT_DENIED = "DB_MULTI_STATEMENT_DENIED"


class DatabaseConnectionTimeoutError(ConnectionFailureError):
    """Raised when a connection cannot be established within its deadline."""

    code = DB_CONNECTION_TIMEOUT


class DatabaseQueryTimeoutError(QueryTimeoutError):
    """Raised when a statement exceeds its query deadline."""

    code = DB_QUERY_TIMEOUT


class DatabaseStatementDeniedError(DatabaseAdapterError):
    """Raised when a statement is rejected by the SQL execution policy."""

    code = DB_STATEMENT_DENIED

    def __init__(self, statement_type: str, message: Optional[str] = None):
        """Initialize the denial with the offending statement type."""
        self.statement_type = statement_type
        super().__init__(message or f"Statement type {statement_type!r} is denied by the SQL policy")


class DatabaseMultiStatementError(DatabaseAdapterError):
    """Raised when multi-statement SQL is submitted while disallowed."""

    code = DB_MULTI_STATEMENT_DENIED

    def __init__(self, statement_count: int):
        """Initialize the error with the number of submitted statements."""
        self.statement_count = statement_count
        super().__init__(f"Multiple SQL statements ({statement_count}) are not allowed")
