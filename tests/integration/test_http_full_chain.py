# -*- coding: utf-8 -*-
"""Location: ./tests/integration/test_http_full_chain.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Integration tests for the full HTTP Registry chain (PR3): import →
candidate → diff → preview → activate → tool sync → MCP invoke, plus
health checks, YAML scanning, external-$ref materialisation, and SSRF
rejection.  Uses a real stdlib HTTP test server
(``tests/http_test_server/server.py``) — no mocks on the transport.
"""

# Standard
import asyncio
import os
import subprocess
import sys
import time
import uuid
from urllib.request import urlopen

# Third-Party
import pytest
from sqlalchemy import select

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import HttpSchemaArtifact
from mcpgateway.db import HttpService as DbHttpService
from mcpgateway.db import Tool as DbTool
from mcpgateway.schemas import HttpServiceCreate
from mcpgateway.services.http_monitoring_service import get_http_monitoring_service
from mcpgateway.services.http_registry_service import HttpRegistryService
from mcpgateway.services.http_service import HttpService
from mcpgateway.services.http_yaml_service import HttpYamlScanService
from mcpgateway.services.tool_service import ToolService
from mcpgateway.utils.http_validation import HttpServiceError

TEST_SERVER_DIR = os.path.join(os.path.dirname(__file__), "..", "http_test_server")

# One loop for the whole module — see ``_run``.
_LOOP = asyncio.new_event_loop()


def _free_port():
    """Return an ephemeral free TCP port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


def _wait_for_server(base_url, timeout=15):
    """Wait until the test server answers /health."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(f"{base_url}/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # pylint: disable=broad-except
            time.sleep(0.2)
    return False


