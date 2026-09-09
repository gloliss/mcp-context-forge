# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/http_registry_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Read-only registry views over HTTP services, their schema artifacts,
operations, and the exposure status of the tools derived from those
operations (PR3, design §20).

These queries never mutate rows: no writes, no tool synchronization, no
schema activation.  They join the immutable schema artifacts with the tool
table so administrators can see which operations of which schema version
are actually served to clients — mirroring ``grpc_registry_service``.
"""

# Standard
from typing import Any, cast, Literal, Optional

# Third-Party
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

# First-Party
from mcpgateway.db import HttpSchemaArtifact
from mcpgateway.db import HttpService as DbHttpService
from mcpgateway.db import Tool as DbTool
from mcpgateway.schemas import (
    HttpRegistryOperationRead,
    HttpRegistrySchemaRead,
    HttpRegistryServiceRead,
    HttpRegistryViewRead,
    HttpToolSyncPreview,
)
from mcpgateway.services.base_service import BaseService
from mcpgateway.services.http_schema_service import (
    _catalog_from_source_info,
    operation_request_fingerprint,
)
from mcpgateway.services.operation_tool_compiler import OperationToolCompiler, ToolCompileOverrides
from mcpgateway.utils.http_validation import HttpServiceError

HttpSchemaSourceType = Literal["openapi", "swagger", "wsdl", "xsd", "manual"]
VisibilityType = Literal["private", "team", "public"]


def _operation_ref(tool: DbTool) -> Optional[str]:
    """Extract the stable operation key a tool was generated from.

    Args:
        tool: A tool row.

    Returns:
        The ``protocol_config.operationRef`` value, or ``None`` for tools
        without a registry protocol config (legacy/manual rows).
    """
    config = tool.protocol_config
    if not isinstance(config, dict):
        return None
    ref = config.get("operationRef")
    return ref if isinstance(ref, str) and ref else None


class HttpRegistryService:
    """Read-only queries that assemble the HTTP registry hierarchy."""

    @staticmethod
    def _operations_for_catalog(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Flatten a stored catalog into ``{operation key: summary}``.

        Args:
            catalog: A stored ``{"operations": [...], "diagnostics": [...]}``
                payload.

        Returns:
            Ordered mapping of operation key to a summary dict carrying
            ``method``, ``path``, and ``summary``.
        """
        operations: dict[str, dict[str, Any]] = {}
        for data in (catalog or {}).get("operations", []):
            key = data.get("key")
            if not key:
                continue
            operations[key] = {
                "key": key,
                "method": data.get("method", ""),
                "path": data.get("path_template", ""),
                "summary": data.get("title") or data.get("description") or "",
            }
        return operations

    @staticmethod
    def _tool_state_for(tool_map: dict[str, DbTool], operation_key: str) -> dict[str, Any]:
        """Describe the exposure of one operation from its derived tool row.

        Args:
            tool_map: Existing tools keyed by operation ref.
            operation_key: The stable operation key.

        Returns:
            The exposure-state dict used to populate
            ``HttpRegistryOperationRead``.
        """
        tool = tool_map.get(operation_key)
        if tool is None:
            return {
                "tool_id": None,
                "tool_enabled": False,
                "tool_deprecated": False,
                "tool_reachable": False,
                "exposed": False,
            }
        exposed = bool(tool.enabled) and not bool(tool.deprecated) and bool(tool.reachable)
        return {
            "tool_id": tool.id,
            "tool_enabled": bool(tool.enabled),
            "tool_deprecated": bool(tool.deprecated),
            "tool_reachable": bool(tool.reachable),
            "exposed": exposed,
        }

    @staticmethod
    def _schema_version_summary(
        artifact: HttpSchemaArtifact,
        tool_map: dict[str, DbTool],
    ) -> HttpRegistrySchemaRead:
        """Summarize one artifact without emitting the artifact bytes.

        Args:
            artifact: The stored artifact version.
            tool_map: Current tools of the owning service keyed by
                operation ref.

        Returns:
            The schema-version view with per-operation exposure state.
        """
        catalog = (artifact.source_info or {}).get("catalog") or {}
        operations = [
            HttpRegistryOperationRead(
                **operation,
                **HttpRegistryService._tool_state_for(tool_map, operation["key"]),
            )
            for operation in HttpRegistryService._operations_for_catalog(catalog).values()
        ]
        return HttpRegistrySchemaRead(
            artifact_id=artifact.id,
            version=artifact.version,
            source_type=cast(HttpSchemaSourceType, artifact.source_type),
            artifact_format=artifact.artifact_format,
            content_hash=artifact.content_hash,
            is_active=artifact.is_active,
            created_by=artifact.created_by,
            created_at=artifact.created_at,
            activated_at=artifact.activated_at,
            operations=operations,
        )

    @staticmethod
    def _service_view(
        service: DbHttpService,
        artifacts: list[HttpSchemaArtifact],
        tools: list[DbTool],
    ) -> HttpRegistryServiceRead:
        """Assemble a single service-level registry view.

        Args:
            service: The service row.
            artifacts: Its artifact versions, ordered by version.
            tools: Its tool rows.

        Returns:
            The nested service view with schema versions and tool counts.
        """
        tool_map = {ref: tool for tool in tools if (ref := _operation_ref(tool)) is not None}
        schema_versions = [HttpRegistryService._schema_version_summary(artifact, tool_map) for artifact in artifacts]

        exposed_tool_count = 0
        active_catalog = (service.discovered_operations or {}).get("catalog") or {}
        for operation in HttpRegistryService._operations_for_catalog(active_catalog).values():
            if HttpRegistryService._tool_state_for(tool_map, operation["key"])["exposed"]:
                exposed_tool_count += 1

        return HttpRegistryServiceRead(
            id=service.id,
            name=service.name,
            slug=service.slug,
            base_url=service.base_url,
            description=service.description,
            enabled=bool(service.enabled),
            reachable=bool(service.reachable),
            health_status=service.health_status or "unknown",
            operation_count=service.operation_count or 0,
            active_schema_hash=service.active_schema_hash,
            schema_drift=bool(service.schema_drift),
            team_id=service.team_id,
            owner_email=service.owner_email,
            visibility=cast(VisibilityType, service.visibility or "public"),
            schema_versions=schema_versions,
            tool_count=len(tools),
            exposed_tool_count=exposed_tool_count,
        )

    @staticmethod
    def build_registry_view(
        db: Session,
        service_ids: Optional[list[str]] = None,
    ) -> HttpRegistryViewRead:
        """Build the full read-only registry across the given services.

        Args:
            db: Database session.
            service_ids: Restrict the view to these service IDs. An empty
                list yields an empty view; None means every service.

        Returns:
            A nested view of services, schema versions, operations, and
            tool state.
        """
        service_statement = select(DbHttpService)
        if service_ids is not None:
            if not service_ids:
                return HttpRegistryViewRead()
            service_statement = service_statement.where(DbHttpService.id.in_(service_ids))
        services = list(db.execute(service_statement.order_by(DbHttpService.name)).scalars().all())
        if not services:
            return HttpRegistryViewRead()

        artifact_statement = (
            select(HttpSchemaArtifact)
            .where(HttpSchemaArtifact.http_service_id.in_([service.id for service in services]))
            .order_by(HttpSchemaArtifact.http_service_id, HttpSchemaArtifact.version)
        )
        artifacts = list(db.execute(artifact_statement).scalars().all())
        artifacts_by_service: dict[str, list[HttpSchemaArtifact]] = {}
        for artifact in artifacts:
            artifacts_by_service.setdefault(artifact.http_service_id, []).append(artifact)

        tool_statement = select(DbTool).where(DbTool.http_service_id.in_([service.id for service in services]))
        tools = list(db.execute(tool_statement).scalars().all())
        tools_by_service: dict[str, list[DbTool]] = {}
        for tool in tools:
            if tool.http_service_id is not None:
                tools_by_service.setdefault(tool.http_service_id, []).append(tool)

        views = [
            HttpRegistryService._service_view(
                service,
                artifacts_by_service.get(service.id, []),
                tools_by_service.get(service.id, []),
            )
            for service in services
        ]

        return HttpRegistryViewRead(
            services=views,
            total_services=len(views),
            total_schema_versions=sum(len(view.schema_versions) for view in views),
            total_operations=sum(view.operation_count for view in views),
            total_exposed_tools=sum(view.exposed_tool_count for view in views),
        )

    @staticmethod
    def build_service_detail(
        db: Session,
        service_id: str,
    ) -> Optional[HttpRegistryServiceRead]:
        """Build the schema-version and operation detail view for one service.

        Args:
            db: Database session.
            service_id: The service ID.

        Returns:
            The service-level view, or ``None`` when the service is missing.
        """
        service = db.get(DbHttpService, service_id)
        if service is None:
            return None

        artifacts = list(
            db.execute(
                select(HttpSchemaArtifact)
                .where(HttpSchemaArtifact.http_service_id == service_id)
                .order_by(HttpSchemaArtifact.version)
            ).scalars().all()
        )
        tools = list(db.execute(select(DbTool).where(DbTool.http_service_id == service_id)).scalars().all())

        return HttpRegistryService._service_view(service, artifacts, tools)

    @staticmethod
    def build_schema_detail(
        db: Session,
        artifact_id: str,
        service_id: Optional[str] = None,
    ) -> Optional[HttpRegistrySchemaRead]:
        """Build one schema version with per-operation tool/exposure state.

        Args:
            db: Database session.
            artifact_id: The artifact ID.
            service_id: Optional owning service ID; when given, artifacts of
                other services are not returned.

        Returns:
            The schema-version view, or ``None`` when the artifact does not
            exist or belongs to another service.
        """
        artifact = db.get(HttpSchemaArtifact, artifact_id)
        if artifact is None or (service_id is not None and artifact.http_service_id != service_id):
            return None

        tools = list(
            db.execute(select(DbTool).where(DbTool.http_service_id == artifact.http_service_id)).scalars().all()
        )
        tool_map = {ref: tool for tool in tools if (ref := _operation_ref(tool)) is not None}
        return HttpRegistryService._schema_version_summary(artifact, tool_map)

    @staticmethod
    def scope_statement(statement: Select, model: type[Any], db: Session, user_email: Optional[str], token_teams: Optional[list[str]]) -> Select:
        """Apply the canonical Layer-1 visibility policy to a registry query.

        Args:
            statement: The query to scope.
            model: The row model (must carry visibility/team/owner columns).
            db: Database session.
            user_email: Caller email, or None for no identity.
            token_teams: Canonical token scope.

        Returns:
            The scoped statement.
        """
        return BaseService._apply_visibility_scope(  # pylint: disable=protected-access
            statement,
            model,
            user_email=user_email,
            token_teams=token_teams,
            team_ids=token_teams or [],
            db=db,
        )

    @staticmethod
    def build_sync_preview(
        db: Session,
        service_id: str,
        candidate_artifact_id: str,
    ) -> HttpToolSyncPreview:
        """Preview what tool synchronization would do for a candidate schema.

        Read-only: mirrors ``HttpService._sync_tools_from_artifact`` without
        mutating the Tool table, activating anything, or committing.  Returns
        the would-be added/modified/disabled tools and the operations whose
        request contract changed between the active and candidate schema
        (re-approval).

        Args:
            db: Database session.
            service_id: Owning HTTP service ID.
            candidate_artifact_id: Candidate schema artifact to preview.

        Returns:
            The sync preview. Empty candidate catalogs over published tools
            produce a warning instead of a mass-disable plan.

        Raises:
            HttpServiceError: If the service is missing or the artifact does
                not belong to it.
        """
        service = db.get(DbHttpService, service_id)
        if service is None:
            raise HttpServiceError(f"HTTP service with ID '{service_id}' not found")
        candidate = db.get(HttpSchemaArtifact, candidate_artifact_id)
        if candidate is None or candidate.http_service_id != service_id:
            raise HttpServiceError("Schema artifact not found for this service")

        candidate_catalog = _catalog_from_source_info(candidate.source_info)
        current_catalog = _catalog_from_source_info(service.discovered_operations)
        current_operations = {operation.key: operation for operation in current_catalog.operations}

        existing_tools = db.execute(select(DbTool).where(DbTool.http_service_id == service.id)).scalars().all()
        existing_map = {ref: tool for tool in existing_tools if (ref := _operation_ref(tool)) is not None}

        # Mirror the sync's last-line defense: an empty candidate over published
        # tools would soft-disable everything, so report instead of planning it.
        if not candidate_catalog.operations and existing_tools:
            return HttpToolSyncPreview(
                service_id=service_id,
                candidate_artifact_id=candidate_artifact_id,
                warning=(
                    f"Candidate schema defines no operations; activating it would disable "
                    f"{len(existing_tools)} existing tools. Synchronization skipped."
                ),
            )

        compiler = OperationToolCompiler()
        added: list[str] = []
        modified: list[str] = []
        disabled: list[str] = []
        reapproval: list[str] = []

        for operation in sorted(candidate_catalog.operations, key=lambda item: item.key):
            compiled = compiler.compile(operation, service, candidate, ToolCompileOverrides())
            existing = existing_map.get(operation.key)
            if existing is None:
                added.append(operation.key)
                continue

            # Would-be update checks, mirroring _sync_tools_from_artifact.
            changed = False
            if existing.original_description != compiled.description:
                changed = True
            if existing.input_schema != compiled.input_schema:
                changed = True
            if existing.output_schema != compiled.output_schema:
                changed = True
            if existing.base_url != service.base_url:
                changed = True
            if existing.visibility != service.visibility:
                changed = True
            if existing.team_id != service.team_id:
                changed = True
            if existing.owner_email != service.owner_email:
                changed = True
            if not existing.enabled or existing.deprecated or not existing.reachable:
                changed = True
            if changed:
                modified.append(operation.key)

            # Re-approval: present on both sides with a changed request contract.
            current = current_operations.get(operation.key)
            if current is not None and operation_request_fingerprint(current) != operation_request_fingerprint(operation):
                reapproval.append(operation.key)

        # Disabled: existing tools whose operation vanished from the candidate catalog.
        expected_keys = {operation.key for operation in candidate_catalog.operations}
        for operation_key in sorted(existing_map):
            if operation_key not in expected_keys and operation_key not in disabled:
                disabled.append(operation_key)

        return HttpToolSyncPreview(
            service_id=service_id,
            candidate_artifact_id=candidate_artifact_id,
            added_tools=sorted(added),
            modified_tools=sorted(modified),
            disabled_tools=sorted(disabled),
            operations_needing_reapproval=sorted(reapproval),
        )
