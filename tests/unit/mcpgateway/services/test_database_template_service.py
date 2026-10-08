# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_database_template_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for query template CRUD and per-template tool materialization (Phase 2).

Covers the two things that make a packaged query usable: the definition is
validated as a whole at registration (single statement, every bound placeholder
declared), and the materialized tool tracks its template exactly — created when
the template is enabled, moved when either slug changes, and retired when the
template is disabled or deleted.  Every database operation is scripted with a
fake adapter; no external driver is required.
"""

# Standard
import uuid

# Third-Party
import pytest
from pydantic import ValidationError
from sqlalchemy import select

# First-Party
from mcpgateway.adapters.database import QueryResult
from mcpgateway.db import DatabaseSource, Tool
from mcpgateway.schemas import (
    DatabaseQueryTemplateCreate,
    DatabaseQueryTemplateUpdate,
    DatabaseSourceUpdate,
)
from mcpgateway.services.database_source_service import DatabaseSourceService
from mcpgateway.services.database_template_service import (
    DatabaseTemplateError,
    DatabaseTemplateNameConflictError,
    DatabaseTemplateNotFoundError,
    DatabaseTemplateService,
)
from mcpgateway.services.database_tool_service import (
    TEMPLATE_TOOL_PREFIX,
    DatabaseToolService,
    DatabaseToolTemplateArgumentInvalidError,
    DatabaseToolTemplateDisabledError,
    DatabaseToolNotFoundError,
)


# --------------------------------------------------------------------------
# Fakes: a scripted adapter that records the statement it was handed.
# --------------------------------------------------------------------------
class RecordingAdapter:
    """A duck-typed adapter that records every execute call."""

    def __init__(self):
        self.calls = []
        self.execute_result = QueryResult(columns=["id"], rows=[[7]], row_count=1)

    def execute(self, sql, params=None, max_rows=None, query_timeout=None):
        self.calls.append({"sql": sql, "params": params, "max_rows": max_rows, "query_timeout": query_timeout})
        return self.execute_result


class FakeRuntime:
    """A runtime client that always returns the same scripted adapter."""

    def __init__(self, adapter):
        self._adapter = adapter

    def adapter_for(self, source):  # pylint: disable=unused-argument
        return self._adapter


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _source(db, enabled=True, **overrides):
    """Persist a database source and return it."""
    token = uuid.uuid4().hex[:10]
    source = DatabaseSource(
        name=overrides.pop("name", f"src-{token}"),
        slug=overrides.pop("slug", f"src-{token}"),
        engine=overrides.pop("engine", "oceanbase"),
        compatibility_mode=overrides.pop("compatibility_mode", "mysql"),
        host=overrides.pop("host", "127.0.0.1"),
        port=overrides.pop("port", 2881),
        enabled=enabled,
        **overrides,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _create(db, source, **overrides):
    """Create a query template through the service and return the read model."""
    payload = {
        "name": overrides.pop("name", f"tpl-{uuid.uuid4().hex[:10]}"),
        "statement": overrides.pop("statement", "SELECT * FROM users WHERE id = :id"),
        "parameter_schema": overrides.pop(
            "parameter_schema", {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}
        ),
    }
    payload.update(overrides)
    return DatabaseTemplateService.create_template(db, source.id, DatabaseQueryTemplateCreate(**payload))


def _tool(db, name):
    """Return the Tool row with ``original_name == name``, or ``None``."""
    return db.execute(
        select(Tool).where(Tool.integration_type == "DATABASE", Tool.original_name == name)
    ).scalar_one_or_none()


# --------------------------------------------------------------------------
# Definition validation
# --------------------------------------------------------------------------
def test_undeclared_placeholder_is_rejected(test_db):
    """A statement binding a parameter the schema omits would be uncallable."""
    source = _source(test_db)

    with pytest.raises(DatabaseTemplateError, match="undeclared parameter"):
        _create(test_db, source, statement="SELECT * FROM users WHERE id = :id", parameter_schema={})


def test_multi_statement_is_rejected(test_db):
    """A template wraps exactly one statement, checked at registration."""
    source = _source(test_db)

    with pytest.raises(DatabaseTemplateError, match="exactly one SQL statement"):
        _create(
            test_db,
            source,
            statement="SELECT 1; DELETE FROM users",
            parameter_schema={},
        )


def test_blank_statement_is_rejected(test_db):
    """Whitespace parses to no statement at all, so it is refused."""
    source = _source(test_db)

    with pytest.raises(DatabaseTemplateError, match="must contain a SQL statement"):
        _create(test_db, source, statement="   ", parameter_schema={})


def test_postgres_cast_is_not_mistaken_for_a_placeholder(test_db):
    """``::text`` is a cast, not a bind parameter, so it needs no declaration."""
    source = _source(test_db)

    read = _create(
        test_db,
        source,
        statement="SELECT amount::text AS amount FROM ledger WHERE id = :id",
        parameter_schema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    )

    assert read.parameter_schema["properties"] == {"id": {"type": "integer"}}


def test_parameter_schema_shape_is_checked_at_the_edge(test_db):
    """A required entry naming no property is refused before the service runs."""
    with pytest.raises(ValidationError):
        DatabaseQueryTemplateCreate(
            name="bad",
            statement="SELECT 1",
            parameter_schema={"type": "object", "properties": {}, "required": ["ghost"]},
        )


def test_duplicate_name_in_the_same_source_is_rejected(test_db):
    """Template slugs are unique per source."""
    source = _source(test_db)
    _create(test_db, source, name="Daily Revenue")

    with pytest.raises(DatabaseTemplateNameConflictError):
        _create(test_db, source, name="daily-revenue")


def test_same_name_in_another_source_is_allowed(test_db):
    """The uniqueness scope is the owning source, not the whole registry."""
    first = _source(test_db)
    second = _source(test_db)
    _create(test_db, first, name="Daily Revenue")

    read = _create(test_db, second, name="Daily Revenue")

    assert read.slug == "daily-revenue"


def test_missing_source_is_not_found(test_db):
    """A template cannot be created against an unknown source."""
    from mcpgateway.services.database_source_service import DatabaseSourceNotFoundError

    with pytest.raises(DatabaseSourceNotFoundError):
        _create(test_db, type("S", (), {"id": "does-not-exist"})())


# --------------------------------------------------------------------------
# Materialization: an enabled template is its own tool
# --------------------------------------------------------------------------
def test_enabled_template_materializes_a_tool(test_db):
    """The tool carries the template's parameters directly, with no wrapper."""
    source = _source(test_db)
    read = _create(test_db, source, name="Revenue By Region")

    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_revenue-by-region"
    tool = _tool(test_db, name)

    assert tool is not None
    assert tool.enabled is True
    assert tool.integration_type == "DATABASE"
    assert tool.name == tool.custom_name_slug  # MCP-exposed slugified name
    assert tool.input_schema["properties"] == {"id": {"type": "integer"}}
    assert tool.input_schema["required"] == ["id"]
    assert tool.input_schema["additionalProperties"] is False
    # The source/template wrapper belongs only to the generic template tool.
    assert "source" not in tool.input_schema["properties"]
    assert "template" not in tool.input_schema["properties"]
    assert read.tool_name == name


