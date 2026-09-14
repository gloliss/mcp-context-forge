# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/alembic/versions/6d7e8f9a0b1c_add_http_health_samples.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Add the ``http_health_samples`` table (PR8, design-document §57).

PR3 deliberately persisted only the rolling health state on
``http_services``; PR8 adds the per-check history table mirroring
``grpc_health_samples``.

Revision ID: 6d7e8f9a0b1c
Revises: 5c6d7e8f9a0b
Create Date: 2026-09-09
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

revision: str = "6d7e8f9a0b1c"
down_revision: Union[str, Sequence[str], None] = "5c6d7e8f9a0b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "http_health_samples"


def upgrade() -> None:
    """Create the http_health_samples table when missing."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME in tables:
        return

    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "http_service_id",
            sa.String(36),
            sa.ForeignKey("http_services.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("healthy", sa.Boolean(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_http_health_samples_service_timestamp",
        TABLE_NAME,
        ["http_service_id", "timestamp"],
    )


def downgrade() -> None:
    """Drop the http_health_samples table when present."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME not in tables:
        return

    op.drop_table(TABLE_NAME)
