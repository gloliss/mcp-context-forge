# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/database_audit_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Database tool audit service (OB-07).

Persists one :class:`DatabaseAudit` row per database tool invocation with the
minimal compliance fields.  Recording is best-effort and never raises, so an
audit failure can never mask the real tool result.  No credential, secret, SQL
statement text, or bound parameter is ever stored.
"""

# Standard
from typing import Optional

# Third-Party
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.db import DatabaseAudit
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)


class DatabaseAuditService:
    """Record per-invocation database tool audit rows."""

    def record(
        self,
        db: Session,
        *,
        tool_name: str,
        trace_id: Optional[str] = None,
        caller: Optional[str] = None,
        source_id: Optional[str] = None,
        template_id: Optional[str] = None,
        statement_type: Optional[str] = None,
        row_count: Optional[int] = None,
        truncated: Optional[bool] = None,
        elapsed_ms: Optional[float] = None,
        success: bool = True,
        error_code: Optional[str] = None,
    ) -> Optional[DatabaseAudit]:
        """Insert one audit row, returning it (or ``None`` on failure).

        Args:
            db: Database session.
            tool_name: Canonical database tool name.
            trace_id: Request trace identifier.
            caller: Caller identity (e.g. user email).
            source_id: Resolved database source id, if any.
            template_id: Resolved query template id, if any.
            statement_type: Classified statement type (``select``, ...), if any.
            row_count: Result row count, if the tool returns rows.
            truncated: Whether the result was truncated, if known.
            elapsed_ms: Total invocation elapsed time in milliseconds.
            success: Whether the invocation succeeded.
            error_code: Stable error code (OB-07) when ``success`` is false.

        Returns:
            Optional[DatabaseAudit]: The persisted row, or ``None`` if logging
            failed (never raises).
        """
        try:
            entry = DatabaseAudit(
                trace_id=trace_id,
                caller=caller,
                tool_name=tool_name,
                source_id=source_id,
                template_id=template_id,
                statement_type=statement_type,
                row_count=row_count,
                truncated=truncated,
                elapsed_ms=elapsed_ms,
                success=success,
                error_code=error_code,
            )
            db.add(entry)
            db.commit()
            db.refresh(entry)
            return entry
        except Exception:  # best-effort: never let audit failure mask a result
            try:
                db.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
            logger.debug("Failed to record database tool audit for %s", tool_name, exc_info=True)
            return None


# Singleton instance.
_database_audit_service: Optional[DatabaseAuditService] = None


def get_database_audit_service() -> DatabaseAuditService:
    """Return the singleton database audit service."""
    global _database_audit_service  # pylint: disable=global-statement
    if _database_audit_service is None:
        _database_audit_service = DatabaseAuditService()
    return _database_audit_service
