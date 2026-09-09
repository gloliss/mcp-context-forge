# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/routers/http_schema.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

HTTP Registry APIs (PR3, design §20): OpenAPI artifact import, immutable
version listing, diff, sync preview, activation, read-only registry views,
and immediate health checks — mirroring ``grpc_schema``.
"""

# Standard
from typing import Any

# Third-Party
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.auth_context import get_token_teams_from_request, get_user_email
from mcpgateway.config import settings
from mcpgateway.db import get_db
from mcpgateway.db import HttpService as DbHttpService
from mcpgateway.middleware.rbac import get_current_user_with_permissions, require_permission
from mcpgateway.schemas import (
    HttpRegistrySchemaRead,
    HttpRegistryServiceRead,
    HttpRegistryViewRead,
    HttpSchemaArtifactRead,
    HttpSchemaDiff,
    HttpToolSyncPreview,
)
from mcpgateway.services.http_monitoring_service import get_http_monitoring_service
from mcpgateway.services.http_registry_service import HttpRegistryService
from mcpgateway.services.http_service import HttpService, HttpServiceError, HttpServiceNotFoundError

router = APIRouter(prefix="/http", tags=["HTTP Registry"])
http_service = HttpService()

# Accepted upload filename suffixes for schema artifacts.
_ALLOWED_SUFFIXES = (".json", ".yaml", ".yml", ".zip")


def _require_http_enabled() -> None:
    """Hide HTTP registry routes while the feature is disabled."""
    if not settings.mcpgateway_http_registry_enabled:
        raise HTTPException(status_code=404, detail="HTTP Registry support is disabled")


def _http_error(exc: HttpServiceError) -> HTTPException:
    """Translate registry service errors into stable HTTP statuses."""
    return HTTPException(status_code=404 if isinstance(exc, HttpServiceNotFoundError) else 422, detail=str(exc))


def _require_service_access(request: Request, user: Any, db: Session, service_id: str) -> DbHttpService:
    """Resolve an HTTP service through the caller's canonical token scope."""
    statement = select(DbHttpService).where(DbHttpService.id == service_id)
    token_teams = get_token_teams_from_request(request)
    if token_teams is not None:
        clauses = [DbHttpService.visibility == "public", DbHttpService.owner_email == get_user_email(user)]
        if token_teams:
            clauses.append(DbHttpService.team_id.in_(token_teams))
        statement = statement.where(or_(*clauses))
    service = db.execute(statement).scalar_one_or_none()
    if service is None:
        raise HTTPException(status_code=404, detail="HTTP service not found")
    return service


def _visible_services(request: Request, user: Any, db: Session) -> list[DbHttpService]:
    """Resolve the services visible to the caller under their token scope."""
    statement = select(DbHttpService).order_by(DbHttpService.name)
    token_teams = get_token_teams_from_request(request)
    if token_teams is not None:
        clauses = [DbHttpService.visibility == "public", DbHttpService.owner_email == get_user_email(user)]
        if token_teams:
            clauses.append(DbHttpService.team_id.in_(token_teams))
        statement = statement.where(or_(*clauses))
    return list(db.execute(statement).scalars().all())


@router.post("/{service_id}/schemas/import", response_model=HttpSchemaArtifactRead, status_code=201)
@require_permission("admin.http", allow_admin_bypass=False)
async def import_schema(
    service_id: str,
    request: Request,
    artifact: UploadFile = File(...),
    activate: bool = Form(True),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
):
    """Import an OpenAPI JSON/YAML document or safe ZIP as an immutable artifact."""
    _require_http_enabled()
    _require_service_access(request, user, db, service_id)
    payload = await artifact.read(settings.mcpgateway_http_max_upload_bytes + 1)
    if len(payload) > settings.mcpgateway_http_max_upload_bytes:
        raise HTTPException(status_code=413, detail="OpenAPI artifact exceeds the upload limit")
    filename = artifact.filename or "openapi.json"
    if not filename.lower().endswith(_ALLOWED_SUFFIXES):
        raise HTTPException(status_code=415, detail="Expected .json, .yaml, .yml or .zip artifact")
    try:
        return await http_service.import_schema(db, service_id, payload, filename, get_user_email(user), activate=activate)
    except HttpServiceError as exc:
        raise _http_error(exc) from exc


