# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/routers/database_sources.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unified database source and query template CRUD APIs (OB-01, Phase 2).

Admin-scoped REST endpoints over the ``database_sources`` and
``database_query_templates`` domain models.  Passwords are never returned and
never logged; errors never echo the credential.
"""

# Third-Party
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.auth_context import get_user_email
from mcpgateway.db import get_db
from mcpgateway.middleware.rbac import get_current_user_with_permissions, require_permission
from mcpgateway.schemas import (
    DatabaseQueryTemplateCreate,
    DatabaseQueryTemplateRead,
    DatabaseQueryTemplateUpdate,
    DatabaseSourceCreate,
    DatabaseSourceRead,
    DatabaseSourceUpdate,
)
from mcpgateway.services.database_source_service import (
    DatabaseSourceError,
    DatabaseSourceNameConflictError,
    DatabaseSourceNotFoundError,
    DatabaseSourceService,
)
from mcpgateway.services.database_template_service import (
    DatabaseTemplateError,
    DatabaseTemplateNameConflictError,
    DatabaseTemplateNotFoundError,
    DatabaseTemplateService,
)

router = APIRouter(prefix="/database-sources", tags=["Database Sources"])


def _database_error(exc: Exception) -> HTTPException:
    """Translate service errors into stable HTTP statuses without echoing secrets."""
    if isinstance(exc, (DatabaseSourceNotFoundError, DatabaseTemplateNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (DatabaseSourceNameConflictError, DatabaseTemplateNameConflictError)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
@router.post("", response_model=DatabaseSourceRead, status_code=201)
@require_permission("admin.database_sources", allow_admin_bypass=False)
async def create_source(
    data: DatabaseSourceCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
):
    """Create a database source with an encrypted credential."""
    try:
        return DatabaseSourceService.create_source(db, data, get_user_email(user))
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Database source name or slug already exists") from exc
    except DatabaseSourceError as exc:
        raise _database_error(exc) from exc


@router.get("", response_model=list[DatabaseSourceRead])
@require_permission("admin.database_sources", allow_admin_bypass=False)
async def list_sources(
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),  # pylint: disable=unused-argument
):
    """List database sources, hiding disabled ones by default."""
    return DatabaseSourceService.list_sources(db, include_inactive=include_inactive)


@router.get("/{source_id}", response_model=DatabaseSourceRead)
@require_permission("admin.database_sources", allow_admin_bypass=False)
async def get_source(
    source_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),  # pylint: disable=unused-argument
):
    """Fetch a database source without its credential."""
    try:
        return DatabaseSourceService.get_source(db, source_id)
    except DatabaseSourceError as exc:
        raise _database_error(exc) from exc


@router.put("/{source_id}", response_model=DatabaseSourceRead)
@require_permission("admin.database_sources", allow_admin_bypass=False)
async def update_source(
    source_id: str,
    data: DatabaseSourceUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
):
    """Update a database source, preserving the credential when none is supplied."""
    try:
        return DatabaseSourceService.update_source(db, source_id, data, get_user_email(user))
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Database source name or slug already exists") from exc
    except DatabaseSourceError as exc:
        raise _database_error(exc) from exc


@router.delete("/{source_id}", status_code=204)
@require_permission("admin.database_sources", allow_admin_bypass=False)
async def delete_source(
    source_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),  # pylint: disable=unused-argument
):
    """Delete a database source."""
    try:
        DatabaseSourceService.delete_source(db, source_id)
    except DatabaseSourceError as exc:
        raise _database_error(exc) from exc


# ---------------------------------------------------------------------------
# Query templates (Phase 2)
# ---------------------------------------------------------------------------
@router.post("/{source_id}/templates", response_model=DatabaseQueryTemplateRead, status_code=201)
@require_permission("admin.database_sources", allow_admin_bypass=False)
async def create_template(
    source_id: str,
    data: DatabaseQueryTemplateCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
):
    """Create a query template and materialize its tool when enabled."""
    try:
        return DatabaseTemplateService.create_template(db, source_id, data, get_user_email(user))
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Query template name or slug already exists") from exc
    except (DatabaseTemplateError, DatabaseSourceError) as exc:
        raise _database_error(exc) from exc


@router.get("/{source_id}/templates", response_model=list[DatabaseQueryTemplateRead])
@require_permission("admin.database_sources", allow_admin_bypass=False)
async def list_templates(
    source_id: str,
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),  # pylint: disable=unused-argument
):
    """List a source's query templates, hiding disabled ones by default."""
    try:
        return DatabaseTemplateService.list_templates(db, source_id, include_inactive=include_inactive)
    except DatabaseSourceError as exc:
        raise _database_error(exc) from exc


@router.get("/{source_id}/templates/{template_id}", response_model=DatabaseQueryTemplateRead)
@require_permission("admin.database_sources", allow_admin_bypass=False)
async def get_template(
    source_id: str,
    template_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),  # pylint: disable=unused-argument
):
    """Fetch one query template."""
    try:
        return DatabaseTemplateService.get_template(db, source_id, template_id)
    except (DatabaseTemplateError, DatabaseSourceError) as exc:
        raise _database_error(exc) from exc


@router.put("/{source_id}/templates/{template_id}", response_model=DatabaseQueryTemplateRead)
@require_permission("admin.database_sources", allow_admin_bypass=False)
async def update_template(
    source_id: str,
    template_id: str,
    data: DatabaseQueryTemplateUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
):
    """Update a query template and re-sync its materialized tool."""
    try:
        return DatabaseTemplateService.update_template(db, source_id, template_id, data, get_user_email(user))
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Query template name or slug already exists") from exc
    except (DatabaseTemplateError, DatabaseSourceError) as exc:
        raise _database_error(exc) from exc


@router.delete("/{source_id}/templates/{template_id}", status_code=204)
@require_permission("admin.database_sources", allow_admin_bypass=False)
async def delete_template(
    source_id: str,
    template_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),  # pylint: disable=unused-argument
):
    """Delete a query template and retire its materialized tool."""
    try:
        DatabaseTemplateService.delete_template(db, source_id, template_id)
    except (DatabaseTemplateError, DatabaseSourceError) as exc:
        raise _database_error(exc) from exc
