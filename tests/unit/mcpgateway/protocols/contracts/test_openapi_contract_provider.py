# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/contracts/test_openapi_contract_provider.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the PR3 OpenAPI contract provider and the framework-free
openapi-core validation shim (design-document §15 and §22).
"""

# Standard
import hashlib
from unittest.mock import patch

# Third-Party
from openapi_core.datatypes import Headers
import orjson
import pytest
import yaml

# First-Party
from mcpgateway.protocols.contracts import openapi as openapi_module
from mcpgateway.protocols.contracts.models import (
    ContractArtifact,
    ContractProviderError,
    DiscoveryContext,
)
from mcpgateway.protocols.contracts.openapi import (
    empty_request_parameters,
    load_openapi,
    OpenAPIContractProvider,
    OpenAPIRequestShim,
    OpenAPIResponseShim,
    validate_openapi_request,
    validate_openapi_response,
)
from mcpgateway.protocols.http.models import HttpBodyVariant, HttpRequestContract, HttpResponseContract


def _spec() -> dict:
    """Build a minimal valid OpenAPI 3.0 document."""
    return {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
    }


def _artifact(payload: bytes) -> ContractArtifact:
    """Wrap raw payload bytes into a ``ContractArtifact``."""
    return ContractArtifact(payload=payload, artifact_format="openapi-json", source_type="openapi-upload")


async def _discover(spec: dict, **limits) -> tuple:
    """Compile a spec dict and return ``(operations, diagnostics)``."""
    catalog = await OpenAPIContractProvider().discover(_artifact(orjson.dumps(spec)), DiscoveryContext(limits=dict(limits)))
    return catalog.operations, catalog.diagnostics


def _contains_ref(node) -> bool:
    """Return True when any mapping in the subtree carries a ``$ref`` key.

    Args:
        node: The compiled schema subtree (dict/list/scalar).

    Returns:
        True when a dangling reference survives compilation.
    """
    if isinstance(node, dict):
        if "$ref" in node:
            return True
        return any(_contains_ref(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_ref(item) for item in node)
    return False


class TestOpenAPIContractProvider:
    """OpenAPI artifact compilation into typed operation IR."""

    @pytest.mark.asyncio
    async def test_basic_get_operation_compiled(self):
        """A GET operation compiles with the stable key and typed request."""
        spec = _spec()
        spec["paths"]["/v1/lots/{lotId}"] = {
            "parameters": [{"name": "lotId", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {
                "operationId": "getLot",
                "summary": "Fetch one lot",
                "tags": ["lots"],
                "responses": {"200": {"description": "ok"}},
            },
        }

        operations, diagnostics = await _discover(spec)

        assert diagnostics == ()
        assert len(operations) == 1
        op = operations[0]
        assert op.key == "GET /v1/lots/{lotId}"
        assert op.protocol == "http"
        assert op.source_operation_id == "getLot"
        assert op.title == "Fetch one lot"
        assert op.tags == ("lots",)
        assert isinstance(op.request, HttpRequestContract)
        assert op.request.method == "GET"
        assert op.request.path_template == "/v1/lots/{lotId}"
        assert len(op.request.parameters) == 1
        assert op.request.parameters[0].name == "lotId"
        assert op.request.bodies == ()

    @pytest.mark.asyncio
    async def test_body_variants_with_codec_mapping(self):
        """requestBody content maps one variant per media type with codecs."""
        spec = _spec()
        spec["paths"]["/pets"] = {
            "post": {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"type": "object"}},
                        "application/x-www-form-urlencoded": {"schema": {"type": "object"}},
                        "multipart/form-data": {"schema": {"type": "object"}},
                    },
                },
                "responses": {"201": {"description": "created"}},
            }
        }

        operations, _diagnostics = await _discover(spec)

        bodies = operations[0].request.bodies
        assert [b.media_type for b in bodies] == [
            "application/json",
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        ]
        assert [b.codec for b in bodies] == ["json", "form", "multipart"]
        assert all(b.required for b in bodies)
        assert all(isinstance(b, HttpBodyVariant) for b in bodies)

    @pytest.mark.asyncio
    async def test_parameter_merge_operation_overrides_path(self):
        """Operation-level parameters override path-level on (name, in)."""
        spec = _spec()
        spec["paths"]["/pets/{id}"] = {
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                {"name": "q", "in": "query", "schema": {"type": "string"}},
            ],
            "get": {
                "parameters": [{"name": "q", "in": "query", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "ok"}},
            },
        }

        operations, _diagnostics = await _discover(spec)

        params = {p.name: p for p in operations[0].request.parameters}
        assert params["id"].location == "path" and params["id"].required
        assert params["q"].schema == {"type": "integer"}  # operation-level wins
        assert params["q"].required

    @pytest.mark.asyncio
    async def test_parameter_flags_survive(self):
        """style/explode/allowReserved survive into the IR."""
        spec = _spec()
        spec["paths"]["/search"] = {
            "get": {
                "parameters": [
                    {
                        "name": "tags",
                        "in": "query",
                        "style": "form",
                        "explode": False,
                        "allowReserved": True,
                        "schema": {"type": "array", "items": {"type": "string"}},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            }
        }

        operations, _diagnostics = await _discover(spec)

        param = operations[0].request.parameters[0]
        assert param.style == "form"
        assert param.explode is False
        assert param.allow_reserved is True

    @pytest.mark.asyncio
    async def test_local_ref_resolved_in_body_schema(self):
        """Internal $ref schemas are dereferenced; the ref is kept for provenance."""
        spec = _spec()
        spec["components"] = {"schemas": {"Pet": {"type": "object"}}}
        spec["paths"]["/pets"] = {
            "post": {
                "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}},
                "responses": {"200": {"description": "ok"}},
            }
        }

        operations, diagnostics = await _discover(spec)

        assert diagnostics == ()
        body = operations[0].request.bodies[0]
        assert body.schema == {"type": "object"}
        assert body.schema_ref == "#/components/schemas/Pet"

    @pytest.mark.asyncio
    async def test_chained_ref_resolved(self):
        """$ref chains (A → B) resolve to the final target."""
        spec = _spec()
        spec["components"] = {
            "schemas": {
                "Alias": {"$ref": "#/components/schemas/Real"},
                "Real": {"type": "string"},
            }
        }
        spec["paths"]["/pets"] = {
            "get": {
                "parameters": [{"name": "q", "in": "query", "schema": {"$ref": "#/components/schemas/Alias"}}],
                "responses": {"200": {"description": "ok"}},
            }
        }

        operations, diagnostics = await _discover(spec)

        assert diagnostics == ()
        assert operations[0].request.parameters[0].schema == {"type": "string"}

    @pytest.mark.asyncio
    async def test_non_local_ref_rejected_before_validation(self):
        """Non-local $ref values fail closed (the provider never fetches)."""
        spec = _spec()
        spec["paths"]["/pets"] = {
            "get": {
                "parameters": [{"name": "q", "in": "query", "schema": {"$ref": "https://example.com/q.json"}}],
                "responses": {"200": {"description": "ok"}},
            }
        }

        with pytest.raises(ContractProviderError, match="non-local \\$ref"):
            await OpenAPIContractProvider().discover(_artifact(orjson.dumps(spec)), DiscoveryContext())

    @pytest.mark.asyncio
    async def test_circular_ref_rejected_by_validation(self):
        """Cyclic reference graphs are invalid specs and fail validation."""
        spec = _spec()
        spec["components"] = {
            "schemas": {
                "A": {"$ref": "#/components/schemas/B"},
                "B": {"$ref": "#/components/schemas/A"},
            }
        }
        spec["paths"]["/pets"] = {
            "get": {
                "parameters": [{"name": "q", "in": "query", "schema": {"$ref": "#/components/schemas/A"}}],
                "responses": {"200": {"description": "ok"}},
            }
        }

        with pytest.raises(ContractProviderError, match="validation failed"):
            await OpenAPIContractProvider().discover(_artifact(orjson.dumps(spec)), DiscoveryContext())

    @pytest.mark.asyncio
    async def test_invalid_spec_raises_provider_error(self):
        """A structurally invalid spec is a hard failure, not an empty catalog."""
        with pytest.raises(ContractProviderError, match="validation failed"):
            await OpenAPIContractProvider().discover(
                _artifact(orjson.dumps({"openapi": "3.0.0", "info": {"title": "t", "version": "1"}})),
                DiscoveryContext(),
            )

    @pytest.mark.asyncio
    async def test_unparseable_payload_raises(self):
        """Payloads that are neither JSON nor YAML raise."""
        with pytest.raises(ContractProviderError, match="not valid JSON or YAML"):
            await OpenAPIContractProvider().discover(_artifact(b"{"), DiscoveryContext())

    @pytest.mark.asyncio
    async def test_non_object_payload_raises(self):
        """JSON arrays are rejected (root must be an object)."""
        with pytest.raises(ContractProviderError, match="not a JSON object"):
            await OpenAPIContractProvider().discover(_artifact(b"[1, 2, 3]"), DiscoveryContext())

    @pytest.mark.asyncio
    async def test_yaml_payload_compiles(self):
        """YAML payloads parse and compile like JSON ones."""
        spec = _spec()
        spec["paths"]["/ping"] = {"get": {"responses": {"200": {"description": "ok"}}}}

        catalog = await OpenAPIContractProvider().discover(
            ContractArtifact(
                payload=yaml.safe_dump(spec, sort_keys=True).encode(),
                artifact_format="openapi-yaml",
                source_type="openapi-upload",
            ),
            DiscoveryContext(),
        )

        assert [op.key for op in catalog.operations] == ["GET /ping"]

    @pytest.mark.asyncio
    async def test_bad_operation_skipped_with_error_diagnostic(self):
        """One failing operation is skipped; the rest still compile."""
        spec = _spec()
        spec["paths"]["/good"] = {"get": {"responses": {"200": {"description": "ok"}}}}
        spec["paths"]["/bad"] = {"get": {"responses": {"200": {"description": "ok"}}}}
        original = OpenAPIContractProvider._compile_operation

        def _boom(self, key, path, method, operation, path_parameters, resolver):
            """Synthetic per-operation failure."""
            if path == "/bad":
                raise RuntimeError("synthetic failure")
            return original(self, key, path, method, operation, path_parameters, resolver)

        with patch.object(OpenAPIContractProvider, "_compile_operation", _boom):
            catalog = await OpenAPIContractProvider().discover(_artifact(orjson.dumps(spec)), DiscoveryContext())

        assert [op.key for op in catalog.operations] == ["GET /good"]
        errors = [d for d in catalog.diagnostics if d.code == "operation-compilation-failed"]
        assert len(errors) == 1
        assert errors[0].operation_key == "GET /bad"

    @pytest.mark.asyncio
    async def test_responses_ordered_2xx_first_then_default_last(self):
        """Response variants sort 2xx first; 'default' comes last."""
        spec = _spec()
        spec["paths"]["/pets"] = {
            "get": {
                "responses": {
                    "500": {"description": "boom"},
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                    "default": {"description": "fallback"},
                    "404": {"description": "missing"},
                }
            }
        }

        operations, _diagnostics = await _discover(spec)

        variants = operations[0].response.variants
        assert [v.status_code for v in variants] == ["200", "404", "500", "default"]
        assert variants[0].schema == {"type": "object"}
        assert isinstance(operations[0].response, HttpResponseContract)

    @pytest.mark.asyncio
    async def test_contentless_response_variant(self):
        """A 204 with no content yields a bodyless variant (empty media type)."""
        spec = _spec()
        spec["paths"]["/pets"] = {"delete": {"responses": {"204": {"description": "gone"}}}}

        operations, _diagnostics = await _discover(spec)

        variants = operations[0].response.variants
        assert len(variants) == 1
        assert variants[0].status_code == "204"
        assert variants[0].media_type == ""
        assert variants[0].schema is None

    @pytest.mark.asyncio
    async def test_source_hash_and_type(self):
        """The catalog carries the payload hash and 'openapi' source type."""
        spec = _spec()
        spec["paths"]["/ping"] = {"get": {"responses": {"200": {"description": "ok"}}}}
        payload = orjson.dumps(spec)

        catalog = await OpenAPIContractProvider().discover(_artifact(payload), DiscoveryContext())

        assert catalog.source_type == "openapi"
        assert catalog.source_hash == hashlib.sha256(payload).hexdigest()

    @pytest.mark.asyncio
    async def test_max_operations_truncates_with_warning(self):
        """The max_operations limit truncates and reports a warning."""
        spec = _spec()
        spec["paths"]["/a"] = {"get": {"responses": {"200": {"description": "ok"}}}}
        spec["paths"]["/b"] = {"get": {"responses": {"200": {"description": "ok"}}}}

        operations, diagnostics = await _discover(spec, max_operations=1)

        assert [op.key for op in operations] == ["GET /a"]
        assert any(d.code == "operation-limit-reached" for d in diagnostics)

    @pytest.mark.asyncio
    async def test_empty_paths_yields_empty_catalog(self):
        """A valid spec with no paths compiles to an empty catalog."""
        operations, diagnostics = await _discover(_spec())

        assert operations == ()
        assert diagnostics == ()

    @pytest.mark.asyncio
    async def test_path_item_ref_resolved(self):
        """A path item that is itself a $ref resolves through components (3.1)."""
        spec = _spec()
        spec["openapi"] = "3.1.0"
        spec["components"] = {"pathItems": {"PingItem": {"get": {"responses": {"200": {"description": "ok"}}}}}}
        spec["paths"]["/ping"] = {"$ref": "#/components/pathItems/PingItem"}

        operations, diagnostics = await _discover(spec)

        assert [op.key for op in operations] == ["GET /ping"]
        assert diagnostics == ()

    async def test_nested_refs_inlined_in_response_schema(self):
        """Response schemas are self-contained: nested $refs are inlined."""
        spec = _spec()
        spec["components"] = {"schemas": {"Pet": {"type": "object", "properties": {"name": {"type": "string"}}}}}
        spec["paths"]["/pets"] = {
            "get": {
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/Pet"},
                                }
                            }
                        },
                    }
                }
            }
        }

        operations, diagnostics = await _discover(spec)

        assert diagnostics == ()
        schema = operations[0].response.variants[0].schema
        assert schema == {
            "type": "array",
            "items": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
        assert not _contains_ref(schema)

    async def test_nested_refs_inlined_in_body_and_parameter_schemas(self):
        """Body and parameter schemas are self-contained: nested refs inlined."""
        spec = _spec()
        spec["components"] = {
            "schemas": {
                "Tag": {"type": "string", "maxLength": 20},
                "NewPet": {
                    "type": "object",
                    "properties": {"tag": {"$ref": "#/components/schemas/Tag"}},
                },
            }
        }
        spec["paths"]["/pets"] = {
            "post": {
                "parameters": [
                    {
                        "name": "verbose",
                        "in": "query",
                        "schema": {"allOf": [{"$ref": "#/components/schemas/Tag"}]},
                    }
                ],
                "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/NewPet"}}}},
                "responses": {"200": {"description": "ok"}},
            }
        }

        operations, diagnostics = await _discover(spec)

        assert diagnostics == ()
        request = operations[0].request
        assert request.parameters[0].schema == {"allOf": [{"type": "string", "maxLength": 20}]}
        assert not _contains_ref(request.parameters[0].schema)
        assert request.bodies[0].schema == {
            "type": "object",
            "properties": {"tag": {"type": "string", "maxLength": 20}},
        }
        assert not _contains_ref(request.bodies[0].schema)

    def test_ref_resolver_materialize_back_edge_collapses(self):
        """Materialise collapses a circular back-edge to a permissive schema."""
        document = {
            "components": {
                "schemas": {
                    "Node": {
                        "type": "object",
                        "properties": {"child": {"$ref": "#/components/schemas/Node"}},
                    }
                }
            }
        }
        resolver = openapi_module._RefResolver(document)  # pylint: disable=protected-access

        result = resolver.materialize({"type": "array", "items": {"$ref": "#/components/schemas/Node"}})

        assert result["items"]["properties"]["child"] == {}
        assert any("Circular $ref chain detected" in w for w in resolver.warnings)
        assert not _contains_ref(result)


class TestOpenAPICoreShim:
    """Framework-free openapi-core request/response validation contract."""

    @staticmethod
    def _validation_spec() -> dict:
        """Build the shim-contract spec (path/query/header/body rules)."""
        return {
            "openapi": "3.0.0",
            "info": {"title": "t", "version": "1"},
            "servers": [{"url": "https://api.example.com"}],
            "paths": {
                "/pets/{pet_id}": {
                    "post": {
                        "parameters": [
                            {"name": "pet_id", "in": "path", "required": True, "schema": {"type": "integer"}},
                            {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["name"],
                                        "properties": {"name": {"type": "string"}},
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "required": ["id"],
                                            "properties": {"id": {"type": "integer"}},
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
        }

    def _request(self, **overrides) -> OpenAPIRequestShim:
        """Build a valid request shim for the contract spec."""
        fields = {
            "host_url": "https://api.example.com",
            "path": "/pets/5",
            "full_url_pattern": "https://api.example.com/pets/{pet_id}",
            "method": "post",
            "parameters": empty_request_parameters(query={"q": "x"}),
            "content_type": "application/json",
            "body": b'{"name": "fido"}',
        }
        fields.update(overrides)
        return OpenAPIRequestShim(**fields)

    def test_validate_request_passes_and_fills_path_params(self):
        """A valid request validates and path params are filled by the spec."""
        openapi = load_openapi("h1", self._validation_spec())

        request = self._request()
        validate_openapi_request(openapi, request)

        assert dict(request.parameters.path) == {"pet_id": "5"}

    def test_validate_request_rejects_missing_required_query(self):
        """A missing required query parameter raises ValueError."""
        openapi = load_openapi("h2", self._validation_spec())

        with pytest.raises(ValueError, match="Request validation failed"):
            validate_openapi_request(openapi, self._request(parameters=empty_request_parameters()))

    def test_validate_request_rejects_bad_body(self):
        """A body violating the schema raises ValueError."""
        openapi = load_openapi("h3", self._validation_spec())

        with pytest.raises(ValueError, match="Request validation failed"):
            validate_openapi_request(openapi, self._request(body=b'{"nope": 1}'))

    def test_validate_response_rejects_schema_mismatch(self):
        """A response missing required fields raises ValueError."""
        openapi = load_openapi("h4", self._validation_spec())
        request = self._request()
        response = OpenAPIResponseShim(
            status_code=200,
            content_type="application/json",
            headers=Headers({"content-type": "application/json"}),
            data=b"{}",
        )

        with pytest.raises(ValueError, match="Response validation failed"):
            validate_openapi_response(openapi, request, response)

    def test_validate_response_passes_valid_body(self):
        """A response satisfying the schema validates."""
        openapi = load_openapi("h5", self._validation_spec())
        request = self._request()
        response = OpenAPIResponseShim(
            status_code=200,
            content_type="application/json",
            headers=Headers({"content-type": "application/json"}),
            data=b'{"id": 7}',
        )

        validate_openapi_response(openapi, request, response)

    def test_load_openapi_lru_caches_by_hash(self):
        """Specs are cached by content hash and evicted beyond 16 entries."""
        openapi_module._spec_cache.clear()

        def _spec_named(n):
            """Build a valid spec variant."""
            return {
                "openapi": "3.0.0",
                "info": {"title": f"t{n}", "version": "1"},
                "paths": {},
            }

        first = load_openapi("hash-0", _spec_named(0))
        assert load_openapi("hash-0", _spec_named(0)) is first
        for n in range(1, 17):
            load_openapi(f"hash-{n}", _spec_named(n))

        assert "hash-0" not in openapi_module._spec_cache
        assert len(openapi_module._spec_cache) == 16
