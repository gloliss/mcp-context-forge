# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/sql_policy.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

SQL execution policy (OB-04).

The single security layer every free-form SQL statement passes through before
it reaches a database:

    SQL -> Parse -> Statement Classification -> Policy Check -> Parameter Bind
    -> Row Limit -> Timeout -> Database

Statement classification is backed by ``sqlparse`` (a real tokenizer) rather
than ``startswith`` string matching, so CTEs, comments, and quoted literals
cannot smuggle a write past the read-only guard.
"""

# Standard
from dataclasses import dataclass
from typing import Any

# Third-Party
import sqlparse

# First-Party
from mcpgateway.adapters.database.exceptions import DatabaseMultiStatementError, DatabaseStatementDeniedError

#: Statement types permitted by the default read-only policy.
DEFAULT_ALLOWED_STATEMENT_TYPES = frozenset({"select", "show", "describe", "explain"})

#: Default row cap enforced on every statement (OB-04).
DEFAULT_MAX_ROWS = 1000

#: Default per-statement query timeout in seconds (OB-04).
DEFAULT_QUERY_TIMEOUT_SECONDS = 15.0


def _as_bool(value: Any, default: bool) -> bool:
    """Coerce a policy value to bool, tolerating JSON string forms."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "null"}
    return bool(value)


def _as_positive_int(value: Any, default: int) -> int:
    """Coerce a policy value to a positive int, falling back to ``default``."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _as_positive_float(value: Any, default: float) -> float:
    """Coerce a policy value to a positive float, falling back to ``default``."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


@dataclass(frozen=True)
class SqlPolicy:
    """Per-source SQL execution policy with OB-04 secure defaults.

    Read-only by default: only ``allowed_statement_types`` execute, multiple
    statements are rejected, and every result is capped at ``max_rows`` rows
    within ``query_timeout_seconds`` seconds.
    """

    readonly: bool = True
    allow_multi_statement: bool = False
    allowed_statement_types: frozenset = DEFAULT_ALLOWED_STATEMENT_TYPES
    max_rows: int = DEFAULT_MAX_ROWS
    query_timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS

    @classmethod
    def from_source(cls, source: Any) -> "SqlPolicy":
        """Build a policy by merging the source's ``policy_config`` over defaults."""
        raw = getattr(source, "policy_config", None)
        if not isinstance(raw, dict):
            raw = {}
        allowed = raw.get("allowed_statement_types")
        if allowed is None:
            allowed_types = DEFAULT_ALLOWED_STATEMENT_TYPES
        else:
            allowed_types = frozenset(str(item).strip().lower() for item in allowed if str(item).strip())
        return cls(
            readonly=_as_bool(raw.get("readonly"), True),
            allow_multi_statement=_as_bool(raw.get("allow_multi_statement"), False),
            allowed_statement_types=allowed_types,
            max_rows=_as_positive_int(raw.get("max_rows"), DEFAULT_MAX_ROWS),
            query_timeout_seconds=_as_positive_float(raw.get("query_timeout_seconds"), DEFAULT_QUERY_TIMEOUT_SECONDS),
        )


@dataclass(frozen=True)
class StatementClassification:
    """The classified shape of a submitted SQL string.

    Attributes:
        statement_types: Normalized lowercase statement types, in order.
        statement_count: Number of meaningful (non-empty) statements.
        is_multi_statement: Whether more than one meaningful statement exists.
    """

    statement_types: tuple = ()
    statement_count: int = 0
    is_multi_statement: bool = False


class SqlStatementClassifier:
    """Classify SQL statements via ``sqlparse`` (no ``startswith`` heuristics)."""

    @staticmethod
    def statement_type(statement: Any) -> str:
        """Return the normalized lowercase statement type, or ``""`` when empty.

        ``sqlparse`` resolves ``WITH ... SELECT`` / ``WITH ... DELETE`` to the
        underlying DML keyword; ``EXPLAIN`` / ``SHOW`` / ``DESCRIBE`` /
        ``GRANT`` / ``REVOKE`` fall back to the leading keyword token.
        """
        first = statement.token_first(skip_cm=True)
        if first is None:
            return ""
        keyword = str(first).strip().lower()
        if not keyword or keyword == ";":
            return ""
        resolved = statement.get_type()
        if resolved and resolved != "UNKNOWN":
            return resolved.lower()
        return keyword

    def classify(self, sql: str) -> StatementClassification:
        """Parse ``sql`` and classify its meaningful statements."""
        types: list[str] = []
        for statement in sqlparse.parse(sql):
            kind = self.statement_type(statement)
            if kind:
                types.append(kind)
        return StatementClassification(
            statement_types=tuple(types),
            statement_count=len(types),
            is_multi_statement=len(types) > 1,
        )


class SqlPolicyGuard:
    """Enforce a :class:`SqlPolicy` against a statement classification."""

    def __init__(self, policy: SqlPolicy):
        """Initialize the guard with a policy."""
        self._policy = policy

    def check(self, classification: StatementClassification) -> None:
        """Raise a structured error when ``classification`` violates the policy."""
        if classification.statement_count == 0:
            raise DatabaseStatementDeniedError("empty", "No SQL statement found")
        if classification.is_multi_statement and not self._policy.allow_multi_statement:
            raise DatabaseMultiStatementError(classification.statement_count)
        if self._policy.readonly:
            for statement_type in classification.statement_types:
                if statement_type not in self._policy.allowed_statement_types:
                    raise DatabaseStatementDeniedError(statement_type)