def test_disabled_template_materializes_no_tool(test_db):
    """A disabled template has no tool, and reports none."""
    source = _source(test_db)
    read = _create(test_db, source, name="Off By Default", enabled=False)

    assert read.tool_name is None
    assert _tool(test_db, f"{TEMPLATE_TOOL_PREFIX}{source.slug}_off-by-default") is None


def test_readonly_hint_follows_the_statement_type(test_db):
    """A template wrapping a write is not advertised as read-only."""
    source = _source(test_db)
    _create(test_db, source, name="Reader", statement="SELECT * FROM users WHERE id = :id")
    _create(
        test_db,
        source,
        name="Writer",
        statement="DELETE FROM users WHERE id = :id",
        parameter_schema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    )

    assert _tool(test_db, f"{TEMPLATE_TOOL_PREFIX}{source.slug}_reader").annotations["readOnlyHint"] is True
    assert _tool(test_db, f"{TEMPLATE_TOOL_PREFIX}{source.slug}_writer").annotations["readOnlyHint"] is False


def test_tool_input_schema_drops_a_stale_required_entry(test_db):
    """``required`` is filtered against ``properties`` so the tool stays callable."""
    source = _source(test_db)
    read = _create(test_db, source, name="No Params", statement="SELECT 1", parameter_schema={})

    tool = _tool(test_db, f"{TEMPLATE_TOOL_PREFIX}{source.slug}_no-params")

    assert tool.input_schema["properties"] == {}
    assert "required" not in tool.input_schema
    assert read.tool_name is not None


