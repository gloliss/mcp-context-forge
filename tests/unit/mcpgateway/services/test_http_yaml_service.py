# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_http_yaml_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for ``HttpYamlScanService`` (PR3, design §21): strict manifest
allowlisting, the §67 secret ban, source resolution, and the create/
candidate/skip upsert semantics driven from a real temp scan root.
"""

# Standard
import asyncio
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

# Third-Party
import pytest
from sqlalchemy import select

# First-Party
from mcpgateway.common.validators import SecurityValidator
from mcpgateway.config import settings
from mcpgateway.db import HttpSchemaArtifact
from mcpgateway.db import HttpService as DbHttpService
from mcpgateway.services.http_yaml_service import HttpYamlScanService
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
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                }
            }
        }
    },
}

_MANIFEST = """apiVersion: contextforge/v1alpha1
kind: HttpService
metadata:
  name: {name}
  visibility: private
spec:
  baseUrl: http://petstore.example.com/v1
  discovery:
    mode: manual
    source:
      url: {source}
"""


def _write_scan_root(tmp_path: Path, name: str, extra_manifest: str = "") -> Path:
    """Create a scan root holding one manifest and its OpenAPI source."""
    root = tmp_path / f"root-{name}"
    root.mkdir()
    (root / "openapi.json").write_text(json.dumps(_OPENAPI_DOC), encoding="utf-8")
    manifest = _MANIFEST.format(name=name, source="openapi.json") + extra_manifest
    (root / "http-service.yaml").write_text(manifest, encoding="utf-8")
    return root


def _enable_scan(monkeypatch, root: Path) -> None:
    """Enable scanning on a single temporary root as the primary worker."""
    monkeypatch.setattr(settings, "mcpgateway_http_yaml_scan_enabled", True)
    monkeypatch.setattr(settings, "mcpgateway_http_yaml_scan_roots", [str(root)])
    monkeypatch.setattr("mcpgateway.services.http_yaml_service.is_primary_worker", lambda: True)


class _FakeClient:
    """Minimal async-client fake returning canned bytes."""

    def __init__(self, payload: bytes) -> None:
        """Store the canned payload."""
        self._payload = payload

    async def __aenter__(self) -> "_FakeClient":
        """Enter the async context."""
        return self

    async def __aexit__(self, *args) -> bool:
        """Exit the async context without closing anything."""
        return False

    async def get(self, url: str) -> SimpleNamespace:
        """Return a canned 200 response."""
        return SimpleNamespace(status_code=200, content=self._payload, raise_for_status=lambda: None)


# --- manifest validation ---


def test_manifest_rejects_unknown_fields(tmp_path):
    """Unknown keys at any allowlisted level are hard errors."""
    root = _write_scan_root(tmp_path, f"yaml-svc-{next(_UNIQUE)}", extra_manifest="\n  health:\n    interval: 60\n    bogus: 1\n")
    manifest_path = root / "http-service.yaml"

    with pytest.raises(HttpServiceError, match="Unknown http-service.yaml fields at spec.health"):
        HttpYamlScanService._load_manifest(manifest_path)  # pylint: disable=protected-access


def test_manifest_rejects_wrong_api_version_and_kind(tmp_path):
    """apiVersion/kind are pinned to the v1alpha1 HttpService contract."""
    root = _write_scan_root(tmp_path, f"yaml-svc-{next(_UNIQUE)}")
    manifest_path = root / "http-service.yaml"
    source = manifest_path.read_text(encoding="utf-8")

    manifest_path.write_text(source.replace("contextforge/v1alpha1", "contextforge/v1beta1"), encoding="utf-8")
    with pytest.raises(HttpServiceError, match="apiVersion must be"):
        HttpYamlScanService._load_manifest(manifest_path)  # pylint: disable=protected-access

    manifest_path.write_text(source.replace("kind: HttpService", "kind: GrpcService"), encoding="utf-8")
    with pytest.raises(HttpServiceError, match="kind must be"):
        HttpYamlScanService._load_manifest(manifest_path)  # pylint: disable=protected-access


def test_manifest_rejects_secret_keys_and_authref(tmp_path):
    """§67 banned key names and authRef are rejected at any nesting level."""
    root = _write_scan_root(tmp_path, f"yaml-svc-{next(_UNIQUE)}")
    manifest_path = root / "http-service.yaml"
    source = manifest_path.read_text(encoding="utf-8")

    manifest_path.write_text(source + "\n  validation:\n    apiKey: plaintext\n", encoding="utf-8")
    with pytest.raises(HttpServiceError, match="secret keys are forbidden"):
        HttpYamlScanService._load_manifest(manifest_path)  # pylint: disable=protected-access

    manifest_path.write_text(source + "\n  validation:\n    authRef: my-vault-ref\n", encoding="utf-8")
    with pytest.raises(HttpServiceError, match="SecretRef 尚不支持"):
        HttpYamlScanService._load_manifest(manifest_path)  # pylint: disable=protected-access


def test_manifest_rejects_high_entropy_tokens_but_accepts_prose(tmp_path):
    """Token-like high-entropy string values are rejected; long prose is not."""
    root = _write_scan_root(tmp_path, f"yaml-svc-{next(_UNIQUE)}")
    manifest_path = root / "http-service.yaml"
    source = manifest_path.read_text(encoding="utf-8")

    manifest_path.write_text(source + f"\n  validation:\n    fallback: {'0123456789abcdef0123456789abcdef'}\n", encoding="utf-8")
    with pytest.raises(HttpServiceError, match="high-entropy token-like values are forbidden"):
        HttpYamlScanService._load_manifest(manifest_path)  # pylint: disable=protected-access

    manifest_path.write_text(
        source + "\n  validation:\n    note: This is a long but ordinary prose sentence with spaces.\n",
        encoding="utf-8",
    )
    data = HttpYamlScanService._load_manifest(manifest_path)  # pylint: disable=protected-access
    assert data["spec"]["validation"]["note"].startswith("This is a long")


# --- source resolution ---


def test_local_source_cannot_escape_scan_root(tmp_path):
    """Relative source paths must resolve inside the allowed scan root."""
    root = _write_scan_root(tmp_path, f"yaml-svc-{next(_UNIQUE)}")
    manifest_path = root / "http-service.yaml"
    data = HttpYamlScanService._load_manifest(manifest_path)  # pylint: disable=protected-access
    data["spec"]["discovery"]["source"]["url"] = "../outside.json"

    with pytest.raises(HttpServiceError, match="escapes its allowed scan root"):
        asyncio.run(HttpYamlScanService._load_source(data, manifest_path, root.resolve()))  # pylint: disable=protected-access


async def test_remote_source_downloads_with_ssrf_validation(tmp_path, monkeypatch):
    """http(s) sources go through SSRF validation and the isolated client."""
    root = _write_scan_root(tmp_path, f"yaml-svc-{next(_UNIQUE)}")
    manifest_path = root / "http-service.yaml"
    payload = json.dumps(_OPENAPI_DOC).encode()
    monkeypatch.setattr(SecurityValidator, "validate_url", staticmethod(lambda url, label: None))
    monkeypatch.setattr("mcpgateway.services.http_yaml_service.get_isolated_http_client", lambda timeout=None, follow_redirects=False: _FakeClient(payload))
    manifest_path.write_text(_MANIFEST.format(name=f"yaml-svc-{next(_UNIQUE)}", source="https://specs.example.com/petstore.json"), encoding="utf-8")
    data = HttpYamlScanService._load_manifest(manifest_path)  # pylint: disable=protected-access

    loaded, filename = await HttpYamlScanService._load_source(data, manifest_path, root.resolve())  # pylint: disable=protected-access
    assert loaded == payload
    assert filename == "petstore.json"


def test_local_source_rejects_unsupported_suffix(tmp_path):
    """Only OpenAPI JSON/YAML/ZIP source files are accepted."""
    root = _write_scan_root(tmp_path, f"yaml-svc-{next(_UNIQUE)}")
    manifest_path = root / "http-service.yaml"
    (root / "spec.txt").write_text("not openapi", encoding="utf-8")
    manifest_path.write_text(_MANIFEST.format(name=f"yaml-svc-{next(_UNIQUE)}", source="spec.txt"), encoding="utf-8")
    data = HttpYamlScanService._load_manifest(manifest_path)  # pylint: disable=protected-access

    with pytest.raises(HttpServiceError, match="must end with .json, .yaml, .yml or .zip"):
        asyncio.run(HttpYamlScanService._load_source(data, manifest_path, root.resolve()))  # pylint: disable=protected-access


# --- scan upsert semantics ---


async def test_scan_creates_service_with_active_artifact(tmp_path, test_db, monkeypatch):
    """First scan registers the service and activates the imported artifact."""
    name = f"yaml-svc-{next(_UNIQUE)}"
    root = _write_scan_root(tmp_path, name)
    _enable_scan(monkeypatch, root)

    result = await HttpYamlScanService().scan(test_db)
    assert result["created"] and result["errors"] == []
    service = test_db.execute(select(DbHttpService).where(DbHttpService.name == name)).scalar_one()
    assert service.discovery_config["manifest_path"] == str((root / "http-service.yaml").resolve())
    artifacts = test_db.execute(select(HttpSchemaArtifact).where(HttpSchemaArtifact.http_service_id == service.id)).scalars().all()
    assert len(artifacts) == 1
    assert artifacts[0].is_active is True
    assert service.candidate_artifact_id is None
    assert service.schema_drift is False


async def test_scan_skips_unchanged_manifest(tmp_path, test_db, monkeypatch):
    """An unchanged manifest hash short-circuits before any import."""
    name = f"yaml-svc-{next(_UNIQUE)}"
    root = _write_scan_root(tmp_path, name)
    _enable_scan(monkeypatch, root)
    service = HttpYamlScanService()
    await service.scan(test_db)

    result = await service.scan(test_db)
    assert result["skipped"] and result["created"] == [] and result["updated"] == []
    stored = test_db.execute(select(DbHttpService).where(DbHttpService.name == name)).scalar_one()
    assert len(test_db.execute(select(HttpSchemaArtifact).where(HttpSchemaArtifact.http_service_id == stored.id)).scalars().all()) == 1


async def test_scan_imports_candidate_on_manifest_change(tmp_path, test_db, monkeypatch):
    """A changed manifest imports a candidate with schema_drift, not activation."""
    name = f"yaml-svc-{next(_UNIQUE)}"
    root = _write_scan_root(tmp_path, name)
    _enable_scan(monkeypatch, root)
    service = HttpYamlScanService()
    await service.scan(test_db)
    stored = test_db.execute(select(DbHttpService).where(DbHttpService.name == name)).scalar_one()
    active_id = stored.active_artifact_id

    changed = dict(_OPENAPI_DOC)
    changed["paths"] = dict(_OPENAPI_DOC["paths"], **{"/status": {"get": {"responses": {"200": {"description": "ok"}}}}})
    (root / "openapi.json").write_text(json.dumps(changed), encoding="utf-8")
    (root / "http-service.yaml").write_text(
        _MANIFEST.format(name=name, source="openapi.json") + "\n  operations:\n    include:\n      - GET /ping\n",
        encoding="utf-8",
    )
    result = await service.scan(test_db)
    assert result["updated"] and result["errors"] == []
    stored = test_db.execute(select(DbHttpService).where(DbHttpService.id == stored.id)).scalar_one()
    assert stored.candidate_artifact_id is not None
    assert stored.active_artifact_id == active_id
    assert stored.schema_drift is True
    versions = test_db.execute(select(HttpSchemaArtifact).where(HttpSchemaArtifact.http_service_id == stored.id)).scalars().all()
    assert len(versions) == 2
    active_versions = [artifact for artifact in versions if artifact.is_active]
    assert len(active_versions) == 1 and active_versions[0].id == active_id


async def test_scan_collects_errors_without_crashing(tmp_path, test_db, monkeypatch):
    """Broken manifests land in the errors summary and leave no service behind."""
    name = f"yaml-svc-{next(_UNIQUE)}"
    root = _write_scan_root(tmp_path, name, extra_manifest="\n  health:\n    interval: -5\n")
    _enable_scan(monkeypatch, root)

    result = await HttpYamlScanService().scan(test_db)
    assert result["created"] == [] and len(result["errors"]) == 1
    assert "positive integer" in result["errors"][0]["error"]
    assert test_db.execute(select(DbHttpService).where(DbHttpService.name == name)).scalar_one_or_none() is None
