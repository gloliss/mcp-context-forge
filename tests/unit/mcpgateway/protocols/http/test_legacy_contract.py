# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_legacy_contract.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for LegacyRestContractBuilder (PR1).
"""

# Standard
from types import SimpleNamespace

# First-Party
from mcpgateway.protocols.http.legacy_contract import LegacyRestContractBuilder


def _make_payload(**overrides) -> dict:
    """Build a minimal legacy REST tool payload with sane defaults."""
    payload = {
        "id": "tool-1",
        "name": "demo_tool",
        "url": "https://api.example.com/data",
        "request_type": "POST",
        "gateway_id": "gw-1",
        "query_mapping": {"a": "x"},
        "header_mapping": {"b": "X-Custom"},
        "output_schema": {"type": "object"},
        "jsonpath_filter": ".result",
    }
    payload.update(overrides)
    return payload


def test_operation_key_uses_legacy_tool_id():
    """The stable key is the PR1 legacy form http:rest:<tool_id>."""
    operation = LegacyRestContractBuilder.from_tool(SimpleNamespace(id="tool-9"), _make_payload(id="tool-9"))
    assert operation.key == "http:rest:tool-9"
    assert operation.protocol == "http"
    assert operation.source_operation_id == "tool-9"


def test_method_is_uppercased_request_type():
    """The method mirrors `request_type.upper()` from the pre-extraction code."""
    operation = LegacyRestContractBuilder.from_tool(None, _make_payload(request_type="get"))
    assert operation.request["method"] == "GET"


def test_method_defaults_to_post_when_request_type_missing():
    """A missing/None request_type defaults to POST, as before the extraction."""
    operation = LegacyRestContractBuilder.from_tool(None, _make_payload(request_type=None))
    assert operation.request["method"] == "POST"
    operation = LegacyRestContractBuilder.from_tool(None, _make_payload())
    assert operation.request["method"] == "POST"


def test_request_carries_url_and_mappings():
    """request carries url, method, query_mapping, and header_mapping."""
    payload = _make_payload()
    operation = LegacyRestContractBuilder.from_tool(None, payload)
    assert operation.request["url"] == "https://api.example.com/data"
    assert operation.request["query_mapping"] == {"a": "x"}
    assert operation.request["header_mapping"] == {"b": "X-Custom"}


def test_non_dict_mappings_become_none():
    """Non-dict mappings are normalized to None, mirroring invoke_tool locals."""
    payload = _make_payload(query_mapping="not-a-dict", header_mapping=["list"])
    operation = LegacyRestContractBuilder.from_tool(None, payload)
    assert operation.request["query_mapping"] is None
    assert operation.request["header_mapping"] is None


def test_response_carries_output_schema_and_jsonpath_filter():
    """response carries the jq/schema pipeline inputs for ToolService."""
    operation = LegacyRestContractBuilder.from_tool(None, _make_payload())
    assert operation.response["output_schema"] == {"type": "object"}
    assert operation.response["jsonpath_filter"] == ".result"


def test_extensions_carry_identity_metadata_only():
    """extensions hold only source/debug metadata (tool/gateway ids, name)."""
    operation = LegacyRestContractBuilder.from_tool(None, _make_payload())
    assert operation.extensions == {"tool_id": "tool-1", "gateway_id": "gw-1", "tool_name_computed": "demo_tool"}
    assert operation.title == "demo_tool"


def test_tool_payload_is_authoritative_over_orm_row():
    """On the cache-hit path tool may be None; payload values always win."""
    orm_tool = SimpleNamespace(id="orm-id")
    operation = LegacyRestContractBuilder.from_tool(orm_tool, _make_payload(id="payload-id", url="https://payload.example.com"))
    assert operation.key == "http:rest:payload-id"
    assert operation.request["url"] == "https://payload.example.com"


def test_orm_tool_id_used_when_payload_has_no_id():
    """The ORM row is the fallback for the identity fields."""
    operation = LegacyRestContractBuilder.from_tool(SimpleNamespace(id="orm-id"), {"url": "https://x.example.com"})
    assert operation.key == "http:rest:orm-id"
    assert operation.source_operation_id == "orm-id"


def test_empty_payload_yields_post_operation_with_empty_identity():
    """An empty payload compiles to a POST operation with an empty key suffix."""
    operation = LegacyRestContractBuilder.from_tool(None, {})
    assert operation.key == "http:rest:"
    assert operation.request["method"] == "POST"
    assert operation.request["url"] is None


def _registry_protocol_config() -> dict:
    """Build a §18-shaped protocol_config for registry-branch tests."""
    return {
        "version": 1,
        "operationRef": "GET /v1/lots/{lotId}",
        "request": {
            "method": "GET",
            "pathTemplate": "/v1/lots/{lotId}",
            "preferredContentType": "application/json",
        },
        "response": {"codec": "auto", "preferredMediaTypes": ["application/json"]},
        "streaming": {"mode": "none"},
    }


def test_registry_tool_uses_http_registry_key():
    """Registry tools with protocol_config compile to http:registry:<tool_id>."""
    payload = _make_payload(protocol_config=_registry_protocol_config())
    operation = LegacyRestContractBuilder.from_tool(None, payload)
    assert operation.key == "http:registry:tool-1"
    assert operation.protocol == "http"
    assert operation.source_operation_id == "GET /v1/lots/{lotId}"


def test_registry_tool_request_carries_base_url_method_and_path():
    """Registry request carries base_url (column fallback), method, pathTemplate."""
    payload = _make_payload(
        protocol_config=_registry_protocol_config(),
        base_url="https://api.example.com",
        url="https://legacy.example.com",
    )
    operation = LegacyRestContractBuilder.from_tool(None, payload)
    assert operation.request["base_url"] == "https://api.example.com"
    assert operation.request["method"] == "GET"
    assert operation.request["path_template"] == "/v1/lots/{lotId}"
    assert operation.request.get("query_mapping") is None


def test_registry_tool_base_url_falls_back_to_url_column():
    """base_url falls back to the legacy url column when unset."""
    payload = _make_payload(protocol_config=_registry_protocol_config())
    operation = LegacyRestContractBuilder.from_tool(None, payload)
    assert operation.request["base_url"] == "https://api.example.com/data"


def test_registry_tool_method_falls_back_to_request_type():
    """A config without request.method falls back to request_type.upper()."""
    config = _registry_protocol_config()
    config["request"].pop("method")
    payload = _make_payload(protocol_config=config, request_type="post")
    operation = LegacyRestContractBuilder.from_tool(None, payload)
    assert operation.request["method"] == "POST"


def test_registry_tool_keeps_identity_extensions():
    """The registry branch keeps the same identity-only extensions."""
    operation = LegacyRestContractBuilder.from_tool(None, _make_payload(protocol_config=_registry_protocol_config()))
    assert operation.extensions == {"tool_id": "tool-1", "gateway_id": "gw-1", "tool_name_computed": "demo_tool"}


def test_registry_tool_ignores_jsonpath_filter():
    """Registry responses go through the PR2 decoder, not the jq pipeline."""
    operation = LegacyRestContractBuilder.from_tool(None, _make_payload(protocol_config=_registry_protocol_config()))
    assert operation.response == {"output_schema": {"type": "object"}}
