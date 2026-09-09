# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/http_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

HTTP Service Management (PR3, design §16).

This module implements HTTP service management for ContextForge: service
registration, listing, retrieval, updates, activation toggling, deletion,
OpenAPI artifact import/activation, and the tool synchronization that turns
compiled operations into MCP tools — mirroring ``grpc_service``.
"""

# Standard
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

# Third-Party
from pydantic import ValidationError
from sqlalchemy import and_, delete, desc, false, or_, select, update
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.db import EmailTeam
from mcpgateway.db import HttpSchemaArtifact
from mcpgateway.db import HttpService as DbHttpService
from mcpgateway.db import server_tool_association
from mcpgateway.db import Tool as DbTool
from mcpgateway.db import ToolMetric
from mcpgateway.schemas import (
    HttpSchemaDiff,
    HttpServiceCreate,
    HttpServiceRead,
    HttpServiceUpdate,
)
from mcpgateway.services.base_service import BaseService
from mcpgateway.services.http_schema_service import _catalog_from_source_info
from mcpgateway.services.http_schema_service import HttpSchemaService
from mcpgateway.services.logging_service import LoggingService
from mcpgateway.services.operation_tool_compiler import OperationToolCompiler, ToolCompileOverrides
from mcpgateway.services.team_management_service import TeamManagementService
from mcpgateway.utils.create_slug import slugify
from mcpgateway.utils.display_name import generate_display_name
from mcpgateway.utils.http_validation import HttpServiceError
from mcpgateway.utils.pagination import unified_paginate

# Initialize logging
logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

_TOKEN_TEAMS_UNSET = object()


class HttpServiceNotFoundError(HttpServiceError):
    """Raised when a requested HTTP service is not found."""


class HttpServiceNameConflictError(HttpServiceError):
    """Raised when an HTTP service name conflicts with an existing one."""

    def __init__(self, name: str, is_active: bool = True, service_id: Optional[str] = None):
        """Initialize the HttpServiceNameConflictError.

        Args:
            name: The conflicting HTTP service name
            is_active: Whether the conflicting service is currently active
            service_id: The ID of the conflicting service, if known
        """
        self.name = name
        self.is_active = is_active
        self.service_id = service_id
        msg = f"HTTP service with name '{name}' already exists"
        if not is_active:
            msg += " (inactive)"
        if service_id:
            msg += f" (ID: {service_id})"
        super().__init__(msg)


class HttpService:
    """Service for managing HTTP services with OpenAPI contract discovery."""

    def __init__(self):
        """Initialize the HTTP service manager."""

    @staticmethod
    def _tool_cache_refs(tools: Any) -> List[Tuple[Optional[str], str, Optional[str]]]:
        """Snapshot cache identifiers before a service mutation commits.

        Args:
            tools: The related tool rows (or an iterable of duck-typed rows).

        Returns:
            List of ``(tool_id, name, gateway_id)`` tuples to invalidate.
        """
        refs: List[Tuple[Optional[str], str, Optional[str]]] = []
        try:
            related_tools = list(tools or [])
        except TypeError:
            return refs
        for tool in related_tools:
            name = getattr(tool, "name", None)
            if not isinstance(name, str) or not name:
                continue
            tool_id = getattr(tool, "id", None)
            gateway_id = getattr(tool, "gateway_id", None)
            refs.append(
                (
                    str(tool_id) if tool_id else None,
                    name,
                    str(gateway_id) if gateway_id else None,
                )
            )
        return refs

    async def _invalidate_tool_caches(self, refs: List[Tuple[Optional[str], str, Optional[str]]]) -> None:
        """Invalidate registry, lookup, and result caches after a committed tool change.

        Args:
            refs: Tool reference tuples collected before the mutation.
        """
        normalized = sorted(set(refs), key=lambda item: (item[1], item[0] or "", item[2] or ""))
        if not normalized:
            return

        # Lazy imports keep cache initialization out of lightweight module imports.
        # First-Party
        from mcpgateway.cache.registry_cache import get_registry_cache  # pylint: disable=import-outside-toplevel
        from mcpgateway.cache.tool_lookup_cache import tool_lookup_cache  # pylint: disable=import-outside-toplevel
        from mcpgateway.cache.tool_result_cache import tool_result_cache  # pylint: disable=import-outside-toplevel

        try:
            await get_registry_cache().invalidate_tools()
            await asyncio.gather(
                *(tool_lookup_cache.invalidate(name, gateway_id=gateway_id) for _tool_id, name, gateway_id in normalized),
                *(tool_result_cache.invalidate_tool(tool_id) for tool_id, _name, _gateway_id in normalized if tool_id),
            )
        except Exception as exc:  # pragma: no cover - cache backends are best effort
            # The resource transaction has already committed. Match the existing
            # cache services' best-effort contract without misreporting the CRUD
            # operation as rolled back.
            logger.warning("Failed to invalidate caches for HTTP-derived tools: %s", exc)

    async def _build_team_visibility_clause(
        self,
        db: Session,
        user_email: Optional[str],
        team_id: Optional[str],
    ) -> Any:
        """Build an access-control WHERE clause for HTTP services.

        Mirrors :meth:`BaseService._apply_visibility_filter` semantics using
        the visibility/team_id/owner_email columns on :class:`HttpService`.

        Args:
            db: Database session
            user_email: Caller email, or None for no identity
            team_id: Optional team filter

        Returns:
            SQLAlchemy clause, or None when no restriction applies
        """
        if team_id:
            user_teams = await TeamManagementService(db).get_user_teams(user_email) if user_email else []
            if not any(team.id == team_id for team in user_teams):
                return false()  # no access: deny everything

            access_conditions = [
                and_(
                    DbHttpService.team_id == team_id,
                    DbHttpService.visibility.in_(["team", "public"]),
                ),
                DbHttpService.visibility == "public",  # globally public items are always visible
            ]
            if user_email:
                access_conditions.append(
                    and_(
                        DbHttpService.team_id == team_id,
                        DbHttpService.owner_email == user_email,
                        DbHttpService.visibility == "private",
                    )
                )
            return or_(*access_conditions)

        if not user_email:
            return None

        user_teams = await TeamManagementService(db).get_user_teams(user_email)
        team_ids = [team.id for team in user_teams]
        clauses = [DbHttpService.visibility == "public"]
        clauses.append(
            and_(
                DbHttpService.visibility == "private",
                DbHttpService.owner_email == user_email,
            )
        )
        if team_ids:
            clauses.append(
                and_(
                    DbHttpService.team_id.in_(team_ids),
                    DbHttpService.visibility.in_(["team", "public"]),
                )
            )
        return or_(*clauses)

    async def register_service(
        self,
        db: Session,
        service_data: HttpServiceCreate,
        user_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> HttpServiceRead:
        """Register a new HTTP service.

        Args:
            db: Database session
            service_data: HTTP service creation data
            user_email: Email of the user creating the service
            metadata: Additional metadata (IP, user agent, etc.)

        Returns:
            HttpServiceRead: The created service

        Raises:
            HttpServiceNameConflictError: If service name already exists
        """
        existing = db.execute(select(DbHttpService).where(DbHttpService.name == service_data.name)).scalar_one_or_none()  # pylint: disable=comparison-with-callable

        if existing:
            raise HttpServiceNameConflictError(name=service_data.name, is_active=existing.enabled, service_id=existing.id)

        db_service = DbHttpService(
            name=service_data.name,
            slug=slugify(service_data.name),
            base_url=service_data.base_url,
            description=service_data.description,
            discovery_mode=service_data.discovery_mode,
            discovery_config=service_data.discovery_config or {},
            runtime_config=service_data.runtime_config or {},
            health_check_enabled=service_data.health_check_enabled,
            health_check_interval=service_data.health_check_interval,
            health_check_timeout=service_data.health_check_timeout,
            health_failure_threshold=service_data.health_failure_threshold,
            tags=[item if isinstance(item, str) else str(item) for item in (service_data.tags or [])],
            team_id=service_data.team_id,
            owner_email=user_email or service_data.owner_email,
            visibility=service_data.visibility,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        if metadata:
            db_service.created_by = user_email
            db_service.created_from_ip = metadata.get("created_from_ip")
            db_service.created_via = metadata.get("created_via")
            db_service.created_user_agent = metadata.get("created_user_agent")

        db.add(db_service)
        db.commit()
        db.refresh(db_service)

        logger.info("Registered HTTP service: %s (base URL: %s)", db_service.name, db_service.base_url)

        return HttpServiceRead.model_validate(db_service)

    async def list_services(
        self,
        db: Session,
        cursor: Optional[str] = None,
        include_inactive: bool = False,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        user_email: Optional[str] = None,
        team_id: Optional[str] = None,
        token_teams: Any = _TOKEN_TEAMS_UNSET,
    ) -> Union[Tuple[List[HttpServiceRead], Optional[str]], Dict[str, Any]]:
        """List HTTP services with pagination and optional filtering.

        Args:
            db: Database session
            cursor: Pagination cursor for keyset pagination
            include_inactive: Include disabled services
            limit: Maximum number of services to return. None for default, 0 for unlimited
            page: Page number for page-based pagination (1-indexed). Mutually exclusive with cursor
            per_page: Items per page for page-based pagination
            user_email: Filter by user email for team access control
            team_id: Filter by team ID
            token_teams: Canonical token scope. When supplied (including
                explicit ``None`` for admin bypass), it takes precedence over
                legacy DB-membership expansion.

        Returns:
            If page is provided: Dict with {"data": [...], "pagination": {...}, "links": {...}}
            If cursor is provided or neither: tuple of (list of HttpServiceRead objects, next_cursor)
        """
        query = select(DbHttpService).order_by(desc(DbHttpService.created_at), desc(DbHttpService.id))

        if token_teams is not _TOKEN_TEAMS_UNSET:
            canonical_teams = token_teams if token_teams is None else list(token_teams)
            query = BaseService._apply_visibility_scope(  # pylint: disable=protected-access
                query,
                DbHttpService,
                user_email=user_email,
                token_teams=canonical_teams,
                team_ids=canonical_teams or [],
                db=db,
            )
            if team_id:
                query = query.where(or_(DbHttpService.team_id == team_id, DbHttpService.visibility == "public"))
        elif user_email or team_id:
            team_filter = await self._build_team_visibility_clause(db, user_email, team_id)
            if team_filter is not None:
                query = query.where(team_filter)

        if not include_inactive:
            query = query.where(DbHttpService.enabled.is_(True))  # pylint: disable=singleton-comparison

        pag_result = await unified_paginate(
            db=db,
            query=query,
            page=page,
            per_page=per_page,
            cursor=cursor,
            limit=limit,
            base_url="/admin/http",
            query_params={"include_inactive": include_inactive} if include_inactive else {},
        )

        next_cursor = None
        if page is not None:
            services_db = pag_result["data"]
        else:
            services_db, next_cursor = pag_result

        team_ids_set = {s.team_id for s in services_db if s.team_id}
        team_map = {}
        if team_ids_set:
            teams = db.execute(select(EmailTeam.id, EmailTeam.name).where(EmailTeam.id.in_(team_ids_set), EmailTeam.is_active.is_(True))).all()
            team_map = {team.id: team.name for team in teams}

        db.commit()  # Release transaction to avoid idle-in-transaction

        result = []
        for s in services_db:
            try:
                s.team = team_map.get(s.team_id) if s.team_id else None
                result.append(HttpServiceRead.model_validate(s))
            except (ValidationError, ValueError, KeyError, TypeError) as e:
                logger.exception("Failed to convert HTTP service %s (%s): %s", getattr(s, "id", "unknown"), getattr(s, "name", "unknown"), e)

        if page is not None:
            return {
                "data": result,
                "pagination": pag_result["pagination"],
                "links": pag_result["links"],
            }

        return (result, next_cursor)

    async def get_service(
        self,
        db: Session,
        service_id: str,
        user_email: Optional[str] = None,
        token_teams: Any = _TOKEN_TEAMS_UNSET,
    ) -> HttpServiceRead:
        """Get a specific HTTP service by ID.

        Args:
            db: Database session
            service_id: Service ID
            user_email: Email for team access control
            token_teams: Canonical token scope. When supplied, it takes
                precedence over legacy DB-membership expansion.

        Returns:
            The HTTP service

        Raises:
            HttpServiceNotFoundError: If service not found or access denied
        """
        query = select(DbHttpService).where(DbHttpService.id == service_id)

        if token_teams is not _TOKEN_TEAMS_UNSET:
            canonical_teams = token_teams if token_teams is None else list(token_teams)
            query = BaseService._apply_visibility_scope(  # pylint: disable=protected-access
                query,
                DbHttpService,
                user_email=user_email,
                token_teams=canonical_teams,
                team_ids=canonical_teams or [],
                db=db,
            )
        elif user_email:
            team_filter = await self._build_team_visibility_clause(db, user_email, None)
            if team_filter is not None:
                query = query.where(team_filter)

        service = db.execute(query).scalar_one_or_none()

        if not service:
            raise HttpServiceNotFoundError(f"HTTP service with ID '{service_id}' not found")

        return HttpServiceRead.model_validate(service)

    async def update_service(
        self,
        db: Session,
        service_id: str,
        service_data: HttpServiceUpdate,
        user_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> HttpServiceRead:
        """Update an existing HTTP service.

        Args:
            db: Database session
            service_id: Service ID to update
            service_data: Update data
            user_email: Email of user performing update
            metadata: Audit metadata

        Returns:
            Updated service

        Raises:
            HttpServiceNotFoundError: If service not found
            HttpServiceNameConflictError: If new name conflicts
        """
        service = db.execute(select(DbHttpService).where(DbHttpService.id == service_id)).scalar_one_or_none()

        if not service:
            raise HttpServiceNotFoundError(f"HTTP service with ID '{service_id}' not found")

        tool_cache_refs = self._tool_cache_refs(service.tools)

        if service_data.name and service_data.name != service.name:
            existing = db.execute(
                select(DbHttpService).where(and_(DbHttpService.name == service_data.name, DbHttpService.id != service_id))
            ).scalar_one_or_none()  # pylint: disable=comparison-with-callable

            if existing:
                raise HttpServiceNameConflictError(name=service_data.name, is_active=existing.enabled, service_id=existing.id)

        update_data = service_data.model_dump(exclude_unset=True)
        # Layer 1 invariant: visibility/team/owner changes on the parent service must propagate
        # to every child tool in the same transaction, or already-generated tools will keep the
        # old token-scoping. Snapshot the previous values before mutation so we know what changed.
        scoping_fields = ("visibility", "team_id", "owner_email")
        previous_scoping = {f: getattr(service, f) for f in scoping_fields}
        for field, value in update_data.items():
            setattr(service, field, value)

        if "name" in update_data:
            service.slug = slugify(service.name)

        service.updated_at = datetime.now(timezone.utc)

        if metadata and user_email:
            service.modified_by = user_email
            service.modified_from_ip = metadata.get("modified_from_ip")
            service.modified_via = metadata.get("modified_via")
            service.modified_user_agent = metadata.get("modified_user_agent")

        service.version += 1

        scoping_changed = {f: getattr(service, f) for f in scoping_fields if getattr(service, f) != previous_scoping[f]}
        if scoping_changed:
            db.execute(update(DbTool).where(DbTool.http_service_id == service.id).values(**scoping_changed, version=DbTool.version + 1))
            logger.info("Propagated %s change(s) on HTTP service %s to child tools", sorted(scoping_changed), service.name)

        db.commit()
        await self._invalidate_tool_caches(tool_cache_refs)
        db.refresh(service)

        logger.info("Updated HTTP service: %s", service.name)

        return HttpServiceRead.model_validate(service)

    async def set_service_state(
        self,
        db: Session,
        service_id: str,
        activate: bool,
    ) -> HttpServiceRead:
        """Set an HTTP service's enabled status.

        Args:
            db: Database session
            service_id: Service ID
            activate: True to enable, False to disable

        Returns:
            Updated service

        Raises:
            HttpServiceNotFoundError: If service not found
        """
        service = db.execute(select(DbHttpService).where(DbHttpService.id == service_id)).scalar_one_or_none()

        if not service:
            raise HttpServiceNotFoundError(f"HTTP service with ID '{service_id}' not found")

        tool_cache_refs = self._tool_cache_refs(service.tools)

        service.enabled = activate
        service.updated_at = datetime.now(timezone.utc)

        db.commit()
        await self._invalidate_tool_caches(tool_cache_refs)
        db.refresh(service)

        action = "activated" if activate else "deactivated"
        logger.info("HTTP service %s %s", service.name, action)

        return HttpServiceRead.model_validate(service)

    async def delete_service(
        self,
        db: Session,
        service_id: str,
    ) -> None:
        """Delete an HTTP service and its associated tools.

        Explicitly deletes child tool records (metrics, server associations, tools)
        before deleting the service itself, following the same pattern as
        gateway_service.delete_gateway() to avoid FK constraint violations.

        Args:
            db: Database session
            service_id: Service ID to delete

        Raises:
            HttpServiceNotFoundError: If service not found
        """
        service = db.execute(select(DbHttpService).where(DbHttpService.id == service_id)).scalar_one_or_none()

        if not service:
            raise HttpServiceNotFoundError(f"HTTP service with ID '{service_id}' not found")

        tool_cache_refs = self._tool_cache_refs(service.tools)
        tool_ids = [t.id for t in service.tools]
        if tool_ids:
            for i in range(0, len(tool_ids), 500):
                chunk = tool_ids[i : i + 500]
                db.execute(delete(ToolMetric).where(ToolMetric.tool_id.in_(chunk)))
                db.execute(delete(server_tool_association).where(server_tool_association.c.tool_id.in_(chunk)))
                db.execute(delete(DbTool).where(DbTool.id.in_(chunk)))

        db.delete(service)
        db.commit()
        await self._invalidate_tool_caches(tool_cache_refs)

        logger.info("Deleted HTTP service: %s (removed %d tools)", service.name, len(tool_ids))

    def _sync_tools_from_artifact(
        self,
        db: Session,
        service: DbHttpService,
        artifact: HttpSchemaArtifact,
    ) -> List[DbTool]:
        """Sync MCP tools from the compiled operations of one artifact.

        Matches existing tools through ``protocol_config.operationRef`` so
        tool identities survive operationId renames and reordering.  Stale
        tools whose operation vanished are soft-disabled (the same triad the
        gRPC sync applies).

        Args:
            db: Database session
            service: HTTP service model instance
            artifact: The artifact whose catalog becomes authoritative

        Returns:
            Tools whose lookup or result cache entries must be invalidated after commit.
        """
        catalog = _catalog_from_source_info(artifact.source_info)
        expected_keys = {operation.key for operation in catalog.operations}

        existing_tools = db.execute(select(DbTool).where(DbTool.http_service_id == service.id)).scalars().all()

        def _ref_for(tool: DbTool) -> Optional[str]:
            """Extract the operationRef key of one tool row."""
            config = tool.protocol_config
            if not isinstance(config, dict):
                return None
            ref = config.get("operationRef")
            return ref if isinstance(ref, str) and ref else None

        existing_map = {ref: tool for tool in existing_tools if (ref := _ref_for(tool)) is not None}

        # Last-line defense: an empty catalog over published tools would soft-disable
        # everything. Import guards this upstream, but keep the sync itself safe.
        if not expected_keys and existing_tools:
            logger.warning(
                "Skipping tool sync for %s: empty catalog would disable %d existing tools",
                service.name,
                len(existing_tools),
            )
            return []

        # Preserve IDs, server relations, and metrics when an operation disappears.
        # Reappearing operations are re-enabled below.
        stale_tools = [tool for tool in existing_tools if _ref_for(tool) not in expected_keys]
        changed_tools: List[DbTool] = []
        for stale_tool in stale_tools:
            if stale_tool.enabled or not stale_tool.deprecated or stale_tool.reachable:
                stale_tool.enabled = False
                stale_tool.deprecated = True
                stale_tool.reachable = False
                stale_tool.version = (stale_tool.version or 1) + 1
                changed_tools.append(stale_tool)
        if stale_tools:
            logger.info("Deprecated %d stale tools for HTTP service %s", len(stale_tools), service.name)

        compiler = OperationToolCompiler()
        tools_created = 0
        tools_updated = 0
        tools_failed = 0
        for operation in sorted(catalog.operations, key=lambda item: item.key):
            # Per-tool try/except: a single bad operation must not poison the whole sync.
            try:
                compiled = compiler.compile(operation, service, artifact, ToolCompileOverrides())
                existing_tool = existing_map.get(operation.key)
                if existing_tool:
                    changed = False
                    if existing_tool.original_description != compiled.description:
                        if existing_tool.description == existing_tool.original_description:
                            existing_tool.description = compiled.description
                        existing_tool.original_description = compiled.description
                        changed = True
                    if existing_tool.input_schema != compiled.input_schema:
                        existing_tool.input_schema = compiled.input_schema
                        changed = True
                    if existing_tool.output_schema != compiled.output_schema:
                        existing_tool.output_schema = compiled.output_schema
                        changed = True
                    if existing_tool.base_url != service.base_url:
                        existing_tool.base_url = service.base_url
                        changed = True
                    if existing_tool.url != service.base_url:
                        existing_tool.url = service.base_url
                        changed = True
                    # Layer 1 invariant: parent visibility/team/owner must propagate to derived tools
                    # so token-scoping changes on the HTTP service take effect immediately.
                    if existing_tool.visibility != service.visibility:
                        existing_tool.visibility = service.visibility
                        changed = True
                    if existing_tool.team_id != service.team_id:
                        existing_tool.team_id = service.team_id
                        changed = True
                    if existing_tool.owner_email != service.owner_email:
                        existing_tool.owner_email = service.owner_email
                        changed = True
                    if not existing_tool.enabled or existing_tool.deprecated or not existing_tool.reachable:
                        existing_tool.enabled = True
                        existing_tool.deprecated = False
                        existing_tool.reachable = True
                        changed = True
                    if existing_tool.http_schema_artifact_id != artifact.id:
                        existing_tool.http_schema_artifact_id = artifact.id
                        changed = True
                    if changed:
                        existing_tool.version = (existing_tool.version or 1) + 1
                        changed_tools.append(existing_tool)
                        tools_updated += 1
                else:
                    db_tool = DbTool(
                        original_name=compiled.name,
                        custom_name=compiled.name,
                        custom_name_slug=slugify(compiled.name),
                        display_name=generate_display_name(compiled.name),
                        url=service.base_url,
                        base_url=service.base_url,
                        original_description=compiled.description,
                        description=compiled.description,
                        integration_type="REST",
                        request_type=compiled.request_type,
                        input_schema=compiled.input_schema,
                        output_schema=compiled.output_schema,
                        annotations=compiled.annotations,
                        protocol_config=compiled.protocol_config,
                        created_by="system",
                        created_via="http-schema-sync",
                        federation_source=service.name,
                        version=1,
                        team_id=service.team_id,
                        owner_email=service.owner_email,
                        visibility=service.visibility,
                        http_service_id=service.id,
                        http_schema_artifact_id=artifact.id,
                    )
                    db.add(db_tool)
                    changed_tools.append(db_tool)
                    tools_created += 1
            except Exception as tool_err:  # pylint: disable=broad-except
                tools_failed += 1
                logger.warning("Skipping tool %s for HTTP service %s: %s", operation.key, service.name, tool_err, exc_info=True)
                continue

        logger.info(
            "Synced tools for HTTP service %s: %d created, %d updated, %d failed",
            service.name,
            tools_created,
            tools_updated,
            tools_failed,
        )
        return changed_tools

    async def import_schema(
        self,
        db: Session,
        service_id: str,
        payload: bytes,
        filename: str,
        user_email: Optional[str],
        activate: bool = True,
        allow_remote: bool = True,
    ) -> HttpSchemaArtifact:
        """Import and optionally activate an OpenAPI artifact.

        Args:
            db: Database session
            service_id: Owning service ID
            payload: Raw uploaded bytes (JSON/YAML/ZIP)
            filename: Client-provided upload filename
            user_email: Email of the importing user
            activate: Whether to promote the artifact to active
            allow_remote: Whether external ``$ref`` targets may be fetched
                (YAML manifests opt in via ``references.allowRemote``)

        Returns:
            The stored artifact row

        Raises:
            HttpServiceNotFoundError: If the service does not exist
        """
        service = db.get(DbHttpService, service_id)
        if service is None:
            raise HttpServiceNotFoundError(f"HTTP service with ID '{service_id}' not found")
        tool_cache_refs = self._tool_cache_refs(service.tools)
        artifact = await HttpSchemaService.import_artifact(
            db, service, payload, filename, user_email, activate=activate, allow_remote=allow_remote
        )
        if activate:
            synced_tools = self._sync_tools_from_artifact(db, service, artifact)
            tool_cache_refs.extend(self._tool_cache_refs(synced_tools))
        db.commit()
        await self._invalidate_tool_caches(tool_cache_refs)
        return artifact

    async def list_schemas(self, db: Session, service_id: str) -> List[HttpSchemaArtifact]:
        """List immutable schema versions newest first.

        Args:
            db: Database session
            service_id: Owning service ID

        Returns:
            The artifact rows ordered by version descending.

        Raises:
            HttpServiceNotFoundError: If the service does not exist
        """
        if db.get(DbHttpService, service_id) is None:
            raise HttpServiceNotFoundError(f"HTTP service with ID '{service_id}' not found")
        return list(db.execute(select(HttpSchemaArtifact).where(HttpSchemaArtifact.http_service_id == service_id).order_by(HttpSchemaArtifact.version.desc())).scalars())

    async def activate_schema(self, db: Session, service_id: str, artifact_id: str) -> HttpSchemaArtifact:
        """Activate an artifact version and synchronize executable operations.

        Args:
            db: Database session
            service_id: Owning service ID
            artifact_id: Artifact version to activate

        Returns:
            The activated artifact row

        Raises:
            HttpServiceNotFoundError: If the service does not exist
            HttpServiceError: If the artifact does not belong to the service
        """
        service = db.get(DbHttpService, service_id)
        artifact = db.get(HttpSchemaArtifact, artifact_id)
        if service is None:
            raise HttpServiceNotFoundError(f"HTTP service with ID '{service_id}' not found")
        if artifact is None or artifact.http_service_id != service_id:
            raise HttpServiceError("Schema artifact not found for this service")
        tool_cache_refs = self._tool_cache_refs(service.tools)
        HttpSchemaService.activate_artifact(db, service, artifact)
        synced_tools = self._sync_tools_from_artifact(db, service, artifact)
        tool_cache_refs.extend(self._tool_cache_refs(synced_tools))
        db.commit()
        await self._invalidate_tool_caches(tool_cache_refs)
        db.refresh(artifact)
        return artifact

    async def diff_schemas(self, db: Session, service_id: str, left_id: str, right_id: str) -> HttpSchemaDiff:
        """Compare two schema versions owned by a service.

        Args:
            db: Database session
            service_id: Owning service ID
            left_id: Baseline artifact ID
            right_id: Comparison artifact ID

        Returns:
            The operation-level diff.

        Raises:
            HttpServiceError: If either artifact does not belong to the service
        """
        left = db.get(HttpSchemaArtifact, left_id)
        right = db.get(HttpSchemaArtifact, right_id)
        if left is None or right is None or left.http_service_id != service_id or right.http_service_id != service_id:
            raise HttpServiceError("Both schema artifacts must belong to the requested service")
        return HttpSchemaService.diff(left, right)
