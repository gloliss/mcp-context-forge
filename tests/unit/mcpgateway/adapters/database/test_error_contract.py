# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/adapters/database/test_error_contract.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the unified database error contract (OB-07).

Every failure maps to one stable code from the OB-07 set; unknown exceptions
fall back to ``DB_INTERNAL_ERROR``; the contract never exposes a stack trace.
"""

# Third-Party
import pytest

# First-Party
from mcpgateway.adapters.database import (
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
)
from mcpgateway.services.database_tool_service import (
    DatabaseToolError,
    DatabaseToolSourceDisabledError,
    DatabaseToolSourceNotFoundError,
    DatabaseToolTemplateArgumentInvalidError,
    DatabaseToolTemplateDisabledError,
    DatabaseToolTemplateNotFoundError,
)


def test_supported_codes_contains_all_ob07_codes():
    """The contract recognizes exactly the 15 OB-07 stable codes."""
    required = {
        DB_SOURCE_NOT_FOUND,
        DB_SOURCE_DISABLED,
        DB_CONNECTION_FAILED,
        DB_CONNECTION_TIMEOUT,
        DB_AUTH_FAILED,
        DB_DATABASE_NOT_FOUND,
        DB_SCHEMA_NOT_FOUND,
        DB_COMPATIBILITY_MODE_MISMATCH,
        DB_QUERY_TIMEOUT,
        DB_QUERY_DENIED,
        DB_MULTI_STATEMENT_DENIED,
        DB_TEMPLATE_NOT_FOUND,
        DB_TEMPLATE_ARGUMENT_INVALID,
        DB_DRIVER_ERROR,
        DB_INTERNAL_ERROR,
    }
    assert required <= set(SUPPORTED_CODES)
    assert len(SUPPORTED_CODES) == 15


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ConnectionFailureError("unreachable"), DB_CONNECTION_FAILED),
        (DBAuthFailedError("bad password"), DB_AUTH_FAILED),
        (DatabaseConnectionTimeoutError("slow connect"), DB_CONNECTION_TIMEOUT),
        (DatabaseQueryTimeoutError("slow query"), DB_QUERY_TIMEOUT),
        (DBDatabaseNotFoundError("no such db"), DB_DATABASE_NOT_FOUND),
        (DBSchemaNotFoundError("no such schema"), DB_SCHEMA_NOT_FOUND),
        (DatabaseCompatModeMismatchError("mysql", "oracle"), DB_COMPATIBILITY_MODE_MISMATCH),
        (DatabaseStatementDeniedError("delete"), DB_STATEMENT_DENIED),
        (DatabaseMultiStatementError(2), DB_MULTI_STATEMENT_DENIED),
        (DatabaseAdapterError("generic driver error"), DB_DRIVER_ERROR),
    ],
)
def test_adapter_exception_codes(exc, expected):
    """Adapter exceptions map to their stable codes."""
    assert code_for(exc) == expected


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (DatabaseToolSourceNotFoundError("missing"), DB_SOURCE_NOT_FOUND),
        (DatabaseToolSourceDisabledError("disabled"), DB_SOURCE_DISABLED),
        (DatabaseToolTemplateNotFoundError("missing"), DB_TEMPLATE_NOT_FOUND),
        (DatabaseToolTemplateDisabledError("disabled"), DB_QUERY_DENIED),
        (DatabaseToolTemplateArgumentInvalidError("bad arg"), DB_TEMPLATE_ARGUMENT_INVALID),
        (DatabaseToolError("unknown"), DB_INTERNAL_ERROR),
    ],
)
def test_tool_exception_codes(exc, expected):
    """Tool-layer exceptions map to their stable codes."""
    assert code_for(exc) == expected


def test_code_for_unknown_exception_falls_back():
    """A non-database exception falls back to DB_INTERNAL_ERROR."""
    assert code_for(RuntimeError("boom")) == DB_INTERNAL_ERROR
    assert code_for(ValueError("boom")) == DB_INTERNAL_ERROR


def test_error_contract_shape():
    """The contract returns only ``code`` and a message — no stack trace."""
    exc = ConnectionFailureError("connection refused")
    assert error_contract(exc) == {"code": DB_CONNECTION_FAILED, "message": "connection refused"}
    assert error_contract(exc, "sanitized") == {"code": DB_CONNECTION_FAILED, "message": "sanitized"}
