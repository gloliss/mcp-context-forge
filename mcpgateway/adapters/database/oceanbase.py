# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/oceanbase.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unified OceanBase adapter (OB-03).

A single :class:`OceanBaseAdapter` serves both the MySQL and Oracle
compatibility modes.  Mode-specific behavior is isolated into a
:class:`ConnectionProvider` (how to connect) and an :class:`OceanBaseDialect`
(how to introspect metadata), both selected from the source's
``compatibility_mode``.  Business logic is never duplicated across the two
modes; the adapter body only delegates to the selected provider/dialect.
"""

# Standard
from abc import ABC, abstractmethod
from typing import Any, Optional

# Third-Party
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL

# First-Party
from mcpgateway.adapters.database.base import SQLAlchemyDatabaseAdapter
from mcpgateway.adapters.database.exceptions import AdapterNotAvailableError, DatabaseAdapterError, DatabaseCompatModeMismatchError
from mcpgateway.adapters.database.types import (
    METADATA_TYPE_COLUMN,
    METADATA_TYPE_FUNCTION,
    METADATA_TYPE_INDEX,
    METADATA_TYPE_PROCEDURE,
    METADATA_TYPE_SCHEMA,
    METADATA_TYPE_TABLE,
    METADATA_TYPE_VIEW,
    MetadataObject,
    QueryResult,
)

#: Detector output labels, uppercased per OB-03.
MODE_MYSQL = "MYSQL"
MODE_ORACLE = "ORACLE"


def _none_if_empty(value: Any) -> Optional[str]:
    """Return ``None`` for empty values, otherwise the string form."""
    if value is None or value == "":
        return None
    return str(value)


class ConnectionProvider(ABC):
    """Builds a SQLAlchemy engine for one OceanBase wire mode."""

    #: SQLAlchemy dialect+driver for the provider's wire mode.
    dialect_driver: str = ""

    @abstractmethod
    def build_url(self, source: Any) -> URL:
        """Render the connection URL for ``source``."""

    def build_connect_args(self, source: Any) -> dict[str, Any]:
        """Return driver ``connect_args`` (default none)."""
        return {}

    def build_engine(self, source: Any, pool_config: Any) -> Engine:
        """Build a pooled engine, translating a missing driver into an error."""
        try:
            return create_engine(
                self.build_url(source),
                connect_args=self.build_connect_args(source),
                pool_pre_ping=bool(pool_config.pre_ping),
                pool_size=int(pool_config.pool_size),
                max_overflow=int(pool_config.max_overflow),
                pool_timeout=float(pool_config.acquire_timeout_seconds),
                pool_recycle=int(pool_config.recycle_seconds),
            )
        except ImportError as exc:  # includes ModuleNotFoundError
            raise AdapterNotAvailableError(f"Database driver for {self.dialect_driver!r} is not installed: {exc}") from exc


class MySQLConnectionProvider(ConnectionProvider):
    """OceanBase MySQL-mode connection over the PyMySQL driver."""

    dialect_driver = "mysql+pymysql"

    def build_url(self, source: Any) -> URL:
        """Render a MySQL-mode URL whose database is the source's database."""
        return URL.create(
            self.dialect_driver,
            username=getattr(source, "username", None),
            password=getattr(source, "password", None),
            host=getattr(source, "host", None) or "localhost",
            port=getattr(source, "port", None),
            database=getattr(source, "database_name", None),
        )

    def build_connect_args(self, source: Any) -> dict[str, Any]:
        """Forward the source's charset to the driver when configured."""
        args: dict[str, Any] = {}
        charset = getattr(source, "charset", None)
        if charset:
            args["charset"] = charset
        return args


class OracleConnectionProvider(ConnectionProvider):
    """OceanBase Oracle-mode connection over the python-oracledb driver."""

    dialect_driver = "oracle+oracledb"

    def build_url(self, source: Any) -> URL:
        """Render an Oracle-mode URL whose database selects the service name."""
        service = (
            getattr(source, "schema_name", None)
            or getattr(source, "tenant_name", None)
            or getattr(source, "database_name", None)
        )
        return URL.create(
            self.dialect_driver,
            username=getattr(source, "username", None),
            password=getattr(source, "password", None),
            host=getattr(source, "host", None) or "localhost",
            port=getattr(source, "port", None),
            database=service,
        )


