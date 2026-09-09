# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/alembic/versions/a4b5c6d7e8f9_add_http_registry.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Add HTTP registry persistence (PR3): HTTP services with OpenAPI-contract-based
tool generation and their immutable schema artifacts.

``tools`` gains two nullable provenance columns: ``http_service_id`` (CASCADE)
and ``http_schema_artifact_id`` (SET NULL).  A ``NULL`` ``http_service_id``
means "not a registry-generated tool", mirroring the gRPC columns.  No backfill
and no seed data.

Revision ID: a4b5c6d7e8f9
Revises: 9f3e2d1c4b5a
Create Date: 2026-09-09
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "9f3e2d1c4b5a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(table_name: str) -> set[str]:
    """Return current columns for an existing table."""
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_columns(table_name: str, columns: list[sa.Column]) -> None:
    """Idempotently add nullable/defaulted columns without rebuilding tables."""
    if table_name not in sa.inspect(op.get_bind()).get_table_names():
        return
    existing = _column_names(table_name)
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def _ensure_index(table_name: str, index_name: str, columns: list[str]) -> None:
    """Create an index only when its table/columns exist and name is absent."""
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names() or not set(columns).issubset(_column_names(table_name)):
        return
    if index_name not in {index["name"] for index in inspector.get_indexes(table_name)}:
        op.create_index(index_name, table_name, columns)


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    """Drop an index before removing any of its columns in SQLite batch mode."""
    inspector = sa.inspect(op.get_bind())
    if table_name in inspector.get_table_names() and index_name in {index["name"] for index in inspector.get_indexes(table_name)}:
        op.drop_index(index_name, table_name=table_name)


def _ensure_foreign_key(table_name: str, constraint_name: str, local_column: str, remote_table: str, remote_column: str, ondelete: str) -> None:
    """Create a named foreign key when the reflected schema does not contain it."""
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names() or remote_table not in inspector.get_table_names() or local_column not in _column_names(table_name):
        return
    if any(local_column in (foreign_key.get("constrained_columns") or []) for foreign_key in inspector.get_foreign_keys(table_name)):
        return
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.create_foreign_key(constraint_name, remote_table, [local_column], [remote_column], ondelete=ondelete)


def upgrade() -> None:
    """Create HTTP registry persistence and tool provenance columns."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "http_services" not in tables:
        op.create_table(
            "http_services",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False, unique=True),
            sa.Column("slug", sa.String(255), nullable=False, unique=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("base_url", sa.String(767), nullable=False),
            sa.Column("discovery_mode", sa.String(20), nullable=False, server_default="manual"),
            sa.Column("discovery_config", sa.JSON(), nullable=False),
            sa.Column("runtime_config", sa.JSON(), nullable=False),
            sa.Column("active_artifact_id", sa.String(36), nullable=True),
            sa.Column("candidate_artifact_id", sa.String(36), nullable=True),
            sa.Column("active_schema_hash", sa.String(64), nullable=True),
            sa.Column("discovered_schema_hash", sa.String(64), nullable=True),
            sa.Column("schema_drift", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("operation_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("discovered_operations", sa.JSON(), nullable=False),
            sa.Column("last_discovery", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_discovery_error", sa.Text(), nullable=True),
            sa.Column("health_check_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("health_check_interval", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("health_check_timeout", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("health_failure_threshold", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("health_status", sa.String(20), nullable=False, server_default="unknown"),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_health_success", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_health_error", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("reachable", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("tags", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_by", sa.String(255), nullable=True),
            sa.Column("created_from_ip", sa.String(45), nullable=True),
            sa.Column("created_via", sa.String(100), nullable=True),
            sa.Column("created_user_agent", sa.Text(), nullable=True),
            sa.Column("modified_by", sa.String(255), nullable=True),
            sa.Column("modified_from_ip", sa.String(45), nullable=True),
            sa.Column("modified_via", sa.String(100), nullable=True),
            sa.Column("modified_user_agent", sa.Text(), nullable=True),
            sa.Column("import_batch_id", sa.String(36), nullable=True),
            sa.Column("federation_source", sa.String(255), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("team_id", sa.String(36), sa.ForeignKey("email_teams.id", ondelete="SET NULL"), nullable=True),
            sa.Column("owner_email", sa.String(255), nullable=True),
            sa.Column("visibility", sa.String(20), nullable=False, server_default="public"),
        )

    if "http_schema_artifacts" not in tables:
        op.create_table(
            "http_schema_artifacts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("http_service_id", sa.String(36), sa.ForeignKey("http_services.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("source_type", sa.String(20), nullable=False),
            sa.Column("artifact_format", sa.String(20), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("artifact_blob", sa.LargeBinary(), nullable=False),
            sa.Column("source_info", sa.JSON(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_by", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("http_service_id", "version", name="uq_http_schema_artifact_version"),
            sa.UniqueConstraint("http_service_id", "content_hash", name="uq_http_schema_artifact_hash"),
        )
        op.create_index("ix_http_schema_artifacts_http_service_id", "http_schema_artifacts", ["http_service_id"])
        op.create_index("ix_http_schema_artifacts_service_active", "http_schema_artifacts", ["http_service_id", "is_active"])

    _add_columns(
        "tools",
        [
            sa.Column("http_service_id", sa.String(36), nullable=True),
            sa.Column("http_schema_artifact_id", sa.String(36), nullable=True),
        ],
    )
    _ensure_foreign_key("tools", "fk_tools_http_service_id_http_services", "http_service_id", "http_services", "id", "CASCADE")
    _ensure_foreign_key("tools", "fk_tools_http_schema_artifact_id", "http_schema_artifact_id", "http_schema_artifacts", "id", "SET NULL")
    _ensure_index("tools", "ix_tools_http_schema_artifact_id", ["http_schema_artifact_id"])


def downgrade() -> None:
    """Remove HTTP registry persistence and tool provenance columns."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "tools" in tables:
        _drop_index_if_exists("tools", "ix_tools_http_schema_artifact_id")
        existing = _column_names("tools")
        if "http_service_id" in existing or "http_schema_artifact_id" in existing:
            foreign_keys = [
                foreign_key
                for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys("tools")
                if set(foreign_key.get("constrained_columns") or []).issubset({"http_service_id", "http_schema_artifact_id"})
            ]
            with op.batch_alter_table("tools") as batch_op:
                for foreign_key in foreign_keys:
                    constraint_name = foreign_key.get("name")
                    if constraint_name:
                        batch_op.drop_constraint(constraint_name, type_="foreignkey")
                for column_name in ["http_schema_artifact_id", "http_service_id"]:
                    if column_name in existing:
                        batch_op.drop_column(column_name)

    for table_name in ["http_schema_artifacts", "http_services"]:
        if table_name in tables:
            op.drop_table(table_name)
