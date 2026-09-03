# -*- coding: utf-8 -*-
"""Tests for canonical Tool definitions and portable bundles."""

# Standard
import hashlib
from io import BytesIO
import json
import uuid
import zipfile

# Third-Party
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorProto, FileDescriptorSet
import pytest
from sqlalchemy import select

# First-Party
from mcpgateway.db import GrpcSchemaArtifact, GrpcService, Tool
from mcpgateway.services.grpc_schema_service import GrpcSchemaService
from mcpgateway.services.permission_service import PermissionService
from mcpgateway.services.tool_portability_service import ToolBundleConflictError, ToolBundleValidationError, ToolPortabilityService


def _descriptor_set() -> bytes:
    file_proto = FileDescriptorProto(name="greeter.proto", package="example", syntax="proto3")
    request = file_proto.message_type.add(name="HelloRequest")
    request.field.add(name="name", number=1, type=FieldDescriptorProto.TYPE_STRING)
    response = file_proto.message_type.add(name="HelloResponse")
    response.field.add(name="message", number=1, type=FieldDescriptorProto.TYPE_STRING)
    service = file_proto.service.add(name="Greeter")
    service.method.add(name="SayHello", input_type=".example.HelloRequest", output_type=".example.HelloResponse")
    descriptor_set = FileDescriptorSet()
    descriptor_set.file.append(file_proto)
    return descriptor_set.SerializeToString()


