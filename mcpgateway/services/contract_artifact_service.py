# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/contract_artifact_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

HTTP contract artifact preparation (PR3, design-document §14).

Prepares raw uploaded OpenAPI content into the canonical, immutable
artifact bundle stored in ``http_schema_artifacts.artifact_blob``:

1. size caps (pre-parse and post-materialisation);
2. upload filename validation;
3. safe ZIP extraction (shared ``utils.artifact_security.safe_zip_members``
   with the HTTP limits — the same rules the gRPC proto path enforces);
4. JSON/YAML parsing with format detection;
5. external ``$ref`` materialisation via ``ContractArtifactResolver``
   (design §66);
6. SHA-256 content addressing over the canonical bundle bytes.

The service is intentionally DB-free: deduplication queries, version
assignment and candidate/active pointer management live in
``http_schema_service`` (which owns the transaction), mirroring the gRPC
split where artifact *content* preparation is separate from registry
state transitions.
"""

# Standard
from dataclasses import dataclass, field
import hashlib
import io
from pathlib import PurePosixPath
from typing import Any, Optional
import zipfile

# Third-Party
import orjson
import yaml

# First-Party
from mcpgateway.config import settings
from mcpgateway.services.safe_reference_fetcher import ContractArtifactResolver
from mcpgateway.utils.artifact_security import ArtifactSecurityError, safe_zip_members
from mcpgateway.utils.http_validation import HttpServiceError

# Accepted upload filename suffixes (case-insensitive).
_ALLOWED_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".zip"})

# Preferred spec entry names inside an artifact ZIP, in precedence order.
_PREFERRED_ZIP_ENTRIES = ("openapi.json", "openapi.yaml", "openapi.yml")


@dataclass(frozen=True)
class PreparedContractArtifact:
    """A validated, canonical artifact bundle ready for storage.

    Attributes:
        artifact_format: The uploaded format (``"json"``, ``"yaml"`` or
            ``"zip"``).  The stored ``artifact_blob`` itself is always the
            canonical JSON serialisation of the materialised bundle.
        content_hash: SHA-256 hex digest of ``bundle`` (the content
            address used for deduplication).
        bundle: The canonical serialised bundle bytes.
        document: The parsed bundle document (pre-serialisation).
        source_info: Provenance metadata for the ``source_info`` column.
    """

    artifact_format: str
    content_hash: str
    bundle: bytes
    document: dict
    source_info: dict = field(default_factory=dict)


class ContractArtifactService:
    """Prepare raw OpenAPI uploads into canonical artifact bundles (§14)."""

    def __init__(self, resolver: Optional[ContractArtifactResolver] = None) -> None:
        """Initialise with an optional shared resolver (for test injection).

        Args:
            resolver: The external-reference resolver; defaults to a fresh
                ``ContractArtifactResolver``.
        """
        self._resolver = resolver or ContractArtifactResolver()

    async def prepare_artifact(
        self, content: bytes, filename: str, *, allow_remote: bool = True
    ) -> PreparedContractArtifact:
        """Prepare raw uploaded content into a canonical artifact bundle.

        Args:
            content: The raw uploaded bytes.
            filename: The client-provided upload filename.
            allow_remote: Whether external ``$ref`` values may be fetched
                (design §66; YAML manifests may disable this).

        Returns:
            A ``PreparedContractArtifact``.

        Raises:
            HttpServiceError: On any size, filename, ZIP, parse, or
                materialisation violation.
        """
        # 1. Pre-parse size cap and filename validation.
        max_upload = settings.mcpgateway_http_max_upload_bytes
        if len(content) > max_upload:
            raise HttpServiceError(f"HTTP artifact upload too large ({len(content)} bytes, max {max_upload})")
        suffix = self._validate_filename(filename)

        # 2. Unwrap ZIP uploads into their spec entry.
        if suffix == ".zip":
            raw_document = self._extract_zip_spec(content)
        else:
            raw_document = self._parse_document(content, filename)

        if not isinstance(raw_document, dict):
            raise HttpServiceError("HTTP artifact is not a JSON object document")
        if not isinstance(raw_document.get("openapi"), str):
            raise HttpServiceError(
                "HTTP artifact is not an OpenAPI document (missing or invalid 'openapi' version field)"
            )

        # 3. Materialise external $ref values (design §66).
        try:
            bundle = await self._resolver.resolve_and_bundle(raw_document, allow_remote=allow_remote)
        except ValueError as exc:
            raise HttpServiceError(str(exc)) from exc

        # 4. Canonical serialisation + content addressing.  The stored
        #    blob is always JSON: the provider parses one format and YAML
        #    round-trips are lossy for some schemas.  ``artifact_format``
        #    keeps the original upload format for provenance.
        try:
            canonical = orjson.dumps(bundle)
        except (TypeError, ValueError) as exc:
            raise HttpServiceError("HTTP artifact contains values that cannot be serialised to JSON") from exc
        if len(canonical) > max_upload:
            raise HttpServiceError(
                f"HTTP artifact too large after external $ref materialisation ({len(canonical)} bytes, max {max_upload})"
            )
        content_hash = hashlib.sha256(canonical).hexdigest()

        return PreparedContractArtifact(
            artifact_format=suffix[1:],
            content_hash=content_hash,
            bundle=canonical,
            document=bundle,
            source_info={"filename": filename, "allow_remote": allow_remote},
        )

    @staticmethod
    def _validate_filename(filename: str) -> str:
        """Validate the upload filename and return its lower-case suffix.

        Args:
            filename: The client-provided filename.

        Returns:
            The lower-case suffix, e.g. ``".json"``.

        Raises:
            HttpServiceError: If the filename contains path separators or
                has a non-OpenAPI suffix.
        """
        if not filename or "\\" in filename or PurePosixPath(filename).name != filename:
            raise HttpServiceError(f"Invalid HTTP artifact filename: {filename!r}")
        suffix = f".{filename.lower().rsplit('.', 1)[-1]}"
        if suffix not in _ALLOWED_SUFFIXES:
            raise HttpServiceError(
                f"Unsupported HTTP artifact file type: {filename!r} (expected .json, .yaml, .yml or .zip)"
            )
        return suffix

    @staticmethod
    def _parse_document(raw: bytes, label: str) -> Any:
        """Parse raw JSON or YAML content (format autodetection).

        Args:
            raw: The raw document bytes.
            label: Human-readable source for error messages.

        Returns:
            The parsed document (dict, list, or scalar).

        Raises:
            HttpServiceError: If the content is neither valid JSON nor
                valid YAML.
        """
        try:
            return orjson.loads(raw)
        except (orjson.JSONDecodeError, ValueError):
            try:
                return yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                raise HttpServiceError(f"HTTP artifact '{label}' is not valid JSON or YAML") from exc

    @classmethod
    def _extract_zip_spec(cls, content: bytes) -> Any:
        """Extract the OpenAPI document from a ZIP artifact.

        Applies the shared ZIP safety rules (entry count, path traversal,
        symlinks, expansion size, compression ratio) with the HTTP limits,
        then picks the spec entry (conventional ``openapi.*`` basenames
        first, otherwise a ZIP holding exactly one spec-suffix file).

        Args:
            content: The raw ZIP bytes.

        Returns:
            The parsed OpenAPI document.

        Raises:
            HttpServiceError: If the ZIP is malformed, violates a safety
                rule, holds no OpenAPI document, or the entry fails to
                parse.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = safe_zip_members(
                    archive,
                    max_entries=settings.mcpgateway_http_max_zip_entries,
                    max_uncompressed_bytes=settings.mcpgateway_http_max_uncompressed_bytes,
                    label="HTTP artifact ZIP",
                )
                entry = cls._pick_spec_entry(members)
                if entry is None:
                    raise HttpServiceError(
                        "HTTP artifact ZIP contains no OpenAPI document (expected openapi.json/openapi.yaml/openapi.yml)"
                    )
                raw = archive.read(entry)
        except zipfile.BadZipFile as exc:
            raise HttpServiceError(f"HTTP artifact is not a valid ZIP: {exc}") from exc
        except ArtifactSecurityError as exc:
            raise HttpServiceError(str(exc)) from exc
        return cls._parse_document(raw, entry.filename)

    @staticmethod
    def _pick_spec_entry(members: list[zipfile.ZipInfo]) -> Optional[zipfile.ZipInfo]:
        """Pick the OpenAPI entry from a validated ZIP member list.

        Args:
            members: The non-directory members from ``safe_zip_members``.

        Returns:
            The chosen entry, or ``None`` when no candidate exists.
        """
        by_name = {PurePosixPath(member.filename).name: member for member in members}
        for preferred in _PREFERRED_ZIP_ENTRIES:
            if preferred in by_name:
                return by_name[preferred]
        candidates = [
            member
            for member in members
            if PurePosixPath(member.filename).name.lower().endswith((".json", ".yaml", ".yml"))
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None
