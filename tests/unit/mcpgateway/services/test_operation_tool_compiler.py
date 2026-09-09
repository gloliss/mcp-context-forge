# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_operation_tool_compiler.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the PR3 ``OperationToolCompiler`` (design §17): grouped input
schemas, §18 protocol_config golden shape, output selection, and naming.
"""

# Standard
from dataclasses import dataclass

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.contracts.models import OperationDefinition
from mcpgateway.protocols.http.models import (
    HttpBodyVariant,
    HttpParameter,
    HttpRequestContract,
    HttpResponseContract,
    HttpResponseVariant,
)
from mcpgateway.services.operation_tool_compiler import OperationToolCompiler, ToolCompileOverrides

_STRING_SCHEMA = {"type": "string"}
_OBJECT_SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}}
_NO_BODY_SCHEMA = {"type": "object", "description": "No response body is returned"}


@dataclass(frozen=True)
class _ServiceStub:
    """Duck-typed HTTP service carrying the attributes the compiler reads."""

    slug: str = "petstore"
    base_url: str = "http://127.0.0.1:8899"


@dataclass(frozen=True)
class _ArtifactStub:
    """Duck-typed artifact carrying provenance attributes."""

    content_hash: str = "abc123hash"
    source_type: str = "openapi"


def _param(name, location, required=True, schema=None):
    """Build an HttpParameter shorthand."""
    return HttpParameter(name=name, location=location, required=required, schema=schema or _STRING_SCHEMA)


def _operation(key="GET /ping", method="GET", path_template="/ping", params=(), bodies=(), variants=(), **kwargs):
    """Build an OperationDefinition with an HttpRequestContract."""
    return OperationDefinition(
        key=key,
        protocol="http",
        request=HttpRequestContract(method=method, path_template=path_template, parameters=tuple(params), bodies=tuple(bodies)),
        response=HttpResponseContract(variants=tuple(variants)),
        **kwargs,
    )


def _json_variant(status_code="200", description="ok", schema=None):
    """Build a JSON response variant shorthand."""
    return HttpResponseVariant(status_code=status_code, media_type="application/json", schema=schema or _OBJECT_SCHEMA, description=description)


class TestInputSchemaGrouping:
    """Grouped input schema compilation (design §17)."""

    def test_groups_path_and_query_parameters(self):
        """Path/query parameters land in separate groups; only path is required."""
        operation = _operation(
            key="GET /v1/lots/{lotId}",
            path_template="/v1/lots/{lotId}",
            params=[_param("lotId", "path", required=False), _param("q", "query", required=False)],
        )
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        schema = tool.input_schema
        assert set(schema["properties"]) == {"path", "query"}
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["path"]  # path params are always required
        assert schema["properties"]["path"]["required"] == ["lotId"]
        assert schema["properties"]["path"]["properties"]["lotId"] == _STRING_SCHEMA
        assert schema["properties"]["query"]["required"] == []
        assert schema["properties"]["query"]["properties"]["q"] == _STRING_SCHEMA

    def test_required_query_param_marks_group_required(self):
        """A required query parameter pulls its group into top-level required."""
        operation = _operation(params=[_param("q", "query", required=True)])
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.input_schema["required"] == ["query"]
        assert tool.input_schema["properties"]["query"]["required"] == ["q"]

    def test_header_and_cookie_groups_use_plural_names(self):
        """Header/cookie locations map to ``headers``/``cookies`` groups."""
        operation = _operation(
            params=[_param("x-token", "header"), _param("session", "cookie", required=False)]
        )
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert set(tool.input_schema["properties"]) == {"headers", "cookies"}

    def test_body_group_picks_json_variant_and_marks_required(self):
        """application/json wins over text/*; required requestBody pulls body into required."""
        bodies = (
            HttpBodyVariant(media_type="text/plain", codec="text", schema=None, required=True),
            HttpBodyVariant(media_type="application/json", codec="json", schema=_OBJECT_SCHEMA, required=True),
        )
        operation = _operation(method="POST", path_template="/things", bodies=bodies)
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.input_schema["properties"]["body"] == {
            **_OBJECT_SCHEMA,
            "description": "Request body (application/json)",
        }
        assert "body" in tool.input_schema["required"]

    def test_body_group_schema_none_becomes_string(self):
        """A schema-less text body becomes a string group carrying the media type."""
        bodies = (HttpBodyVariant(media_type="text/plain", codec="text", schema=None, required=False),)
        operation = _operation(method="POST", path_template="/notes", bodies=bodies)
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.input_schema["properties"]["body"] == {
            "type": "string",
            "description": "Request body (text/plain)",
        }
        assert "body" not in tool.input_schema["required"]

    def test_bodyless_operation_has_no_body_group(self):
        """Operations without a requestBody omit the body group entirely."""
        tool = OperationToolCompiler().compile(_operation(), _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert "body" not in tool.input_schema["properties"]


class TestOutputSchemaSelection:
    """Output schema selection (design §17)."""

    def test_exact_200_preferred(self):
        """An exact 200 wins over other 2xx variants regardless of order."""
        operation = _operation(variants=[_json_variant("201"), _json_variant("200", schema=_STRING_SCHEMA)])
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.output_schema == _STRING_SCHEMA

    def test_numeric_2xx_ascending(self):
        """Without a 200 the lowest numeric 2xx wins."""
        operation = _operation(variants=[_json_variant("203"), _json_variant("201", schema=_STRING_SCHEMA)])
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.output_schema == _STRING_SCHEMA

    def test_range_form_2xx_selected(self):
        """The ``2XX`` range form is used when no numeric 2xx exists."""
        operation = _operation(variants=[_json_variant("500"), HttpResponseVariant(status_code="2XX", media_type="application/json", schema=_STRING_SCHEMA)])
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.output_schema == _STRING_SCHEMA

    def test_204_declared_yields_no_body_schema(self):
        """Declaring a no-content status yields the no-body output schema."""
        no_content = HttpResponseVariant(status_code="204", media_type="", schema=None)
        operation = _operation(variants=[_json_variant("200"), no_content])
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.output_schema == _NO_BODY_SCHEMA

    def test_head_always_no_body(self):
        """HEAD yields the no-body output schema even with a 200 JSON variant."""
        operation = _operation(method="HEAD", variants=[_json_variant("200")])
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.output_schema == _NO_BODY_SCHEMA

    def test_unsupported_response_falls_back(self):
        """When nothing usable exists the empty output schema is used."""
        tool = OperationToolCompiler().compile(_operation(), _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.output_schema == {"type": "object", "description": "No supported response schema"}


class TestProtocolConfigGoldenShape:
    """Strict §18 protocol_config payload."""

    def test_golden_shape_with_json_body(self):
        """A POST with a JSON body compiles the exact §18 payload."""
        bodies = (HttpBodyVariant(media_type="application/json", codec="json", schema=_OBJECT_SCHEMA, required=True),)
        operation = _operation(
            key="POST /things",
            method="POST",
            path_template="/things",
            bodies=bodies,
            variants=[_json_variant("200")],
        )
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.protocol_config == {
            "version": 1,
            "operationRef": "POST /things",
            "request": {
                "method": "POST",
                "pathTemplate": "/things",
                "preferredContentType": "application/json",
                "body": {"codec": "json", "mediaType": "application/json"},
            },
            "response": {"codec": "auto", "preferredMediaTypes": ["application/json"]},
            "streaming": {"mode": "none"},
        }

    def test_bodyless_shape_omits_body_and_content_type(self):
        """A GET compiles the §18 shape without body keys."""
        operation = _operation(key="GET /ping", variants=[_json_variant("200")])
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.protocol_config == {
            "version": 1,
            "operationRef": "GET /ping",
            "request": {"method": "GET", "pathTemplate": "/ping"},
            "response": {"codec": "auto", "preferredMediaTypes": ["application/json"]},
            "streaming": {"mode": "none"},
        }

    def test_protocol_config_never_embeds_json_schema(self):
        """No JSON Schema vocabulary may appear inside protocol_config."""
        bodies = (HttpBodyVariant(media_type="application/json", codec="json", schema=_OBJECT_SCHEMA, required=True),)
        operation = _operation(method="POST", bodies=bodies, variants=[_json_variant("200")])
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        text = repr(tool.protocol_config)
        assert "properties" not in text
        assert "$ref" not in text
        assert '"type"' not in text


class TestNamingAndAnnotations:
    """Tool naming, descriptions, annotations, and overrides."""

    def test_default_name_uses_operation_id_and_path_slug(self):
        """The deterministic name is slug__operationId__path-slug."""
        operation = _operation(source_operation_id="getPing", path_template="/v1/ping")
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.name == "petstore__getPing__v1-ping"

    def test_default_name_falls_back_to_method(self):
        """Without an operationId the method fills the middle segment."""
        operation = _operation(path_template="/ping")
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.name == "petstore__get__ping"

    def test_description_derivation_order(self):
        """description > title > method+path fallback."""
        tool = OperationToolCompiler().compile(
            _operation(description="Long description", title="Short title"),
            _ServiceStub(),
            _ArtifactStub(),
            ToolCompileOverrides(),
        )
        assert tool.description == "Long description"

        tool = OperationToolCompiler().compile(
            _operation(title="Short title"), _ServiceStub(), _ArtifactStub(), ToolCompileOverrides()
        )
        assert tool.description == "Short title"

        tool = OperationToolCompiler().compile(_operation(), _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())
        assert tool.description == "GET /ping"

    def test_annotations_carry_responses_and_provenance(self):
        """Annotations hold UI-only response details plus artifact provenance."""
        operation = _operation(variants=[_json_variant("200", description="ok"), _json_variant("404", description="missing")])
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.annotations["readOnlyHint"] is True
        assert tool.annotations["httpArtifact"] == {"contentHash": "abc123hash", "sourceType": "openapi"}
        assert tool.annotations["httpResponses"]["200"]["mediaType"] == "application/json"
        assert tool.annotations["httpResponses"]["404"]["description"] == "missing"

    def test_tool_fields_mirror_contract(self):
        """base_url, integration_type, and request_type mirror the contract."""
        operation = _operation(method="DELETE", path_template="/things/{id}", params=[_param("id", "path")])
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())

        assert tool.base_url == "http://127.0.0.1:8899"
        assert tool.integration_type == "REST"
        assert tool.request_type == "DELETE"

    def test_overrides_win(self):
        """Caller overrides replace the compiled values."""
        operation = _operation()
        overrides = ToolCompileOverrides(name="custom-name", description="custom", input_schema={"type": "object"}, output_schema={"type": "string"})
        tool = OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), overrides)

        assert tool.name == "custom-name"
        assert tool.description == "custom"
        assert tool.input_schema == {"type": "object"}
        assert tool.output_schema == {"type": "string"}

    def test_non_http_request_rejected(self):
        """Operations without an HttpRequestContract are rejected."""
        operation = OperationDefinition(key="GET /x", protocol="http", request={"url": "http://x"})
        with pytest.raises(ValueError, match="no HTTP request contract"):
            OperationToolCompiler().compile(operation, _ServiceStub(), _ArtifactStub(), ToolCompileOverrides())
