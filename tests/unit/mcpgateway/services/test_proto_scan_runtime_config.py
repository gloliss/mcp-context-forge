# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_proto_scan_runtime_config.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the ``runtime`` manifest field in grpc-service.yaml (PR6, §43).
"""

# Standard
from pathlib import Path

# Third-Party
import pytest

# First-Party
from mcpgateway.services.proto_scan_service import ProtoScanService
from mcpgateway.utils.grpc_validation import GrpcServiceError


def _write_manifest(path: Path, extra: str = "") -> Path:
    """Write a valid grpc-service.yaml manifest, optionally with extra lines."""
    manifest = path / "grpc-service.yaml"
    manifest.write_text(
        "\n".join(
            [
                "service_name: catalog",
                "target: catalog.example.com:443",
                "reflection_mode: artifact",
                "proto_root: proto",
                "entry: catalog.proto",
                "visibility: private",
                extra,
            ]
        ),
        encoding="utf-8",
    )
    return manifest


class TestRuntimeManifestField:
    """grpc-service.yaml accepts a ``runtime`` policy block (§43)."""

    def test_runtime_is_an_allowed_manifest_field(self, tmp_path):
        """A manifest with a runtime block loads without an unknown-field error."""
        manifest = _write_manifest(
            tmp_path,
            "\n".join(
                [
                    "runtime:",
                    "  maxSendBytes: 4194304",
                    "  streaming:",
                    "    maxItems: 1000",
                    "    maxBytes: 16777216",
                ]
            ),
        )

        data = ProtoScanService._load_manifest(manifest)  # pylint: disable=protected-access

        assert data["runtime"] == {
            "maxSendBytes": 4194304,
            "streaming": {"maxItems": 1000, "maxBytes": 16777216},
        }

    def test_unknown_field_still_rejected(self, tmp_path):
        """Unrelated unknown fields continue to be rejected."""
        manifest = _write_manifest(tmp_path, "not_a_field: true")

        with pytest.raises(GrpcServiceError, match="Unknown grpc-service.yaml fields"):
            ProtoScanService._load_manifest(manifest)  # pylint: disable=protected-access

    def test_manifest_without_runtime_defaults_to_empty(self, tmp_path):
        """Absent runtime yields no runtime key (callers default to {})."""
        manifest = _write_manifest(tmp_path)

        data = ProtoScanService._load_manifest(manifest)  # pylint: disable=protected-access

        assert "runtime" not in data
