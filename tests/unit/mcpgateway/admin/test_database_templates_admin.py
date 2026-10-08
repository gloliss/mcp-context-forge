# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/admin/test_database_templates_admin.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the Query Templates admin UI form parsing and rendering (Phase 2).

The form carries two JSON Schema documents as free text, so the parser is where
malformed input becomes a readable error instead of a 500.  The error response
is also asserted to be swappable by htmx: a refused template is only useful if
the admin can read why it was refused.
"""

# Third-Party
import json

import pytest
from pydantic import ValidationError

# First-Party
from mcpgateway.admin import (
    _database_template_error_response,
    _database_template_form_context,
    _database_template_raw_fields,
)
from mcpgateway.schemas import DatabaseQueryTemplateCreate, DatabaseQueryTemplateRead


def _form(**overrides) -> dict:
    """Build a realistic flat query-template form submission."""
    data = {
        "name": "Daily Revenue",
        "statement": "SELECT region, SUM(amount) FROM ledger WHERE day = :day GROUP BY region",
        "parameter_schema": json.dumps(
            {"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]}
        ),
        "result_schema": json.dumps({"type": "object", "properties": {"region": {"type": "string"}}}),
        "max_rows": "250",
        "timeout_seconds": "12.5",
        "enabled": "on",
    }
    data.update(overrides)
    return data


def _read(**overrides) -> DatabaseQueryTemplateRead:
    """Build a read model as the service would return it."""
    from datetime import datetime, timezone

    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    base = {
        "id": "tpl-1",
        "source_id": "src-1",
        "name": "Daily Revenue",
        "slug": "daily-revenue",
        "statement": "SELECT region FROM ledger WHERE day = :day",
        "parameter_schema": {"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]},
        "result_schema": {"type": "object", "properties": {"region": {"type": "string"}}},
        "max_rows": 250,
        "timeout_seconds": 12.5,
        "enabled": True,
        "version": 1,
        "tool_name": "db_tmpl_src-1_daily-revenue",
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return DatabaseQueryTemplateRead(**base)


# --------------------------------------------------------------------------
# Form parsing
# --------------------------------------------------------------------------
def test_raw_fields_parses_json_schemas():
    """Both schema textareas become real dicts, and limits are typed."""
    fields = _database_template_raw_fields(_form())

    assert fields["name"] == "Daily Revenue"
    assert fields["statement"].startswith("SELECT region")
    assert fields["parameter_schema"]["required"] == ["day"]
    assert fields["result_schema"]["properties"] == {"region": {"type": "string"}}
    assert fields["max_rows"] == 250
    assert fields["timeout_seconds"] == 12.5
    assert fields["enabled"] is True


def test_raw_fields_parses_a_blank_schema_as_empty():
    """An empty textarea means "no schema", not a parse error."""
    fields = _database_template_raw_fields(_form(parameter_schema="", result_schema="   "))

    assert fields["parameter_schema"] == {}
    assert fields["result_schema"] == {}


def test_raw_fields_defaults_limits_when_blank():
    """Blank numeric inputs fall back to the model's own defaults."""
    fields = _database_template_raw_fields(_form(max_rows="", timeout_seconds=""))

    assert fields["max_rows"] == 1000
    assert fields["timeout_seconds"] == 15.0


def test_raw_fields_unchecked_enabled_defaults_false():
    """An absent checkbox means unchecked, matching the source form's convention."""
    fields = _database_template_raw_fields({})

    assert fields["name"] is None
    assert fields["statement"] is None
    assert fields["parameter_schema"] == {}
    assert fields["enabled"] is False


@pytest.mark.parametrize("field", ["parameter_schema", "result_schema"])
def test_raw_fields_rejects_malformed_json(field):
    """Broken JSON is reported against the field that carries it."""
    with pytest.raises(ValueError, match=field):
        _database_template_raw_fields(_form(**{field: "{not json"}))


@pytest.mark.parametrize("field", ["parameter_schema", "result_schema"])
def test_raw_fields_rejects_non_object_json(field):
    """A JSON array or scalar cannot describe properties, so it is refused."""
    with pytest.raises(ValueError, match="must be a JSON object"):
        _database_template_raw_fields(_form(**{field: "[1, 2, 3]"}))


def test_raw_fields_builds_a_valid_create_payload():
    """The parsed fields satisfy the create schema the service consumes."""
    data = DatabaseQueryTemplateCreate(**_database_template_raw_fields(_form()))

    assert data.name == "Daily Revenue"
    assert data.parameter_schema["required"] == ["day"]
    assert data.max_rows == 250


def test_raw_fields_surfaces_schema_shape_errors():
    """The edge validator still runs, so a phantom ``required`` entry is refused."""
    with pytest.raises(ValidationError):
        DatabaseQueryTemplateCreate(
            **_database_template_raw_fields(
                _form(parameter_schema=json.dumps({"type": "object", "properties": {}, "required": ["ghost"]}))
            )
        )


# --------------------------------------------------------------------------
# Form context
# --------------------------------------------------------------------------
def test_form_context_prefills_a_usable_parameter_schema():
    """The create form starts from the smallest valid schema, not a blank box."""
    create = _database_template_form_context()

    assert json.loads(create["parameter_schema"]) == {"type": "object", "properties": {}}
    assert json.loads(create["result_schema"]) == {}
    assert create["max_rows"] == 1000
    assert create["timeout_seconds"] == 15.0
    # A new template materializes a tool unless the admin opts out.
    assert create["enabled"] is True
    assert create["name"] == ""


def test_form_context_serializes_existing_schemas():
    """Edit mode renders the stored schemas as JSON text that round-trips."""
    context = _database_template_form_context(_read())

    assert context["name"] == "Daily Revenue"
    assert context["statement"].startswith("SELECT region")
    assert json.loads(context["parameter_schema"]) == _read().parameter_schema
    assert json.loads(context["result_schema"]) == _read().result_schema
    assert context["max_rows"] == 250
    assert context["timeout_seconds"] == 12.5
    assert context["enabled"] is True


def test_form_context_renders_an_empty_schema_as_an_object():
    """A template that takes no parameters still renders valid JSON Schema."""
    context = _database_template_form_context(_read(parameter_schema={}, result_schema={}))

    assert json.loads(context["parameter_schema"]) == {}
    assert json.loads(context["result_schema"]) == {}


# --------------------------------------------------------------------------
# Error rendering
# --------------------------------------------------------------------------
def test_error_response_is_swappable_by_htmx():
    """A refused template carries HX-Retarget so the form shows the reason."""
    response = _database_template_error_response("<div>undeclared parameter: id</div>", 422)

    assert response.status_code == 422
    assert response.headers["HX-Retarget"] == "#database-template-form-error"
    assert b"undeclared parameter" in response.body


def test_error_response_escapes_nothing_extra():
    """The body is passed through unchanged, so the caller owns escaping."""
    response = _database_template_error_response('&lt;script&gt;', 409)

    assert response.status_code == 409
    assert response.body == b"&lt;script&gt;"
