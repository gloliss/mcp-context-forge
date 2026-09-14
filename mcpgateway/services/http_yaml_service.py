# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/http_yaml_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Primary-worker manifest scanner for HTTP Registry services (PR3, design §21).

Mirrors ``proto_scan_service``: scan only explicitly configured roots and
idempotently import ``http-service.yaml`` manifests.  The first import
activates the artifact; a subsequent manifest change imports the OpenAPI
source as a *candidate* (schema_drift=True) without auto-activating it.

Manifests are strictly allowlisted (§21) and secret-free (§67): banned key
names, ``authRef``, and high-entropy token-like string values are rejected
before any network or database access.

Difference vs the design's storage table (§11.1): ``http_services`` has no
manifest path/hash columns, so the scanner records them in
``discovery_config`` as ``manifest_path`` and ``manifest_hash``.
"""

# Standard
import asyncio
from hashlib import sha256
import json
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

# Third-Party
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
import yaml

# First-Party
from mcpgateway._security_constants import calculate_entropy
from mcpgateway.common.validators import SecurityValidator
from mcpgateway.config import settings
from mcpgateway.db import EmailTeam, fresh_db_session
from mcpgateway.db import HttpService as DbHttpService
from mcpgateway.schemas import HttpServiceCreate, HttpServiceUpdate
from mcpgateway.services.http_client_service import get_isolated_http_client
from mcpgateway.services.http_service import HttpService
from mcpgateway.protocols.http.activation_gate import ACTIVATION_GATES
from mcpgateway.utils.http_validation import HttpServiceError
from mcpgateway.utils.primary_worker import is_primary_worker

_API_VERSION = "contextforge/v1alpha1"
_MANIFEST_KIND = "HttpService"

# Strict per-level field allowlists (§21).  Unknown keys are errors.
_TOP_LEVEL_FIELDS = {"apiVersion", "kind", "metadata", "spec"}
_METADATA_FIELDS = {"name", "description", "team", "visibility", "tags"}
_SPEC_FIELDS = {"baseUrl", "discovery", "runtime", "validation", "testing", "operations", "health"}
_DISCOVERY_FIELDS = {"mode", "source", "references"}
_SOURCE_FIELDS = {"url"}
_REFERENCES_FIELDS = {"allowRemote"}
_RUNTIME_FIELDS = {"http2", "redirects", "timeout", "limits"}
_REDIRECTS_FIELDS = {"follow"}
_OPERATIONS_FIELDS = {"include", "manual"}
# §27 manual XML operations: an operation declared outright rather than
# discovered from an OpenAPI document, optionally bound to an XSD.
_MANUAL_OPERATION_FIELDS = {"name", "method", "path", "body", "response"}
_MANUAL_BODY_FIELDS = {"mediaType", "xsd"}
_MANUAL_XSD_FIELDS = {"schema", "file", "schema11"}
_MANUAL_METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
# Filename the synthesized document is imported under.  It must carry a
# suffix the artifact pipeline accepts, and be stable so re-scans diff cleanly.
_MANUAL_ARTIFACT_FILENAME = "manual-operations.json"
_HEALTH_FIELDS = {"enabled", "interval", "timeout", "failureThreshold"}
_TESTING_FIELDS = {"allowMutatingOperations"}

_ALLOWED_SUFFIXES = (".json", ".yaml", ".yml", ".zip")

# §67: key names that may never appear in a manifest at any nesting level
# (compared on their normalized form, so apiKey/API-KEY/api_key all match).
_SECRET_KEY_PATTERNS = (
    "password",
    "token",
    "apikey",
    "apisecret",
    "clientsecret",
    "privatekey",
    "secret",
    "authorization",
)
# §67: string values that look like embedded tokens are rejected.  The
# charset floors keep ordinary prose out: a long English string fails the
# base64 rule below 4.5 bits/char, while a 32-char random hex token
# (~3.98 bits/char) is caught by the second rule.
_BASE64_CHARSET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
_HEX_CHARSET = frozenset("0123456789abcdefABCDEF")

# Download timeout for http(s) manifest sources (no dedicated setting).
_SOURCE_FETCH_TIMEOUT = 30.0

logger = logging.getLogger(__name__)


def _normalized_key(key: Any) -> str:
    """Normalize a YAML key for secret-name comparison."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _check_not_secret_key(key: Any, path: str) -> None:
    """Reject §67-banned secret keys at any nesting level.

    Args:
        key: The YAML mapping key to check
        path: Human-readable location for error messages

    Raises:
        HttpServiceError: If the key is banned or an unsupported ``authRef``
    """
    normalized = _normalized_key(key)
    if normalized == "authref":
        raise HttpServiceError(f"{path}: SecretRef 尚不支持")
    if normalized in _SECRET_KEY_PATTERNS:
        raise HttpServiceError(f"{path}: secret keys are forbidden in http-service.yaml manifests")