class OceanBaseDialect(ABC):
    """Mode-specific SQL fragments and metadata mapping for one wire mode."""

    @abstractmethod
    def mode_detect_sql(self) -> str:
        """Return the SQL that reveals the tenant's actual mode."""

    def infer_mode(self, raw: str) -> str:
        """Map a raw version/banner string to ``MYSQL`` or ``ORACLE``."""
        return MODE_ORACLE if "oracle" in raw.lower() else MODE_MYSQL

    @abstractmethod
    def explain_sql(self, sql: str) -> str:
        """Wrap ``sql`` in the mode's EXPLAIN statement."""

    @abstractmethod
    def schemas_sql(self) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing schemas."""

    @abstractmethod
    def tables_sql(self, schema: Optional[str]) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing tables."""

    @abstractmethod
    def views_sql(self, schema: Optional[str]) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing views."""

    @abstractmethod
    def columns_sql(self, schema: Optional[str], name: str) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing columns of a table."""

    @abstractmethod
    def indexes_sql(self, schema: Optional[str], name: str) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing indexes of a table."""

    @abstractmethod
    def procedures_sql(self, schema: Optional[str]) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing procedures/functions."""


class OceanBaseMySQLDialect(OceanBaseDialect):
    """MySQL-mode metadata via ``INFORMATION_SCHEMA``."""

    def mode_detect_sql(self) -> str:
        """Return the SQL that reveals the tenant's actual mode."""
        return "SELECT VERSION()"

    def explain_sql(self, sql: str) -> str:
        """Wrap ``sql`` in the MySQL-mode EXPLAIN statement."""
        return f"EXPLAIN {sql}"

    @staticmethod
    def _schema_clause(column: str, schema: Optional[str]) -> tuple[str, dict]:
        """Return a ``column = X`` clause defaulting to the current database."""
        if schema:
            return f"{column} = :schema", {"schema": schema}
        return f"{column} = DATABASE()", {}

    def schemas_sql(self) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing schemas."""
        return (
            "SELECT SCHEMA_NAME AS schema, SCHEMA_NAME AS name FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME",
            {},
        )

    def tables_sql(self, schema: Optional[str]) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing tables."""
        clause, params = self._schema_clause("TABLE_SCHEMA", schema)
        sql = (
            "SELECT TABLE_SCHEMA AS schema, TABLE_NAME AS name, TABLE_COMMENT AS description "
            "FROM information_schema.TABLES WHERE TABLE_TYPE = 'BASE TABLE' AND " + clause + " ORDER BY TABLE_NAME"
        )
        return sql, params

    def views_sql(self, schema: Optional[str]) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing views."""
        clause, params = self._schema_clause("TABLE_SCHEMA", schema)
        sql = (
            "SELECT TABLE_SCHEMA AS schema, TABLE_NAME AS name, TABLE_COMMENT AS description "
            "FROM information_schema.TABLES WHERE TABLE_TYPE = 'VIEW' AND " + clause + " ORDER BY TABLE_NAME"
        )
        return sql, params

    def columns_sql(self, schema: Optional[str], name: str) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing columns of a table."""
        clause, params = self._schema_clause("TABLE_SCHEMA", schema)
        params["name"] = name
        sql = (
            "SELECT TABLE_SCHEMA AS schema, COLUMN_NAME AS name, COLUMN_COMMENT AS description "
            "FROM information_schema.COLUMNS WHERE " + clause + " AND TABLE_NAME = :name ORDER BY ORDINAL_POSITION"
        )
        return sql, params

    def indexes_sql(self, schema: Optional[str], name: str) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing indexes of a table."""
        clause, params = self._schema_clause("TABLE_SCHEMA", schema)
        params["name"] = name
        sql = (
            "SELECT TABLE_SCHEMA AS schema, INDEX_NAME AS name, COLUMN_NAME AS column_name "
            "FROM information_schema.STATISTICS WHERE " + clause + " AND TABLE_NAME = :name ORDER BY INDEX_NAME, SEQ_IN_INDEX"
        )
        return sql, params

    def procedures_sql(self, schema: Optional[str]) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing procedures/functions."""
        clause, params = self._schema_clause("ROUTINE_SCHEMA", schema)
        sql = (
            "SELECT ROUTINE_SCHEMA AS schema, ROUTINE_NAME AS name, ROUTINE_TYPE AS type "
            "FROM information_schema.ROUTINES WHERE " + clause + " ORDER BY ROUTINE_NAME"
        )
        return sql, params