@router.get("/{service_id}/schemas", response_model=list[HttpSchemaArtifactRead])
@require_permission("admin.http", allow_admin_bypass=False)
async def list_schemas(service_id: str, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)):
    """List schema versions without returning artifact bytes."""
    _require_http_enabled()
    _require_service_access(request, user, db, service_id)
    try:
        return await http_service.list_schemas(db, service_id)
    except HttpServiceError as exc:
        raise _http_error(exc) from exc


@router.get("/registry", response_model=HttpRegistryViewRead)
@require_permission("admin.http", allow_admin_bypass=False)
async def registry_overview(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)):
    """Read-only registry summary: services with schema versions, operations, and tool exposure."""
    _require_http_enabled()
    visible = _visible_services(request, user, db)
    return HttpRegistryService.build_registry_view(db, [service.id for service in visible])


@router.get("/{service_id}/registry", response_model=HttpRegistryServiceRead)
@require_permission("admin.http", allow_admin_bypass=False)
async def registry_service_detail(service_id: str, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)):
    """Read-only detail for one service: schema versions and per-operation tool status."""
    _require_http_enabled()
    _require_service_access(request, user, db, service_id)
    view = HttpRegistryService.build_service_detail(db, service_id)
    if view is None:
        raise HTTPException(status_code=404, detail="HTTP service not found")
    return view


@router.get("/{service_id}/schemas/{artifact_id}/registry", response_model=HttpRegistrySchemaRead)
@require_permission("admin.http", allow_admin_bypass=False)
async def registry_schema_detail(service_id: str, artifact_id: str, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)):
    """Read-only detail for one schema version: operations with exposure and tool state."""
    _require_http_enabled()
    _require_service_access(request, user, db, service_id)
    view = HttpRegistryService.build_schema_detail(db, artifact_id, service_id=service_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Schema artifact not found")
    return view


@router.post("/{service_id}/schemas/{artifact_id}/activate", response_model=HttpSchemaArtifactRead)
@require_permission("admin.http", allow_admin_bypass=False)
async def activate_schema(service_id: str, artifact_id: str, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)):
    """Activate one immutable schema version and resynchronize tools."""
    _require_http_enabled()
    _require_service_access(request, user, db, service_id)
    try:
        return await http_service.activate_schema(db, service_id, artifact_id)
    except HttpServiceError as exc:
        raise _http_error(exc) from exc


@router.get("/{service_id}/schemas/diff", response_model=HttpSchemaDiff)
@require_permission("admin.http", allow_admin_bypass=False)
async def diff_schemas(
    service_id: str,
    request: Request,
    from_artifact_id: str = Query(..., alias="from"),
    to_artifact_id: str = Query(..., alias="to"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),  # pylint: disable=unused-argument
):
    """Compare operation signatures between two schema versions."""
    _require_http_enabled()
    _require_service_access(request, user, db, service_id)
    try:
        return await http_service.diff_schemas(db, service_id, from_artifact_id, to_artifact_id)
    except HttpServiceError as exc:
        raise _http_error(exc) from exc


@router.get("/{service_id}/schemas/{artifact_id}/preview", response_model=HttpToolSyncPreview)
@require_permission("admin.http", allow_admin_bypass=False)
async def preview_tool_sync(
    service_id: str,
    artifact_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),  # pylint: disable=unused-argument
):
    """Preview tool synchronization for a candidate schema without mutating anything."""
    _require_http_enabled()
    _require_service_access(request, user, db, service_id)
    try:
        return HttpRegistryService.build_sync_preview(db, service_id, artifact_id)
    except HttpServiceError as exc:
        raise _http_error(exc) from exc


@router.post("/{service_id}/health")
@require_permission("admin.http", allow_admin_bypass=False)
async def check_health(service_id: str, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)):
    """Run an immediate SSRF-checked HTTP health check."""
    _require_http_enabled()
    _require_service_access(request, user, db, service_id)
    result = await get_http_monitoring_service().check_service(service_id)
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="HTTP service not found")
    return result
