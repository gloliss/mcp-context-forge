# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_http_registry_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for ``HttpRegistryService`` (PR3): read-only registry views,
per-operation tool exposure state, visibility scoping, and the sync preview
classification (added/modified/disabled/re-approval).
"""

# Standard
from datetime import datetime, timezone
import itertools

# Third-Party
import pytest
from sqlalchemy import select

# First-Party
from mcpgateway.db import HttpSchemaArtifact
from mcpgateway.db import HttpService as DbHttpService
from mcpgateway.db import Tool as DbTool
from mcpgateway.protocols.contracts.models import OperationDefinition
from mcpgateway.protocols.http.models import HttpParameter, HttpRequestContract, HttpResponseContract
from mcpgateway.services.http_registry_service import HttpRegistryService, _operation_ref
from mcpgateway.services.http_schema_service import _operation_to_dict
from mcpgateway.utils.http_validation import HttpServiceError

_UNIQUE = itertools.count(1)


def _operation(key="GET /ping", method="GET", path_template="/ping", param_schema=None, title=None, description=None):
    """Build one IR operation with a single optional query parameter."""
    parameters = ()
    if param_schema is not None:
        parameters = (HttpParameter(name="q", location="query", required=False, schema=param_schema),)
    return OperationDefinition(
        key=key,
        protocol="http",
        title=title,
        description=description,
        request=HttpRequestContract(method=method, path_template=path_template, parameters=parameters, bodies=()),
        response=HttpResponseContract(),
    )


_CATALOG = {"operations": [_operation_to_dict(_operation(key="GET /greet", path_template="/greet")), _operation_to_dict(_operation(key="POST /subscribe", method="POST", path_template="/subscribe"))], "diagnostics": []}


def _service(**overrides):
    """Build a detached DbHttpService row."""
    n = next(_UNIQUE)
    defaults = {
        "name": f"reg-svc-{n}",
        "slug": f"reg-svc-{n}",
        "base_url": "http://127.0.0.1:8899",
        "visibility": "public",
        "enabled": True,
        "reachable": True,
        "health_status": "unknown",
        "operation_count": 2,
        "active_schema_hash": "hash-2",
        "schema_drift": False,
        "discovered_operations": {"catalog": _CATALOG},
    }
    defaults.update(overrides)
    return DbHttpService(**defaults)


def _artifact(service, *, version=1, active=True, catalog=None, content_hash=None):
    """Build a detached HttpSchemaArtifact row."""
    return HttpSchemaArtifact(
        http_service_id=service.id,
        version=version,
        source_type="openapi",
        artifact_format="json",
        content_hash=content_hash or f"hash-{version}",
        artifact_blob=b"{}",
        source_info={"filename": "openapi.json", "catalog": catalog if catalog is not None else dict(_CATALOG)},
        is_active=active,
        created_by="admin@example.com",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc) if active else None,
    )


def _tool(service, operation_key, *, enabled=True, deprecated=False, reachable=True, protocol_config=True, input_schema=None, description="HTTP op"):
    """Build a detached DbTool row bound to one operation."""
    config = None
    if protocol_config:
        config = {"version": 1, "operationRef": operation_key, "request": {"method": "GET", "pathTemplate": "/ping"}}
    return DbTool(
        original_name=f"{service.slug}__op",
        custom_name=f"{service.slug}__op",
        custom_name_slug=f"{service.slug}-op",
        display_name="Op",
        url=service.base_url,
        base_url=service.base_url,
        original_description=description,
        description=description,
        integration_type="REST",
        request_type="GET",
        input_schema=input_schema or {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        output_schema={"type": "object"},
        annotations={},
        protocol_config=config,
        created_by="system",
        visibility=service.visibility,
        team_id=service.team_id,
        owner_email=service.owner_email,
        http_service_id=service.id,
        enabled=enabled,
        deprecated=deprecated,
        reachable=reachable,
    )


def test_operation_ref_extracts_stable_key():
    """operationRef comes from protocol_config; missing config yields None."""
    service = _service()
    assert _operation_ref(_tool(service, "GET /ping")) == "GET /ping"
    assert _operation_ref(_tool(service, "GET /ping", protocol_config=False)) is None
    assert _operation_ref(_tool(service, "GET /ping", protocol_config={})) is None


def test_registry_view_builds_hierarchy(test_db):
    """Services, schema versions, operations, and exposure counts nest correctly."""
    service = _service()
    test_db.add(service)
    test_db.flush()
    test_db.add(_artifact(service, version=1, active=False))
    test_db.add(_artifact(service, version=2, active=True))
    test_db.add(_tool(service, "GET /greet"))
    test_db.add(_tool(service, "POST /subscribe", enabled=False))
    test_db.commit()

    view = HttpRegistryService.build_registry_view(test_db, service_ids=[service.id])

    assert view.total_services == 1
    assert view.total_schema_versions == 2
    assert view.total_operations == 2
    assert view.total_exposed_tools == 1
    service_view = view.services[0]
    assert service_view.name.startswith("reg-svc-")
    assert service_view.tool_count == 2
    assert service_view.exposed_tool_count == 1
    assert [v.version for v in service_view.schema_versions] == [1, 2]
    assert service_view.schema_versions[0].is_active is False
    assert service_view.schema_versions[1].is_active is True
    operations = {op.key: op for op in service_view.schema_versions[1].operations}
    assert operations["GET /greet"].exposed is True
    assert operations["GET /greet"].tool_enabled is True
    assert operations["POST /subscribe"].exposed is False
    assert operations["POST /subscribe"].tool_enabled is False


def test_registry_view_exposes_no_artifact_bytes(test_db):
    """Schema version payloads never leak the stored artifact blob."""
    service = _service()
    test_db.add(service)
    test_db.flush()
    test_db.add(_artifact(service, version=1, active=True))
    test_db.commit()

    view = HttpRegistryService.build_registry_view(test_db, service_ids=[service.id])
    payload = view.model_dump()

    schema_version = payload["services"][0]["schema_versions"][0]
    assert "artifact_blob" not in schema_version
    assert "source_info" not in schema_version
    assert schema_version["operations"][0]["key"] == "GET /greet"


def test_registry_view_respects_service_id_filter(test_db):
    """An explicit ID list scopes the view; an empty list yields an empty view."""
    service_a = _service(name="svc-a", slug="svc-a")
    service_b = _service(name="svc-b", slug="svc-b")
    test_db.add_all([service_a, service_b])
    test_db.commit()

    view = HttpRegistryService.build_registry_view(test_db, service_ids=[service_a.id])
    assert view.total_services == 1
    assert view.services[0].name == "svc-a"

    empty = HttpRegistryService.build_registry_view(test_db, service_ids=[])
    assert empty.services == []
    assert empty.total_services == 0


def test_registry_view_ignores_tools_without_operation_ref(test_db):
    """Legacy tools with no protocol_config never bind to operations."""
    service = _service()
    test_db.add(service)
    test_db.flush()
    test_db.add(_artifact(service, version=1, active=True))
    test_db.add(_tool(service, "GET /greet", protocol_config=False))
    test_db.commit()

    view = HttpRegistryService.build_registry_view(test_db, service_ids=[service.id])

    service_view = view.services[0]
    assert service_view.tool_count == 1
    assert service_view.exposed_tool_count == 0
    greet = service_view.schema_versions[0].operations[0]
    assert greet.tool_id is None
    assert greet.exposed is False


def test_service_detail_missing_returns_none(test_db):
    """A missing service has no detail view."""
    assert HttpRegistryService.build_service_detail(test_db, "missing-id") is None


def test_schema_detail_joins_tool_state(test_db):
    """Schema detail carries per-operation tool IDs and exposure state."""
    service = _service()
    test_db.add(service)
    test_db.flush()
    artifact = _artifact(service, version=1, active=True)
    test_db.add(artifact)
    test_db.flush()
    tool = _tool(service, "GET /greet")
    disabled = _tool(service, "POST /subscribe", enabled=False)
    test_db.add_all([tool, disabled])
    test_db.commit()

    detail = HttpRegistryService.build_schema_detail(test_db, artifact.id)

    assert detail is not None
    assert detail.artifact_id == artifact.id
    assert detail.version == 1
    operations = {op.key: op for op in detail.operations}
    assert operations["GET /greet"].tool_id == tool.id
    assert operations["GET /greet"].exposed is True
    assert operations["POST /subscribe"].tool_id == disabled.id
    assert operations["POST /subscribe"].exposed is False


def test_schema_detail_respects_service_boundary(test_db):
    """Schema detail refuses artifacts that do not belong to the service."""
    service = _service()
    other = _service()
    test_db.add_all([service, other])
    test_db.flush()
    artifact = _artifact(service, version=1)
    test_db.add(artifact)
    test_db.commit()

    assert HttpRegistryService.build_schema_detail(test_db, artifact.id, service_id=other.id) is None
    assert HttpRegistryService.build_schema_detail(test_db, "missing-id") is None


def test_scope_statement_hides_private_from_public_token(test_db):
    """A public-only token scope sees public services only."""
    service = _service(owner_email="alice@example.com", visibility="private")
    public_service = _service(owner_email="bob@example.com", visibility="public")
    test_db.add_all([service, public_service])
    test_db.commit()

    statement = HttpRegistryService.scope_statement(select(DbHttpService), DbHttpService, test_db, "alice@example.com", [])
    visible = set(test_db.execute(statement).scalars().all())

    assert public_service in visible
    assert service not in visible


def test_scope_statement_owner_sees_own_private(test_db):
    """A caller with no token scope sees public rows plus their own private rows."""
    service = _service(owner_email="alice@example.com", visibility="private")
    other_private = _service(owner_email="bob@example.com", visibility="private")
    test_db.add_all([service, other_private])
    test_db.commit()

    statement = HttpRegistryService.scope_statement(select(DbHttpService), DbHttpService, test_db, "alice@example.com", None)
    visible = set(test_db.execute(statement).scalars().all())

    assert service in visible
    assert other_private not in visible


def test_sync_preview_classifies_all_outcomes(test_db):
    """Preview splits sync into added/modified/disabled plus re-approval."""
    service = _service()
    test_db.add(service)
    test_db.flush()
    current = _artifact(
        service,
        version=1,
        active=True,
        catalog={"operations": [_operation_to_dict(_operation(key="GET /ping", param_schema={"type": "string"})), _operation_to_dict(_operation(key="GET /old", path_template="/old"))], "diagnostics": []},
        content_hash="hash-current",
    )
    test_db.add(current)
    test_db.flush()
    # Create the tool rows through the real sync path so their schemas
    # exactly match the compiled output of the current catalog.
    from mcpgateway.services.http_service import HttpService  # pylint: disable=import-outside-toplevel

    HttpService()._sync_tools_from_artifact(test_db, service, current)  # pylint: disable=protected-access
    service.discovered_operations = {"catalog": current.source_info["catalog"]}
    test_db.flush()

    candidate = _artifact(
        service,
        version=2,
        active=False,
        catalog={"operations": [_operation_to_dict(_operation(key="GET /ping", param_schema={"type": "integer"})), _operation_to_dict(_operation(key="POST /new", method="POST", path_template="/new"))], "diagnostics": []},
        content_hash="hash-candidate",
    )
    test_db.add(candidate)
    test_db.commit()

    preview = HttpRegistryService.build_sync_preview(test_db, service.id, candidate.id)

    assert preview.service_id == service.id
    assert preview.candidate_artifact_id == candidate.id
    assert preview.added_tools == ["POST /new"]
    assert preview.modified_tools == ["GET /ping"]
    assert preview.disabled_tools == ["GET /old"]
    assert preview.operations_needing_reapproval == ["GET /ping"]
    assert preview.warning is None


def test_sync_preview_reapproval_requires_request_change(test_db):
    """Metadata-only changes update tools without client re-approval."""
    service = _service()
    test_db.add(service)
    test_db.flush()
    current = _artifact(service, version=1, active=True, catalog={"operations": [_operation_to_dict(_operation(key="GET /ping", param_schema={"type": "string"}, title="Ping"))], "diagnostics": []}, content_hash="hash-current")
    test_db.add(current)
    test_db.flush()
    from mcpgateway.services.http_service import HttpService  # pylint: disable=import-outside-toplevel

    HttpService()._sync_tools_from_artifact(test_db, service, current)  # pylint: disable=protected-access
    service.discovered_operations = {"catalog": current.source_info["catalog"]}
    test_db.flush()

    candidate = _artifact(service, version=2, active=False, catalog={"operations": [_operation_to_dict(_operation(key="GET /ping", param_schema={"type": "string"}, title="Ping renamed"))], "diagnostics": []}, content_hash="hash-candidate")
    test_db.add(candidate)
    test_db.commit()

    preview = HttpRegistryService.build_sync_preview(test_db, service.id, candidate.id)

    assert preview.operations_needing_reapproval == []
    assert preview.modified_tools == ["GET /ping"]
    assert preview.disabled_tools == []


def test_sync_preview_empty_candidate_warns(test_db):
    """An empty candidate over published tools warns instead of disabling all."""
    service = _service()
    test_db.add(service)
    test_db.flush()
    test_db.add(_tool(service, "GET /ping"))
    test_db.commit()
    candidate = _artifact(service, version=2, active=False, catalog={"operations": [], "diagnostics": []}, content_hash="hash-empty")
    test_db.add(candidate)
    test_db.commit()

    preview = HttpRegistryService.build_sync_preview(test_db, service.id, candidate.id)

    assert preview.added_tools == []
    assert preview.modified_tools == []
    assert preview.disabled_tools == []
    assert "defines no operations" in (preview.warning or "")


def test_sync_preview_rejects_missing_service(test_db):
    """Previewing a missing service raises."""
    with pytest.raises(HttpServiceError, match="not found"):
        HttpRegistryService.build_sync_preview(test_db, "missing-id", "missing-artifact")


def test_sync_preview_rejects_foreign_artifact(test_db):
    """Previewing another service's artifact raises."""
    service = _service()
    other = _service()
    test_db.add_all([service, other])
    test_db.flush()
    candidate = _artifact(other, version=1)
    test_db.add(candidate)
    test_db.commit()

    with pytest.raises(HttpServiceError, match="not found for this service"):
        HttpRegistryService.build_sync_preview(test_db, service.id, candidate.id)
