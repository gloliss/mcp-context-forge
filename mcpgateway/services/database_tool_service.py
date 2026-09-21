# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/database_tool_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Database MCP Tool layer (OB-05).

Wires the database runtime (OB-02/03/04) into the ContextForge Tool Registry
as five fixed, source-agnostic tools: ``db_search_objects``,
``db_execute_query``, ``db_explain_query``, ``db_health_check``, and
``db_execute_template``.  A database source is selected per invocation via the
``source`` argument, so there is exactly one copy of each tool regardless of
how many sources are registered.

``db_execute_query`` is seeded disabled by default; the other four tools are
enabled.  ``db_execute_template`` executes a pre-registered
:class:`DatabaseQueryTemplate` statement with only the arguments supplied by
the agent — the statement is immutable at call time.
"""

# Standard
from typing import Any, Optional

# Third-Party
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.adapters.database import DatabaseRuntimeClient
from mcpgateway.db import DatabaseQueryTemplate, DatabaseSource, Tool
from mcpgateway.services.logging_service import LoggingService
from mcpgateway.utils.create_slug import slugify

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

INTEGRATION_TYPE = "DATABASE"

# Canonical (underscored) names, retained verbatim as ``original_name`` for
# dispatch.  The ``before_insert`` event slugifies ``custom_name`` into the
# MCP-exposed ``name`` (``db_search_objects`` -> ``db-search-objects``).
TOOL_SEARCH_OBJECTS = "db_search_objects"
TOOL_EXECUTE_QUERY = "db_execute_query"
TOOL_EXPLAIN_QUERY = "db_explain_query"
TOOL_HEALTH_CHECK = "db_health_check"
TOOL_EXECUTE_TEMPLATE = "db_execute_template"


class DatabaseToolError(ValueError):
    """Base error for database tool operations."""


class DatabaseToolNotFoundError(DatabaseToolError):
    """Raised when an unknown database tool name is requested."""


class DatabaseToolSourceNotFoundError(DatabaseToolError):
    """Raised when the requested database source is not found."""


class DatabaseToolSourceDisabledError(DatabaseToolError):
    """Raised when the requested database source is disabled."""


class DatabaseToolTemplateNotFoundError(DatabaseToolError):
    """Raised when the requested query template is not found."""


class DatabaseToolTemplateDisabledError(DatabaseToolError):
    """Raised when the requested query template is disabled."""


# --------------------------------------------------------------------------
# JSON Schema fragments shared by the tool registry rows.
# --------------------------------------------------------------------------
_SOURCE_PROPERTY = {
    "source": {
        "type": "string",
        "description": "Database source identifier (slug, name, or id).",
    },
}

_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "columns": {"type": "array", "items": {"type": "string"}},
        "rows": {"type": "array", "items": {"type": "array"}},
        "row_count": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "elapsed_ms": {"type": "number"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}

_HEALTH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "healthy": {"type": "boolean"},
        "engine": {"type": "string"},
        "compatibility_mode": {"type": ["string", "null"]},
        "detected_mode": {"type": ["string", "null"]},
        "latency": {"type": ["number", "null"]},
        "error": {"type": ["string", "null"]},
        "pool": {"type": "object"},
    },
}

_OBJECT_KINDS = ["table", "view", "column", "index", "procedure"]


def _object_search_schema() -> dict[str, Any]:
    """Return the input schema for ``db_search_objects``."""
    return {
        "type": "object",
        "properties": {
            **_SOURCE_PROPERTY,
            "kind": {"type": "string", "enum": _OBJECT_KINDS},
            "name": {
                "type": "string",
                "description": "Object name filter; for column/index this is the parent table name.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
        },
        "required": ["source"],
        "additionalProperties": False,
    }


#: The five built-in tools, keyed by canonical original name.  ``enabled`` is
#: the seed default applied only when the row is first created.
_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": TOOL_SEARCH_OBJECTS,
        "display_name": "Search database objects",
        "description": "Search database metadata objects (table, view, column, index, procedure).",
        "enabled": True,
        "input_schema": _object_search_schema(),
        "output_schema": _RESULT_SCHEMA,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": TOOL_EXECUTE_QUERY,
        "display_name": "Execute database query",
        "description": "Execute a SQL statement against a database source under the SQL policy.",
        "enabled": False,
        "input_schema": {
            "type": "object",
            "properties": {
                **_SOURCE_PROPERTY,
                "sql": {"type": "string", "description": "SQL statement to execute."},
                "params": {"type": "object", "description": "Bound parameter values."},
                "max_rows": {"type": "integer", "minimum": 1},
            },
            "required": ["source", "sql"],
            "additionalProperties": False,
        },
        "output_schema": _RESULT_SCHEMA,
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": TOOL_EXPLAIN_QUERY,
        "display_name": "Explain database query",
        "description": "Return the engine execution plan for a SQL statement without running it.",
        "enabled": True,
        "input_schema": {
            "type": "object",
            "properties": {
                **_SOURCE_PROPERTY,
                "sql": {"type": "string", "description": "SQL statement to explain."},
            },
            "required": ["source", "sql"],
            "additionalProperties": False,
        },
        "output_schema": _RESULT_SCHEMA,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": TOOL_HEALTH_CHECK,
        "display_name": "Check database health",
        "description": "Check the connectivity and pool health of a database source.",
        "enabled": True,
        "input_schema": {
            "type": "object",
            "properties": _SOURCE_PROPERTY,
            "required": ["source"],
            "additionalProperties": False,
        },
        "output_schema": _HEALTH_SCHEMA,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": TOOL_EXECUTE_TEMPLATE,
        "display_name": "Execute database template",
        "description": "Execute a pre-registered query template with the supplied arguments.",
        "enabled": True,
        "input_schema": {
            "type": "object",
            "properties": {
                **_SOURCE_PROPERTY,
                "template": {"type": "string", "description": "Query template identifier (slug, name, or id)."},
                "arguments": {"type": "object", "description": "Parameter values bound into the template statement."},
            },
            "required": ["source", "template"],
            "additionalProperties": False,
        },
        "output_schema": _RESULT_SCHEMA,
        "annotations": {"readOnlyHint": True},
    },
]


class DatabaseToolService:
    """Register and dispatch the five built-in database tools (OB-05)."""

    #: Module-level runtime client so pools are reused across invocations.
    _runtime = DatabaseRuntimeClient()

    # ------------------------------------------------------------------
    # Tool Registry seeding
    # ------------------------------------------------------------------
    @classmethod
    def ensure_registered(cls, db: Session, *, commit: bool = True) -> list[Tool]:
        """Idempotently register the five built-in database tools.

        On first run each tool is created with its seed ``enabled`` flag
        (``db_execute_query`` disabled).  On later runs only the declarative
        fields (description, schemas, annotations) are refreshed, so an admin
        who toggles a tool's ``enabled`` state is never reverted.

        Args:
            db: Database session (transaction owned by the caller).
            commit: Whether to commit before returning.

        Returns:
            list[Tool]: The five registered tool rows.
        """
        result: list[Tool] = []
        for spec in _TOOL_SPECS:
            name = spec["name"]
            tool = db.execute(
                select(Tool).where(Tool.integration_type == INTEGRATION_TYPE, Tool.original_name == name)
            ).scalar_one_or_none()
            if tool is None:
                tool = Tool(
                    original_name=name,
                    custom_name=name,
                    custom_name_slug=slugify(name),
                    display_name=spec["display_name"],
                    original_description=spec["description"],
                    description=spec["description"],
                    integration_type=INTEGRATION_TYPE,
                    request_type="POST",
                    input_schema=spec["input_schema"],
                    output_schema=spec["output_schema"],
                    annotations=spec["annotations"],
                    tags=["database"],
                    created_by="system",
                    created_via="database-tool-registry",
                    visibility="public",
                    enabled=spec["enabled"],
                )
                db.add(tool)
                db.flush()
            else:
                tool.description = spec["description"]
                tool.input_schema = spec["input_schema"]
                tool.output_schema = spec["output_schema"]
                tool.annotations = spec["annotations"]
                tool.version = (tool.version or 0) + 1
            result.append(tool)
        if commit:
            db.commit()
        logger.info("Registered %d database tools", len(result))
        return result

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------
    @staticmethod
    def resolve_source(db: Session, identifier: Optional[str]) -> DatabaseSource:
        """Resolve a source by slug, name, or id, enforcing ``enabled``.

        Args:
            db: Database session.
            identifier: Source slug, name, or id.

        Returns:
            DatabaseSource: The enabled source row.

        Raises:
            DatabaseToolSourceNotFoundError: If no source matches.
            DatabaseToolSourceDisabledError: If the source is disabled.
        """
        if not identifier:
            raise DatabaseToolSourceNotFoundError("A database source is required")
        source = db.execute(
            select(DatabaseSource).where(
                or_(
                    DatabaseSource.slug == identifier,
                    DatabaseSource.name == identifier,
                    DatabaseSource.id == identifier,
                )
            )
        ).scalar_one_or_none()
        if source is None:
            raise DatabaseToolSourceNotFoundError(f"Database source '{identifier}' not found")
        if not source.enabled:
            raise DatabaseToolSourceDisabledError(f"Database source '{identifier}' is disabled")
        return source

    @staticmethod
    def resolve_template(db: Session, source: DatabaseSource, identifier: Optional[str]) -> DatabaseQueryTemplate:
        """Resolve a template scoped to ``source`` by slug, name, or id.

        Args:
            db: Database session.
            source: The resolved source row.
            identifier: Template slug, name, or id.

        Returns:
            DatabaseQueryTemplate: The enabled template row.

        Raises:
            DatabaseToolTemplateNotFoundError: If no template matches.
            DatabaseToolTemplateDisabledError: If the template is disabled.
        """
        if not identifier:
            raise DatabaseToolTemplateNotFoundError("A query template is required")
        template = db.execute(
            select(DatabaseQueryTemplate).where(
                DatabaseQueryTemplate.source_id == source.id,
                or_(
                    DatabaseQueryTemplate.slug == identifier,
                    DatabaseQueryTemplate.name == identifier,
                    DatabaseQueryTemplate.id == identifier,
                ),
            )
        ).scalar_one_or_none()
        if template is None:
            raise DatabaseToolTemplateNotFoundError(
                f"Query template '{identifier}' not found for source '{source.name}'"
            )
        if not template.enabled:
            raise DatabaseToolTemplateDisabledError(f"Query template '{identifier}' is disabled")
        return template

    # ------------------------------------------------------------------
    # Invocation dispatch
    # ------------------------------------------------------------------
    @classmethod
    def call(cls, db: Session, name: str, arguments: Optional[dict], runtime: Optional[DatabaseRuntimeClient] = None) -> dict[str, Any]:
        """Execute one database tool against the selected source.

        Args:
            db: Database session.
            name: Canonical tool name (``original_name``).
            arguments: Tool arguments from the caller.
            runtime: Optional runtime client override (test injection).

        Returns:
            dict[str, Any]: The serialized tool result.

        Raises:
            DatabaseToolError: For missing/disabled sources, templates, or tools.
        """
        args = arguments or {}
        client = runtime if runtime is not None else cls._runtime

        if name == TOOL_SEARCH_OBJECTS:
            source = cls.resolve_source(db, args.get("source"))
            adapter = client.adapter_for(source)
            return adapter.search_objects(
                name=args.get("name"),
                kind=args.get("kind"),
                limit=int(args.get("limit") or 100),
            ).to_dict()

        if name == TOOL_EXECUTE_QUERY:
            source = cls.resolve_source(db, args.get("source"))
            adapter = client.adapter_for(source)
            return adapter.execute(
                sql=args.get("sql"),
                params=args.get("params"),
                max_rows=args.get("max_rows"),
            ).to_dict()

        if name == TOOL_EXPLAIN_QUERY:
            source = cls.resolve_source(db, args.get("source"))
            adapter = client.adapter_for(source)
            return adapter.explain(args.get("sql")).to_dict()

        if name == TOOL_HEALTH_CHECK:
            source = cls.resolve_source(db, args.get("source"))
            adapter = client.adapter_for(source)
            health = adapter.health_check()
            detected = adapter.detect_mode()
            return {
                "healthy": health.get("ok"),
                "engine": source.engine,
                "compatibility_mode": getattr(source, "compatibility_mode", None),
                "detected_mode": detected,
                "latency": health.get("latency_ms"),
                "error": health.get("error"),
                "pool": health.get("pool"),
            }

        if name == TOOL_EXECUTE_TEMPLATE:
            source = cls.resolve_source(db, args.get("source"))
            template = cls.resolve_template(db, source, args.get("template"))
            adapter = client.adapter_for(source)
            return adapter.execute(
                sql=template.statement,
                params=args.get("arguments") or {},
                max_rows=template.max_rows,
                query_timeout=template.timeout_seconds,
            ).to_dict()

        raise DatabaseToolNotFoundError(f"Unknown database tool '{name}'")