def _run(coro):
    """Run a coroutine synchronously from sync or async tests.

    The whole module shares one event loop: production singletons
    (MetricsBufferService and friends) hold loop-bound asyncio primitives,
    and a fresh ``asyncio.run`` per call would rebind them to a new loop on
    every call and crash the lazily-started flush task.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _LOOP.run_until_complete(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, coro).result()


# ══════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def http_server():
    """Start the stdlib HTTP test server on an ephemeral port."""
    port = _free_port()
    with subprocess.Popen(
        [sys.executable, os.path.join(TEST_SERVER_DIR, "server.py"), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        base_url = f"http://127.0.0.1:{port}"
        try:
            if not _wait_for_server(base_url):
                proc.kill()
                pytest.fail("HTTP test server did not start within timeout")
            yield base_url
        finally:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _http_settings(monkeypatch, test_db):
    """Enable the HTTP registry features and route internal DB sessions to test_db.

    ``expire_on_commit`` is disabled so attribute access after invoke_tool's
    internal commit/close behaves like production: there, the invoke session
    is never the session that commits mid-invoke, so attributes stay loaded
    on the detached instance.
    """
    monkeypatch.setattr(settings, "mcpgateway_http_registry_enabled", True)
    monkeypatch.setattr(settings, "mcpgateway_http_health_enabled", True)
    monkeypatch.setattr(settings, "mcpgateway_http_yaml_scan_enabled", True)
    monkeypatch.setattr(settings, "ssrf_allow_localhost", True)
    monkeypatch.setattr(settings, "ssrf_allow_private_networks", True)
    test_db.expire_on_commit = False

    from contextlib import contextmanager

    import mcpgateway.services.http_monitoring_service as mon_mod
    import mcpgateway.services.http_yaml_service as yaml_mod
    import mcpgateway.services.tool_service as ts_mod

    @contextmanager
    def _use_test_db():
        """Yield the shared test session so internal sessions see the same data."""
        try:
            yield test_db
            test_db.commit()
        except Exception:
            test_db.rollback()
            raise

    for module in (mon_mod, yaml_mod, ts_mod):
        monkeypatch.setattr(module, "fresh_db_session", _use_test_db)


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════


def _create_service(db, base_url, name=None) -> DbHttpService:
    """Register one HTTP service and return its DB row."""
    service_name = name or f"chain-{uuid.uuid4().hex[:6]}"
    created = _run(
        HttpService().register_service(
            db,
            HttpServiceCreate(name=service_name, base_url=base_url, visibility="public"),
            user_email="admin@example.com",
        )
    )
    return db.get(DbHttpService, created.id)


def _import(db, service_id, server_url, path, filename=None, activate=True, allow_remote=True) -> HttpSchemaArtifact:
    """Download a spec from the test server and import it."""
    with urlopen(f"{server_url}{path}", timeout=10) as response:
        payload = response.read()
    return _run(
        HttpService().import_schema(
            db,
            service_id,
            payload,
            filename or path.rsplit("/", 1)[-1],
            "admin@example.com",
            activate=activate,
            allow_remote=allow_remote,
        )
    )


def _tool_by_ref(db, service_id, operation_ref) -> DbTool:
    """Resolve one generated tool by its protocol_config.operationRef."""
    for tool in db.execute(select(DbTool).where(DbTool.http_service_id == service_id)).scalars():
        if (tool.protocol_config or {}).get("operationRef") == operation_ref:
            return tool
    return None


def _invoke(db, tool, arguments):
    """Invoke a generated tool through the MCP invoke path.

    The MCP wire name is the ``name`` column (``custom_name`` slugified by
    the ``set_custom_name_and_slug`` before_insert hook); call_tool sends
    that name back verbatim, and ``_load_invocable_tools`` matches on it.
    """
    return _run(ToolService().invoke_tool(db, tool.name, arguments, skip_pre_invoke=True))


class TestHttpFullChain:
    """End-to-end registry lifecycle against the real HTTP test server."""

    def test_import_candidate_without_activation(self, test_db, http_server):
        """activate=False imports a candidate without publishing tools."""
        service = _create_service(test_db, http_server)
        artifact = _import(test_db, service.id, http_server, "/openapi.json", activate=False)

        stored = test_db.get(DbHttpService, service.id)
        assert stored.candidate_artifact_id == artifact.id
        assert stored.active_artifact_id is None
        assert stored.operation_count > 0
        assert test_db.execute(select(DbTool).where(DbTool.http_service_id == service.id)).scalars().all() == []

    def test_import_activate_creates_tools(self, test_db, http_server):
        """activate=True publishes one tool per compiled operation."""
        service = _create_service(test_db, http_server)
        artifact = _import(test_db, service.id, http_server, "/openapi.json", activate=True)

        stored = test_db.get(DbHttpService, service.id)
        assert stored.active_artifact_id == artifact.id
        assert stored.candidate_artifact_id is None
        assert stored.schema_drift is False
        tools = test_db.execute(select(DbTool).where(DbTool.http_service_id == service.id)).scalars().all()
        expected_refs = {
            "GET /v1/pets",
            "POST /v1/pets",
            "GET /v1/pets/{petId}",
            "DELETE /v1/pets/{petId}",
            "HEAD /v1/pets/{petId}/head",
            "POST /v1/pets/echo-form",
            "POST /v1/pets/photo",
            "GET /v1/external-redirect",
        }
        assert {tool.protocol_config["operationRef"] for tool in tools} == expected_refs
        for tool in tools:
            assert tool.integration_type == "REST"
            assert tool.base_url == http_server
            assert tool.enabled is True

    def test_reimport_same_content_reuses_artifact(self, test_db, http_server):
        """Content-addressed import is idempotent."""
        service = _create_service(test_db, http_server)
        first = _import(test_db, service.id, http_server, "/openapi.json", activate=True)
        second = _import(test_db, service.id, http_server, "/openapi.json", activate=True)

        artifacts = test_db.execute(select(HttpSchemaArtifact).where(HttpSchemaArtifact.http_service_id == service.id)).scalars().all()
        assert len(artifacts) == 1
        assert second.id == first.id

    def test_invoke_get_query(self, test_db, http_server):
        """GET with query parameters decodes the JSON array response."""
        service = _create_service(test_db, http_server)
        _import(test_db, service.id, http_server, "/openapi.json", activate=True)
        tool = _tool_by_ref(test_db, service.id, "GET /v1/pets")

        result = _invoke(test_db, tool, {"query": {"limit": 5}})
        assert result.is_error is False
        assert "Fido" in result.content[0].text

    def test_invoke_post_json(self, test_db, http_server):
        """POST with a JSON body creates a pet (201)."""
        service = _create_service(test_db, http_server)
        _import(test_db, service.id, http_server, "/openapi.json", activate=True)
        tool = _tool_by_ref(test_db, service.id, "POST /v1/pets")

        result = _invoke(test_db, tool, {"body": {"name": "Rex", "tag": "puppy"}})
        assert result.is_error is False
        assert "Rex" in result.content[0].text

    def test_invoke_get_path(self, test_db, http_server):
        """Path parameters render into the URL template."""
        service = _create_service(test_db, http_server)
        _import(test_db, service.id, http_server, "/openapi.json", activate=True)
        tool = _tool_by_ref(test_db, service.id, "GET /v1/pets/{petId}")

        result = _invoke(test_db, tool, {"path": {"petId": 1}})
        assert result.is_error is False
        assert "Fido" in result.content[0].text

    def test_invoke_delete_204(self, test_db, http_server):
        """204 No Content decodes to the bodyless success text."""
        service = _create_service(test_db, http_server)
        _import(test_db, service.id, http_server, "/openapi.json", activate=True)
        tool = _tool_by_ref(test_db, service.id, "DELETE /v1/pets/{petId}")

        result = _invoke(test_db, tool, {"path": {"petId": 1}})
        assert result.is_error is False
        assert "No Content" in result.content[0].text

    def test_invoke_head(self, test_db, http_server):
        """HEAD responses decode to the bodyless success text."""
        service = _create_service(test_db, http_server)
        _import(test_db, service.id, http_server, "/openapi.json", activate=True)
        tool = _tool_by_ref(test_db, service.id, "HEAD /v1/pets/{petId}/head")

        result = _invoke(test_db, tool, {"path": {"petId": 1}})
        assert result.is_error is False
        assert "No Content" in result.content[0].text

    def test_invoke_form(self, test_db, http_server):
        """Form-encoded bodies round-trip through the form codec."""
        service = _create_service(test_db, http_server)
        _import(test_db, service.id, http_server, "/openapi.json", activate=True)
        tool = _tool_by_ref(test_db, service.id, "POST /v1/pets/echo-form")

        result = _invoke(test_db, tool, {"body": {"name": "Bella"}})
        assert result.is_error is False
        assert "Bella" in result.content[0].text

    def test_invoke_multipart(self, test_db, http_server):
        """Multipart bodies round-trip through the multipart codec."""
        service = _create_service(test_db, http_server)
        _import(test_db, service.id, http_server, "/openapi.json", activate=True)
        tool = _tool_by_ref(test_db, service.id, "POST /v1/pets/photo")

        result = _invoke(test_db, tool, {"body": {"petId": 1, "caption": "a fine dog"}})
        assert result.is_error is False
        assert "received" in result.content[0].text

    def test_invoke_blocks_redirect_to_metadata_service(self, test_db, http_server, monkeypatch):
        """A redirect hop to a blocked destination raises the URL-policy error."""
        monkeypatch.setattr(settings, "ssrf_allow_private_networks", False)
        service = _create_service(test_db, http_server)
        _import(test_db, service.id, http_server, "/openapi.json", activate=True)
        tool = _tool_by_ref(test_db, service.id, "GET /v1/external-redirect")

        from mcpgateway.services.tool_service import ToolInvocationError

        with pytest.raises(ToolInvocationError, match="Outbound URL blocked by URL policy"):
            _invoke(test_db, tool, {})

    def test_schema_v2_drift_diff_preview_activate(self, test_db, http_server):
        """v2 import drifts to candidate; diff/preview then activate sync tools."""
        service = _create_service(test_db, http_server)
        active = _import(test_db, service.id, http_server, "/openapi.json", activate=True)
        candidate = _import(test_db, service.id, http_server, "/openapi-v2.json", activate=False)

        stored = test_db.get(DbHttpService, service.id)
        assert stored.candidate_artifact_id == candidate.id
        assert stored.active_artifact_id == active.id
        assert stored.schema_drift is True

        diff = _run(HttpService().diff_schemas(test_db, service.id, active.id, candidate.id))
        assert "GET /v1/pets/filter" in diff.added_operations
        assert "POST /v1/pets/echo-form" in diff.removed_operations

        preview = HttpRegistryService.build_sync_preview(test_db, service.id, candidate.id)
        assert "GET /v1/pets/filter" in preview.added_tools
        assert "POST /v1/pets/echo-form" in preview.disabled_tools

        activated = _run(HttpService().activate_schema(test_db, service.id, candidate.id))
        assert activated.id == candidate.id
        stored = test_db.get(DbHttpService, service.id)
        assert stored.active_artifact_id == candidate.id
        assert stored.schema_drift is False

        filter_tool = _tool_by_ref(test_db, service.id, "GET /v1/pets/filter")
        assert filter_tool is not None and filter_tool.enabled is True
        form_tool = _tool_by_ref(test_db, service.id, "POST /v1/pets/echo-form")
        assert form_tool is not None
        assert form_tool.enabled is False and form_tool.deprecated is True

    def test_health_check_healthy(self, test_db, http_server):
        """A live server reports healthy and reachable."""
        service = _create_service(test_db, http_server)

        result = _run(get_http_monitoring_service().check_service(service.id))
        assert result["status"] == "healthy"
        assert result["healthy"] is True
        stored = test_db.get(DbHttpService, service.id)
        assert stored.reachable is True
        assert stored.consecutive_failures == 0

    def test_health_check_unreachable(self, test_db):
        """A closed port reports unreachable with degraded status."""
        service = _create_service(test_db, "http://127.0.0.1:1")

        result = _run(get_http_monitoring_service().check_service(service.id))
        assert result["status"] in ("degraded", "unhealthy")
        assert result["healthy"] is False
        stored = test_db.get(DbHttpService, service.id)
        assert stored.reachable is False
        assert stored.consecutive_failures == 1

    def test_health_check_ssrf_blocked(self, test_db, monkeypatch):
        """A metadata-service base URL is refused by SSRF validation."""
        monkeypatch.setattr(settings, "ssrf_allow_localhost", False)
        monkeypatch.setattr(settings, "ssrf_allow_private_networks", False)
        service = _create_service(test_db, "http://169.254.169.254/")

        result = _run(get_http_monitoring_service().check_service(service.id))
        assert result["healthy"] is False
        stored = test_db.get(DbHttpService, service.id)
        assert stored.reachable is False
        assert stored.last_health_error

    def test_external_ref_materialized_and_gated(self, test_db, http_server):
        """allow_remote materialises external $refs; the gate rejects them otherwise."""
        service = _create_service(test_db, http_server)
        _import(test_db, service.id, http_server, "/openapi-extref.json", activate=True, allow_remote=True)
        tool = _tool_by_ref(test_db, service.id, "GET /v1/pets/external")
        assert tool is not None

        result = _invoke(test_db, tool, {})
        assert result.is_error is False
        assert "Fido" in result.content[0].text

        gated = _create_service(test_db, http_server)
        with pytest.raises(HttpServiceError):
            _import(test_db, gated.id, http_server, "/openapi-extref.json", activate=False, allow_remote=False)

    def test_yaml_scan_imports_remote_source(self, test_db, http_server, tmp_path, monkeypatch):
        """The YAML scanner imports an http(s) source through SSRF-checked download."""
        name = f"chain-yaml-{uuid.uuid4().hex[:6]}"
        manifest = tmp_path / "http-service.yaml"
        manifest.write_text(
            "\n".join(
                [
                    "apiVersion: contextforge/v1alpha1",
                    "kind: HttpService",
                    "metadata:",
                    f"  name: {name}",
                    "  visibility: private",
                    "spec:",
                    "  baseUrl: " + http_server,
                    "  discovery:",
                    "    mode: manual",
                    "    source:",
                    f"      url: {http_server}/openapi.json",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(settings, "mcpgateway_http_yaml_scan_roots", [str(tmp_path)])
        monkeypatch.setattr("mcpgateway.services.http_yaml_service.is_primary_worker", lambda: True)

        result = _run(HttpYamlScanService().scan(test_db))
        assert result["created"] == [name] and result["errors"] == []
        service = test_db.execute(select(DbHttpService).where(DbHttpService.name == name)).scalar_one()
        assert service.active_artifact_id is not None
        tools = test_db.execute(select(DbTool).where(DbTool.http_service_id == service.id)).scalars().all()
        assert len(tools) == 8
