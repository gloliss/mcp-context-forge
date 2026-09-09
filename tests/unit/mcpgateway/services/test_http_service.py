# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_http_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for ``HttpService`` (PR3): service CRUD, scoping propagation to
child tools, deletion, OpenAPI import/activation, and the tool
synchronization that turns compiled operations into MCP tools.
"""

# Standard
from datetime import datetime, timezone
import itertools
import json

# Third-Party
import pytest
from sqlalchemy import select

# First-Party
from mcpgateway.db import HttpSchemaArtifact
from mcpgateway.db import HttpService as DbHttpService
from mcpgateway.db import Tool as DbTool
from mcpgateway.protocols.contracts.models import OperationDefinition
from mcpgateway.protocols.http.models import HttpParameter, HttpRequestContract, HttpResponseContract
from mcpgateway.schemas import HttpServiceCreate, HttpServiceUpdate
from mcpgateway.services.http_schema_service import _operation_to_dict
from mcpgateway.services.http_service import (
    HttpService,
    HttpServiceNameConflictError,
    HttpServiceNotFoundError,
)
from mcpgateway.utils.create_slug import slugify
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


def _service(**overrides):
    """Build a detached DbHttpService row."""
    n = next(_UNIQUE)
    defaults = {
        "name": f"crud-svc-{n}",
        "slug": f"crud-svc-{n}",
        "base_url": "http://127.0.0.1:8899",
        "visibility": "public",
        "enabled": True,
        "reachable": True,
    }
    defaults.update(overrides)
    return DbHttpService(**defaults)


def _artifact(service, *, version=1, catalog=None, content_hash=None):
    """Build a detached HttpSchemaArtifact row."""
    return HttpSchemaArtifact(
        http_service_id=service.id,
        version=version,
        source_type="openapi",
        artifact_format="json",
        content_hash=content_hash or f"hash-{version}",
        artifact_blob=b"{}",
        source_info={
            "filename": "openapi.json",
            "catalog": catalog
            if catalog is not None
            else {"operations": [_operation_to_dict(_operation())], "diagnostics": []},
        },
        is_active=False,
        created_by="admin@example.com",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _create_data(name: str, **overrides) -> HttpServiceCreate:
    """Build an HttpServiceCreate payload."""
    defaults = {"name": name, "base_url": "http://127.0.0.1:8899"}
    defaults.update(overrides)
    return HttpServiceCreate(**defaults)


async def _register(db, name: str, **overrides) -> DbHttpService:
    """Register one service through the service layer and reload the row."""
    read = await HttpService().register_service(db, _create_data(name, **overrides), user_email="admin@example.com")
    return db.get(DbHttpService, read.id)


class TestRegister:
    """Service registration."""

    async def test_register_sets_slug_and_owner(self, test_db):
        """Registration slugifies the name and records the creating user."""
        read = await HttpService().register_service(test_db, _create_data("Demo HTTP API"), user_email="admin@example.com")

        assert read.slug == slugify("Demo HTTP API")
        assert read.owner_email == "admin@example.com"
        assert read.visibility == "public"
        row = test_db.get(DbHttpService, read.id)
        assert row.base_url == "http://127.0.0.1:8899"

    async def test_register_duplicate_name_conflicts(self, test_db):
        """A second service with the same name is rejected."""
        await HttpService().register_service(test_db, _create_data("dupe-api"), user_email="admin@example.com")

        with pytest.raises(HttpServiceNameConflictError, match="already exists"):
            await HttpService().register_service(test_db, _create_data("dupe-api"), user_email="admin@example.com")


class TestListAndGet:
    """Listing and single-service retrieval."""

    async def test_list_defaults_to_enabled_only(self, test_db):
        """Listing hides disabled services unless include_inactive is set."""
        keep = await _register(test_db, "keep-api")
        await _register(test_db, "hidden-api")
        await HttpService().set_service_state(test_db, keep.id, False)

        result, _cursor = await HttpService().list_services(test_db)
        names = {item.name for item in result}
        assert "hidden-api" in names
        assert "keep-api" not in names

        result, _cursor = await HttpService().list_services(test_db, include_inactive=True)
        assert {"keep-api", "hidden-api"} <= {item.name for item in result}

    async def test_list_scopes_private_services(self, test_db):
        """A caller only sees public services plus their own private ones."""
        await _register(test_db, "public-api")
        await _register(test_db, "private-api", visibility="private")

        result, _cursor = await HttpService().list_services(test_db, user_email="alice@example.com")
        names = {item.name for item in result}
        assert "public-api" in names
        assert "private-api" not in names

    async def test_get_missing_service_raises(self, test_db):
        """A missing service raises HttpServiceNotFoundError."""
        with pytest.raises(HttpServiceNotFoundError, match="not found"):
            await HttpService().get_service(test_db, "missing-id")


class TestUpdateAndState:
    """Updates, scoping propagation, and activation state."""

    async def test_update_propagates_scoping_to_child_tools(self, test_db):
        """Visibility changes propagate to derived tools (team/owner are immutable on update)."""
        service = await _register(test_db, "scoped-api", visibility="public", team_id="team-a")
        tool = DbTool(
            original_name="scoped-api__op",
            custom_name="scoped-api__op",
            custom_name_slug="scoped-api-op",
            display_name="Op",
            url=service.base_url,
            base_url=service.base_url,
            original_description="HTTP op",
            description="HTTP op",
            integration_type="REST",
            input_schema={"type": "object"},
            annotations={},
            protocol_config={"operationRef": "GET /ping"},
            created_by="system",
            visibility="public",
            team_id="team-a",
            owner_email="admin@example.com",
            http_service_id=service.id,
            version=1,
        )
        test_db.add(tool)
        test_db.commit()

        await HttpService().update_service(
            test_db,
            service.id,
            HttpServiceUpdate(visibility="private"),
            user_email="admin@example.com",
        )

        test_db.refresh(tool)
        assert tool.visibility == "private"
        assert tool.team_id == "team-a"
        assert tool.owner_email == "admin@example.com"
        assert tool.version == 2

    async def test_update_refreshes_slug_on_rename(self, test_db):
        """Renaming a service refreshes its slug."""
        service = await _register(test_db, "old-name-api")

        read = await HttpService().update_service(test_db, service.id, HttpServiceUpdate(name="new-name-api"))

        assert read.slug == slugify("new-name-api")

    async def test_set_service_state_toggles(self, test_db):
        """State toggling flips the enabled flag."""
        service = await _register(test_db, "state-api")

        await HttpService().set_service_state(test_db, service.id, False)
        assert test_db.get(DbHttpService, service.id).enabled is False

        await HttpService().set_service_state(test_db, service.id, True)
        assert test_db.get(DbHttpService, service.id).enabled is True


class TestDelete:
    """Service deletion cascades."""

    async def test_delete_removes_service_tools_and_artifacts(self, test_db):
        """Deleting a service removes its tools and artifacts."""
        service = await _register(test_db, "delete-api")
        artifact = _artifact(service, version=1)
        tool = DbTool(
            original_name="delete-api__op",
            custom_name="delete-api__op",
            custom_name_slug="delete-api-op",
            display_name="Op",
            url=service.base_url,
            original_description="HTTP op",
            description="HTTP op",
            integration_type="REST",
            input_schema={"type": "object"},
            annotations={},
            protocol_config={"operationRef": "GET /ping"},
            created_by="system",
            http_service_id=service.id,
            http_schema_artifact_id=None,
        )
        test_db.add_all([artifact, tool])
        test_db.commit()

        await HttpService().delete_service(test_db, service.id)

        assert test_db.get(DbHttpService, service.id) is None
        assert test_db.get(HttpSchemaArtifact, artifact.id) is None
        assert test_db.get(DbTool, tool.id) is None


class TestSyncTools:
    """Tool synchronization from artifact catalogs."""

    def test_sync_creates_updates_and_soft_disables(self, test_db):
        """Sync creates new tools, updates changed ones, and soft-disables stale ones."""
        service = _service()
        test_db.add(service)
        test_db.flush()
        v1 = _artifact(
            service,
            version=1,
            catalog={"operations": [_operation_to_dict(_operation(key="GET /ping", param_schema={"type": "string"})), _operation_to_dict(_operation(key="GET /old", path_template="/old"))], "diagnostics": []},
            content_hash="hash-v1",
        )
        test_db.add(v1)
        test_db.flush()

        HttpService()._sync_tools_from_artifact(test_db, service, v1)  # pylint: disable=protected-access
        test_db.flush()

        v2 = _artifact(
            service,
            version=2,
            catalog={"operations": [_operation_to_dict(_operation(key="GET /ping", param_schema={"type": "integer"})), _operation_to_dict(_operation(key="POST /new", method="POST", path_template="/new"))], "diagnostics": []},
            content_hash="hash-v2",
        )
        test_db.add(v2)
        test_db.flush()

        HttpService()._sync_tools_from_artifact(test_db, service, v2)  # pylint: disable=protected-access
        test_db.flush()

        tools = {tool.protocol_config["operationRef"]: tool for tool in test_db.execute(select(DbTool).where(DbTool.http_service_id == service.id)).scalars().all()}
        assert set(tools) == {"GET /ping", "GET /old", "POST /new"}

        stale = tools["GET /old"]
        assert stale.enabled is False
        assert stale.deprecated is True
        assert stale.reachable is False

        updated = tools["GET /ping"]
        assert updated.http_schema_artifact_id == v2.id
        assert updated.version == 2
        assert updated.input_schema["properties"]["query"]["properties"]["q"]["type"] == "integer"
        assert updated.base_url == service.base_url
        assert updated.url == service.base_url

        created = tools["POST /new"]
        assert created.http_service_id == service.id
        assert created.http_schema_artifact_id == v2.id
        assert created.integration_type == "REST"
        assert created.base_url == service.base_url
        assert created.created_via == "http-schema-sync"
        assert created.version == 1
        assert created.enabled is True
        assert created.protocol_config["operationRef"] == "POST /new"

    def test_sync_skips_empty_catalog_over_tools(self, test_db):
        """An empty catalog never soft-disables published tools."""
        service = _service()
        test_db.add(service)
        test_db.flush()
        v1 = _artifact(service, version=1, catalog={"operations": [_operation_to_dict(_operation())], "diagnostics": []}, content_hash="hash-v1")
        test_db.add(v1)
        test_db.flush()
        HttpService()._sync_tools_from_artifact(test_db, service, v1)  # pylint: disable=protected-access
        test_db.flush()

        empty = _artifact(service, version=2, catalog={"operations": [], "diagnostics": []}, content_hash="hash-empty")
        test_db.add(empty)
        test_db.flush()

        changed = HttpService()._sync_tools_from_artifact(test_db, service, empty)  # pylint: disable=protected-access

        assert changed == []
        tool = test_db.execute(select(DbTool).where(DbTool.http_service_id == service.id)).scalar_one()
        assert tool.enabled is True


class TestImportAndActivate:
    """Full import/activate lifecycle through the service layer."""

    async def test_import_activate_creates_tool(self, test_db):
        """Importing with activation stores the artifact and generates tools."""
        service = await _register(test_db, "import-api")

        artifact = await HttpService().import_schema(test_db, service.id, _payload(), "openapi.json", "admin@example.com", activate=True)

        assert artifact.is_active is True
        test_db.refresh(service)
        assert service.active_artifact_id == artifact.id
        assert service.operation_count == 1

        tool = test_db.execute(select(DbTool).where(DbTool.http_service_id == service.id)).scalar_one()
        assert tool.protocol_config["operationRef"] == "GET /ping"
        assert tool.http_schema_artifact_id == artifact.id

        schemas = await HttpService().list_schemas(test_db, service.id)
        assert [schema.id for schema in schemas] == [artifact.id]

    async def test_activate_schema_rejects_foreign_artifact(self, test_db):
        """Activating another service's artifact raises."""
        service = await _register(test_db, "activate-api")
        other = await _register(test_db, "other-api")
        artifact = _artifact(other, version=1)
        test_db.add(artifact)
        test_db.commit()

        with pytest.raises(HttpServiceError, match="not found for this service"):
            await HttpService().activate_schema(test_db, service.id, artifact.id)

    async def test_diff_schemas_rejects_foreign_artifact(self, test_db):
        """Diffing requires both artifacts to belong to the service."""
        service = await _register(test_db, "diff-api")
        other = await _register(test_db, "diff-other-api")
        left = _artifact(service, version=1)
        right = _artifact(other, version=1)
        test_db.add_all([left, right])
        test_db.commit()

        with pytest.raises(HttpServiceError, match="must belong to the requested service"):
            await HttpService().diff_schemas(test_db, service.id, left.id, right.id)
