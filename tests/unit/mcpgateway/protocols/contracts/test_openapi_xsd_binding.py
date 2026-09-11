# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/contracts/test_openapi_xsd_binding.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

The XSD vendor extension must survive OpenAPI → compiler → protocol_config
(PR4, design §27).  These tests pin the whole handoff, because a binding that
is read by the provider but dropped by the compiler is indistinguishable from
having no binding at all at runtime.
"""

# Standard
import json

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.contracts.models import ContractArtifact, DiscoveryContext
from mcpgateway.protocols.contracts.openapi import OpenAPIContractProvider
from mcpgateway.services.operation_tool_compiler import OperationToolCompiler, ToolCompileOverrides

_REQUEST_XSD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' id='req'/>"
_RESPONSE_XSD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' id='resp'/>"


def _document() -> dict:
    """Build an OpenAPI document declaring XML bodies bound to XSDs."""
    return {
        "openapi": "3.0.3",
        "info": {"title": "xml-report", "version": "1.0.0"},
        "paths": {
            "/query": {
                "post": {
                    "operationId": "queryReport",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/xml": {
                                "x-contextforge-xsd": {"schema": _REQUEST_XSD},
                                "schema": {"type": "object"},
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/xml": {
                                    "x-contextforge-xsd": {"schema": _RESPONSE_XSD},
                                    "schema": {"type": "object"},
                                }
                            },
                        }
                    },
                }
            }
        },
    }


@pytest.fixture
def compiled_config() -> dict:
    """Compile the document and return the resulting protocol_config."""
    import asyncio

    artifact = ContractArtifact(payload=json.dumps(_document()).encode(), artifact_format="openapi-json", source_type="openapi-upload")
    catalog = asyncio.run(OpenAPIContractProvider().discover(artifact, DiscoveryContext()))
    assert catalog.operations, "the document must yield an operation"
    compiled = OperationToolCompiler().compile(
        catalog.operations[0],
        type("Service", (), {"slug": "report", "base_url": "http://upstream.invalid"})(),
        type("Artifact", (), {"content_hash": catalog.source_hash, "source_type": "openapi"})(),
        ToolCompileOverrides(),
    )
    return compiled.protocol_config or {}


class TestXsdBindingThroughOpenApi:
    """The extension reaches both sides of protocol_config (§27)."""

    def test_request_body_uses_the_xml_codec(self, compiled_config):
        """An application/xml body compiles to the xml codec, not binary."""
        assert compiled_config["request"]["body"]["codec"] == "xml"

    def test_request_xsd_is_carried(self, compiled_config):
        """The request-side binding survives compilation."""
        assert compiled_config["request"]["body"]["xsd"]["schema"] == _REQUEST_XSD

    def test_response_xsd_is_carried(self, compiled_config):
        """The response-side binding survives compilation."""
        assert compiled_config["response"]["xsd"]["schema"] == _RESPONSE_XSD

    def test_the_runtime_binding_reader_finds_it(self, compiled_config):
        """The runtime reads back exactly what the compiler emitted."""
        # First-Party
        from mcpgateway.protocols.http.xsd_binding import build_xsd_type_system

        assert build_xsd_type_system(compiled_config, side="request") is not None
        assert build_xsd_type_system(compiled_config, side="response") is not None

    def test_a_document_without_the_extension_declares_no_binding(self):
        """A plain OpenAPI document is unaffected by the extension support."""
        import asyncio

        document = _document()
        del document["paths"]["/query"]["post"]["requestBody"]["content"]["application/xml"]["x-contextforge-xsd"]
        del document["paths"]["/query"]["post"]["responses"]["200"]["content"]["application/xml"]["x-contextforge-xsd"]
        artifact = ContractArtifact(payload=json.dumps(document).encode(), artifact_format="openapi-json", source_type="openapi-upload")
        catalog = asyncio.run(OpenAPIContractProvider().discover(artifact, DiscoveryContext()))
        compiled = OperationToolCompiler().compile(
            catalog.operations[0],
            type("Service", (), {"slug": "report", "base_url": "http://upstream.invalid"})(),
            type("Artifact", (), {"content_hash": catalog.source_hash, "source_type": "openapi"})(),
            ToolCompileOverrides(),
        )

        assert "xsd" not in compiled.protocol_config["request"]["body"]
        assert "xsd" not in compiled.protocol_config["response"]
