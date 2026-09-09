# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/utils/artifact_security.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Shared artifact (ZIP) safety checks (PR3, design-document §14).

The gRPC schema service and the HTTP contract artifact service must apply
identical ZIP safety rules (entry count, path traversal, symlinks,
expansion size, compression ratio).  These rules lived inside
``GrpcSchemaService._safe_zip_members`` since the gRPC PRs; PR3 extracts
them here so both protocols share one implementation (design §14 explicitly
calls for not keeping two copies of the ZIP safety rules).

The shared function raises the neutral ``ArtifactSecurityError``; each
service wraps it back into its own error type with identical messages.
"""

# Standard
from pathlib import PurePosixPath
import stat
from typing import Any
import zipfile

# Default maximum file_size / compress_size ratio accepted from ZIP entries.
MAX_ZIP_RATIO = 100


class ArtifactSecurityError(Exception):
    """Raised when an uploaded artifact violates ZIP safety rules."""


def safe_zip_members(
    archive: zipfile.ZipFile,
    *,
    max_entries: int,
    max_uncompressed_bytes: int,
    max_ratio: float = MAX_ZIP_RATIO,
    label: str = "Artifact ZIP",
) -> list[zipfile.ZipInfo]:
    """Validate ZIP paths, expansion size, entry count, and compression ratio.

    Rejects: absolute paths, ``..`` traversal, empty paths, symlink entries,
    more than ``max_entries`` members, more than ``max_uncompressed_bytes``
    of expanded data, zero-compressed entries carrying data (bombs), and
    compression ratios above ``max_ratio``.

    Args:
        archive: The opened ZIP archive.
        max_entries: Maximum accepted member count.
        max_uncompressed_bytes: Maximum accepted total expanded size.
        max_ratio: Maximum accepted ``file_size / compress_size`` ratio.
        label: Human-readable artifact label embedded in error messages so
            each caller keeps its own wording (e.g. ``"Proto ZIP"``).

    Returns:
        The non-directory members that passed validation.

    Raises:
        ArtifactSecurityError: If any member violates a rule.
    """
    members = archive.infolist()
    if len(members) > max_entries:
        raise ArtifactSecurityError(f"{label} contains too many entries")
    expanded = 0
    safe: list[zipfile.ZipInfo] = []
    for member in members:
        path = PurePosixPath(member.filename)
        mode = member.external_attr >> 16
        if path.is_absolute() or ".." in path.parts or not path.parts or stat.S_ISLNK(mode):
            raise ArtifactSecurityError(f"Unsafe {label} entry: {member.filename}")
        expanded += member.file_size
        if expanded > max_uncompressed_bytes:
            raise ArtifactSecurityError(f"{label} expanded size exceeds the configured limit")
        if member.compress_size == 0 and member.file_size > 0:
            raise ArtifactSecurityError(f"{label} contains an invalid compressed entry")
        if member.compress_size and member.file_size / member.compress_size > max_ratio:
            raise ArtifactSecurityError(f"{label} compression ratio exceeds the safety limit")
        if not member.is_dir():
            safe.append(member)
    return safe


def json_pointer_get(document: Any, pointer: str) -> Any:
    """Navigate a JSON Pointer fragment (RFC 6901) inside ``document``.

    Only fragments starting with ``#/`` are supported; the empty fragment
    (``#``) returns the whole document.  Segment escapes ``~1`` (``/``) and
    ``~0`` (``~``) are honoured.

    Args:
        document: The parsed document (dict/list/Any).
        pointer: The JSON Pointer fragment, optionally with a leading ``#``.

    Returns:
        The referenced value.

    Raises:
        KeyError: If a segment does not exist.
        ValueError: If the fragment is not a local fragment.
    """
    fragment = pointer[1:] if pointer.startswith("#") else pointer
    if not fragment or fragment == "/":
        return document
    if not fragment.startswith("/"):
        raise ValueError(f"Non-local JSON Pointer is not navigable: {pointer}")
    current = document
    for raw_segment in fragment[1:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[segment]
        elif isinstance(current, list):
            current = current[int(segment)]
        else:
            raise KeyError(f"JSON Pointer segment {segment!r} does not exist")
    return current
