# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_http_schema_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for ``HttpSchemaService`` (PR3): IR serialization round-trips,
request fingerprints, artifact diffs, content-addressed import, and the
candidate/activate pointer lifecycle.
"""

# Standard
from copy import deepcopy
from datetime import datetime, timezone
import itertools
import json

# Third-Party
import pytest

# First-Party
from mcpgateway.db import HttpSchemaArtifact
from mcpgateway.db import HttpService as DbHttpService
from mcpgateway.protocols.contracts.models import OperationCatalog, OperationDefinition
from mcpgateway.protocols.http.models import HttpParameter, HttpRequestContract, HttpResponseContract
from mcpgateway.services.http_schema_service import (
    HttpSchemaService,
    _catalog_from_source_info,
    _catalog_to_source_info,
    _operation_from_dict,
    _operation_to_dict,
    operation_request_fingerprint,
)
from mcpgateway.utils.http_validation import HttpServiceError

_UNIQUE = itertools.count(1)

_OPENAPI_DOC = {
    "openapi": "3.0.3",
    "info": {"title": "Mini", "version": "1.0.0"},
    "paths": {
        "/ping": {
            "get": {
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}},
                    }
                }
            }
        }
    },
}


def _payload(document=None) -> bytes:
    """Serialize a test OpenAPI document to JSON bytes."""
    return json.dumps(document if document is not None else _OPENAPI_DOC).encode()


def _service(**overrides):
    """Build a detached DbHttpService row."""
    n = next(_UNIQUE)
    defaults = {
        "name": f"http-svc-{n}",
        "slug": f"http-svc-{n}",
        "base_url": "http://127.0.0.1:8899",
        "enabled": True,
        "reachable": True,
    }
    defaults.update(overrides)
    return DbHttpService(**defaults)


def _artifact(service, *, version=1, active=False, content_hash="hash-1", catalog=None):
    """Build a detached HttpSchemaArtifact row."""
    return HttpSchemaArtifact(
        id=f"artifact-{next(_UNIQUE)}",
        http_service_id=service.id,
        version=version,
        source_type="openapi",
        artifact_format="json",
        content_hash=content_hash,
        artifact_blob=b"{}",
        source_info={"filename": "openapi.json", "catalog": catalog or {"operations": [], "diagnostics": []}},
        is_active=active,
        created_by="admin@example.com",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc) if active else None,
    )


def _operation(key="GET /ping", method="GET", path_template="/ping", param_schema=None):
    """Build one IR operation with a single optional query parameter."""
    parameters = ()
    if param_schema is not None:
        parameters = (HttpParameter(name="q", location="query", required=False, schema=param_schema),)
    return OperationDefinition(
        key=key,
        protocol="http",
        request=HttpRequestContract(method=method, path_template=path_template, parameters=parameters, bodies=()),
        response=HttpResponseContract(),
    )


class TestSerialization:
    """IR and catalog serialization round-trips."""

    def test_ir_round_trip_preserves_contract(self):
        """Every typed request/response facet survives serialization."""
        operation = OperationDefinition(
            key="GET /v1/ping",
            protocol="http",
            source_operation_id="getPing",
            title="Ping",
            description="Pings",
            deprecated=True,
            tags=("health",),
            request=HttpRequestContract(
                method="GET",
                path_template="/v1/ping",
                parameters=(HttpParameter(name="q", location="query", required=True, schema={"type": "integer"}, style="form"),),
                bodies=(),
            ),
            response=HttpResponseContract(),
        )

        rebuilt = _operation_from_dict(_operation_to_dict(operation))

        assert rebuilt.key == "GET /v1/ping"
        assert rebuilt.source_operation_id == "getPing"
        assert rebuilt.title == "Ping"
        assert rebuilt.description == "Pings"
        assert rebuilt.deprecated is True
        assert rebuilt.tags == ("health",)
        assert rebuilt.request.method == "GET"
        assert rebuilt.request.path_template == "/v1/ping"
        assert rebuilt.request.parameters[0].name == "q"
        assert rebuilt.request.parameters[0].location == "query"
        assert rebuilt.request.parameters[0].required is True
        assert rebuilt.request.parameters[0].schema == {"type": "integer"}
        assert rebuilt.request.parameters[0].style == "form"

    def test_catalog_round_trip_via_wrapped_payload(self):
        """Wrapped catalog payloads rebuild equivalent catalogs."""
        catalog = OperationCatalog(source_type="openapi", source_hash="x", operations=(_operation(),))
        wrapped = {"catalog": _catalog_to_source_info(catalog)}

        rebuilt = _catalog_from_source_info(wrapped)

        assert len(rebuilt.operations) == 1
        assert rebuilt.operations[0].key == "GET /ping"

    def test_catalog_from_empty_payload(self):
        """None payloads yield an empty catalog."""
        assert _catalog_from_source_info(None).operations == ()


class TestFingerprintsAndDiff:
    """Request fingerprints and artifact diffs."""

    def test_fingerprint_ignores_response_and_metadata(self):
        """Fingerprints cover only the request contract."""
        base = _operation(param_schema={"type": "string"})
        assert operation_request_fingerprint(base) == operation_request_fingerprint(_operation(param_schema={"type": "string"}))

    def test_fingerprint_sensitive_to_schema_change(self):
        """A parameter schema change flips the fingerprint."""
        before = _operation(param_schema={"type": "string"})
        after = _operation(param_schema={"type": "integer"})
        assert operation_request_fingerprint(before) != operation_request_fingerprint(after)

    def test_diff_classifies_added_removed_changed(self):
        """Diff compares full operation fingerprints on both sides."""
        service = _service()
        left = _artifact(service, content_hash="hash-l", catalog={"operations": [_operation_to_dict(_operation(key="GET /keep")), _operation_to_dict(_operation(key="GET /gone"))], "diagnostics": []})
        right = _artifact(service, content_hash="hash-r", catalog={"operations": [_operation_to_dict(_operation(key="GET /keep")), _operation_to_dict(_operation(key="POST /fresh", method="POST", path_template="/fresh"))], "diagnostics": []})

        diff = HttpSchemaService.diff(left, right)

        assert diff.from_artifact_id == left.id
        assert diff.to_artifact_id == right.id
        assert diff.added_operations == ["POST /fresh"]
        assert diff.removed_operations == ["GET /gone"]
        assert diff.changed_operations == []

    def test_diff_flags_changed_operation(self):
        """An edited operation appears under changed, not added/removed."""
        service = _service()
        left = _artifact(service, content_hash="hash-l", catalog={"operations": [_operation_to_dict(_operation(param_schema={"type": "string"}))], "diagnostics": []})
        right = _artifact(service, content_hash="hash-r", catalog={"operations": [_operation_to_dict(_operation(param_schema={"type": "integer"}))], "diagnostics": []})

        diff = HttpSchemaService.diff(left, right)

        assert diff.added_operations == []
        assert diff.removed_operations == []
        assert diff.changed_operations == ["GET /ping"]


class TestImportArtifact:
    """Content-addressed import and pointer management."""

    async def test_import_deduplicates_same_content(self, test_db):
        """Importing identical bytes twice reuses the artifact row."""
        service = _service()
        test_db.add(service)
        test_db.commit()

        first = await HttpSchemaService.import_artifact(test_db, service, _payload(), "openapi.json", "admin@example.com", activate=False)
        second = await HttpSchemaService.import_artifact(test_db, service, _payload(), "openapi.json", "admin@example.com", activate=False)
        test_db.commit()

        assert first.id == second.id
        assert first.version == 1
        assert service.candidate_artifact_id == first.id
        assert service.discovered_schema_hash == first.content_hash
        assert service.schema_drift is False
        assert service.operation_count == 1
        assert service.discovered_operations["catalog"]["operations"][0]["key"] == "GET /ping"

    async def test_import_with_activate_promotes(self, test_db):
        """Activating import lands the active pointer and the catalog."""
        service = _service()
        test_db.add(service)
        test_db.commit()

        artifact = await HttpSchemaService.import_artifact(test_db, service, _payload(), "openapi.json", "admin@example.com", activate=True)
        test_db.commit()

        assert service.active_artifact_id == artifact.id
        assert service.active_schema_hash == artifact.content_hash
        assert service.candidate_artifact_id is None
        assert service.schema_drift is False
        assert artifact.is_active is True
        assert artifact.activated_at is not None
        assert service.operation_count == 1

    async def test_import_candidate_after_active_sets_drift(self, test_db):
        """A changed candidate import sets schema_drift without activating."""
        service = _service()
        test_db.add(service)
        test_db.commit()
        await HttpSchemaService.import_artifact(test_db, service, _payload(), "openapi.json", "admin@example.com", activate=True)
        test_db.commit()

        changed = deepcopy(_OPENAPI_DOC)
        changed["paths"]["/ping"]["get"]["description"] = "changed"
        candidate = await HttpSchemaService.import_artifact(test_db, service, _payload(changed), "openapi.json", "admin@example.com", activate=False)
        test_db.commit()

        assert service.candidate_artifact_id == candidate.id
        assert service.discovered_schema_hash == candidate.content_hash
        assert service.schema_drift is True
        assert service.active_schema_hash != candidate.content_hash

    async def test_import_rejects_non_openapi_document(self):
        """Documents without an openapi version field are rejected."""
        service = _service()
        with pytest.raises(HttpServiceError, match="not an OpenAPI document"):
            await HttpSchemaService.import_artifact(None, service, b'{"info": {}}', "bad.json", "admin@example.com", activate=False)

    async def test_import_rejects_invalid_spec(self, test_db):
        """Documents failing OpenAPI structural validation are rejected."""
        service = _service()
        test_db.add(service)
        test_db.commit()
        broken = deepcopy(_OPENAPI_DOC)
        broken["paths"]["/ping"]["get"]["responses"]["200"].pop("description")
        with pytest.raises(HttpServiceError):
            await HttpSchemaService.import_artifact(test_db, service, _payload(broken), "broken.json", "admin@example.com", activate=False)


class TestActivateArtifact:
    """Activation guards and pointer convergence."""

    def test_activate_rejects_foreign_artifact(self, test_db):
        """Artifacts of another service cannot be activated."""
        service = _service()
        other = _service()
        test_db.add_all([service, other])
        test_db.flush()
        artifact = _artifact(other)

        with pytest.raises(HttpServiceError, match="belongs to another HTTP service"):
            HttpSchemaService.activate_artifact(test_db, service, artifact)

    def test_activate_rejects_empty_over_active(self, test_db):
        """An empty schema cannot replace a non-empty active schema."""
        service = _service(active_artifact_id="some-active", operation_count=3)
        test_db.add(service)
        test_db.flush()
        artifact = _artifact(service, content_hash="hash-empty", catalog={"operations": [], "diagnostics": []})

        with pytest.raises(HttpServiceError, match="Refusing to activate empty schema"):
            HttpSchemaService.activate_artifact(test_db, service, artifact)

    def test_activate_promotes_and_deactivates_others(self, test_db):
        """Activation promotes one version, demotes others, and converges pointers."""
        service = _service()
        test_db.add(service)
        test_db.flush()
        old_active = _artifact(service, version=1, active=True, content_hash="hash-old", catalog={"operations": [], "diagnostics": []})
        new_active = _artifact(service, version=2, active=False, content_hash="hash-new", catalog={"operations": [], "diagnostics": []})
        test_db.add_all([old_active, new_active])
        service.candidate_artifact_id = new_active.id
        service.discovered_schema_hash = "hash-new"
        test_db.flush()

        HttpSchemaService.activate_artifact(test_db, service, new_active)

        assert new_active.is_active is True
        assert new_active.activated_at is not None
        assert old_active.is_active is False
        assert old_active.activated_at is None
        assert service.active_artifact_id == new_active.id
        assert service.active_schema_hash == "hash-new"
        assert service.candidate_artifact_id is None
        assert service.schema_drift is False
