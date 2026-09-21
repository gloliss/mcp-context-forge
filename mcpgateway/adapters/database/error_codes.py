# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/error_codes.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unified database error contract (OB-07).

Every database failure — whether raised by the tool layer, the adapter layer,
or a driver — maps to one of these stable, agent-facing codes.  Callers rely on
:func:`code_for` and :func:`error_contract` and never on a specific engine's
exception type, so a driver stack trace or engine-specific detail never leaks
to the agent.
"""

# Standard
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Tool-layer codes
# ---------------------------------------------------------------------------
DB_SOURCE_NOT_FOUND = "DB_SOURCE_NOT_FOUND"
DB_SOURCE_DISABLED = "DB_SOURCE_DISABLED"
DB_TEMPLATE_NOT_FOUND = "DB_TEMPLATE_NOT_FOUND"
DB_TEMPLATE_ARGUMENT_INVALID = "DB_TEMPLATE_ARGUMENT_INVALID"

# ---------------------------------------------------------------------------
# Connection-layer codes
# ---------------------------------------------------------------------------
DB_CONNECTION_FAILED = "DB_CONNECTION_FAILED"
DB_CONNECTION_TIMEOUT = "DB_CONNECTION_TIMEOUT"
DB_AUTH_FAILED = "DB_AUTH_FAILED"

# ---------------------------------------------------------------------------
# Schema / object-layer codes
# ---------------------------------------------------------------------------
DB_DATABASE_NOT_FOUND = "DB_DATABASE_NOT_FOUND"
DB_SCHEMA_NOT_FOUND = "DB_SCHEMA_NOT_FOUND"
DB_COMPATIBILITY_MODE_MISMATCH = "DB_COMPATIBILITY_MODE_MISMATCH"

# ---------------------------------------------------------------------------
# Query-layer codes
# ---------------------------------------------------------------------------
DB_QUERY_TIMEOUT = "DB_QUERY_TIMEOUT"
DB_QUERY_DENIED = "DB_QUERY_DENIED"
DB_MULTI_STATEMENT_DENIED = "DB_MULTI_STATEMENT_DENIED"

# Backward-compatible OB-04 name for a statement rejected by the SQL policy;
# ``DB_QUERY_DENIED`` above is the OB-07 umbrella term for the same condition.
DB_STATEMENT_DENIED = "DB_STATEMENT_DENIED"

# ---------------------------------------------------------------------------
# Fallback codes
# ---------------------------------------------------------------------------
DB_DRIVER_ERROR = "DB_DRIVER_ERROR"
DB_INTERNAL_ERROR = "DB_INTERNAL_ERROR"

#: The complete set of stable codes the unified contract recognizes (OB-07).
SUPPORTED_CODES = frozenset(
    {
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
)


def code_for(exc: BaseException) -> str:
    """Return the stable error code for ``exc``, defaulting to ``DB_INTERNAL_ERROR``.

    Args:
        exc: Any exception (tool, adapter, or driver).

    Returns:
        str: The stable, agent-facing error code.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return DB_INTERNAL_ERROR


def error_contract(exc: BaseException, message: Optional[str] = None) -> dict[str, Any]:
    """Return a stable, stack-trace-free error contract for ``exc``.

    Args:
        exc: The underlying exception.
        message: Optional pre-sanitized message; defaults to ``str(exc)`` which
            is the exception message only — never a traceback.

    Returns:
        dict[str, Any]: ``{"code", "message"}``.
    """
    return {"code": code_for(exc), "message": message if message is not None else str(exc)}