# --------------------------------------------------------------------------
# Materialization follows enable/disable, rename, and delete
# --------------------------------------------------------------------------
def test_disabling_a_template_retires_its_tool(test_db):
    """Disabling removes the tool rather than leaving a dead entry."""
    source = _source(test_db)
    read = _create(test_db, source, name="Toggle Me")
    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_toggle-me"
    assert _tool(test_db, name) is not None

    updated = DatabaseTemplateService.update_template(
        test_db, source.id, read.id, DatabaseQueryTemplateUpdate(enabled=False)
    )

    assert _tool(test_db, name) is None
    assert updated.tool_name is None


def test_reenabling_a_template_restores_its_tool(test_db):
    """Enabling brings the tool back under the same name."""
    source = _source(test_db)
    read = _create(test_db, source, name="Toggle Me", enabled=False)
    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_toggle-me"
    assert _tool(test_db, name) is None

    updated = DatabaseTemplateService.update_template(
        test_db, source.id, read.id, DatabaseQueryTemplateUpdate(enabled=True)
    )

    assert _tool(test_db, name) is not None
    assert updated.tool_name == name


def test_renaming_a_template_moves_its_tool(test_db):
    """A rename leaves no tool behind under the old name."""
    source = _source(test_db)
    read = _create(test_db, source, name="Before Rename")
    old_name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_before-rename"
    assert _tool(test_db, old_name) is not None

    updated = DatabaseTemplateService.update_template(
        test_db, source.id, read.id, DatabaseQueryTemplateUpdate(name="After Rename")
    )

    new_name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_after-rename"
    assert _tool(test_db, old_name) is None
    assert _tool(test_db, new_name) is not None
    assert updated.tool_name == new_name


def test_deleting_a_template_retires_its_tool(test_db):
    """Deleting the template deletes the tool it materialized."""
    source = _source(test_db)
    read = _create(test_db, source, name="Doomed")
    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_doomed"

    DatabaseTemplateService.delete_template(test_db, source.id, read.id)

    assert _tool(test_db, name) is None
    with pytest.raises(DatabaseTemplateNotFoundError):
        DatabaseTemplateService.get_template(test_db, source.id, read.id)


def test_renaming_a_source_moves_its_template_tools(test_db):
    """Template tool names embed the source slug, so a source rename moves them."""
    source = _source(test_db, name="Old Source", slug="old-source")
    _create(test_db, source, name="Daily Revenue")

    DatabaseSourceService.update_source(test_db, source.id, DatabaseSourceUpdate(name="New Source"))

    assert _tool(test_db, f"{TEMPLATE_TOOL_PREFIX}old-source_daily-revenue") is None
    assert _tool(test_db, f"{TEMPLATE_TOOL_PREFIX}new-source_daily-revenue") is not None


def test_deleting_a_source_retires_its_template_tools(test_db):
    """Tool rows do not cascade with the source, so they are retired explicitly."""
    source = _source(test_db)
    _create(test_db, source, name="Daily Revenue")
    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_daily-revenue"
    assert _tool(test_db, name) is not None

    DatabaseSourceService.delete_source(test_db, source.id)

    assert _tool(test_db, name) is None


def test_update_validates_the_merged_definition(test_db):
    """Changing only the statement still checks it against the existing schema."""
    source = _source(test_db)
    read = _create(test_db, source, name="Stable", statement="SELECT * FROM users WHERE id = :id")

    with pytest.raises(DatabaseTemplateError, match="undeclared parameter"):
        DatabaseTemplateService.update_template(
            test_db, source.id, read.id, DatabaseQueryTemplateUpdate(statement="SELECT * FROM users WHERE email = :email")
        )

    # The refused update left the stored definition untouched.
    unchanged = DatabaseTemplateService.get_template(test_db, source.id, read.id)
    assert unchanged.statement == "SELECT * FROM users WHERE id = :id"


