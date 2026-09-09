# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_contract_artifact_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for PR3 contract artifact preparation and external ``$ref``
materialisation (design-document §14 and §66).
"""

# Standard
from contextlib import asynccontextmanager
import hashlib
import io
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch
import zipfile

# Third-Party
import httpx
import orjson
import pytest
import yaml

# First-Party
from mcpgateway.config import settings
from mcpgateway.services.contract_artifact_service import ContractArtifactService
from mcpgateway.services.safe_reference_fetcher import (
    BUNDLED_REFS_KEY,
    ContractArtifactResolver,
    SafeReferenceFetcher,
)
from mcpgateway.utils.http_validation import HttpServiceError

_MINIMAL_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "t", "version": "1"},
    "paths": {"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}},
}

_PATCH_FETCH_CLIENT = "mcpgateway.services.safe_reference_fetcher.get_isolated_http_client"
_PATCH_VALIDATE_URL = "mcpgateway.services.safe_reference_fetcher.SecurityValidator.validate_url"


def _mock_response(body: bytes, headers: Optional[dict] = None, status_code: int = 200):
    """Build a canned HTTPX-stream response object.

    Args:
        body: The canned response body.
        headers: The canned response headers.
        status_code: The canned status code.

    Returns:
        A response object usable inside ``async with``.
    """

    async def _aiter_bytes(chunk_size=8192):
        """Yield the canned body in chunks."""
        for i in range(0, len(body), chunk_size):
            yield body[i : i + chunk_size]

    response = MagicMock()
    response.headers = headers or {}
    response.status_code = status_code
    response.is_redirect = status_code in (301, 302, 303, 307, 308)
    response.raise_for_status = MagicMock()
    response.aiter_bytes = _aiter_bytes
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)
    return response


@asynccontextmanager
async def _mock_fetch(body: bytes, headers: Optional[dict] = None, status_code: int = 200):
    """Async context manager mimicking ``get_isolated_http_client`` with canned data.

    Args:
        body: The canned response body.
        headers: The canned response headers.
        status_code: The canned status code.
    """
    client = MagicMock()
    client.stream = MagicMock(return_value=_mock_response(body, headers, status_code))
    yield client


@asynccontextmanager
async def _mock_fetch_by_url(by_url: dict[str, bytes]):
    """Async context manager serving a different canned body per requested URL.

    Args:
        by_url: Request URL to response body mapping.
    """
    client = MagicMock()
    client.stream = MagicMock(side_effect=lambda method, url: _mock_response(by_url[url]))
    yield client


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    """Build an in-memory ZIP from a name-to-bytes mapping.

    Args:
        entries: ZIP entry names mapped to their content.

    Returns:
        The serialised ZIP bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buf.getvalue()


class TestContractArtifactService:
    """Artifact preparation: parsing, ZIP handling, caps, and addressing."""

    @pytest.mark.asyncio
    async def test_json_spec_prepares_canonical_bundle(self):
        """A plain JSON spec round-trips to identical canonical bytes."""
        raw = orjson.dumps(_MINIMAL_SPEC)
        prepared = await ContractArtifactService().prepare_artifact(raw, "openapi.json")

        assert prepared.artifact_format == "json"
        assert prepared.bundle == raw
        assert prepared.content_hash == hashlib.sha256(raw).hexdigest()
        assert prepared.document["openapi"] == "3.0.0"

    @pytest.mark.asyncio
    async def test_yaml_spec_converts_to_json_bundle(self):
        """YAML input is parsed and stored as the canonical JSON bundle."""
        raw = yaml.safe_dump(_MINIMAL_SPEC, sort_keys=True).encode()
        prepared = await ContractArtifactService().prepare_artifact(raw, "openapi.yaml")

        assert prepared.artifact_format == "yaml"
        assert orjson.loads(prepared.bundle)["openapi"] == "3.0.0"

    @pytest.mark.asyncio
    async def test_zip_spec_extracts_openapi_json(self):
        """A ZIP holding openapi.json (plus noise) extracts that entry."""
        prepared = await ContractArtifactService().prepare_artifact(
            _zip_bytes({"openapi.json": orjson.dumps(_MINIMAL_SPEC), "README.txt": b"noise"}),
            "bundle.zip",
        )

        assert prepared.artifact_format == "zip"
        assert orjson.loads(prepared.bundle)["openapi"] == "3.0.0"

    @pytest.mark.asyncio
    async def test_zip_path_traversal_rejected(self):
        """Traversal entries are rejected by the shared ZIP safety rules."""
        with pytest.raises(HttpServiceError, match="Unsafe HTTP artifact ZIP entry"):
            await ContractArtifactService().prepare_artifact(
                _zip_bytes({"../evil.yaml": b"openapi: 3.0.0"}), "evil.zip"
            )

    @pytest.mark.asyncio
    async def test_zip_without_spec_rejected(self):
        """A ZIP without any OpenAPI document is rejected."""
        with pytest.raises(HttpServiceError, match="no OpenAPI document"):
            await ContractArtifactService().prepare_artifact(_zip_bytes({"notes.txt": b"hi"}), "bundle.zip")

    @pytest.mark.asyncio
    async def test_malformed_zip_rejected(self):
        """Non-ZIP bytes with a .zip suffix are rejected."""
        with pytest.raises(HttpServiceError, match="not a valid ZIP"):
            await ContractArtifactService().prepare_artifact(b"not a zip", "x.zip")

    @pytest.mark.asyncio
    async def test_invalid_filenames_rejected(self):
        """Path separators and non-OpenAPI suffixes are rejected."""
        svc = ContractArtifactService()
        for filename in ("../openapi.json", "spec.txt", "spec.xml", "a\\openapi.json", ""):
            with pytest.raises(HttpServiceError):
                await svc.prepare_artifact(orjson.dumps(_MINIMAL_SPEC), filename)

    @pytest.mark.asyncio
    async def test_oversize_upload_rejected(self, monkeypatch):
        """Content above the upload cap is rejected before parsing."""
        monkeypatch.setattr(settings, "mcpgateway_http_max_upload_bytes", 64)
        with pytest.raises(HttpServiceError, match="upload too large"):
            await ContractArtifactService().prepare_artifact(orjson.dumps(_MINIMAL_SPEC), "openapi.json")

    @pytest.mark.asyncio
    async def test_non_object_document_rejected(self):
        """JSON arrays and YAML scalars are rejected (root must be an object)."""
        svc = ContractArtifactService()
        with pytest.raises(HttpServiceError, match="not a JSON object document"):
            await svc.prepare_artifact(b"[1, 2, 3]", "openapi.json")
        with pytest.raises(HttpServiceError, match="not a JSON object document"):
            await svc.prepare_artifact(b"just a string", "openapi.yaml")

    @pytest.mark.asyncio
    async def test_missing_openapi_field_rejected(self):
        """A JSON object without the openapi version field is rejected."""
        with pytest.raises(HttpServiceError, match="not an OpenAPI document"):
            await ContractArtifactService().prepare_artifact(orjson.dumps({"paths": {}}), "openapi.json")


