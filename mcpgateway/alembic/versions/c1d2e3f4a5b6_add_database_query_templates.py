# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/alembic/versions/c1d2e3f4a5b6_add_database_query_templates.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Add the ``database_query_templates`` table (OB-05).

Named, parameter-bound query templates for ``db_execute_template``.  A
template is scoped to one ``database_sources`` row and carries its own
``max_rows`` / ``timeout_seconds`` so the agent supplies only arguments, never
the statement.

Revision ID: c1d2e3f4a5b6
Revises: 9b2e4c6d8f0a
Create Date: 2026-09-21
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "9b2e4c6d8f0a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "database_query_templates"


def upgrade() -> None:
    """Create the database_query_templates table when missing."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME in tables:
        return

    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("database_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("parameter_schema", sa.JSON(), nullable=False),
        sa.Column("result_schema", sa.JSON(), nullable=False),
        sa.Column("max_rows", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "slug", name="uq_database_query_template_source_slug"),
    )
    op.create_index(f"ix_{TABLE_NAME}_source_id", TABLE_NAME, ["source_id"])


def downgrade() -> None:
    """Drop the database_query_templates table when present."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME not in tables:
        return

    op.drop_table(TABLE_NAME)