def test_reseeding_the_builtin_tools_leaves_template_tools_alone(test_db):
    """Registry seeding only touches its five declarative specs."""
    source = _source(test_db)
    _create(test_db, source, name="Daily Revenue")
    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_daily-revenue"
    version_before = _tool(test_db, name).version

    DatabaseToolService.ensure_registered(test_db)

    tool = _tool(test_db, name)
    assert tool is not None
    assert tool.version == version_before


# --------------------------------------------------------------------------
# Dispatch: the materialized tool takes only the template's parameters
# --------------------------------------------------------------------------
def _materialized_call(db, source, name, arguments, adapter):
    """Invoke a materialized template tool through the service dispatcher."""
    return DatabaseToolService.call(db, name, arguments, FakeRuntime(adapter))


def test_materialized_tool_binds_arguments_into_the_fixed_statement(test_db):
    """The caller's arguments become the bound params of the stored statement."""
    source = _source(test_db)
    _create(test_db, source, name="Daily Revenue", statement="SELECT * FROM users WHERE id = :id")
    adapter = RecordingAdapter()
    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_daily-revenue"

    result = _materialized_call(test_db, source, name, {"id": 7}, adapter)

    assert result["columns"] == ["id"]
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["sql"] == "SELECT * FROM users WHERE id = :id"
    assert call["params"] == {"id": 7}
    # The template's own limits apply, not the caller's.
    assert call["max_rows"] == 1000


def test_materialized_tool_honours_the_template_limits(test_db):
    """``max_rows`` and ``timeout_seconds`` come from the template row."""
    source = _source(test_db)
    _create(test_db, source, name="Bounded", max_rows=25, timeout_seconds=3.5)
    adapter = RecordingAdapter()
    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_bounded"

    _materialized_call(test_db, source, name, {"id": 1}, adapter)

    assert adapter.calls[0]["max_rows"] == 25
    assert adapter.calls[0]["query_timeout"] == 3.5


def test_materialized_tool_requires_its_declared_arguments(test_db):
    """A missing required parameter is refused before any SQL runs."""
    source = _source(test_db)
    _create(test_db, source, name="Needs Id")
    adapter = RecordingAdapter()
    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_needs-id"

    with pytest.raises(DatabaseToolTemplateArgumentInvalidError, match="Missing required"):
        _materialized_call(test_db, source, name, {}, adapter)

    assert adapter.calls == []


def test_materialized_tool_rejects_undeclared_arguments(test_db):
    """A caller cannot smuggle extra bindings into the statement."""
    source = _source(test_db)
    _create(test_db, source, name="Needs Id")
    adapter = RecordingAdapter()
    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_needs-id"

    with pytest.raises(DatabaseToolTemplateArgumentInvalidError, match="Undeclared"):
        _materialized_call(test_db, source, name, {"id": 1, "admin": True}, adapter)

    assert adapter.calls == []


def test_materialized_tool_reflects_a_disabled_template(test_db):
    """Disabling the template refuses the call even though the name is known."""
    source = _source(test_db)
    read = _create(test_db, source, name="Toggle Me")
    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_toggle-me"
    DatabaseTemplateService.update_template(test_db, source.id, read.id, DatabaseQueryTemplateUpdate(enabled=False))

    with pytest.raises(DatabaseToolTemplateDisabledError):
        _materialized_call(test_db, source, name, {"id": 1}, RecordingAdapter())


def test_materialized_tool_reflects_a_disabled_source(test_db):
    """Disabling the source refuses the call through the same resolution path."""
    from mcpgateway.services.database_tool_service import DatabaseToolSourceDisabledError

    source = _source(test_db)
    _create(test_db, source, name="Daily Revenue")
    name = f"{TEMPLATE_TOOL_PREFIX}{source.slug}_daily-revenue"
    DatabaseSourceService.update_source(test_db, source.id, DatabaseSourceUpdate(enabled=False))

    with pytest.raises(DatabaseToolSourceDisabledError):
        _materialized_call(test_db, source, name, {"id": 1}, RecordingAdapter())


def test_template_tool_name_without_both_slugs_is_unknown(test_db):
    """A malformed materialized name is an unknown tool, not a crash."""
    with pytest.raises(DatabaseToolNotFoundError):
        DatabaseToolService.call(test_db, f"{TEMPLATE_TOOL_PREFIX}onlysource", {}, FakeRuntime(RecordingAdapter()))