class TestContractArtifactResolver:
    """External ``$ref`` materialisation into ``x-contextforge-bundled-refs``."""

    @pytest.mark.asyncio
    async def test_external_ref_rewritten_into_bundle(self):
        """An external ref (with fragment) becomes a local bundle pointer."""
        spec = orjson.loads(orjson.dumps(_MINIMAL_SPEC))
        spec["paths"]["/ping"]["get"]["parameters"] = [
            {"name": "q", "in": "query", "schema": {"$ref": "https://example.com/schemas.json#/Query"}}
        ]
        sub_doc = {"Query": {"type": "string"}}
        svc = ContractArtifactService()
        with patch(_PATCH_VALIDATE_URL, return_value="https://example.com/schemas.json"), patch(
            _PATCH_FETCH_CLIENT, return_value=_mock_fetch(orjson.dumps(sub_doc))
        ):
            prepared = await svc.prepare_artifact(orjson.dumps(spec), "openapi.json")

        bundle = prepared.document
        assert bundle["paths"]["/ping"]["get"]["parameters"][0]["schema"]["$ref"] == (
            f"#/{BUNDLED_REFS_KEY}/0/Query"
        )
        assert bundle[BUNDLED_REFS_KEY] == [sub_doc]

    @pytest.mark.asyncio
    async def test_nested_external_refs_materialised_once(self):
        """Nested external refs are fetched once each and cross-linked."""
        spec = orjson.loads(orjson.dumps(_MINIMAL_SPEC))
        spec["paths"]["/ping"]["get"]["responses"]["200"]["description"] = "x"
        spec["paths"]["/ping"]["get"]["parameters"] = [
            {"name": "a", "in": "query", "schema": {"$ref": "https://example.com/a.json#/A"}}
        ]
        doc_a = {"A": {"type": "object", "properties": {"b": {"$ref": "https://example.com/b.json#/B"}}}}
        doc_b = {"B": {"type": "number"}}
        fetches = {
            "https://example.com/a.json": orjson.dumps(doc_a),
            "https://example.com/b.json": orjson.dumps(doc_b),
        }
        svc = ContractArtifactService()
        with patch(_PATCH_VALIDATE_URL, return_value="https://example.com/a.json"), patch(
            _PATCH_FETCH_CLIENT, side_effect=lambda *a, **k: _mock_fetch_by_url(fetches)
        ):
            prepared = await svc.prepare_artifact(orjson.dumps(spec), "openapi.json")

        bundle = prepared.document
        assert bundle["paths"]["/ping"]["get"]["parameters"][0]["schema"]["$ref"] == f"#/{BUNDLED_REFS_KEY}/0/A"
        assert bundle[BUNDLED_REFS_KEY][0]["A"]["properties"]["b"]["$ref"] == f"#/{BUNDLED_REFS_KEY}/1/B"
        assert bundle[BUNDLED_REFS_KEY][1] == doc_b

    @pytest.mark.asyncio
    async def test_local_refs_inside_sub_documents_are_prefixed(self):
        """Local pointers inside a materialised doc resolve within it."""
        spec = orjson.loads(orjson.dumps(_MINIMAL_SPEC))
        spec["paths"]["/ping"]["get"]["parameters"] = [
            {"name": "a", "in": "query", "schema": {"$ref": "https://example.com/a.json#/A"}}
        ]
        doc_a = {"A": {"$ref": "#/definitions/Base"}, "definitions": {"Base": {"type": "object"}}}
        svc = ContractArtifactService()
        with patch(_PATCH_VALIDATE_URL, return_value="https://example.com/a.json"), patch(
            _PATCH_FETCH_CLIENT, return_value=_mock_fetch(orjson.dumps(doc_a))
        ):
            prepared = await svc.prepare_artifact(orjson.dumps(spec), "openapi.json")

        bundle = prepared.document
        assert bundle[BUNDLED_REFS_KEY][0]["A"]["$ref"] == f"#/{BUNDLED_REFS_KEY}/0/definitions/Base"

    @pytest.mark.asyncio
    async def test_allow_remote_false_rejects_external_refs(self):
        """allowRemote=false turns any external ref into an error."""
        spec = orjson.loads(orjson.dumps(_MINIMAL_SPEC))
        spec["paths"]["/ping"]["get"]["parameters"] = [
            {"name": "a", "in": "query", "schema": {"$ref": "https://example.com/a.json"}}
        ]
        with pytest.raises(HttpServiceError, match="not allowed by policy"):
            await ContractArtifactService().prepare_artifact(
                orjson.dumps(spec), "openapi.json", allow_remote=False
            )

    @pytest.mark.asyncio
    async def test_no_external_refs_leaves_document_untouched(self):
        """Without external refs the bundle has no bundled-refs key."""
        bundle = await ContractArtifactResolver().resolve_and_bundle(
            orjson.loads(orjson.dumps(_MINIMAL_SPEC))
        )

        assert BUNDLED_REFS_KEY not in bundle
        assert bundle["openapi"] == "3.0.0"

    @pytest.mark.asyncio
    async def test_redirect_response_refused(self):
        """A 3xx response is refused (redirects are not followed for specs)."""
        with patch(_PATCH_VALIDATE_URL, return_value="https://example.com/x.json"), patch(
            _PATCH_FETCH_CLIENT, return_value=_mock_fetch(b"", status_code=302)
        ):
            with pytest.raises(HttpServiceError, match="redirects are not followed"):
                spec = orjson.loads(orjson.dumps(_MINIMAL_SPEC))
                spec["paths"]["/ping"]["get"]["parameters"] = [
                    {"name": "a", "in": "query", "schema": {"$ref": "https://example.com/x.json"}}
                ]
                await ContractArtifactService().prepare_artifact(orjson.dumps(spec), "openapi.json")

    @pytest.mark.asyncio
    async def test_http_error_wrapped(self):
        """Transport errors are wrapped into HttpServiceError."""
        svc = ContractArtifactService()
        spec = orjson.loads(orjson.dumps(_MINIMAL_SPEC))
        spec["paths"]["/ping"]["get"]["parameters"] = [
            {"name": "a", "in": "query", "schema": {"$ref": "https://example.com/x.json"}}
        ]
        with patch(_PATCH_VALIDATE_URL, return_value="https://example.com/x.json"), patch(
            _PATCH_FETCH_CLIENT, side_effect=httpx.ConnectError("boom")
        ):
            with pytest.raises(HttpServiceError, match="Failed to fetch external \\$ref"):
                await svc.prepare_artifact(orjson.dumps(spec), "openapi.json")


class TestSafeReferenceFetcher:
    """Direct fetcher behaviour: size caps and status handling."""

    @pytest.mark.asyncio
    async def test_fetch_returns_raw_bytes(self):
        """A successful fetch returns the raw body."""
        with patch(_PATCH_VALIDATE_URL, return_value="https://example.com/x.json"), patch(
            _PATCH_FETCH_CLIENT, return_value=_mock_fetch(b"{\"a\": 1}")
        ):
            body = await SafeReferenceFetcher().fetch("https://example.com/x.json")

        assert body == b'{"a": 1}'

    @pytest.mark.asyncio
    async def test_oversize_content_length_rejected(self, monkeypatch):
        """A large Content-Length header is rejected before streaming."""
        monkeypatch.setattr(settings, "mcpgateway_http_spec_max_bytes", 128)
        with patch(_PATCH_VALIDATE_URL, return_value="https://example.com/x.json"), patch(
            _PATCH_FETCH_CLIENT, return_value=_mock_fetch(b"", headers={"content-length": "1024"})
        ):
            with pytest.raises(ValueError, match="too large"):
                await SafeReferenceFetcher().fetch("https://example.com/x.json")
