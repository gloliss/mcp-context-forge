# -*- coding: utf-8 -*-
"""Canonical Tool definitions and dependency-aware import/export bundles."""

# Standard
import base64
import binascii
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import PurePosixPath
import stat
from typing import Any, Iterable, Optional
import zipfile

# Third-Party
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import GrpcSchemaArtifact
from mcpgateway.db import GrpcService as DbGrpcService
from mcpgateway.db import Tool as DbTool
from mcpgateway.schemas import GrpcServiceCreate, ToolCreate, ToolUpdate
from mcpgateway.services.audit_trail_service import get_audit_trail_service
from mcpgateway.services.grpc_registry_service import GrpcRegistryService
from mcpgateway.services.grpc_schema_service import GrpcSchemaService
from mcpgateway.services.mcp_apps import optional_extension_metadata, validate_extension_metadata
from mcpgateway.services.permission_service import PermissionService
from mcpgateway.services.tool_service import ToolNameConflictError, ToolNotFoundError, ToolVersionConflictError, tool_service
from mcpgateway.utils.grpc_validation import _validate_grpc_target, GrpcServiceError
from mcpgateway.utils.header_filtering import filter_sensitive_headers

TOOL_DEFINITION_FORMAT_VERSION = "1.0"
TOOL_DEFINITION_KIND = "Tool"
TOOL_BUNDLE_KIND = "ToolBundle"
TOOL_BUNDLE_MANIFEST = "manifest.json"
_BUNDLE_ARTIFACT_PREFIX = "artifacts/grpc/"


class ToolPortabilityError(Exception):
    """Base error for Tool definition and bundle operations."""


class ToolBundleValidationError(ToolPortabilityError):
    """Raised when a Tool definition or bundle is malformed or unsafe."""


class ToolBundleConflictError(ToolPortabilityError):
    """Raised when a Tool bundle cannot be safely applied to the target."""


