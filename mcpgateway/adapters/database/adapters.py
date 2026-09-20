# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/adapters.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Built-in database adapters (OB-02).

Each adapter supplies only its dialect driver and dialect-specific SQL
fragments.  OB-02 establishes the framework, so OceanBase-specific SQL is
deliberately minimal; richer per-engine behavior lands in later issues without
changing the :class:`DatabaseAdapter` contract.
"""

# Standard
from typing import Any, Optional

# First-Party
from mcpgateway.adapters.database.base import SQLAlchemyDatabaseAdapter


class MySQLAdapter(SQLAlchemyDatabaseAdapter):
    """MySQL (and MySQL-compatible) engine over the PyMySQL driver."""

    dialect_driver = "mysql+pymysql"

    def _version_sql(self) -> str:
        return "SELECT VERSION()"

    def _infer_mode(self, version: str) -> Optional[str]:
        return "mysql"

    def _search_objects_query(self, name: Optional[str], kind: Optional[str], limit: int) -> tuple[str, dict]:
        conditions = ["TABLE_SCHEMA = DATABASE()"]
        params: dict = {"limit": int(limit)}
        if name:
            conditions.append("TABLE_NAME LIKE :name")
            params["name"] = f"%{name}%"
        if kind == "table":
            conditions.append("TABLE_TYPE = 'BASE TABLE'")
        elif kind == "view":
            conditions.append("TABLE_TYPE = 'VIEW'")
        sql = (
            "SELECT TABLE_NAME AS name, TABLE_TYPE AS kind "
            "FROM information_schema.TABLES WHERE "
            + " AND ".join(conditions)
            + " ORDER BY TABLE_NAME LIMIT :limit"
        )
        return sql, params

    def _explain_sql(self, sql: str) -> str:
        return f"EXPLAIN {sql}"


class OceanBaseMySQLAdapter(MySQLAdapter):
    """OceanBase in MySQL compatibility mode."""

    def _infer_mode(self, version: str) -> Optional[str]:
        # OceanBase reports itself in the version string; without a verified
        # probe this falls back to the source's configured mode.
        return getattr(self._source, "compatibility_mode", None) or "mysql"


class PostgreSQLAdapter(SQLAlchemyDatabaseAdapter):
    """PostgreSQL engine over the psycopg3 driver."""

    dialect_driver = "postgresql+psycopg"

    def _version_sql(self) -> str:
        return "SELECT VERSION()"

    def _infer_mode(self, version: str) -> Optional[str]:
        return "postgresql"

    def _search_objects_query(self, name: Optional[str], kind: Optional[str], limit: int) -> tuple[str, dict]:
        conditions = ["table_schema = current_schema()"]
        params: dict = {"limit": int(limit)}
        if name:
            conditions.append("table_name LIKE :name")
            params["name"] = f"%{name}%"
        if kind == "table":
            conditions.append("table_type = 'BASE TABLE'")
        elif kind == "view":
            conditions.append("table_type = 'VIEW'")
        sql = (
            "SELECT table_name AS name, table_type AS kind "
            "FROM information_schema.tables WHERE "
            + " AND ".join(conditions)
            + " ORDER BY table_name LIMIT :limit"
        )
        return sql, params

    def _explain_sql(self, sql: str) -> str:
        return f"EXPLAIN {sql}"

    @classmethod
    def build_connect_args(cls, source: Any) -> dict[str, Any]:
        """Set the search path when the source names a schema."""
        args: dict[str, Any] = {}
        schema = getattr(source, "schema_name", None)
        if schema:
            args["options"] = f"-csearch_path={schema}"
        return args


class OracleAdapter(SQLAlchemyDatabaseAdapter):
    """Oracle (and Oracle-compatible) engine over the python-oracledb driver."""

    dialect_driver = "oracle+oracledb"

    def _version_sql(self) -> str:
        return "SELECT BANNER FROM V$VERSION WHERE ROWNUM <= 1"

    def _infer_mode(self, version: str) -> Optional[str]:
        return "oracle"

    def _search_objects_query(self, name: Optional[str], kind: Optional[str], limit: int) -> tuple[str, dict]:
        conditions = ["object_type IN ('TABLE', 'VIEW')"]
        params: dict = {"limit": int(limit)}
        if name:
            conditions.append("object_name LIKE :name")
            params["name"] = f"%{name}%".upper()
        if kind in ("table", "view"):
            conditions.append("object_type = :kind")
            params["kind"] = kind.upper()
        sql = (
            "SELECT object_name AS name, object_type AS kind "
            "FROM all_objects WHERE "
            + " AND ".join(conditions)
            + " ORDER BY object_name FETCH FIRST :limit ROWS ONLY"
        )
        return sql, params

    def _explain_sql(self, sql: str) -> str:
        # Oracle plans land in PLAN_TABLE; a two-step readback is deferred.
        return f"EXPLAIN PLAN FOR {sql}"

    @classmethod
    def _url_database(cls, source: Any) -> Optional[str]:
        # In Oracle mode the tenant/schema selects the service rather than a
        # catalog database name.
        return (
            getattr(source, "schema_name", None)
            or getattr(source, "tenant_name", None)
            or getattr(source, "database_name", None)
        )


class OceanBaseOracleAdapter(OracleAdapter):
    """OceanBase in Oracle compatibility mode."""

    def _infer_mode(self, version: str) -> Optional[str]:
        return getattr(self._source, "compatibility_mode", None) or "oracle"
