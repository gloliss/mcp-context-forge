# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/alembic/versions/e7f8a9b0c1d2_add_database_tool_audits.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Add the ``database_tool_audits`` table (OB-07).

Per-invocation audit trail for the five built-in database tools.  Stores only
the compliance fields (correlation, caller, source/template ids, classified
statement type, result summary, outcome) and never any credential, secret, SQL
statement text, or bound parameter.  ``source_id``/``template_id`` carry no
foreign key so audit rows survive the deletion of their source or template.

Revision ID: e7f8a9b0c1d2
Revises: c1d2e3f4a5b6
Create Date: 2026-09-21
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "database_tool_audits"

#: Columns with a plain ``index=True`` on the SQLAlchemy model, whose default
#: index names must match the ones ``Base.metadata.create_all`` would produce.
_INDEXED_COLUMNS = (
    "trace_id",
    "caller",
    "tool_name",
    "source_id",
    "template_id",
    "success",
    "error_code",
    "created_at",
)


def upgrade() -> None:
    """Create the database_tool_audits table when missing."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME in tables:
        return

    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("caller", sa.String(255), nullable=True),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=True),
        sa.Column("template_id", sa.String(36), nullable=True),
        sa.Column("statement_type", sa.String(32), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=True),
        sa.Column("elapsed_ms", sa.Float(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in _INDEXED_COLUMNS:
        op.create_index(f"ix_{TABLE_NAME}_{column}", TABLE_NAME, [column])


def downgrade() -> None:
    """Drop the database_tool_audits table when present."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME not in tables:
        return

    op.drop_table(TABLE_NAME)
