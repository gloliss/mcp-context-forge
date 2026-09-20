# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/alembic/versions/9b2e4c6d8f0a_add_database_sources.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Add the unified ``database_sources`` table (OB-01).

OceanBase/Oracle/MySQL/PostgreSQL adapters share one domain model.  The
password column is encrypted at rest by the application-layer
``EncryptedText`` type (underlying storage is ``Text``); the migration only
defines the raw column, encryption happens on ORM write.

Revision ID: 9b2e4c6d8f0a
Revises: 6d7e8f9a0b1c
Create Date: 2026-09-20
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

revision: str = "9b2e4c6d8f0a"
down_revision: Union[str, Sequence[str], None] = "6d7e8f9a0b1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "database_sources"


def upgrade() -> None:
    """Create the database_sources table when missing."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME in tables:
        return

    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("engine", sa.String(40), nullable=False),
        sa.Column("compatibility_mode", sa.String(20), nullable=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("cluster_name", sa.String(255), nullable=True),
        sa.Column("tenant_name", sa.String(255), nullable=True),
        sa.Column("database_name", sa.String(255), nullable=True),
        sa.Column("schema_name", sa.String(255), nullable=True),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("password", sa.Text(), nullable=True),
        sa.Column("credential_encrypted", sa.Boolean(), nullable=False),
        sa.Column("ssl_mode", sa.String(20), nullable=True),
        sa.Column("charset", sa.String(64), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("connection_config", sa.JSON(), nullable=False),
        sa.Column("pool_config", sa.JSON(), nullable=False),
        sa.Column("policy_config", sa.JSON(), nullable=False),
        sa.Column("tool_config", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("reachable", sa.Boolean(), nullable=False),
        sa.Column("detected_compatibility_mode", sa.String(20), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("modified_by", sa.String(255), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Drop the database_sources table when present."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME not in tables:
        return

    op.drop_table(TABLE_NAME)