class OceanBaseOracleDialect(OceanBaseDialect):
    """Oracle-mode metadata via Oracle-compatible metadata views."""

    def mode_detect_sql(self) -> str:
        """Return the SQL that reveals the tenant's actual mode."""
        return "SELECT BANNER FROM V$VERSION WHERE ROWNUM <= 1"

    def explain_sql(self, sql: str) -> str:
        """Wrap ``sql`` in the Oracle-mode EXPLAIN statement."""
        return f"EXPLAIN PLAN FOR {sql}"

    @staticmethod
    def _schema_clause(column: str, schema: Optional[str]) -> tuple[str, dict]:
        """Return a ``column = X`` clause defaulting to the current user."""
        if schema:
            return f"{column} = :schema", {"schema": schema}
        return f"{column} = USER", {}

    def schemas_sql(self) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing schemas."""
        return "SELECT username AS schema, username AS name FROM all_users ORDER BY username", {}

    def tables_sql(self, schema: Optional[str]) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing tables."""
        clause, params = self._schema_clause("owner", schema)
        sql = "SELECT owner AS schema, table_name AS name, NULL AS description FROM all_tables WHERE " + clause + " ORDER BY table_name"
        return sql, params

    def views_sql(self, schema: Optional[str]) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing views."""
        clause, params = self._schema_clause("owner", schema)
        sql = "SELECT owner AS schema, view_name AS name, NULL AS description FROM all_views WHERE " + clause + " ORDER BY view_name"
        return sql, params

    def columns_sql(self, schema: Optional[str], name: str) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing columns of a table."""
        clause, params = self._schema_clause("owner", schema)
        params["name"] = name
        sql = (
            "SELECT owner AS schema, column_name AS name, NULL AS description "
            "FROM all_tab_columns WHERE " + clause + " AND table_name = :name ORDER BY column_id"
        )
        return sql, params

    def indexes_sql(self, schema: Optional[str], name: str) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing indexes of a table."""
        clause, params = self._schema_clause("i.owner", schema)
        params["name"] = name
        sql = (
            "SELECT i.owner AS schema, i.index_name AS name, ic.column_name AS column_name "
            "FROM all_indexes i JOIN all_ind_columns ic ON ic.index_owner = i.owner AND ic.index_name = i.index_name "
            "WHERE " + clause + " AND i.table_name = :name ORDER BY i.index_name, ic.column_position"
        )
        return sql, params

    def procedures_sql(self, schema: Optional[str]) -> tuple[str, dict]:
        """Return ``(sql, params)`` listing procedures/functions."""
        clause, params = self._schema_clause("owner", schema)
        sql = "SELECT owner AS schema, object_name AS name, object_type AS type FROM all_procedures WHERE " + clause + " ORDER BY object_name"
        return sql, params


class OceanBaseModeDetector:
    """Detect a tenant's actual compatibility mode (``MYSQL`` or ``ORACLE``)."""

    def detect(self, adapter: "OceanBaseAdapter") -> str:
        """Read the tenant's mode from a live connection.

        Args:
            adapter: A connected :class:`OceanBaseAdapter`.

        Returns:
            str: ``MYSQL`` or ``ORACLE``.

        Raises:
            DatabaseAdapterError: When the mode probe fails.
        """
        result = adapter.execute(adapter.dialect.mode_detect_sql(), max_rows=1)
        raw = " ".join(str(value) for row in result.rows for value in row)
        return adapter.dialect.infer_mode(raw)


