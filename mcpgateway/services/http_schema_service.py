# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/http_schema_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Versioned HTTP contract artifacts (PR3, design §14/§15).

Mirrors ``grpc_schema_service`` for the HTTP registry: immutable OpenAPI
artifact import (content-addressed, version = max + 1), candidate/activate
pointer management, operation-fingerprint diffs, and catalog serialisation
helpers shared with the registry view and the tool synchronizer.

No method here commits: the caller owns the transaction so schema
activation and tool synchronization land in the same commit (or roll back
together), exactly like the gRPC path.
"""

# Standard
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Optional

# Third-Party
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.db import HttpSchemaArtifact, HttpService
from mcpgateway.protocols.contracts.models import (
    ContractArtifact,
    ContractProviderError,
    DiscoveryContext,
    OperationCatalog,
    OperationDefinition,
)
from mcpgateway.protocols.contracts.openapi import OpenAPIContractProvider
from mcpgateway.protocols.http.models import (
    HttpBodyVariant,
    HttpParameter,
    HttpRequestContract,
    HttpResponseContract,
    HttpResponseVariant,
)
from mcpgateway.schemas import HttpSchemaDiff
from mcpgateway.services.contract_artifact_service import ContractArtifactService
from mcpgateway.utils.http_validation import HttpServiceError

# Source type recorded on every imported artifact (PR3 ships OpenAPI only;
# wsdl/xsd/manual are rejected by the router with an explicit 400).
_SOURCE_TYPE = "openapi"


def _operation_to_dict(operation: OperationDefinition) -> dict[str, Any]:
    """Serialize one compiled operation into a JSON-storable dict.

    Args:
        operation: The compiled IR operation.

    Returns:
        A plain dict carrying every field needed to rebuild the IR and to
        compute fingerprints.  JSON Schema fragments are embedded here —
        this is the artifact catalog, never ``protocol_config`` (§18).
    """
    request = operation.request
    response = operation.response
    return {
        "key": operation.key,
        "source_operation_id": operation.source_operation_id,
        "title": operation.title,
        "description": operation.description,
        "deprecated": operation.deprecated,
        "tags": list(operation.tags),
        "method": request.method,
        "path_template": request.path_template,
        "parameters": [
            {
                "name": param.name,
                "location": param.location,
                "required": param.required,
                "schema": param.schema,
                "style": param.style,
                "explode": param.explode,
                "allow_reserved": param.allow_reserved,
            }
            for param in request.parameters
        ],
        "bodies": [
            {
                "media_type": body.media_type,
                "codec": body.codec,
                "schema": body.schema,
                "required": body.required,
                "schema_ref": body.schema_ref,
            }
            for body in request.bodies
        ],
        "responses": [
            {
                "status_code": variant.status_code,
                "media_type": variant.media_type,
                "schema": variant.schema,
                "description": variant.description,
            }
            for variant in response.variants
        ]
        if response is not None and hasattr(response, "variants")
        else [],
    }


def _operation_from_dict(data: dict[str, Any]) -> OperationDefinition:
    """Rebuild one IR operation from its stored serialization.

    Args:
        data: The dict produced by :func:`_operation_to_dict`.

    Returns:
        The equivalent ``OperationDefinition`` with typed request/response.
    """
    return OperationDefinition(
        key=data["key"],
        protocol="http",
        source_operation_id=data.get("source_operation_id"),
        title=data.get("title"),
        description=data.get("description"),
        deprecated=bool(data.get("deprecated", False)),
        tags=tuple(data.get("tags") or ()),
        request=HttpRequestContract(
            method=data["method"],
            path_template=data["path_template"],
            parameters=tuple(
                HttpParameter(
                    name=param["name"],
                    location=param["location"],
                    required=param["required"],
                    schema=param["schema"],
                    style=param.get("style"),
                    explode=param.get("explode"),
                    allow_reserved=bool(param.get("allow_reserved", False)),
                )
                for param in data.get("parameters", [])
            ),
            bodies=tuple(
                HttpBodyVariant(
                    media_type=body["media_type"],
                    codec=body["codec"],
                    schema=body.get("schema"),
                    required=bool(body.get("required", False)),
                    schema_ref=body.get("schema_ref"),
                )
                for body in data.get("bodies", [])
            ),
        ),
        response=HttpResponseContract(
            variants=tuple(
                HttpResponseVariant(
                    status_code=variant["status_code"],
                    media_type=variant["media_type"],
                    schema=variant.get("schema"),
                    description=variant.get("description"),
                )
                for variant in data.get("responses", [])
            )
        ),
    )


def _catalog_to_source_info(catalog: OperationCatalog) -> dict[str, Any]:
    """Serialize a compiled catalog into the ``source_info`` payload.

    Args:
        catalog: The compiled operation catalog.

    Returns:
        The ``{"operations": [...], "diagnostics": [...]}`` dict stored in
        ``http_schema_artifacts.source_info`` and mirrored on
        ``http_services.discovered_operations``.
    """
    return {
        "operations": [_operation_to_dict(operation) for operation in catalog.operations],
        "diagnostics": [
            {
                "severity": diagnostic.severity,
                "code": diagnostic.code,
                "message": diagnostic.message,
                "location": diagnostic.location,
                "operation_key": diagnostic.operation_key,
            }
            for diagnostic in catalog.diagnostics
        ],
    }


def _catalog_from_source_info(source_info: Optional[dict[str, Any]]) -> OperationCatalog:
    """Rebuild a catalog from a stored registry payload.

    Both ``http_schema_artifacts.source_info`` and
    ``http_services.discovered_operations`` wrap the serialized catalog
    under a ``catalog`` key.

    Args:
        source_info: The stored payload (``None`` yields an empty catalog).

    Returns:
        The equivalent ``OperationCatalog``.
    """
    stored = (source_info or {}).get("catalog") or {}
    return OperationCatalog(
        source_type=_SOURCE_TYPE,
        source_hash="",
        operations=tuple(_operation_from_dict(item) for item in stored.get("operations", [])),
        diagnostics=(),
    )


def operation_request_fingerprint(operation: OperationDefinition) -> str:
    """Hash the request facets that trigger tool re-approval when changed.

    Compares only the request side (method, path template, parameters,
    bodies): response-schema changes update tool output without requiring
    client re-approval of the input contract.

    Args:
        operation: The compiled IR operation.

    Returns:
        A SHA-256 hex digest of the canonical request JSON.
    """
    request = operation.request
    payload = {
        "method": request.method,
        "path_template": request.path_template,
        "parameters": [
            {
                "name": param.name,
                "location": param.location,
                "required": param.required,
                "schema": param.schema,
                "style": param.style,
                "explode": param.explode,
            }
            for param in request.parameters
        ],
        "bodies": [
            {"media_type": body.media_type, "codec": body.codec, "schema": body.schema, "required": body.required}
            for body in request.bodies
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class HttpSchemaService:
    """Normalize, version, compare, and activate OpenAPI contract artifacts."""

    @staticmethod
    async def import_artifact(
        db: Session,
        service: HttpService,
        payload: bytes,
        filename: str,
        created_by: Optional[str],
        activate: bool = True,
        allow_remote: bool = True,
    ) -> HttpSchemaArtifact:
        """Prepare, compile, and store an immutable contract artifact.

        Content-addressed: importing the exact same bundle again reuses the
        existing artifact row instead of creating a new version.  External
        ``$ref`` targets are materialised by ``ContractArtifactService``
        before the provider ever sees the document (§66).

        Args:
            db: Database session (transaction owned by the caller).
            service: The owning ``HttpService``.
            payload: Raw uploaded bytes (JSON/YAML/ZIP).
            filename: Client-provided upload filename.
            created_by: Email of the importing user.
            activate: When true, promote the artifact to active.
            allow_remote: Whether external ``$ref`` values may be fetched.

        Returns:
            The stored (or reused) artifact row.

        Raises:
            HttpServiceError: On preparation, validation, or compilation
                failure (the provider raises ``ContractProviderError``).
        """
        prepared = await ContractArtifactService().prepare_artifact(payload, filename, allow_remote=allow_remote)
        provider = OpenAPIContractProvider()
        try:
            catalog = await provider.discover(
                ContractArtifact(
                    payload=prepared.bundle,
                    artifact_format=prepared.artifact_format,
                    source_type=_SOURCE_TYPE,
                    source_info=prepared.source_info,
                ),
                DiscoveryContext(),
            )
        except ContractProviderError as exc:
            raise HttpServiceError(str(exc)) from exc

        content_hash = prepared.content_hash
        existing = db.execute(
            select(HttpSchemaArtifact).where(
                HttpSchemaArtifact.http_service_id == service.id,
                HttpSchemaArtifact.content_hash == content_hash,
            )
        ).scalar_one_or_none()
        if existing is None:
            next_version = (
                db.execute(select(func.max(HttpSchemaArtifact.version)).where(HttpSchemaArtifact.http_service_id == service.id)).scalar_one_or_none() or 0
            ) + 1
            existing = HttpSchemaArtifact(
                http_service_id=service.id,
                version=next_version,
                source_type=_SOURCE_TYPE,
                artifact_format=prepared.artifact_format,
                content_hash=content_hash,
                artifact_blob=prepared.bundle,
                source_info={
                    "filename": (prepared.source_info or {}).get("filename", filename),
                    "allow_remote": allow_remote,
                    "catalog": _catalog_to_source_info(catalog),
                },
                created_by=created_by,
            )
            db.add(existing)
            db.flush()

        if activate:
            HttpSchemaService.activate_artifact(db, service, existing, catalog=catalog)
        else:
            service.candidate_artifact_id = existing.id
            service.discovered_schema_hash = content_hash
            service.schema_drift = bool(service.active_schema_hash and service.active_schema_hash != content_hash)
            service.last_discovery = datetime.now(timezone.utc)
            service.last_discovery_error = None
            service.discovered_operations = {"catalog": _catalog_to_source_info(catalog)}
            service.operation_count = len(catalog.operations)
        # No commit here: the caller owns the transaction so schema activation and
        # tool synchronization land in the same commit (or roll back together).
        return existing

    @staticmethod
    def activate_artifact(
        db: Session,
        service: HttpService,
        artifact: HttpSchemaArtifact,
        catalog: Optional[OperationCatalog] = None,
    ) -> None:
        """Activate one artifact version without changing tool identities.

        Args:
            db: Database session (transaction owned by the caller).
            service: The owning ``HttpService``.
            artifact: The artifact version to promote.
            catalog: The compiled catalog; when omitted it is rebuilt from
                the stored ``source_info``.

        Raises:
            HttpServiceError: If the artifact belongs to another service or
                is empty while a non-empty schema is active.
        """
        if artifact.http_service_id != service.id:
            raise HttpServiceError("Schema artifact belongs to another HTTP service")
        if catalog is None:
            catalog = _catalog_from_source_info(artifact.source_info)
        operation_count = len(catalog.operations)
        if operation_count == 0 and service.active_artifact_id and (service.operation_count or 0) > 0:
            raise HttpServiceError("Refusing to activate empty schema while an active schema with operations exists")
        db.execute(update(HttpSchemaArtifact).where(HttpSchemaArtifact.http_service_id == service.id).values(is_active=False, activated_at=None))
        artifact.is_active = True
        artifact.activated_at = datetime.now(timezone.utc)
        service.active_artifact_id = artifact.id
        service.active_schema_hash = artifact.content_hash
        service.candidate_artifact_id = None
        service.schema_drift = bool(service.discovered_schema_hash and service.discovered_schema_hash != artifact.content_hash)
        service.discovered_operations = {"catalog": _catalog_to_source_info(catalog)}
        service.operation_count = operation_count
        service.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _operation_fingerprints(artifact: HttpSchemaArtifact) -> dict[str, str]:
        """Hash the full contract of each operation for deterministic diffs.

        Args:
            artifact: The stored artifact.

        Returns:
            Mapping of operation key to SHA-256 hex digest.
        """
        catalog = _catalog_from_source_info(artifact.source_info)
        fingerprints: dict[str, str] = {}
        for operation in catalog.operations:
            fingerprints[operation.key] = hashlib.sha256(
                json.dumps(_operation_to_dict(operation), sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        return fingerprints

    @classmethod
    def diff(cls, left: HttpSchemaArtifact, right: HttpSchemaArtifact) -> HttpSchemaDiff:
        """Compare two artifact versions at operation-signature level.

        Args:
            left: The baseline artifact.
            right: The comparison artifact.

        Returns:
            Added/removed/changed operation keys, sorted for determinism.
        """
        left_ops = cls._operation_fingerprints(left)
        right_ops = cls._operation_fingerprints(right)
        return HttpSchemaDiff(
            from_artifact_id=left.id,
            to_artifact_id=right.id,
            added_operations=sorted(set(right_ops) - set(left_ops)),
            removed_operations=sorted(set(left_ops) - set(right_ops)),
            changed_operations=sorted(key for key in set(left_ops) & set(right_ops) if left_ops[key] != right_ops[key]),
        )
