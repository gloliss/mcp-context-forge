# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_database_audit_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the database tool audit service (OB-07).

Verifies the compliance fields are persisted, failures are recorded with an
error code, the schema never stores SQL/credential text, and recording is
best-effort (never raises).
"""

# Third-Party
# First-Party
from mcpgateway.db import DatabaseAudit
from mcpgateway.services.database_audit_service import DatabaseAuditService


def test_record_writes_full_compliance_row(test_db):
    """A success row persists every required compliance field."""
    service = DatabaseAuditService()
    entry = service.record(
        test_db,
        tool_name="db_execute_query",
        trace_id="trace-123",
        caller="alice@example.com",
        source_id="src-1",
        template_id=None,
        statement_type="select",
        row_count=10,
        truncated=False,
        elapsed_ms=12.5,
        success=True,
        error_code=None,
    )

    assert entry is not None
    assert entry.id
    assert entry.trace_id == "trace-123"
    assert entry.caller == "alice@example.com"
    assert entry.tool_name == "db_execute_query"
    assert entry.source_id == "src-1"
    assert entry.template_id is None
    assert entry.statement_type == "select"
    assert entry.row_count == 10
    assert entry.truncated is False
    assert entry.elapsed_ms == 12.5
    assert entry.success is True
    assert entry.error_code is None
    assert entry.created_at is not None

    persisted = test_db.get(DatabaseAudit, entry.id)
    assert persisted is not None
    assert persisted.tool_name == "db_execute_query"


def test_record_failure_row(test_db):
    """A failed invocation persists ``success=False`` and its error code."""
    service = DatabaseAuditService()
    entry = service.record(
        test_db,
        tool_name="db_execute_query",
        success=False,
        error_code="DB_CONNECTION_FAILED",
        source_id="src-1",
    )

    assert entry.success is False
    assert entry.error_code == "DB_CONNECTION_FAILED"


def test_schema_never_stores_sql_or_credentials():
    """The audit table has no column for SQL text, params, or credentials."""
    column_names = {column.name for column in DatabaseAudit.__table__.columns}
    for forbidden in ("sql", "statement", "query", "params", "parameters", "password", "credential", "secret"):
        assert forbidden not in column_names


def test_record_is_best_effort_without_session():
    """Recording against a missing session returns ``None`` instead of raising."""
    service = DatabaseAuditService()
    assert service.record(None, tool_name="db_execute_query") is None  # type: ignore[arg-type]
