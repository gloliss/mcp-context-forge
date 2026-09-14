# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/alembic/versions/5c6d7e8f9a0b_add_grpc_runtime_config.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Add the nullable ``runtime_config`` JSON column to ``grpc_services`` (PR6,
design-document §42).

The column carries per-service gRPC runtime policy (max send/receive bytes,
keepalive, streaming limits).  It is nullable and deliberately not
backfilled; a ``NULL`` value means "default runtime policy".

Revision ID: 5c6d7e8f9a0b
Revises: a4b5c6d7e8f9
Create Date: 2026-09-09
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

revision: str = "5c6d7e8f9a0b"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "grpc_services"
COLUMN_NAME = "runtime_config"


def _column_names() -> set[str]:
    """Return the current grpc_services columns."""
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)}


def upgrade() -> None:
    """Add a nullable ``runtime_config`` JSON column to grpc_services."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME not in tables:
        return

    if COLUMN_NAME not in _column_names():
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.JSON(), nullable=True))


def downgrade() -> None:
    """Remove the ``runtime_config`` JSON column from grpc_services."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME not in tables or COLUMN_NAME not in _column_names():
        return

    op.drop_column(TABLE_NAME, COLUMN_NAME)