def _canonical_bytes(value: Any) -> bytes:
    """Return stable UTF-8 JSON bytes for hashing and archive manifests."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(payload: bytes) -> str:
    """Return a lowercase SHA-256 hex digest."""
    return hashlib.sha256(payload).hexdigest()


def _artifact_path(content_hash: str) -> str:
    """Return the deterministic safe bundle path for a descriptor artifact."""
    return f"{_BUNDLE_ARTIFACT_PREFIX}{content_hash}.protoset"


class ToolPortabilityService:
    """Build canonical Tool documents and dependency-aware portable bundles."""

    @staticmethod
    def _portable_headers(headers: Optional[dict[str, Any]]) -> dict[str, str]:
        """Return only plain, non-credential headers suitable for a Tool file.

        Sensitive values are encrypted at rest and legacy databases may still
        contain plaintext credentials. Both must be omitted; encrypted envelope
        objects are never serialized as if they were portable header values.
        """
        filtered = filter_sensitive_headers(headers or {})
        return {key: value for key, value in filtered.items() if isinstance(key, str) and isinstance(value, str)}

    @staticmethod
    def _grpc_artifact(db: Session, tool: DbTool) -> Optional[GrpcSchemaArtifact]:
        """Resolve the immutable artifact that produced a gRPC Tool.

        New rows carry ``grpc_schema_artifact_id``. Older rows fall back to the
        parent service's active artifact so they remain viewable before/backfill.
        """
        artifact_id = getattr(tool, "grpc_schema_artifact_id", None)
        service = db.get(DbGrpcService, tool.grpc_service_id) if tool.grpc_service_id else None
        artifact = db.get(GrpcSchemaArtifact, artifact_id) if artifact_id else None
        if artifact is not None and artifact.grpc_service_id != tool.grpc_service_id:
            return None
        if artifact is not None:
            return artifact
        if service is None:
            return None

        # Migration-era rows have no direct provenance. Prefer the active
        # artifact when it still contains the method, otherwise walk backward
        # to the newest immutable artifact that does (important for stale tools).
        candidates = list(
            db.execute(
                select(GrpcSchemaArtifact)
                .where(GrpcSchemaArtifact.grpc_service_id == service.id)
                .order_by(GrpcSchemaArtifact.version.desc())
            ).scalars()
        )
        if service.active_artifact_id:
            candidates.sort(key=lambda item: item.id != service.active_artifact_id)
        for candidate in candidates:
            catalog = (candidate.source_info or {}).get("catalog", {})
            if ToolPortabilityService._catalog_method(catalog, tool.original_name) is not None:
                return candidate
        return None

    @staticmethod
    def _catalog_method(catalog: dict[str, Any], full_method_name: str) -> Optional[dict[str, Any]]:
        """Find one fully-qualified method in a descriptor catalog."""
        service_name, separator, method_name = full_method_name.rpartition(".")
        if not separator:
            return None
        service = catalog.get(service_name)
        if not isinstance(service, dict):
            return None
        for method in service.get("methods", []):
            if isinstance(method, dict) and method.get("name") == method_name:
                return deepcopy(method)
        return None

    def build_definition(self, db: Session, tool: DbTool) -> dict[str, Any]:
        """Build a deterministic, secret-free representation of a Tool row."""
        integration_type = tool.integration_type or "REST"
        binding: dict[str, Any] = {"type": integration_type}
        credentials_required = bool(tool.auth_type)

        if integration_type == "gRPC":
            service = db.get(DbGrpcService, tool.grpc_service_id) if tool.grpc_service_id else None
            artifact = self._grpc_artifact(db, tool)
            binding.update(
                {
                    "method": tool.original_name,
                    "service": {
                        "name": service.name if service else None,
                        "target": service.target if service else tool.url,
                    },
                    "schemaArtifact": {
                        "id": artifact.id if artifact else None,
                        "version": artifact.version if artifact else None,
                        "sha256": artifact.content_hash if artifact else None,
                        "sourceType": artifact.source_type if artifact else None,
                    },
                }
            )
        elif integration_type == "REST":
            portable_headers = self._portable_headers(tool.headers)
            credentials_required = credentials_required or len(portable_headers) != len(tool.headers or {})
            binding.update(
                {
                    "url": tool.url,
                    "requestType": tool.request_type,
                    "headers": portable_headers,
                    "baseUrl": tool.base_url,
                    "pathTemplate": tool.path_template,
                    "queryMapping": tool.query_mapping,
                    "headerMapping": tool.header_mapping,
                    "timeoutMs": tool.timeout_ms,
                    "exposePassthrough": tool.expose_passthrough,
                    "allowlist": tool.allowlist,
                    "pluginChainPre": tool.plugin_chain_pre,
                    "pluginChainPost": tool.plugin_chain_post,
                }
            )
        else:
            binding.update(
                {
                    "url": tool.url,
                    "requestType": tool.request_type,
                    "gatewayId": tool.gateway_id,
                    "sqlTableId": tool.sql_table_id,
                    "sourceOperation": tool.source_operation,
                    "federationSource": tool.federation_source,
                }
            )

        spec: dict[str, Any] = {
            "description": tool.description,
            "title": tool.title,
            "inputSchema": tool.input_schema or {"type": "object", "properties": {}},
            "outputSchema": tool.output_schema,
            "annotations": tool.annotations or {},
            "extensionMetadata": tool.extension_metadata,
            "jsonpathFilter": tool.jsonpath_filter or "",
            "tags": tool.tags or [],
            "deprecated": bool(tool.deprecated),
            "integration": binding,
            "authentication": {
                "type": tool.auth_type,
                "credentialsIncluded": False,
                "credentialsRequired": credentials_required,
            },
        }
        content_hash = _sha256(_canonical_bytes({"kind": TOOL_DEFINITION_KIND, "spec": spec}))
        return {
            "formatVersion": TOOL_DEFINITION_FORMAT_VERSION,
            "kind": TOOL_DEFINITION_KIND,
            "metadata": {
                "sourceId": tool.id,
                "name": tool.original_name,
                "customName": tool.custom_name,
                "displayName": tool.display_name,
                "toolRevision": tool.version or 1,
                "contentHash": f"sha256:{content_hash}",
                "sourceManaged": bool(tool.gateway_id or tool.grpc_service_id or tool.sql_table_id or integration_type != "REST"),
            },
            "spec": spec,
        }

    def build_source(self, db: Session, tool: DbTool) -> dict[str, Any]:
        """Build lazy source/provenance metadata for the Tool details view."""
        definition = self.build_definition(db, tool)
        source: dict[str, Any] = {
            "toolId": tool.id,
            "toolRevision": tool.version or 1,
            "provider": tool.integration_type,
            "sourceManaged": definition["metadata"]["sourceManaged"],
            "exactSourceAvailable": False,
        }
        if tool.integration_type != "gRPC":
            source["binding"] = definition["spec"]["integration"]
            return source

        service = db.get(DbGrpcService, tool.grpc_service_id) if tool.grpc_service_id else None
        artifact = self._grpc_artifact(db, tool)
        catalog = (artifact.source_info or {}).get("catalog", {}) if artifact else {}
        source_info = artifact.source_info or {} if artifact else {}
        source.update(
            {
                "grpcService": {
                    "id": service.id if service else None,
                    "name": service.name if service else None,
                    "target": service.target if service else tool.url,
                },
                "schemaArtifact": {
                    "id": artifact.id if artifact else None,
                    "version": artifact.version if artifact else None,
                    "sha256": artifact.content_hash if artifact else None,
                    "sourceType": artifact.source_type if artifact else None,
                    "isActive": bool(artifact and service and service.active_artifact_id == artifact.id),
                    "originalFilename": source_info.get("filename"),
                },
                "method": self._catalog_method(catalog, tool.original_name),
                "files": [
                    {
                        "name": source_info.get("filename") or f"{tool.original_name}.descriptor.json",
                        "contentKind": "descriptor-derived",
                        "exactSourceAvailable": False,
                    }
                ],
                "notice": "The original Proto source is not retained; this view is derived from the immutable descriptor set.",
            }
        )
        return source

    def export_bundle(self, db: Session, tools: Iterable[DbTool], exported_by: str) -> dict[str, Any]:
        """Export Tool definitions and required gRPC descriptors as one bundle."""
        definitions: list[dict[str, Any]] = []
        grpc_dependencies: dict[tuple[str, str], dict[str, Any]] = {}
        for tool in tools:
            definition = self.build_definition(db, tool)
            if tool.integration_type == "gRPC":
                service = db.get(DbGrpcService, tool.grpc_service_id) if tool.grpc_service_id else None
                artifact = self._grpc_artifact(db, tool)
                if service is None or artifact is None:
                    raise ToolBundleValidationError(f"gRPC Tool '{tool.original_name}' has no resolvable service/schema artifact")
                dependency_key = f"grpc:{service.name}:{artifact.content_hash}"
                definition["spec"]["integration"]["dependencyKey"] = dependency_key
                definition["metadata"]["contentHash"] = f"sha256:{_sha256(_canonical_bytes({'kind': TOOL_DEFINITION_KIND, 'spec': definition['spec']}))}"
                grpc_dependencies[(service.id, artifact.id)] = {
                    "key": dependency_key,
                    "service": {
                        "name": service.name,
                        "target": service.target,
                        "description": service.description,
                        "tlsEnabled": bool(service.tls_enabled),
                        "tags": service.tags or [],
                        "credentialsRequired": bool(service.grpc_metadata or service.tls_cert_path or service.tls_key_path),
                    },
                    "artifact": {
                        "version": artifact.version,
                        "sourceType": artifact.source_type,
                        "sha256": artifact.content_hash,
                        "filename": (artifact.source_info or {}).get("filename"),
                        "descriptorPath": _artifact_path(artifact.content_hash),
                        "descriptorSet": base64.b64encode(artifact.descriptor_set).decode("ascii"),
                    },
                }
            definitions.append(definition)

        bundle: dict[str, Any] = {
            "formatVersion": TOOL_DEFINITION_FORMAT_VERSION,
            "kind": TOOL_BUNDLE_KIND,
            "exportedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "exportedBy": exported_by,
            "tools": definitions,
            "dependencies": {"grpcSchemas": list(grpc_dependencies.values())},
            "security": {"secretsIncluded": False, "ownershipIncluded": False},
        }
        digest_payload = {key: value for key, value in bundle.items() if key not in {"exportedAt", "bundleHash"}}
        bundle["bundleHash"] = f"sha256:{_sha256(_canonical_bytes(digest_payload))}"
        get_audit_trail_service().log_action(
            action="export_tool_bundle",
            resource_type="tool_bundle",
            resource_id=bundle["bundleHash"],
            user_id=exported_by,
            user_email=exported_by,
            details={"tool_count": len(definitions), "grpc_dependency_count": len(grpc_dependencies), "secrets_included": False},
        )
        return bundle

    def bundle_to_zip(self, bundle: dict[str, Any]) -> bytes:
        """Serialize a ToolBundle to a deterministic, dependency-carrying ZIP."""
        self.validate_bundle(bundle)
        manifest = deepcopy(bundle)
        artifact_payloads: dict[str, bytes] = {}
        for dependency in manifest.get("dependencies", {}).get("grpcSchemas", []):
            artifact = dependency["artifact"]
            descriptor = base64.b64decode(artifact.pop("descriptorSet"), validate=True)
            path = artifact["descriptorPath"]
            previous_payload = artifact_payloads.get(path)
            if previous_payload is not None and previous_payload != descriptor:  # pragma: no cover - checksum validation makes this defensive
                raise ToolBundleValidationError(f"Conflicting descriptor payloads for {path}")
            artifact_payloads[path] = descriptor

        output = BytesIO()
        with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(TOOL_BUNDLE_MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
            for path, payload in sorted(artifact_payloads.items()):
                archive.writestr(path, payload)
        return output.getvalue()

    def bundle_from_zip(self, payload: bytes) -> dict[str, Any]:
        """Read and validate a bounded ToolBundle ZIP without extracting to disk."""
        if not payload or len(payload) > settings.mcpgateway_proto_max_upload_bytes:
            raise ToolBundleValidationError("Tool bundle is empty or exceeds the upload limit")
        try:
            archive = zipfile.ZipFile(BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise ToolBundleValidationError("Tool bundle is not a valid ZIP archive") from exc

        with archive:
            members = archive.infolist()
            if len(members) > settings.mcpgateway_proto_max_zip_entries:
                raise ToolBundleValidationError("Tool bundle contains too many files")
            expanded = 0
            names: set[str] = set()
            for member in members:
                member_path = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if member_path.is_absolute() or ".." in member_path.parts or not member_path.parts or member.is_dir() or stat.S_ISLNK(mode):
                    raise ToolBundleValidationError(f"Unsafe Tool bundle entry: {member.filename}")
                if member.filename in names:
                    raise ToolBundleValidationError(f"Duplicate Tool bundle entry: {member.filename}")
                names.add(member.filename)
                expanded += member.file_size
                if expanded > settings.mcpgateway_proto_max_uncompressed_bytes:
                    raise ToolBundleValidationError("Expanded Tool bundle exceeds the size limit")
            if TOOL_BUNDLE_MANIFEST not in names:
                raise ToolBundleValidationError("Tool bundle is missing manifest.json")
            try:
                manifest = json.loads(archive.read(TOOL_BUNDLE_MANIFEST))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ToolBundleValidationError("Tool bundle manifest is invalid JSON") from exc
            if not isinstance(manifest, dict):
                raise ToolBundleValidationError("Tool bundle manifest must be a JSON object")
            dependency_container = manifest.get("dependencies")
            if not isinstance(dependency_container, dict):
                raise ToolBundleValidationError("Tool bundle dependencies must be an object")
            dependencies = dependency_container.get("grpcSchemas")
            if not isinstance(dependencies, list):
                raise ToolBundleValidationError("gRPC dependencies must be a list")
            expected_entries = {TOOL_BUNDLE_MANIFEST}
            encoded_artifacts: dict[str, str] = {}
            for dependency in dependencies:
                if not isinstance(dependency, dict):
                    raise ToolBundleValidationError("Invalid gRPC dependency")
                artifact = dependency.get("artifact", {})
                if not isinstance(artifact, dict):
                    raise ToolBundleValidationError("Invalid gRPC descriptor artifact")
                descriptor_path = artifact.get("descriptorPath")
                if not isinstance(descriptor_path, str) or descriptor_path not in names or not descriptor_path.startswith(_BUNDLE_ARTIFACT_PREFIX):
                    raise ToolBundleValidationError("Tool bundle references a missing descriptor artifact")
                expected_entries.add(descriptor_path)
                encoded_descriptor = encoded_artifacts.get(descriptor_path)
                if encoded_descriptor is None:
                    encoded_descriptor = base64.b64encode(archive.read(descriptor_path)).decode("ascii")
                    encoded_artifacts[descriptor_path] = encoded_descriptor
                artifact["descriptorSet"] = encoded_descriptor
            unexpected_entries = names - expected_entries
            if unexpected_entries:
                raise ToolBundleValidationError(f"Tool bundle contains undeclared files: {', '.join(sorted(unexpected_entries))}")
        self.validate_bundle(manifest)
        return manifest

    def validate_bundle(self, bundle: dict[str, Any]) -> None:
        """Validate format, Tool hashes, descriptor hashes, and dependency references."""
        if not isinstance(bundle, dict):
            raise ToolBundleValidationError("Tool bundle must be a JSON object")
        if bundle.get("formatVersion") != TOOL_DEFINITION_FORMAT_VERSION or bundle.get("kind") != TOOL_BUNDLE_KIND:
            raise ToolBundleValidationError("Unsupported Tool bundle format")
        tools = bundle.get("tools")
        if not isinstance(tools, list) or not tools:
            raise ToolBundleValidationError("Tool bundle must contain at least one Tool")
        if len(tools) > settings.mcpgateway_bulk_import_max_tools:
            raise ToolBundleValidationError("Tool bundle contains too many Tools")

        dependency_container = bundle.get("dependencies")
        if not isinstance(dependency_container, dict):
            raise ToolBundleValidationError("Tool bundle dependencies must be an object")
        dependencies = dependency_container.get("grpcSchemas")
        if not isinstance(dependencies, list):
            raise ToolBundleValidationError("gRPC dependencies must be a list")
        if len(dependencies) > settings.mcpgateway_bulk_import_max_tools:
            raise ToolBundleValidationError("Tool bundle contains too many gRPC dependencies")
        security = bundle.get("security")
        if not isinstance(security, dict) or security.get("secretsIncluded") is not False or security.get("ownershipIncluded") is not False:
            raise ToolBundleValidationError("Tool bundles must explicitly exclude secrets and ownership")
        dependencies_by_key: dict[str, dict[str, Any]] = {}
        for dependency in dependencies:
            if not isinstance(dependency, dict) or not isinstance(dependency.get("key"), str):
                raise ToolBundleValidationError("Invalid gRPC dependency")
            artifact = dependency.get("artifact")
            service = dependency.get("service")
            if not isinstance(artifact, dict) or not isinstance(service, dict) or not isinstance(service.get("name"), str) or not isinstance(service.get("target"), str):
                raise ToolBundleValidationError("Incomplete gRPC service dependency")
            encoded = artifact.get("descriptorSet")
            if not isinstance(encoded, str):
                raise ToolBundleValidationError("Invalid base64 descriptor artifact")
            try:
                descriptor = base64.b64decode(encoded, validate=True)
            except (binascii.Error, TypeError, ValueError) as exc:
                raise ToolBundleValidationError("Invalid base64 descriptor artifact") from exc
            if len(descriptor) > settings.mcpgateway_proto_max_upload_bytes:
                raise ToolBundleValidationError("Descriptor artifact exceeds the upload limit")
            content_hash = _sha256(descriptor)
            if artifact.get("sha256") != content_hash or artifact.get("descriptorPath") != _artifact_path(content_hash):
                raise ToolBundleValidationError("Descriptor checksum/path mismatch")
            expected_key = f"grpc:{service['name']}:{content_hash}"
            if dependency["key"] != expected_key:
                raise ToolBundleValidationError("gRPC dependency key does not match its service and descriptor")
            try:
                GrpcSchemaService.normalize_descriptor_set(descriptor)
            except GrpcServiceError as exc:
                raise ToolBundleValidationError(str(exc)) from exc
            if dependency["key"] in dependencies_by_key:
                raise ToolBundleValidationError(f"Duplicate gRPC dependency: {dependency['key']}")
            dependencies_by_key[dependency["key"]] = dependency

        tool_identities: set[tuple[str, str, str]] = set()
        referenced_dependency_keys: set[str] = set()
        for definition in tools:
            if not isinstance(definition, dict) or definition.get("formatVersion") != TOOL_DEFINITION_FORMAT_VERSION or definition.get("kind") != TOOL_DEFINITION_KIND:
                raise ToolBundleValidationError("Invalid Tool definition")
            metadata = definition.get("metadata")
            spec = definition.get("spec")
            if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not metadata["name"] or not isinstance(spec, dict):
                raise ToolBundleValidationError("Incomplete Tool definition")
            expected_hash = f"sha256:{_sha256(_canonical_bytes({'kind': TOOL_DEFINITION_KIND, 'spec': spec}))}"
            if metadata.get("contentHash") != expected_hash:
                raise ToolBundleValidationError(f"Tool definition checksum mismatch: {metadata.get('name')}")
            integration = spec.get("integration")
            if not isinstance(integration, dict) or not integration.get("type"):
                raise ToolBundleValidationError("Tool definition has no integration binding")
            integration_type = str(integration["type"])
            dependency_identity = str(integration.get("dependencyKey") or "") if integration_type == "gRPC" else ""
            source_id = metadata.get("sourceId")
            identity = ("source", source_id, "") if isinstance(source_id, str) and source_id else (integration_type, dependency_identity, metadata["name"])
            if identity in tool_identities:
                raise ToolBundleValidationError(f"Duplicate Tool definition: {metadata['name']}")
            tool_identities.add(identity)
            if integration.get("type") == "gRPC":
                dependency_key = integration.get("dependencyKey")
                if not isinstance(dependency_key, str) or dependency_key not in dependencies_by_key:
                    raise ToolBundleValidationError(f"gRPC Tool '{metadata.get('name')}' has no descriptor dependency")
                referenced_dependency_keys.add(dependency_key)
            authentication = spec.get("authentication")
            if not isinstance(authentication, dict) or authentication.get("credentialsIncluded") is not False:
                raise ToolBundleValidationError(f"Tool definition must not contain credentials: {metadata.get('name')}")
            if integration.get("type") == "REST":
                headers = integration.get("headers", {})
                if not isinstance(headers, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()):
                    raise ToolBundleValidationError(f"REST Tool has invalid headers: {metadata.get('name')}")
                if filter_sensitive_headers(headers) != headers:
                    raise ToolBundleValidationError(f"REST Tool contains sensitive headers: {metadata.get('name')}")

        unreferenced_dependency_keys = set(dependencies_by_key) - referenced_dependency_keys
        if unreferenced_dependency_keys:
            raise ToolBundleValidationError("Tool bundle contains unreferenced gRPC dependencies")

        claimed_bundle_hash = bundle.get("bundleHash")
        if claimed_bundle_hash:
            digest_payload = {key: value for key, value in bundle.items() if key not in {"exportedAt", "bundleHash"}}
            expected_bundle_hash = f"sha256:{_sha256(_canonical_bytes(digest_payload))}"
            if claimed_bundle_hash != expected_bundle_hash:
                raise ToolBundleValidationError("Tool bundle checksum mismatch")

    @staticmethod
    def _visible_grpc_service(db: Session, name: str, user_email: Optional[str], token_teams: Optional[list[str]]) -> Optional[DbGrpcService]:
        """Return a same-name service only when it is in the caller's Layer-1 scope."""
        statement = select(DbGrpcService).where(DbGrpcService.name == name)
        statement = GrpcRegistryService.scope_statement(statement, DbGrpcService, db, user_email, token_teams)
        return db.execute(statement).scalar_one_or_none()

    @staticmethod
    async def _can_manage_grpc_service(
        permission_service: PermissionService,
        service: DbGrpcService,
        user_email: Optional[str],
        token_teams: Optional[list[str]],
    ) -> bool:
        """Check ownership plus the exact service-team permission boundary."""
        if not user_email:
            return False
        if await permission_service.check_resource_ownership(user_email, service):
            return True
        return await permission_service.check_permission(
            user_email=user_email,
            permission="admin.grpc",
            resource_type="grpc_service",
            resource_id=service.id,
            team_id=service.team_id,
            token_teams=token_teams,
        )

    @staticmethod
    def _grpc_service_create(service_data: dict[str, Any], owner_email: Optional[str]) -> GrpcServiceCreate:
        """Validate the secret-free service configuration recreated by import."""
        name = service_data.get("name")
        target = service_data.get("target")
        if not isinstance(name, str) or not isinstance(target, str):
            raise ToolBundleValidationError("Incomplete gRPC service dependency")
        return GrpcServiceCreate(
            name=name,
            target=target,
            description=service_data.get("description"),
            reflection_enabled=False,
            tls_enabled=False,
            discovery_mode="artifact",
            health_check_enabled=False,
            tags=service_data.get("tags") or [],
            owner_email=owner_email,
            visibility="private",
        )

    @staticmethod
    def _metadata_fields(definition: dict[str, Any]) -> dict[str, Any]:
        """Validate and return presentation metadata shared by imported Tools."""
        metadata = definition["metadata"]
        spec = definition["spec"]
        extension_metadata = optional_extension_metadata(spec.get("extensionMetadata"))
        validate_extension_metadata(extension_metadata)
        return {
            "custom_name": metadata.get("customName") or metadata["name"],
            "displayName": metadata.get("displayName"),
            "title": spec.get("title"),
            "description": spec.get("description"),
            "tags": spec.get("tags") or [],
            "annotations": spec.get("annotations") or {},
            "extension_metadata": extension_metadata,
            "jsonpath_filter": spec.get("jsonpathFilter") or "",
        }

    def _grpc_metadata_update(self, definition: dict[str, Any], expected_version: Optional[int] = None) -> ToolUpdate:
        """Build a validated metadata-only update for a source-managed Tool."""
        return ToolUpdate(expected_version=expected_version, **self._metadata_fields(definition))

    def _rest_create(self, definition: dict[str, Any], owner_email: Optional[str]) -> ToolCreate:
        """Build and validate a private REST Tool from a portable definition."""
        metadata = definition["metadata"]
        spec = definition["spec"]
        binding = spec["integration"]
        return ToolCreate(
            name=metadata["name"],
            **self._metadata_fields(definition),
            url=binding.get("url"),
            integration_type="REST",
            request_type=binding.get("requestType") or "GET",
            headers=binding.get("headers") or {},
            input_schema=spec.get("inputSchema"),
            output_schema=spec.get("outputSchema"),
            deprecated=bool(spec.get("deprecated", False)),
            visibility="private",
            owner_email=owner_email,
            base_url=binding.get("baseUrl"),
            path_template=binding.get("pathTemplate"),
            query_mapping=binding.get("queryMapping"),
            header_mapping=binding.get("headerMapping"),
            timeout_ms=binding.get("timeoutMs"),
            expose_passthrough=binding.get("exposePassthrough", True),
            allowlist=binding.get("allowlist"),
            plugin_chain_pre=binding.get("pluginChainPre"),
            plugin_chain_post=binding.get("pluginChainPost"),
        )

    def _rest_update(self, definition: dict[str, Any], expected_version: int) -> ToolUpdate:
        """Build and validate a full REST Tool update from a portable definition."""
        spec = definition["spec"]
        binding = spec["integration"]
        return ToolUpdate(
            expected_version=expected_version,
            **self._metadata_fields(definition),
            url=binding.get("url"),
            integration_type="REST",
            request_type=binding.get("requestType") or "GET",
            headers=binding.get("headers") or {},
            input_schema=spec.get("inputSchema"),
            output_schema=spec.get("outputSchema"),
            deprecated=bool(spec.get("deprecated", False)),
            base_url=binding.get("baseUrl"),
            path_template=binding.get("pathTemplate"),
            query_mapping=binding.get("queryMapping"),
            header_mapping=binding.get("headerMapping"),
            timeout_ms=binding.get("timeoutMs"),
            expose_passthrough=binding.get("exposePassthrough", True),
            allowlist=binding.get("allowlist"),
            plugin_chain_pre=binding.get("pluginChainPre"),
            plugin_chain_post=binding.get("pluginChainPost"),
        )

    @staticmethod
    def _existing_rest_tool(db: Session, definition: dict[str, Any], owner_email: str) -> Optional[DbTool]:
        """Resolve the private REST Tool identity used by package re-imports."""
        return db.execute(
            select(DbTool).where(
                DbTool.original_name == definition["metadata"]["name"],
                DbTool.owner_email == owner_email,
                DbTool.visibility == "private",
                DbTool.integration_type == "REST",
            )
        ).scalar_one_or_none()

    async def preview_import(
        self,
        db: Session,
        bundle: dict[str, Any],
        user_email: Optional[str],
        token_teams: Optional[list[str]],
        grpc_enabled: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Return a read-only import plan, including validation and sync side effects."""
        self.validate_bundle(bundle)
        grpc_import_enabled = settings.mcpgateway_grpc_enabled if grpc_enabled is None else grpc_enabled
        dependencies = {item["key"]: item for item in bundle["dependencies"]["grpcSchemas"]}
        items: list[dict[str, Any]] = []
        all_ready = True
        permission_service = PermissionService(db, audit_enabled=False)
        target_counts: dict[tuple[str, str, str], int] = {}
        for definition in bundle["tools"]:
            metadata = definition["metadata"]
            integration = definition["spec"]["integration"]
            target_identity = (
                str(integration["type"]),
                str(integration.get("dependencyKey") or "") if integration["type"] == "gRPC" else "",
                metadata["name"],
            )
            target_counts[target_identity] = target_counts.get(target_identity, 0) + 1
        for definition in bundle["tools"]:
            metadata = definition["metadata"]
            integration = definition["spec"]["integration"]
            item: dict[str, Any] = {"name": metadata["name"], "integrationType": integration["type"], "status": "ready", "warnings": []}
            target_identity = (
                str(integration["type"]),
                str(integration.get("dependencyKey") or "") if integration["type"] == "gRPC" else "",
                metadata["name"],
            )
            if target_counts[target_identity] > 1:
                item.update(status="blocked", action="none", reason="Multiple package entries resolve to the same target Tool")
                all_ready = False
                items.append(item)
                continue
            try:
                if integration["type"] == "gRPC":
                    if not grpc_import_enabled:
                        item.update(status="blocked", action="none", reason="gRPC support is disabled")
                        all_ready = False
                        items.append(item)
                        continue
                    dependency = dependencies[integration["dependencyKey"]]
                    service_data = dependency["service"]
                    service_create = self._grpc_service_create(service_data, user_email)
                    _validate_grpc_target(service_create.target)
                    self._grpc_metadata_update(definition)
                    service = self._visible_grpc_service(db, service_data["name"], user_email, token_teams)
                    any_same_name = db.execute(select(DbGrpcService.id).where(DbGrpcService.name == service_data["name"])).scalar_one_or_none()
                    if service is None and any_same_name is not None:
                        item.update(status="blocked", action="none", reason="A same-name gRPC service exists outside the caller's visibility scope")
                        all_ready = False
                    elif service is not None and not await self._can_manage_grpc_service(permission_service, service, user_email, token_teams):
                        item.update(status="blocked", action="none", reason="The existing gRPC service is visible but is not owned or manageable by the caller")
                        all_ready = False
                    else:
                        descriptor = base64.b64decode(dependency["artifact"]["descriptorSet"], validate=True)
                        _normalized, catalog = GrpcSchemaService.normalize_descriptor_set(descriptor)
                        generated_methods = sorted(
                            f"{service_name}.{method['name']}"
                            for service_name, service_spec in catalog.items()
                            for method in service_spec.get("methods", [])
                            if not method.get("client_streaming")
                        )
                        if metadata["name"] not in generated_methods:
                            item.update(status="blocked", action="none", reason="The descriptor does not generate this Tool method")
                            all_ready = False
                        else:
                            item.update(
                                action="activate_and_sync" if service else "create_service_activate_and_sync",
                                serviceName=service_data["name"],
                                schemaHash=dependency["artifact"]["sha256"],
                                generatedTools=generated_methods,
                            )
                            if service and service.target != service_data["target"]:
                                item["warnings"].append("Existing service target will be retained")
                            if service_data.get("credentialsRequired"):
                                item["warnings"].append("Credentials/TLS material are not included and must be configured on the target")
                elif integration["type"] == "REST":
                    self._rest_create(definition, user_email)
                    existing = self._existing_rest_tool(db, definition, user_email) if user_email else None
                    if existing is not None:
                        self._rest_update(definition, existing.version or 1)
                    item["action"] = "update_existing" if existing else "create"
                    item["conflict"] = existing is not None
                    if definition["spec"].get("authentication", {}).get("credentialsRequired"):
                        item["warnings"].append("Authentication credentials are not included and must be configured on the target")
                else:
                    item.update(status="blocked", action="none", reason=f"Portable import is not supported for {integration['type']} generated Tools")
                    all_ready = False
            except (GrpcServiceError, ValidationError, ValueError) as exc:
                item.update(status="blocked", action="none", reason=str(exc))
                all_ready = False
            items.append(item)
        return {"ready": all_ready, "formatVersion": bundle["formatVersion"], "bundleHash": bundle.get("bundleHash"), "items": items}

    async def import_bundle(
        self,
        db: Session,
        bundle: dict[str, Any],
        imported_by: str,
        user_email: Optional[str],
        token_teams: Optional[list[str]],
        conflict_strategy: str = "update",
        grpc_enabled: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Apply a validated bundle and recreate gRPC Tools through schema sync."""
        if conflict_strategy not in {"skip", "update", "fail"}:
            raise ToolBundleValidationError("conflict_strategy must be skip, update, or fail")
        preview = await self.preview_import(db, bundle, user_email, token_teams, grpc_enabled=grpc_enabled)
        if not preview["ready"]:
            reasons = [item.get("reason", item["name"]) for item in preview["items"] if item["status"] == "blocked"]
            raise ToolBundleConflictError("; ".join(reasons))

        definitions_by_grpc_dependency: dict[str, list[dict[str, Any]]] = {}
        rest_definitions: list[dict[str, Any]] = []
        for definition in bundle["tools"]:
            integration = definition["spec"]["integration"]
            if integration["type"] == "gRPC":
                definitions_by_grpc_dependency.setdefault(integration["dependencyKey"], []).append(definition)
            elif integration["type"] == "REST":
                rest_definitions.append(definition)

        created_services = 0
        synced_tools = 0
        created_tools = 0
        updated_tools = 0
        skipped_tools = 0
        dependencies = {item["key"]: item for item in bundle["dependencies"]["grpcSchemas"]}

        # Resolve all predictable conflicts before the first service method can
        # commit. This keeps the ``fail`` strategy from partially applying an
        # otherwise valid package before discovering a later name conflict.
        existing_grpc_services = {
            dependency_key: self._visible_grpc_service(db, dependencies[dependency_key]["service"]["name"], user_email, token_teams)
            for dependency_key in definitions_by_grpc_dependency
        }
        existing_rest_tools = {definition["metadata"]["name"]: self._existing_rest_tool(db, definition, imported_by) for definition in rest_definitions}
        if conflict_strategy == "fail":
            conflicts = [dependencies[key]["service"]["name"] for key, service in existing_grpc_services.items() if service is not None]
            conflicts.extend(name for name, tool in existing_rest_tools.items() if tool is not None)
            if conflicts:
                raise ToolBundleConflictError("Import conflicts: " + ", ".join(sorted(conflicts)))

        grpc_manager = None
        if definitions_by_grpc_dependency:
            # Lazy import avoids loading the optional gRPC runtime for REST-only requests.
            # First-Party
            from mcpgateway.services.grpc_service import GrpcService  # pylint: disable=import-outside-toplevel

            grpc_manager = GrpcService()
        for dependency_key, definitions in definitions_by_grpc_dependency.items():
            assert grpc_manager is not None  # nosec B101 - established when gRPC definitions are present
            dependency = dependencies[dependency_key]
            service_data = dependency["service"]
            service = existing_grpc_services[dependency_key]
            if service is not None and conflict_strategy == "skip":
                skipped_tools += len(definitions)
                continue
            if service is None:
                service_create = self._grpc_service_create(service_data, imported_by)
                _validate_grpc_target(service_create.target)
                service_read = await grpc_manager.register_service(
                    db,
                    service_create,
                    user_email=imported_by,
                    metadata={"created_by": imported_by, "created_via": "tool-bundle-import"},
                )
                service = db.get(DbGrpcService, service_read.id)
                created_services += 1
            if service is None:  # pragma: no cover - defensive after successful registration
                raise ToolBundleConflictError("Unable to resolve imported gRPC service")

            descriptor = base64.b64decode(dependency["artifact"]["descriptorSet"], validate=True)
            descriptor_filename = PurePosixPath(dependency["artifact"]["descriptorPath"]).name
            await grpc_manager.import_schema(db, service.id, descriptor, descriptor_filename, imported_by, activate=True)

            selected_names = {definition["metadata"]["name"] for definition in definitions}
            generated = list(db.execute(select(DbTool).where(DbTool.grpc_service_id == service.id, DbTool.original_name.in_(selected_names))).scalars())
            generated_by_name = {tool.original_name: tool for tool in generated}
            for definition in definitions:
                tool = generated_by_name.get(definition["metadata"]["name"])
                if tool is None:
                    raise ToolBundleConflictError(f"Schema did not generate expected Tool: {definition['metadata']['name']}")
                previous_version = tool.version or 1
                update = self._grpc_metadata_update(definition, expected_version=previous_version)
                try:
                    updated = await tool_service.update_tool(
                        db,
                        tool.id,
                        update,
                        modified_by=imported_by,
                        modified_via="tool-bundle-import",
                        user_email=imported_by,
                        source_sync=True,
                    )
                except ToolVersionConflictError as exc:
                    raise ToolBundleConflictError(str(exc)) from exc
                if updated.version != previous_version:
                    updated_tools += 1
            synced_tools += len(generated)

        for definition in rest_definitions:
            metadata = definition["metadata"]
            create = self._rest_create(definition, imported_by)
            existing = existing_rest_tools[metadata["name"]]
            if existing is None:
                try:
                    await tool_service.register_tool(db, create, created_by=imported_by, created_via="tool-bundle-import", owner_email=imported_by, visibility="private")
                except (IntegrityError, ToolNameConflictError) as exc:
                    raise ToolBundleConflictError(f"Tool name conflict: {metadata['name']}") from exc
                created_tools += 1
                continue
            if conflict_strategy == "skip":
                skipped_tools += 1
                continue
            previous_version = existing.version or 1
            update = self._rest_update(definition, previous_version)
            try:
                updated = await tool_service.update_tool(db, existing.id, update, modified_by=imported_by, modified_via="tool-bundle-import", user_email=imported_by)
            except ToolVersionConflictError as exc:
                raise ToolBundleConflictError(str(exc)) from exc
            if updated.version != previous_version:
                updated_tools += 1

        result = {
            "status": "completed",
            "bundleHash": bundle.get("bundleHash"),
            "createdServices": created_services,
            "syncedGrpcTools": synced_tools,
            "createdTools": created_tools,
            "updatedTools": updated_tools,
            "skippedTools": skipped_tools,
        }
        get_audit_trail_service().log_action(
            action="import_tool_bundle",
            resource_type="tool_bundle",
            resource_id=bundle.get("bundleHash") or "unhashed",
            user_id=imported_by,
            user_email=imported_by,
            details=result,
        )
        return result

    @staticmethod
    def require_tool(db: Session, tool_id: str) -> DbTool:
        """Load an ORM Tool after the route has completed its scoped access check."""
        tool = db.get(DbTool, tool_id)
        if tool is None:
            raise ToolNotFoundError(f"Tool not found: {tool_id}")
        return tool


tool_portability_service = ToolPortabilityService()
