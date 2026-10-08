# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/database_tool_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Database MCP Tool layer (OB-05), hardened for production (OB-07).

Wires the database runtime (OB-02/03/04) into the ContextForge Tool Registry
as five fixed, source-agnostic tools: ``db_search_objects``,
``db_execute_query``, ``db_explain_query``, ``db_health_check``, and
``db_execute_template``.  A database source is selected per invocation via the
``source`` argument, so there is exactly one copy of each tool regardless of
how many sources are registered.

OB-07 additions: every invocation is recorded in the database tool audit
trail (``database_tool_audits``), metadata lookups are served from the TTL
metadata cache, and every failure maps to a stable error code from the
unified error contract.

Phase 2 additions: an enabled ``DatabaseQueryTemplate`` is materialized as its
own tool (``db_tmpl_<source-slug>_<template-slug>``) whose input schema is the
template's declared ``parameter_schema``.  Such a tool carries no ``source`` or
``template`` argument — the caller supplies only the template's parameters, so
a packaged query is as directly callable as a hand-written one.  Template tools
live under the same ``DATABASE`` integration type and therefore travel the
identical visibility, plugin, audit, and metrics chain.
"""

# Standard
from time import monotonic
from typing import Any, Optional

# Third-Party
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.adapters.database import DatabaseRuntimeClient
from mcpgateway.adapters.database.error_codes import (
    DB_INTERNAL_ERROR,
    DB_QUERY_DENIED,
    DB_SOURCE_DISABLED,
    DB_SOURCE_NOT_FOUND,
    DB_TEMPLATE_ARGUMENT_INVALID,
    DB_TEMPLATE_NOT_FOUND,
    code_for,
)
from mcpgateway.adapters.database.sql_policy import SqlStatementClassifier
from mcpgateway.db import DatabaseQueryTemplate, DatabaseSource, Tool
from mcpgateway.services.database_audit_service import get_database_audit_service
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

#: Prefix of the per-template tools materialized from enabled query templates
#: (``db_tmpl_<source-slug>_<template-slug>``).  ``slugify`` folds ``[\W_]+``
#: to ``-``, so no slug ever contains an underscore and the generated name
#: parses back into its two slugs unambiguously.
TEMPLATE_TOOL_PREFIX = "db_tmpl_"


class DatabaseToolError(ValueError):
    """Base error for database tool operations (OB-05, OB-07)."""

    code = DB_INTERNAL_ERROR


class DatabaseToolNotFoundError(DatabaseToolError):
    """Raised when an unknown database tool name is requested."""


class DatabaseToolSourceNotFoundError(DatabaseToolError):
    """Raised when the requested database source is not found."""

    code = DB_SOURCE_NOT_FOUND


class DatabaseToolSourceDisabledError(DatabaseToolError):
    """Raised when the requested database source is disabled."""

    code = DB_SOURCE_DISABLED


class DatabaseToolTemplateNotFoundError(DatabaseToolError):
    """Raised when the requested query template is not found."""

    code = DB_TEMPLATE_NOT_FOUND


class DatabaseToolTemplateDisabledError(DatabaseToolError):
    """Raised when the requested query template is disabled."""

    code = DB_QUERY_DENIED


class DatabaseToolTemplateArgumentInvalidError(DatabaseToolError):
    """Raised when the supplied template arguments are invalid."""

    code = DB_TEMPLATE_ARGUMENT_INVALID


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

#: Canonical tool names accepted by :meth:`DatabaseToolService.call`.
_TOOL_NAMES = frozenset(spec["name"] for spec in _TOOL_SPECS)


class DatabaseToolService:
    """Register and dispatch the built-in database tools and template tools (OB-05, OB-07, Phase 2)."""

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
    # Per-template tool materialization
    # ------------------------------------------------------------------
    @classmethod
    def template_tool_name(cls, source_slug: str, template_slug: str) -> str:
        """Return the canonical ``original_name`` of a materialized template tool.

        Args:
            source_slug: Slug of the owning database source.
            template_slug: Slug of the query template.

        Returns:
            str: The tool's ``original_name``.
        """
        return f"{TEMPLATE_TOOL_PREFIX}{source_slug}_{template_slug}"

    @classmethod
    def _template_tool_row(cls, db: Session, name: str) -> Optional[Tool]:
        """Return the Tool row backing a materialized template, if it exists.

        Args:
            db: Database session.
            name: The tool's ``original_name``.

        Returns:
            Optional[Tool]: The existing row, or ``None``.
        """
        return db.execute(
            select(Tool).where(Tool.integration_type == INTEGRATION_TYPE, Tool.original_name == name)
        ).scalar_one_or_none()

    @staticmethod
    def template_input_schema(template: DatabaseQueryTemplate) -> dict[str, Any]:
        """Build the tool input schema from a template's declared parameters.

        A materialized tool takes the template's own parameters directly — the
        ``source``/``template``/``arguments`` wrapper only exists on the generic
        ``db_execute_template`` tool.  ``required`` is filtered against
        ``properties`` so a stale entry can never make the tool uncallable.

        Args:
            template: The query template row.

        Returns:
            dict[str, Any]: A closed object schema for the tool's arguments.
        """
        schema = template.parameter_schema or {}
        properties = dict(schema.get("properties") or {})
        declared_required = [key for key in (schema.get("required") or []) if key in properties]
        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if declared_required:
            input_schema["required"] = declared_required
        return input_schema

    @classmethod
    def materialize_template(cls, db: Session, template: DatabaseQueryTemplate, source: DatabaseSource) -> Tool:
        """Create or refresh the Tool row that exposes one enabled template.

        Idempotent: the declarative fields are rewritten on every call, while
        ``enabled`` follows the template so the tool exists exactly when the
        template is enabled.  ``readOnlyHint`` is derived from the template's
        own statement type, so a template wrapping an ``INSERT`` is advertised
        as a write rather than silently claimed to be read-only.

        Args:
            db: Database session (transaction owned by the caller).
            template: The enabled query template.
            source: The owning database source (its slug names the tool).

        Returns:
            Tool: The created or refreshed tool row.
        """
        name = cls.template_tool_name(source.slug, template.slug)
        input_schema = cls.template_input_schema(template)
        signature = ", ".join(input_schema["properties"]) or "no arguments"
        description = f"Run the '{template.name}' query template on the '{source.name}' database source ({signature})."
        statement_type = cls._classify_statement(template.statement) or "select"
        annotations = {"readOnlyHint": statement_type in {"select", "show", "describe", "explain"}}

        tool = cls._template_tool_row(db, name)
        if tool is None:
            tool = Tool(
                original_name=name,
                custom_name=name,
                custom_name_slug=slugify(name),
                display_name=template.name,
                original_description=description,
                description=description,
                integration_type=INTEGRATION_TYPE,
                request_type="POST",
                input_schema=input_schema,
                output_schema=_RESULT_SCHEMA,
                annotations=annotations,
                tags=["database", "template"],
                created_by=template.created_by or "system",
                created_via="database-template-registry",
                visibility="public",
                enabled=True,
            )
            db.add(tool)
            db.flush()
        else:
            tool.display_name = template.name
            tool.original_description = description
            tool.description = description
            tool.input_schema = input_schema
            tool.output_schema = _RESULT_SCHEMA
            tool.annotations = annotations
            tool.version = (tool.version or 0) + 1
        return tool

    @classmethod
    def dematerialize_template(cls, db: Session, name: str) -> None:
        """Delete the Tool row for ``name`` when it exists.

        Args:
            db: Database session (transaction owned by the caller).
            name: The tool's ``original_name``.
        """
        tool = cls._template_tool_row(db, name)
        if tool is not None:
            db.delete(tool)
            db.flush()

    @classmethod
    def sync_template(
        cls,
        db: Session,
        template: DatabaseQueryTemplate,
        source: DatabaseSource,
        previous_name: Optional[str] = None,
    ) -> Optional[Tool]:
        """Bring one template's tool row in line with the template's state.

        ``previous_name`` is the name the template's tool used before this
        change — its own slug moved, or the owning source's slug did.  That
        stale row is removed first so a rename never leaves an orphan tool
        behind under the old name.

        Args:
            db: Database session (transaction owned by the caller).
            template: The template after the change.
            source: The owning database source after the change.
            previous_name: The tool name to retire, when it moved.

        Returns:
            Optional[Tool]: The materialized tool, or ``None`` when the
            template is disabled.
        """
        current_name = cls.template_tool_name(source.slug, template.slug)
        if previous_name is not None and previous_name != current_name:
            cls.dematerialize_template(db, previous_name)
        if not template.enabled:
            cls.dematerialize_template(db, current_name)
            return None
        return cls.materialize_template(db, template, source)

    @classmethod
    def resync_source_templates(cls, db: Session, source: DatabaseSource, previous_slug: Optional[str] = None) -> None:
        """Re-point every template tool of ``source`` after its slug moved.

        Args:
            db: Database session (transaction owned by the caller).
            source: The source after the change.
            previous_slug: The source's slug before the change, when it moved.
        """
        for template in cls._source_templates(db, source):
            moved = previous_slug is not None and previous_slug != source.slug
            previous_name = cls.template_tool_name(previous_slug, template.slug) if moved else None
            cls.sync_template(db, template, source, previous_name=previous_name)

    @classmethod
    def remove_source_templates(cls, db: Session, source: DatabaseSource) -> None:
        """Delete the template tools of a source that is being removed.

        The template rows themselves cascade with the source, but tool rows do
        not, so they are retired explicitly to avoid orphaned tools.

        Args:
            db: Database session (transaction owned by the caller).
            source: The source being deleted.
        """
        for template in cls._source_templates(db, source):
            cls.dematerialize_template(db, cls.template_tool_name(source.slug, template.slug))

    @staticmethod
    def _source_templates(db: Session, source: DatabaseSource) -> list[DatabaseQueryTemplate]:
        """Return every query template belonging to ``source``.

        Args:
            db: Database session.
            source: The owning database source.

        Returns:
            list[DatabaseQueryTemplate]: The source's templates, enabled or not.
        """
        return list(
            db.execute(select(DatabaseQueryTemplate).where(DatabaseQueryTemplate.source_id == source.id)).scalars().all()
        )

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
    def call(
        cls,
        db: Session,
        name: str,
        arguments: Optional[dict],
        runtime: Optional[DatabaseRuntimeClient] = None,
        caller: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Execute one database tool against the selected source.

        Every invocation is recorded in the database tool audit trail; failures
        map to the unified error contract (a stable ``code``), never a driver
        stack trace.

        Args:
            db: Database session.
            name: Canonical tool name (``original_name``).
            arguments: Tool arguments from the caller.
            runtime: Optional runtime client override (test injection).
            caller: Optional caller identity for the audit trail.
            trace_id: Optional request trace identifier for the audit trail.

        Returns:
            dict[str, Any]: The serialized tool result.

        Raises:
            DatabaseToolError: For missing/disabled sources, templates, or tools.
            DatabaseAdapterError: For policy, connection, or query failures.
        """
        args = arguments or {}
        client = runtime if runtime is not None else cls._runtime
        started = monotonic()

        if name not in _TOOL_NAMES and not name.startswith(TEMPLATE_TOOL_PREFIX):
            raise DatabaseToolNotFoundError(f"Unknown database tool '{name}'")

        source = None
        source_id: Optional[str] = None
        template_id: Optional[str] = None
        statement_type: Optional[str] = None
        result: Optional[dict[str, Any]] = None
        error_code: Optional[str] = None

        try:
            if name.startswith(TEMPLATE_TOOL_PREFIX):
                # A materialized template tool: the caller's arguments are the
                # template's own parameters, and the statement is fixed by the
                # registration rather than supplied per call.
                source, template = cls._resolve_materialized_template(db, name)
                source_id = source.id
                template_id = template.id
                cls._validate_template_arguments(template, args)
                adapter = client.adapter_for(source)
                statement_type = cls._classify_statement(template.statement)
                result = adapter.execute(
                    sql=template.statement,
                    params=args,
                    max_rows=template.max_rows,
                    query_timeout=template.timeout_seconds,
                ).to_dict()
                return result

            source = cls.resolve_source(db, args.get("source"))
            source_id = source.id
            adapter = client.adapter_for(source)

            if name == TOOL_SEARCH_OBJECTS:
                result = cls._search_objects(client, source, adapter, args)
            elif name == TOOL_EXECUTE_QUERY:
                statement_type = cls._classify_statement(args.get("sql"))
                result = adapter.execute(
                    sql=args.get("sql"),
                    params=args.get("params"),
                    max_rows=args.get("max_rows"),
                ).to_dict()
            elif name == TOOL_EXPLAIN_QUERY:
                statement_type = "explain"
                result = adapter.explain(args.get("sql")).to_dict()
            elif name == TOOL_HEALTH_CHECK:
                result = cls._health_check(source, adapter)
            elif name == TOOL_EXECUTE_TEMPLATE:
                template = cls.resolve_template(db, source, args.get("template"))
                template_id = template.id
                statement_type = cls._classify_statement(template.statement)
                result = adapter.execute(
                    sql=template.statement,
                    params=args.get("arguments") or {},
                    max_rows=template.max_rows,
                    query_timeout=template.timeout_seconds,
                ).to_dict()

            return result
        except Exception as exc:
            error_code = code_for(exc)
            raise
        finally:
            row_count, truncated = cls._result_metrics(result)
            elapsed_ms = round((monotonic() - started) * 1000.0, 3)
            cls._record_audit(
                db,
                name=name,
                caller=caller,
                trace_id=trace_id,
                source_id=source_id,
                template_id=template_id,
                statement_type=statement_type,
                row_count=row_count,
                truncated=truncated,
                elapsed_ms=elapsed_ms,
                success=(error_code is None and result is not None),
                error_code=error_code,
            )

    # ------------------------------------------------------------------
    # Dispatch helpers
    # ------------------------------------------------------------------
    @classmethod
    def _resolve_materialized_template(cls, db: Session, name: str) -> tuple[DatabaseSource, DatabaseQueryTemplate]:
        """Recover the source and template behind a materialized tool name.

        Resolving through the live rows rather than trusting the name means a
        source or template disabled after the tool was listed is still refused
        at call time.

        Args:
            db: Database session.
            name: The materialized tool's ``original_name``.

        Returns:
            tuple[DatabaseSource, DatabaseQueryTemplate]: The enabled pair.

        Raises:
            DatabaseToolNotFoundError: If the name does not carry both slugs.
            DatabaseToolSourceNotFoundError: If the source no longer exists.
            DatabaseToolSourceDisabledError: If the source is disabled.
            DatabaseToolTemplateNotFoundError: If the template no longer exists.
            DatabaseToolTemplateDisabledError: If the template is disabled.
        """
        source_slug, _, template_slug = name[len(TEMPLATE_TOOL_PREFIX) :].partition("_")
        if not source_slug or not template_slug:
            raise DatabaseToolNotFoundError(f"Unknown database tool '{name}'")
        source = cls.resolve_source(db, source_slug)
        template = cls.resolve_template(db, source, template_slug)
        return source, template

    @staticmethod
    def _validate_template_arguments(template: DatabaseQueryTemplate, arguments: dict[str, Any]) -> None:
        """Check the supplied arguments against the template's parameter schema.

        Rejecting unknown arguments here keeps the bound parameters limited to
        what the template declared, so the caller cannot smuggle extra bindings
        into the statement.

        Args:
            template: The query template being executed.
            arguments: The caller-supplied arguments.

        Raises:
            DatabaseToolTemplateArgumentInvalidError: If a required argument is
                missing or an undeclared one was supplied.
        """
        schema = template.parameter_schema or {}
        properties = schema.get("properties") or {}
        missing = sorted(key for key in (schema.get("required") or []) if arguments.get(key) is None)
        if missing:
            raise DatabaseToolTemplateArgumentInvalidError(
                f"Missing required template argument(s): {', '.join(missing)}"
            )
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise DatabaseToolTemplateArgumentInvalidError(
                f"Undeclared template argument(s): {', '.join(unknown)}"
            )

    @classmethod
    def _search_objects(cls, client: Any, source: Any, adapter: Any, args: dict[str, Any]) -> dict[str, Any]:
        """Serve ``db_search_objects`` through the metadata cache when present."""
        kind = args.get("kind")
        name = args.get("name")
        limit = int(args.get("limit") or 100)
        cache = getattr(client, "metadata_cache", None)
        if cache is None:
            return adapter.search_objects(name=name, kind=kind, limit=limit).to_dict()

        key = cache.key_for(source, kind=kind, name=name, limit=limit)
        cached = cache.get(key)
        if cached is not None:
            return cached

        result = adapter.search_objects(name=name, kind=kind, limit=limit).to_dict()
        cache.put(key, result)
        return result

    @staticmethod
    def _health_check(source: Any, adapter: Any) -> dict[str, Any]:
        """Render the ``db_health_check`` result without ever exposing a credential."""
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

    @staticmethod
    def _classify_statement(sql: Optional[str]) -> Optional[str]:
        """Return the leading statement type of ``sql``, or ``None`` when absent."""
        if not sql:
            return None
        try:
            classification = SqlStatementClassifier().classify(sql)
        except Exception:  # never let classification fail the invocation
            return None
        return classification.statement_types[0] if classification.statement_types else None

    @staticmethod
    def _result_metrics(result: Optional[dict[str, Any]]) -> tuple[Optional[int], Optional[bool]]:
        """Extract ``(row_count, truncated)`` from a serialized tool result."""
        if isinstance(result, dict):
            return result.get("row_count"), result.get("truncated")
        return None, None

    @staticmethod
    def _record_audit(
        db: Session,
        *,
        name: str,
        caller: Optional[str],
        trace_id: Optional[str],
        source_id: Optional[str],
        template_id: Optional[str],
        statement_type: Optional[str],
        row_count: Optional[int],
        truncated: Optional[bool],
        elapsed_ms: Optional[float],
        success: bool,
        error_code: Optional[str],
    ) -> None:
        """Record one audit row, never letting an audit failure mask a result."""
        try:
            get_database_audit_service().record(
                db,
                tool_name=name,
                trace_id=trace_id,
                caller=caller,
                source_id=source_id,
                template_id=template_id,
                statement_type=statement_type,
                row_count=row_count,
                truncated=truncated,
                elapsed_ms=elapsed_ms,
                success=success,
                error_code=error_code,
            )
        except Exception:  # pragma: no cover - defensive, audit must never raise
            logger.debug("Failed to record database tool audit for %s", name, exc_info=True)

    # ------------------------------------------------------------------
    # Runtime invalidation (OB-07)
    # ------------------------------------------------------------------
    @classmethod
    def invalidate_source(cls, source_id: str) -> None:
        """Drop the pooled adapter and cached metadata for ``source_id``.

        Called whenever a source's configuration changes so the next invocation
        rebuilds the pool and re-fetches metadata.
        """
        cls._runtime.invalidate(source_id)