class OceanBaseAdapter(SQLAlchemyDatabaseAdapter):
    """Unified OceanBase adapter for MySQL and Oracle modes (OB-03)."""

    def __init__(self, source: Any, pool: Any = None, pool_config: Any = None):
        """Bind an adapter, selecting provider and dialect from the source's mode."""
        self._provider = self._provider_for(source)
        self._dialect = self._dialect_for(source)
        self._mode_detector = OceanBaseModeDetector()
        super().__init__(source, pool=pool, pool_config=pool_config)

    @property
    def dialect(self) -> OceanBaseDialect:
        """Return the active dialect for the source's mode."""
        return self._dialect

    @property
    def connection_provider(self) -> ConnectionProvider:
        """Return the active connection provider for the source's mode."""
        return self._provider

    @staticmethod
    def _configured_mode(source: Any) -> str:
        """Return the source's configured mode uppercased (``""`` when unset)."""
        return str(getattr(source, "compatibility_mode", "") or "").upper()

    @classmethod
    def _provider_for(cls, source: Any) -> ConnectionProvider:
        """Select the connection provider for the source's configured mode."""
        if cls._configured_mode(source) == MODE_ORACLE:
            return OracleConnectionProvider()
        return MySQLConnectionProvider()

    @classmethod
    def _dialect_for(cls, source: Any) -> OceanBaseDialect:
        """Select the dialect for the source's configured mode."""
        if cls._configured_mode(source) == MODE_ORACLE:
            return OceanBaseOracleDialect()
        return OceanBaseMySQLDialect()

    # Engine construction stays a classmethod so the pool manager can build an
    # engine without an adapter instance, but it delegates to the provider the
    # source's mode selects.
    @classmethod
    def build_engine(cls, source: Any, pool_config: Any) -> Engine:
        """Build the engine via the provider selected by the source's mode."""
        return cls._provider_for(source).build_engine(source, pool_config)

    @classmethod
    def build_url(cls, source: Any) -> URL:
        """Render the connection URL via the mode's provider."""
        return cls._provider_for(source).build_url(source)

    @classmethod
    def build_connect_args(cls, source: Any) -> dict[str, Any]:
        """Return driver connect args via the mode's provider."""
        return cls._provider_for(source).build_connect_args(source)

    def _version_sql(self) -> str:
        return self._dialect.mode_detect_sql()

    def _explain_sql(self, sql: str) -> str:
        return self._dialect.explain_sql(sql)

    def detect_mode(self) -> Optional[str]:
        """Detect the tenant's actual mode, returning ``None`` on failure."""
        try:
            return self._mode_detector.detect(self)
        except DatabaseAdapterError:
            return None

    def validate_mode(self) -> Optional[str]:
        """Validate the detected mode against the configured mode.

        Returns:
            Optional[str]: The detected mode, or ``None`` when detection failed.

        Raises:
            DatabaseCompatModeMismatchError: If detected != configured.
        """
        detected = self.detect_mode()
        configured = self._configured_mode(self._source)
        if detected is not None and configured and detected != configured:
            raise DatabaseCompatModeMismatchError(configured=configured, detected=detected)
        return detected

    def search_objects(self, name: Optional[str] = None, kind: Optional[str] = None, limit: int = 100) -> QueryResult:
        """List tables/views as a raw result, reusing the metadata API."""
        objects: list[MetadataObject] = []
        if kind in (None, METADATA_TYPE_TABLE):
            objects.extend(self.list_tables())
        if kind in (None, METADATA_TYPE_VIEW):
            objects.extend(self.list_views())
        if name:
            objects = [obj for obj in objects if name.lower() in obj.name.lower()]
        objects = objects[: int(limit)]
        columns = ["schema", "name", "type", "description", "columns"]
        rows = [[obj.schema, obj.name, obj.type, obj.description, obj.columns] for obj in objects]
        return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=False, elapsed_ms=0.0, warnings=[])

    # Metadata API — one unified contract for both modes.
    def list_schemas(self) -> list[MetadataObject]:
        """List schemas visible to the source."""
        sql, params = self._dialect.schemas_sql()
        result = self.execute(sql, params)
        return [
            MetadataObject(name=str(row["name"]), type=METADATA_TYPE_SCHEMA, schema=_none_if_empty(row.get("schema")))
            for row in self._rows(result)
        ]

    def list_tables(self, schema: Optional[str] = None) -> list[MetadataObject]:
        """List tables, optionally filtered by schema."""
        sql, params = self._dialect.tables_sql(schema)
        return self._objects(self.execute(sql, params), METADATA_TYPE_TABLE)

    def list_views(self, schema: Optional[str] = None) -> list[MetadataObject]:
        """List views, optionally filtered by schema."""
        sql, params = self._dialect.views_sql(schema)
        return self._objects(self.execute(sql, params), METADATA_TYPE_VIEW)

    def list_columns(self, schema: Optional[str], name: str) -> list[MetadataObject]:
        """List columns of a table/view."""
        sql, params = self._dialect.columns_sql(schema, name)
        return self._objects(self.execute(sql, params), METADATA_TYPE_COLUMN)

    def list_indexes(self, schema: Optional[str], name: str) -> list[MetadataObject]:
        """List indexes of a table, aggregating indexed columns."""
        sql, params = self._dialect.indexes_sql(schema, name)
        result = self.execute(sql, params)
        grouped: dict[str, MetadataObject] = {}
        for row in self._rows(result):
            index_name = str(row["name"])
            if index_name not in grouped:
                grouped[index_name] = MetadataObject(
                    name=index_name, type=METADATA_TYPE_INDEX, schema=_none_if_empty(row.get("schema")), columns=[]
                )
            column = row.get("column_name")
            if column:
                grouped[index_name].columns.append(str(column))
        return list(grouped.values())

    def list_procedures(self, schema: Optional[str] = None) -> list[MetadataObject]:
        """List procedures and functions, optionally filtered by schema."""
        sql, params = self._dialect.procedures_sql(schema)
        result = self.execute(sql, params)
        objects: list[MetadataObject] = []
        for row in self._rows(result):
            kind = str(row.get("type") or "").lower()
            mtype = METADATA_TYPE_FUNCTION if kind == "function" else METADATA_TYPE_PROCEDURE
            objects.append(MetadataObject(name=str(row["name"]), type=mtype, schema=_none_if_empty(row.get("schema"))))
        return objects

    def _rows(self, result: QueryResult) -> list[dict]:
        """Convert a query result into a list of column-keyed dicts."""
        return [dict(zip(result.columns, row)) for row in result.rows]

    def _objects(self, result: QueryResult, mtype: str) -> list[MetadataObject]:
        """Map a canonical ``schema/name/description`` result to objects."""
        objects: list[MetadataObject] = []
        for row in self._rows(result):
            objects.append(
                MetadataObject(
                    name=str(row["name"]),
                    type=mtype,
                    schema=_none_if_empty(row.get("schema")),
                    description=_none_if_empty(row.get("description")),
                )
            )
        return objects