def _check_not_secret_value(value: str, path: str) -> None:
    """Reject high-entropy token-like string values (§67).

    Args:
        value: The string value to check
        path: Human-readable location for error messages

    Raises:
        HttpServiceError: If the value matches a token charset and length
            floor with entropy above the threshold
    """
    charset = set(value)
    if len(value) >= 20 and charset <= _BASE64_CHARSET and calculate_entropy(value) >= 4.5:
        raise HttpServiceError(f"{path}: high-entropy token-like values are forbidden; use a SecretRef instead")
    if len(value) >= 32 and charset <= _HEX_CHARSET and calculate_entropy(value) >= 3.8:
        raise HttpServiceError(f"{path}: high-entropy token-like values are forbidden; use a SecretRef instead")


def _reject_secrets(node: Any, path: str = "manifest") -> None:
    """Walk every mapping key and string value enforcing the §67 secret ban.

    Args:
        node: The YAML subtree to walk
        path: Human-readable location for error messages

    Raises:
        HttpServiceError: On any banned key or token-like value
    """
    if isinstance(node, dict):
        for key, value in node.items():
            _check_not_secret_key(key, f"{path}.{key}")
            _reject_secrets(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _reject_secrets(item, f"{path}[{index}]")
    elif isinstance(node, str):
        _check_not_secret_value(node, path)


def _mapping(value: Any, path: str, fields: set[str]) -> dict[str, Any]:
    """Require a mapping whose keys come from the strict allowlist.

    Args:
        value: The YAML subtree to validate
        path: Human-readable location for error messages
        fields: Allowed keys for this level

    Returns:
        The validated mapping

    Raises:
        HttpServiceError: If the value is not a mapping or has unknown keys
    """
    if not isinstance(value, dict):
        raise HttpServiceError(f"{path} must be a mapping")
    unknown = set(value) - fields
    if unknown:
        raise HttpServiceError(f"Unknown http-service.yaml fields at {path}: {', '.join(sorted(str(key) for key in unknown))}")
    return value


class HttpYamlScanService:
    """Scan only configured roots and idempotently import HTTP service manifests."""

    def __init__(self) -> None:
        """Initialize the scanner lifecycle state."""
        self.http = HttpService()
        self._task: asyncio.Task | None = None
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        """Start the primary-worker scan loop when explicitly enabled."""
        if not settings.mcpgateway_http_yaml_scan_enabled or not is_primary_worker():
            return
        if self._task is None or self._task.done():
            self._shutdown.clear()
            self._task = asyncio.create_task(self._scan_loop())

    async def shutdown(self) -> None:
        """Stop the scan loop without interrupting an in-flight import."""
        self._shutdown.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _scan_loop(self) -> None:
        """Run an immediate scan, then rescan at the configured interval."""
        while not self._shutdown.is_set():
            try:
                with fresh_db_session() as db:
                    result = await self.scan(db)
                if result["errors"]:
                    logger.warning("HTTP YAML scan completed with %d manifest errors", len(result["errors"]))
            except Exception:  # pylint: disable=broad-except
                logger.exception("HTTP YAML manifest scan failed")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=settings.mcpgateway_http_yaml_scan_interval)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _resolve_roots() -> list[Path]:
        """Resolve and validate the explicitly configured scan roots."""
        roots: list[Path] = []
        for configured in settings.mcpgateway_http_yaml_scan_roots:
            root = Path(configured).resolve()
            if not root.is_dir():
                raise HttpServiceError(f"Configured HTTP YAML scan root is not a directory: {configured}")
            roots.append(root)
        return roots

    @staticmethod
    def _validate_manual_operations(manual: Any) -> None:
        """Validate the ``spec.operations.manual`` declarations (§27).

        Args:
            manual: The raw ``manual`` list from the manifest.

        Raises:
            HttpServiceError: On any structural or naming violation.  The
                checks are shape-only; resolving an XSD ``file`` needs the
                scan root and happens during synthesis.
        """
        if not isinstance(manual, list) or not manual:
            raise HttpServiceError("spec.operations.manual must be a non-empty list")
        seen: set[str] = set()
        for index, entry in enumerate(manual):
            label = f"spec.operations.manual[{index}]"
            operation = _mapping(entry, label, _MANUAL_OPERATION_FIELDS)
            name = operation.get("name")
            if not isinstance(name, str) or not name.strip():
                raise HttpServiceError(f"{label}.name must be a non-empty string")
            if name in seen:
                raise HttpServiceError(f"{label}.name is duplicated: {name}")
            seen.add(name)
            method = operation.get("method")
            if not isinstance(method, str) or method.upper() not in _MANUAL_METHODS:
                raise HttpServiceError(f"{label}.method must be one of {', '.join(_MANUAL_METHODS)}")
            path = operation.get("path")
            if not isinstance(path, str) or not path.startswith("/"):
                raise HttpServiceError(f"{label}.path must be a path starting with '/'")
            if "body" not in operation and "response" not in operation:
                raise HttpServiceError(f"{label} must declare a body, a response, or both")
            for side in ("body", "response"):
                if side not in operation:
                    continue
                block = _mapping(operation[side], f"{label}.{side}", _MANUAL_BODY_FIELDS)
                media_type = block.get("mediaType")
                if not isinstance(media_type, str) or not media_type.strip():
                    raise HttpServiceError(f"{label}.{side}.mediaType must be a non-empty string")
                if "xsd" in block:
                    HttpYamlScanService._validate_manual_xsd(block["xsd"], f"{label}.{side}")

    @staticmethod
    def _validate_manual_xsd(xsd: Any, label: str) -> None:
        """Validate an XSD binding declared for one side of an operation (§27).

        Args:
            xsd: The raw ``xsd`` mapping.
            label: Dotted manifest path, for the error message.

        Raises:
            HttpServiceError: When neither or both of ``schema``/``file`` are
                given, or ``file`` looks like an escape attempt.
        """
        binding = _mapping(xsd, f"{label}.xsd", _MANUAL_XSD_FIELDS)
        # Both must be plain booleans: comparing a truthy string against a
        # bool (``'<x/>' == True``) is False, so the "both given" case would
        # slip through the check entirely.
        has_schema = bool(isinstance(binding.get("schema"), str) and binding["schema"].strip())
        has_file = bool(isinstance(binding.get("file"), str) and binding["file"].strip())
        if has_schema == has_file:
            raise HttpServiceError(f"{label}.xsd must declare exactly one of schema or file")
        if has_file:
            candidate = str(binding["file"])
            if candidate.startswith(("/", "\\")) or ".." in Path(candidate).parts:
                raise HttpServiceError(f"{label}.xsd.file must be a relative path inside the scan root")
        if "schema11" in binding and not isinstance(binding["schema11"], bool):
            raise HttpServiceError(f"{label}.xsd.schema11 must be a boolean")

    @staticmethod
    def _load_manifest(manifest_path: Path) -> dict[str, Any]:
        """Load one strict, secret-free HTTP service manifest.

        Args:
            manifest_path: Path to the ``http-service.yaml`` file

        Returns:
            The validated manifest mapping

        Raises:
            HttpServiceError: On unreadable YAML, unknown fields, wrong
                apiVersion/kind, invalid values, or §67 violations
        """
        try:
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise HttpServiceError(f"Unable to read manifest {manifest_path.name}") from exc
        if not isinstance(data, dict):
            raise HttpServiceError("http-service.yaml must contain a mapping")
        _mapping(data, "manifest", _TOP_LEVEL_FIELDS)
        _reject_secrets(data)
        if data.get("apiVersion") != _API_VERSION:
            raise HttpServiceError(f"apiVersion must be {_API_VERSION}")
        if data.get("kind") != _MANIFEST_KIND:
            raise HttpServiceError(f"kind must be {_MANIFEST_KIND}")

        metadata = _mapping(data.get("metadata"), "metadata", _METADATA_FIELDS)
        name = metadata.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HttpServiceError("metadata.name must be a non-empty string")
        if metadata.get("visibility", "private") not in {"private", "team", "public"}:
            raise HttpServiceError("metadata.visibility must be private, team, or public")
        if metadata.get("description") is not None and not isinstance(metadata["description"], str):
            raise HttpServiceError("metadata.description must be a string")
        if metadata.get("team") is not None and not isinstance(metadata["team"], str):
            raise HttpServiceError("metadata.team must be a string")
        tags = metadata.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise HttpServiceError("metadata.tags must be a list of strings")

        spec = _mapping(data.get("spec"), "spec", _SPEC_FIELDS)
        base_url = spec.get("baseUrl")
        if not isinstance(base_url, str) or not base_url.lower().startswith(("http://", "https://")):
            raise HttpServiceError("spec.baseUrl must be an http:// or https:// URL")
        # Manual XML operations (§27) are declared outright rather than
        # discovered, so they are the one case where a manifest carries no
        # discovery source at all.  Everything else still requires one.
        operations_section = spec.get("operations")
        manual_operations = (operations_section or {}).get("manual") if isinstance(operations_section, dict) else None
        has_manual = bool(manual_operations)

        if "discovery" in spec or not has_manual:
            discovery = _mapping(spec.get("discovery"), "spec.discovery", _DISCOVERY_FIELDS)
            if discovery.get("mode", "manual") != "manual":
                raise HttpServiceError("spec.discovery.mode must be manual")
            source = _mapping(discovery.get("source"), "spec.discovery.source", _SOURCE_FIELDS)
            if not isinstance(source.get("url"), str) or not source["url"].strip():
                raise HttpServiceError("spec.discovery.source.url must be a non-empty string")
            references = _mapping(discovery.get("references") or {}, "spec.discovery.references", _REFERENCES_FIELDS)
            if "allowRemote" in references and not isinstance(references["allowRemote"], bool):
                raise HttpServiceError("spec.discovery.references.allowRemote must be a boolean")
        if "runtime" in spec:
            runtime = _mapping(spec["runtime"], "spec.runtime", _RUNTIME_FIELDS)
            if "http2" in runtime and not isinstance(runtime["http2"], bool):
                raise HttpServiceError("spec.runtime.http2 must be a boolean")
            if "redirects" in runtime:
                redirects = _mapping(runtime["redirects"], "spec.runtime.redirects", _REDIRECTS_FIELDS)
                if "follow" in redirects and not isinstance(redirects["follow"], bool):
                    raise HttpServiceError("spec.runtime.redirects.follow must be a boolean")
            if "timeout" in runtime and not isinstance(runtime["timeout"], (int, float)):
                raise HttpServiceError("spec.runtime.timeout must be a number")
            if "limits" in runtime and not isinstance(runtime["limits"], dict):
                raise HttpServiceError("spec.runtime.limits must be a mapping")
        if "validation" in spec and not isinstance(spec["validation"], dict):
            raise HttpServiceError("spec.validation must be a mapping")
        # §59 Activation Gate: off/warn/strict (mutating contract tests are
        # opt-in via spec.testing.allowMutatingOperations).
        activation_gate = (spec.get("validation") or {}).get("activationGate")
        if activation_gate is not None and activation_gate not in ACTIVATION_GATES:
            raise HttpServiceError(f"spec.validation.activationGate must be one of {', '.join(ACTIVATION_GATES)}")
        if "testing" in spec:
            testing = _mapping(spec["testing"], "spec.testing", _TESTING_FIELDS)
            if "allowMutatingOperations" in testing and not isinstance(testing["allowMutatingOperations"], bool):
                raise HttpServiceError("spec.testing.allowMutatingOperations must be a boolean")
        if "operations" in spec:
            operations = _mapping(spec["operations"], "spec.operations", _OPERATIONS_FIELDS)
            include = operations.get("include") or []
            if not isinstance(include, list) or not all(isinstance(item, str) for item in include):
                raise HttpServiceError("spec.operations.include must be a list of strings")
            if "manual" in operations:
                HttpYamlScanService._validate_manual_operations(operations["manual"])
        if "health" in spec:
            health = _mapping(spec["health"], "spec.health", _HEALTH_FIELDS)
            if "enabled" in health and not isinstance(health["enabled"], bool):
                raise HttpServiceError("spec.health.enabled must be a boolean")
            for field in ("interval", "timeout", "failureThreshold"):
                value = health.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                    raise HttpServiceError(f"spec.health.{field} must be a positive integer")
        return data

    @staticmethod
    async def _load_source(manifest: dict[str, Any], manifest_path: Path, allowed_root: Path) -> tuple[bytes, str]:
        """Load the manifest's OpenAPI source from an in-root file or SSRF-checked URL.

        Args:
            manifest: The validated manifest mapping
            manifest_path: Path to the manifest (relative URLs resolve here)
            allowed_root: The configured scan root sources must stay inside

        Returns:
            Tuple of (payload bytes, derived filename)

        Raises:
            HttpServiceError: On path escape, download failure, oversized
                payload, or unsupported file suffix
        """
        source_url = str(manifest["spec"]["discovery"]["source"]["url"])
        if source_url.lower().startswith(("http://", "https://")):
            await asyncio.to_thread(SecurityValidator.validate_url, source_url, "HTTP YAML manifest source URL")
            try:
                async with get_isolated_http_client(timeout=_SOURCE_FETCH_TIMEOUT, follow_redirects=False) as client:
                    response = await client.get(source_url)
                response.raise_for_status()
                payload = response.content
            except Exception as exc:  # pylint: disable=broad-except
                raise HttpServiceError(f"Unable to download manifest source {source_url}") from exc
            filename = Path(urlparse(source_url).path).name or "openapi.json"
        else:
            source_path = manifest_path.parent.joinpath(source_url).resolve()
            if not source_path.is_relative_to(allowed_root) or source_path.is_symlink() or not source_path.is_file():
                raise HttpServiceError(f"Manifest source escapes its allowed scan root: {source_url}")
            try:
                payload = source_path.read_bytes()
            except OSError as exc:
                raise HttpServiceError(f"Unable to read manifest source {source_url}") from exc
            filename = source_path.name
        if len(payload) > settings.mcpgateway_http_spec_max_bytes:
            raise HttpServiceError(f"Manifest source exceeds the {settings.mcpgateway_http_spec_max_bytes}-byte limit")
        if not filename.lower().endswith(_ALLOWED_SUFFIXES):
            raise HttpServiceError("Manifest source must end with .json, .yaml, .yml or .zip")
        return payload, filename

    @staticmethod
    def _resolve_manual_xsd(binding: dict[str, Any], manifest_path: Path, allowed_root: Path) -> dict[str, Any]:
        """Read an XSD binding into the shape the OpenAPI extension expects.

        Args:
            binding: The validated ``xsd`` mapping.
            manifest_path: Path to the manifest (relative ``file`` resolves here).
            allowed_root: The configured scan root the file must stay inside.

        Returns:
            A ``{"schema": <text>, "schema11": bool}`` mapping.

        Raises:
            HttpServiceError: On path escape, unreadable file, or oversized XSD.
        """
        resolved: dict[str, Any] = {"schema11": bool(binding.get("schema11", False))}
        if "schema" in binding and isinstance(binding["schema"], str) and binding["schema"].strip():
            resolved["schema"] = binding["schema"]
            return resolved

        candidate = manifest_path.parent.joinpath(str(binding["file"])).resolve()
        # Same guard as the OpenAPI source loader: a manifest may only reach
        # files under the scan root it was discovered in.
        if not candidate.is_relative_to(allowed_root) or candidate.is_symlink() or not candidate.is_file():
            raise HttpServiceError(f"Manual XSD escapes its allowed scan root: {binding['file']}")
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise HttpServiceError(f"Unable to read manual XSD {binding['file']}") from exc
        if len(text.encode("utf-8")) > settings.mcpgateway_http_spec_max_bytes:
            raise HttpServiceError(f"Manual XSD exceeds the {settings.mcpgateway_http_spec_max_bytes}-byte limit")
        resolved["schema"] = text
        return resolved

    @classmethod
    def _synthesize_manual_openapi(cls, manifest: dict[str, Any], manifest_path: Path, allowed_root: Path) -> bytes:
        """Build an OpenAPI document from ``spec.operations.manual`` (§27).

        The manual declarations are expressed as an OpenAPI document rather
        than as a bespoke pipeline so that everything downstream — artifact
        hashing, schema drift, candidate-then-activate, tool compilation —
        is the same code path a discovered OpenAPI service already uses.
        The XSD binding rides along in the ``x-contextforge-xsd`` vendor
        extension the compiler already understands.

        Args:
            manifest: The validated manifest mapping.
            manifest_path: Path to the manifest.
            allowed_root: The configured scan root.

        Returns:
            The serialized OpenAPI document bytes.
        """
        spec = manifest["spec"]
        # First-Party
        from mcpgateway.protocols.contracts.openapi import _XSD_EXTENSION  # pylint: disable=import-outside-toplevel,protected-access

        paths: dict[str, Any] = {}
        for entry in spec["operations"]["manual"]:
            media_content: dict[str, Any] = {}
            for side in ("body", "response"):
                block = entry.get(side)
                if not block:
                    continue
                media_type = str(block["mediaType"])
                media: dict[str, Any] = {"schema": {"type": "object"}}
                if block.get("xsd"):
                    media[_XSD_EXTENSION] = cls._resolve_manual_xsd(block["xsd"], manifest_path, allowed_root)
                media_content[side] = {"mediaType": media_type, "media": media}

            operation: dict[str, Any] = {"operationId": str(entry["name"])}
            if "body" in media_content:
                body = media_content["body"]
                operation["requestBody"] = {"required": True, "content": {body["mediaType"]: body["media"]}}
            response_media = media_content.get("response")
            operation["responses"] = {
                "200": {
                    "description": "Manual operation",
                    **( {"content": {response_media["mediaType"]: response_media["media"]}} if response_media else {} ),
                }
            }
            path_item = paths.setdefault(str(entry["path"]), {})
            method = str(entry["method"]).lower()
            if method in path_item:
                raise HttpServiceError(f"spec.operations.manual declares {method.upper()} {entry['path']} twice")
            path_item[method] = operation

        document = {
            "openapi": "3.0.3",
            "info": {"title": str(manifest["metadata"]["name"]), "version": "1.0.0"},
            "paths": paths,
        }
        return json.dumps(document).encode("utf-8")

    @staticmethod
    def _matches_managed_state(service: DbHttpService | None, manifest_hash: str) -> bool:
        """Return whether the manifest driving this service is unchanged."""
        return bool(service and (service.discovery_config or {}).get("manifest_hash") == manifest_hash)

    @staticmethod
    def _resolve_team(db: Session, value: Any) -> str | None:
        """Resolve an active team by ID or display name."""
        if value in (None, ""):
            return None
        team = db.execute(
            select(EmailTeam).where(
                or_(EmailTeam.id == str(value), EmailTeam.name == str(value)),
                EmailTeam.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if team is None:
            raise HttpServiceError("Manifest team does not exist or is inactive")
        return team.id

    @staticmethod
    def _build_service_fields(
        manifest: dict[str, Any],
        manifest_hash: str,
        team_id: str | None,
        manifest_path: Path,
        source_url: str | None,
    ) -> dict[str, Any]:
        """Map a validated manifest onto HttpServiceCreate/Update field values.

        Args:
            manifest: The validated manifest mapping
            manifest_hash: Content hash of the manifest file
            team_id: Resolved team ID (or None)
            manifest_path: Resolved manifest path (stored in discovery_config)
            source_url: The manifest's spec.discovery.source.url value, or
                ``None`` for a manifest that declares manual operations

        Returns:
            Field values accepted by both HttpServiceCreate and
            HttpServiceUpdate (team_id is only accepted by Create)
        """
        metadata = manifest["metadata"]
        spec = manifest["spec"]
        health = spec.get("health") or {}
        return {
            "name": str(metadata["name"]),
            "base_url": str(spec["baseUrl"]),
            "description": metadata.get("description"),
            "discovery_mode": "manual",
            "discovery_config": {
                "manifest_path": str(manifest_path),
                "manifest_hash": manifest_hash,
                "source_url": source_url,
                "references": (manifest["spec"].get("discovery") or {}).get("references") or {},
            },
            "runtime_config": spec.get("runtime") or {},
            "health_check_enabled": health.get("enabled", True),
            "health_check_interval": health.get("interval", 60),
            "health_check_timeout": health.get("timeout", 5),
            "health_failure_threshold": health.get("failureThreshold", 3),
            "tags": [str(tag) for tag in (metadata.get("tags") or [])],
            "team_id": team_id,
            "visibility": metadata.get("visibility", "private"),
        }

    async def scan(self, db: Session) -> dict[str, Any]:
        """Run one primary-worker scan and return a credential-free summary.

        Args:
            db: Database session

        Returns:
            Summary of created/updated/skipped services and per-manifest errors

        Raises:
            HttpServiceError: If scanning is disabled or not on the primary
                worker, or a configured root is not a directory
        """
        if not settings.mcpgateway_http_yaml_scan_enabled:
            raise HttpServiceError("HTTP YAML directory scanning is disabled")
        if not is_primary_worker():
            raise HttpServiceError("HTTP YAML scanning may only run on the primary worker")
        roots = self._resolve_roots()
        result: dict[str, Any] = {"created": [], "updated": [], "skipped": [], "errors": []}
        for root in roots:
            for manifest_path in sorted(root.rglob("http-service.yaml")):
                resolved_manifest = manifest_path.resolve()
                if not resolved_manifest.is_relative_to(root) or manifest_path.is_symlink():
                    continue
                try:
                    manifest = self._load_manifest(resolved_manifest)
                    manual = (manifest["spec"].get("operations") or {}).get("manual")
                    if manual:
                        # Manual operations (§27): the declarations *are* the
                        # contract, so synthesize the document the rest of the
                        # pipeline already knows how to consume.
                        payload = self._synthesize_manual_openapi(manifest, resolved_manifest, root)
                        filename = _MANUAL_ARTIFACT_FILENAME
                        source_url: str | None = None
                    else:
                        payload, filename = await self._load_source(manifest, resolved_manifest, root)
                        source_url = str(manifest["spec"]["discovery"]["source"]["url"])
                    team_id = self._resolve_team(db, manifest["metadata"].get("team"))
                    manifest_hash = sha256(resolved_manifest.read_bytes()).hexdigest()
                    references = (manifest["spec"].get("discovery") or {}).get("references") or {}
                    allow_remote = bool(references.get("allowRemote", False))
                    name = str(manifest["metadata"]["name"])
                    service = db.execute(select(DbHttpService).where(DbHttpService.name == name)).scalar_one_or_none()
                    managed_path = (service.discovery_config or {}).get("manifest_path") if service else None
                    if managed_path and managed_path != str(resolved_manifest):
                        raise HttpServiceError("Service name is already managed by another manifest")
                    if self._matches_managed_state(service, manifest_hash):
                        result["skipped"].append(name)
                        continue
                    fields = self._build_service_fields(manifest, manifest_hash, team_id, resolved_manifest, source_url)
                    if service is None:
                        created = await self.http.register_service(
                            db,
                            HttpServiceCreate(owner_email="system", **fields),
                            user_email="system",
                            metadata={"created_via": "http-yaml-scan"},
                        )
                        service = db.get(DbHttpService, created.id)
                        await self.http.import_schema(db, service.id, payload, filename, "system", activate=True, allow_remote=allow_remote)
                        action = "created"
                    else:
                        update_fields = {key: value for key, value in fields.items() if key != "team_id"}
                        await self.http.update_service(
                            db,
                            service.id,
                            HttpServiceUpdate(**update_fields),
                            user_email="system",
                            metadata={"modified_via": "http-yaml-scan"},
                        )
                        service = db.get(DbHttpService, service.id)
                        service.team_id = team_id
                        await self.http.import_schema(db, service.id, payload, filename, "system", activate=False, allow_remote=allow_remote)
                        action = "updated"
                    if service is None:
                        raise HttpServiceError("Unable to load scanned HTTP service")
                    result[action].append(name)
                except Exception as exc:  # pylint: disable=broad-except
                    db.rollback()
                    result["errors"].append({"manifest": str(resolved_manifest), "error": str(exc)[:1000]})
        return result


_http_yaml_scan_service = HttpYamlScanService()


def get_http_yaml_scan_service() -> HttpYamlScanService:
    """Return the process-local manifest scanner singleton."""
    return _http_yaml_scan_service