def _rehash_definition(definition: dict) -> None:
    """Update a Tool definition hash after a test mutates its spec."""
    payload = json.dumps({"kind": "Tool", "spec": definition["spec"]}, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    definition["metadata"]["contentHash"] = f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _grpc_tool(test_db):
    normalized, catalog = GrpcSchemaService.normalize_descriptor_set(_descriptor_set())
    suffix = uuid.uuid4().hex[:8]
    service_name = f"portable-greeter-{suffix}"
    service = GrpcService(
        name=service_name,
        slug=service_name,
        target="greeter.example.com:443",
        discovery_mode="artifact",
        reflection_enabled=False,
        active_schema_hash="pending",
        discovered_services=catalog,
        owner_email="owner@example.com",
        visibility="private",
    )
    test_db.add(service)
    test_db.flush()
    artifact = GrpcSchemaArtifact(
        grpc_service_id=service.id,
        version=1,
        source_type="protoset",
        content_hash=hashlib.sha256(normalized).hexdigest(),
        descriptor_set=normalized,
        source_info={"filename": "greeter.protoset", "catalog": catalog},
        is_active=True,
    )
    test_db.add(artifact)
    test_db.flush()
    service.active_artifact_id = artifact.id
    service.active_schema_hash = artifact.content_hash
    tool = Tool(
        original_name="example.Greeter.SayHello",
        custom_name="example.Greeter.SayHello",
        custom_name_slug=f"example-greeter-sayhello-{suffix}",
        display_name="Say Hello",
        name=f"example-greeter-sayhello-{suffix}",
        url=service.target,
        description="Friendly greeting",
        original_description="gRPC method example.Greeter.SayHello",
        integration_type="gRPC",
        request_type="POST",
        input_schema=catalog["example.Greeter"]["methods"][0]["input_schema"],
        output_schema=catalog["example.Greeter"]["methods"][0]["output_schema"],
        annotations={"readOnlyHint": True},
        tags=["demo"],
        grpc_service_id=service.id,
        grpc_schema_artifact_id=artifact.id,
        owner_email="owner@example.com",
        visibility="private",
        version=3,
    )
    test_db.add(tool)
    test_db.commit()
    return tool, service, artifact


def _rest_tool(test_db):
    suffix = uuid.uuid4().hex[:8]
    tool = Tool(
        original_name=f"portable.rest.{suffix}",
        custom_name=f"portable_rest_{suffix}",
        custom_name_slug=f"portable-rest-{suffix}",
        display_name="Portable REST",
        name=f"portable-rest-{suffix}",
        url="https://api.example.com/v1/items/{item_id}",
        description="Fetch one item",
        original_description="Fetch one item",
        integration_type="REST",
        request_type="GET",
        headers={"Accept": "application/json", "Authorization": "encrypted-secret", "Auth-Token": "legacy-plaintext-secret"},
        input_schema={"type": "object", "properties": {"item_id": {"type": "string"}}},
        output_schema={"type": "object"},
        annotations={"readOnlyHint": True},
        tags=["portable"],
        deprecated=True,
        base_url="https://api.example.com",
        path_template="/v1/items/{item_id}",
        query_mapping={"limit": "limit"},
        header_mapping={"tenant": "X-Tenant"},
        timeout_ms=3000,
        expose_passthrough=False,
        allowlist=["api.example.com"],
        owner_email="owner@example.com",
        visibility="private",
        version=2,
    )
    test_db.add(tool)
    test_db.commit()
    return tool


def test_build_definition_and_source_expose_exact_grpc_provenance_without_secrets(test_db):
    tool, _service, artifact = _grpc_tool(test_db)
    service = ToolPortabilityService()

    definition = service.build_definition(test_db, tool)
    source = service.build_source(test_db, tool)

    assert definition["metadata"]["toolRevision"] == 3
    assert definition["metadata"]["sourceManaged"] is True
    assert definition["spec"]["integration"]["schemaArtifact"]["sha256"] == artifact.content_hash
    assert definition["spec"]["authentication"]["credentialsIncluded"] is False
    assert source["schemaArtifact"]["id"] == artifact.id
    assert source["method"]["name"] == "SayHello"
    assert source["exactSourceAvailable"] is False


@pytest.mark.asyncio
async def test_tool_bundle_zip_round_trip_preserves_descriptor_and_checksums(test_db):
    tool, _service, artifact = _grpc_tool(test_db)
    service = ToolPortabilityService()

    exported = service.export_bundle(test_db, [tool], exported_by="owner@example.com")
    payload = service.bundle_to_zip(exported)
    restored = service.bundle_from_zip(payload)

    assert restored["bundleHash"] == exported["bundleHash"]
    dependency = restored["dependencies"]["grpcSchemas"][0]
    assert dependency["artifact"]["sha256"] == artifact.content_hash
    preview = await service.preview_import(test_db, restored, user_email="owner@example.com", token_teams=None, grpc_enabled=True)
    assert preview["ready"] is True
    assert preview["items"][0]["action"] == "activate_and_sync"
    assert preview["items"][0]["generatedTools"] == ["example.Greeter.SayHello"]


@pytest.mark.asyncio
async def test_grpc_preview_is_blocked_when_feature_is_disabled(test_db):
    tool, _service, _artifact = _grpc_tool(test_db)
    portability = ToolPortabilityService()
    bundle = portability.export_bundle(test_db, [tool], exported_by="owner@example.com")

    preview = await portability.preview_import(test_db, bundle, user_email="owner@example.com", token_teams=None, grpc_enabled=False)

    assert preview["ready"] is False
    assert preview["items"][0]["reason"] == "gRPC support is disabled"

    artifact_count = test_db.query(GrpcSchemaArtifact).count()
    with pytest.raises(ToolBundleConflictError, match="gRPC support is disabled"):
        await portability.import_bundle(
            test_db,
            bundle,
            imported_by="owner@example.com",
            user_email="owner@example.com",
            token_teams=None,
            grpc_enabled=False,
        )
    assert test_db.query(GrpcSchemaArtifact).count() == artifact_count


@pytest.mark.asyncio
async def test_grpc_preview_rejects_visible_service_not_manageable_by_caller(test_db):
    tool, grpc_service, _artifact = _grpc_tool(test_db)
    grpc_service.owner_email = "different-owner@example.com"
    grpc_service.visibility = "public"
    test_db.commit()
    portability = ToolPortabilityService()
    bundle = portability.export_bundle(test_db, [tool], exported_by="owner@example.com")

    preview = await portability.preview_import(test_db, bundle, user_email="owner@example.com", token_teams=None, grpc_enabled=True)

    assert preview["ready"] is False
    assert "not owned or manageable" in preview["items"][0]["reason"]


@pytest.mark.asyncio
async def test_grpc_import_allows_exact_resource_permission_without_child_ownership(test_db, monkeypatch):
    tool, grpc_service, _artifact = _grpc_tool(test_db)
    grpc_service.owner_email = "different-owner@example.com"
    grpc_service.visibility = "public"
    tool.owner_email = "different-owner@example.com"
    tool.visibility = "public"
    test_db.commit()
    portability = ToolPortabilityService()
    bundle = portability.export_bundle(test_db, [tool], exported_by="manager@example.com")

    async def deny_ownership(_self, _user_email, _resource, allow_team_admin=True):
        del allow_team_admin
        return False

    async def grant_exact_permission(_self, **kwargs):
        assert kwargs["permission"] == "admin.grpc"
        assert kwargs["resource_id"] == grpc_service.id
        return True

    monkeypatch.setattr(PermissionService, "check_resource_ownership", deny_ownership)
    monkeypatch.setattr(PermissionService, "check_permission", grant_exact_permission)

    result = await portability.import_bundle(
        test_db,
        bundle,
        imported_by="manager@example.com",
        user_email="manager@example.com",
        token_teams=None,
        grpc_enabled=True,
    )

    assert result["status"] == "completed"
    assert result["syncedGrpcTools"] == 1


@pytest.mark.asyncio
async def test_import_bundle_reactivates_schema_and_syncs_expected_tool(test_db):
    tool, _service, artifact = _grpc_tool(test_db)
    service = ToolPortabilityService()
    bundle = service.export_bundle(test_db, [tool], exported_by="owner@example.com")

    result = await service.import_bundle(
        test_db,
        bundle,
        imported_by="owner@example.com",
        user_email="owner@example.com",
        token_teams=None,
        grpc_enabled=True,
    )

    assert result["status"] == "completed"
    assert result["createdServices"] == 0
    assert result["syncedGrpcTools"] == 1
    refreshed = test_db.get(Tool, tool.id)
    assert refreshed.grpc_schema_artifact_id == artifact.id


@pytest.mark.asyncio
@pytest.mark.parametrize("source_type,source_filename", [("proto", "greeter.proto"), ("zip", "greeter.zip")])
async def test_grpc_import_uses_descriptor_filename_for_source_artifact(test_db, source_type, source_filename):
    tool, _grpc_service, artifact = _grpc_tool(test_db)
    artifact.source_type = source_type
    artifact.source_info = {**artifact.source_info, "filename": source_filename}
    test_db.commit()
    portability = ToolPortabilityService()
    bundle = portability.export_bundle(test_db, [tool], exported_by="owner@example.com")

    result = await portability.import_bundle(
        test_db,
        bundle,
        imported_by="owner@example.com",
        user_email="owner@example.com",
        token_teams=None,
        grpc_enabled=True,
    )

    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_grpc_conflict_strategies_apply_before_schema_sync(test_db):
    tool, _grpc_service, artifact = _grpc_tool(test_db)
    portability = ToolPortabilityService()
    bundle = portability.export_bundle(test_db, [tool], exported_by="owner@example.com")

    skipped = await portability.import_bundle(
        test_db,
        bundle,
        imported_by="owner@example.com",
        user_email="owner@example.com",
        token_teams=None,
        conflict_strategy="skip",
        grpc_enabled=True,
    )

    assert skipped["skippedTools"] == 1
    assert skipped["syncedGrpcTools"] == 0
    assert test_db.get(GrpcSchemaArtifact, artifact.id).version == 1

    with pytest.raises(ToolBundleConflictError, match="Import conflicts"):
        await portability.import_bundle(
            test_db,
            bundle,
            imported_by="owner@example.com",
            user_email="owner@example.com",
            token_teams=None,
            conflict_strategy="fail",
            grpc_enabled=True,
        )


def test_export_two_services_with_same_descriptor_has_one_zip_artifact(test_db):
    first_tool, _first_service, _first_artifact = _grpc_tool(test_db)
    second_tool, _second_service, _second_artifact = _grpc_tool(test_db)
    portability = ToolPortabilityService()

    payload = portability.bundle_to_zip(portability.export_bundle(test_db, [first_tool, second_tool], exported_by="owner@example.com"))
    restored = portability.bundle_from_zip(payload)

    assert len(restored["tools"]) == 2
    assert len(restored["dependencies"]["grpcSchemas"]) == 2
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        artifact_names = [name for name in archive.namelist() if name.startswith("artifacts/grpc/")]
    assert len(artifact_names) == 1


def test_export_allows_same_original_name_from_distinct_sources(test_db):
    first_tool = _rest_tool(test_db)
    second_tool = _rest_tool(test_db)
    for tool, source_name in ((first_tool, "gateway-a"), (second_tool, "gateway-b")):
        tool.original_name = "shared.remote.method"
        tool.integration_type = "MCP"
        tool.request_type = "SSE"
        tool.federation_source = source_name
        tool.headers = {}
    test_db.commit()
    portability = ToolPortabilityService()

    payload = portability.bundle_to_zip(portability.export_bundle(test_db, [first_tool, second_tool], exported_by="owner@example.com"))
    restored = portability.bundle_from_zip(payload)

    assert [definition["metadata"]["name"] for definition in restored["tools"]] == ["shared.remote.method", "shared.remote.method"]
    assert [definition["metadata"]["sourceId"] for definition in restored["tools"]] == [first_tool.id, second_tool.id]


@pytest.mark.asyncio
async def test_grpc_preview_rejects_unsafe_extension_metadata(test_db):
    tool, _grpc_service, _artifact = _grpc_tool(test_db)
    tool.extension_metadata = {"io.modelcontextprotocol/ui": {"resourceUri": "javascript:alert(1)"}}
    test_db.commit()
    portability = ToolPortabilityService()
    bundle = portability.export_bundle(test_db, [tool], exported_by="owner@example.com")

    preview = await portability.preview_import(test_db, bundle, user_email="owner@example.com", token_teams=None, grpc_enabled=True)

    assert preview["ready"] is False
    assert "ui://" in preview["items"][0]["reason"]

    with pytest.raises(ToolBundleConflictError, match="ui://"):
        await portability.import_bundle(
            test_db,
            bundle,
            imported_by="owner@example.com",
            user_email="owner@example.com",
            token_teams=None,
            grpc_enabled=True,
        )


@pytest.mark.asyncio
async def test_import_bundle_recreates_missing_parent_before_tool_sync(test_db, monkeypatch):
    tool, source_service, _artifact = _grpc_tool(test_db)
    service = ToolPortabilityService()
    bundle = service.export_bundle(test_db, [tool], exported_by="owner@example.com")
    service_name = source_service.name
    test_db.delete(source_service)
    test_db.commit()
    monkeypatch.setattr("mcpgateway.services.tool_portability_service._validate_grpc_target", lambda _target: None)

    result = await service.import_bundle(
        test_db,
        bundle,
        imported_by="owner@example.com",
        user_email="owner@example.com",
        token_teams=None,
        grpc_enabled=True,
    )

    assert result["createdServices"] == 1
    recreated = test_db.execute(select(GrpcService).where(GrpcService.name == service_name)).scalar_one()
    generated = test_db.execute(select(Tool).where(Tool.grpc_service_id == recreated.id)).scalars().all()
    assert [item.original_name for item in generated] == ["example.Greeter.SayHello"]
    assert generated[0].owner_email == "owner@example.com"
    assert generated[0].visibility == "private"


def test_bundle_from_zip_rejects_path_traversal():
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../manifest.json", "{}")

    with pytest.raises(ToolBundleValidationError, match="Unsafe Tool bundle entry"):
        ToolPortabilityService().bundle_from_zip(payload.getvalue())


def test_bundle_from_zip_rejects_undeclared_files(test_db):
    tool, _service, _artifact = _grpc_tool(test_db)
    service = ToolPortabilityService()
    original = service.bundle_to_zip(service.export_bundle(test_db, [tool], exported_by="owner@example.com"))
    payload = BytesIO()
    with zipfile.ZipFile(BytesIO(original)) as source, zipfile.ZipFile(payload, "w") as target:
        for member in source.infolist():
            target.writestr(member.filename, source.read(member.filename))
        target.writestr("unexpected.txt", "not declared by the manifest")

    with pytest.raises(ToolBundleValidationError, match="undeclared files"):
        service.bundle_from_zip(payload.getvalue())


def test_validate_bundle_rejects_invalid_dependency_shape(test_db):
    tool, _service, _artifact = _grpc_tool(test_db)
    service = ToolPortabilityService()
    bundle = service.export_bundle(test_db, [tool], exported_by="owner@example.com")
    bundle["dependencies"] = []

    with pytest.raises(ToolBundleValidationError, match="dependencies must be an object"):
        service.validate_bundle(bundle)


def test_validate_bundle_rejects_non_x_sensitive_header(test_db):
    tool = _rest_tool(test_db)
    service = ToolPortabilityService()
    bundle = service.export_bundle(test_db, [tool], exported_by="owner@example.com")
    definition = bundle["tools"][0]
    definition["spec"]["integration"]["headers"]["Auth-Token"] = "must-not-be-portable"
    _rehash_definition(definition)
    bundle.pop("bundleHash")

    with pytest.raises(ToolBundleValidationError, match="contains sensitive headers"):
        service.validate_bundle(bundle)


@pytest.mark.asyncio
async def test_fail_strategy_preflights_all_rest_conflicts_before_writes(test_db):
    missing_target = _rest_tool(test_db)
    existing_target = _rest_tool(test_db)
    portability = ToolPortabilityService()
    bundle = portability.export_bundle(test_db, [missing_target, existing_target], exported_by="owner@example.com")
    missing_id = missing_target.id
    missing_name = missing_target.original_name
    test_db.delete(missing_target)
    test_db.commit()

    with pytest.raises(ToolBundleConflictError, match="Import conflicts"):
        await portability.import_bundle(
            test_db,
            bundle,
            imported_by="owner@example.com",
            user_email="owner@example.com",
            token_teams=None,
            conflict_strategy="fail",
        )

    assert test_db.get(Tool, missing_id) is None
    assert test_db.execute(select(Tool).where(Tool.original_name == missing_name)).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_rest_bundle_update_restores_portable_fields_without_duplicates(test_db):
    tool = _rest_tool(test_db)
    service = ToolPortabilityService()
    bundle = service.export_bundle(test_db, [tool], exported_by="owner@example.com")
    assert bundle["tools"][0]["spec"]["integration"]["headers"] == {"Accept": "application/json"}
    assert bundle["tools"][0]["spec"]["authentication"]["credentialsRequired"] is True

    tool.custom_name = "locally_changed"
    tool.description = "locally changed"
    tool.path_template = "/changed"
    tool.expose_passthrough = True
    tool.deprecated = False
    test_db.commit()

    result = await service.import_bundle(
        test_db,
        bundle,
        imported_by="owner@example.com",
        user_email="owner@example.com",
        token_teams=None,
        conflict_strategy="update",
    )

    assert result["createdTools"] == 0
    assert result["updatedTools"] == 1
    restored = test_db.get(Tool, tool.id)
    assert restored.custom_name.startswith("portable_rest_")
    assert restored.description == "Fetch one item"
    assert restored.path_template == "/v1/items/{item_id}"
    assert restored.expose_passthrough is False
    assert restored.deprecated is True
    same_name = test_db.execute(select(Tool).where(Tool.original_name == tool.original_name)).scalars().all()
    assert [item.id for item in same_name] == [tool.id]


def test_validate_bundle_rejects_tampered_tool_definition(test_db):
    tool, _service, _artifact = _grpc_tool(test_db)
    service = ToolPortabilityService()
    bundle = service.export_bundle(test_db, [tool], exported_by="owner@example.com")
    bundle["tools"][0]["spec"]["description"] = "tampered"

    with pytest.raises(ToolBundleValidationError, match="Tool definition checksum mismatch"):
        service.validate_bundle(bundle)
