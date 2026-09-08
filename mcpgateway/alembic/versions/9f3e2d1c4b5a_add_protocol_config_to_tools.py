# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/alembic/versions/9f3e2d1c4b5a_add_protocol_config_to_tools.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Add the nullable ``protocol_config`` JSON column to tools (PR2).

A ``NULL`` ``protocol_config`` means "legacy path" (design-document §9.11):
the HTTP adapter keeps running the extracted PR1 REST runtime.  A
non-``NULL`` value selects the new RequestBuilder / ResponseDecoder /
RedirectSecurity path.  The column is deliberately not backfilled.

Revision ID: 9f3e2d1c4b5a
Revises: 3c9d5e7a1b2f
Create Date: 2026-09-08
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

revision: str = "9f3e2d1c4b5a"
down_revision: Union[str, Sequence[str], None] = "3c9d5e7a1b2f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "tools"
COLUMN_NAME = "protocol_config"


def _column_names() -> set[str]:
    """Return the current tool columns."""
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)}


def upgrade() -> None:
    """Add a nullable ``protocol_config`` JSON column to tools."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME not in tables:
        return

    if COLUMN_NAME not in _column_names():
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.JSON(), nullable=True))


def downgrade() -> None:
    """Remove the ``protocol_config`` JSON column from tools."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME not in tables or COLUMN_NAME not in _column_names():
        return

    op.drop_column(TABLE_NAME, COLUMN_NAME)
