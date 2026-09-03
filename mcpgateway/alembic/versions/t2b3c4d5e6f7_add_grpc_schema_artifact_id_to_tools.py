# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/alembic/versions/t2b3c4d5e6f7_add_grpc_schema_artifact_id_to_tools.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Track the gRPC schema artifact that produced each generated tool.

Revision ID: t2b3c4d5e6f7
Revises: s1a2b3c4d5e6
Create Date: 2026-08-27
"""

# Standard
from typing import Any, Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

revision: str = "t2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "s1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "tools"
ARTIFACT_TABLE_NAME = "grpc_schema_artifacts"
COLUMN_NAME = "grpc_schema_artifact_id"
FOREIGN_KEY_NAME = "fk_tools_grpc_schema_artifact_id"
INDEX_NAME = "ix_tools_grpc_schema_artifact_id"


def _column_names() -> set[str]:
    """Return the current tool columns."""
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)}


def _index_names() -> set[str]:
    """Return the current tool indexes."""
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(TABLE_NAME)}


def _artifact_foreign_keys() -> list[dict[str, Any]]:
    """Return foreign keys that use the artifact provenance column."""
    return [foreign_key for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(TABLE_NAME) if COLUMN_NAME in (foreign_key.get("constrained_columns") or [])]


def upgrade() -> None:
    """Add nullable gRPC schema-artifact provenance to tools."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME not in tables or ARTIFACT_TABLE_NAME not in tables:
        return

    if COLUMN_NAME not in _column_names():
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.String(36), nullable=True))

    if not any(foreign_key.get("referred_table") == ARTIFACT_TABLE_NAME for foreign_key in _artifact_foreign_keys()):
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.create_foreign_key(
                FOREIGN_KEY_NAME,
                ARTIFACT_TABLE_NAME,
                [COLUMN_NAME],
                ["id"],
                ondelete="SET NULL",
            )

    if INDEX_NAME not in _index_names():
        op.create_index(INDEX_NAME, TABLE_NAME, [COLUMN_NAME])


def downgrade() -> None:
    """Remove gRPC schema-artifact provenance from tools."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME not in tables or COLUMN_NAME not in _column_names():
        return

    if INDEX_NAME in _index_names():
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)

    foreign_keys = _artifact_foreign_keys()
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        for foreign_key in foreign_keys:
            constraint_name = foreign_key.get("name")
            if constraint_name:
                batch_op.drop_constraint(constraint_name, type_="foreignkey")
        batch_op.drop_column(COLUMN_NAME)
