# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_grpc_status_details.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for gRPC status → error-category mapping and grpc-status-details-bin
parsing (PR7, design §55).
"""

# Third-Party
from google.rpc import status_pb2

# First-Party
from mcpgateway.services.grpc_service import map_grpc_status_to_category, parse_grpc_status_details


class TestMapGrpcStatusToCategory:
    """gRPC StatusCode → canonical ErrorCategory (§55/§73)."""

    def test_key_mappings(self):
        """The §73 gRPC rows map to canonical categories."""
        assert map_grpc_status_to_category("INVALID_ARGUMENT") == "INVALID_ARGUMENT"
        assert map_grpc_status_to_category("UNAUTHENTICATED") == "UNAUTHENTICATED"
        assert map_grpc_status_to_category("PERMISSION_DENIED") == "PERMISSION_DENIED"
        assert map_grpc_status_to_category("RESOURCE_EXHAUSTED") == "RATE_LIMITED"
        assert map_grpc_status_to_category("UNAVAILABLE") == "UNAVAILABLE"
        assert map_grpc_status_to_category("DEADLINE_EXCEEDED") == "UNAVAILABLE"
        assert map_grpc_status_to_category("UNIMPLEMENTED") == "UNSUPPORTED"
        assert map_grpc_status_to_category("INTERNAL") == "INTERNAL"

    def test_unknown_code_falls_back(self):
        """Unknown/None codes map to UPSTREAM_ERROR."""
        assert map_grpc_status_to_category(None) == "UPSTREAM_ERROR"
        assert map_grpc_status_to_category("SOMETHING_ELSE") == "UPSTREAM_ERROR"


class TestParseGrpcStatusDetails:
    """grpc-status-details-bin parsing (§55)."""

    def _trailers_with(self, status_bytes: bytes):
        """Build trailing-metadata pairs with a base64 status-details-bin."""
        import base64

        return [("content-type", "application/grpc"), ("grpc-status-details-bin", base64.b64encode(status_bytes))]

    def test_parses_status_message(self):
        """A google.rpc.Status in the details-bin yields its message."""
        status = status_pb2.Status(code=3, message="field x required")

        parsed = parse_grpc_status_details(self._trailers_with(status.SerializeToString()))

        assert parsed is not None
        assert parsed.code == 3
        assert parsed.message == "field x required"

    def test_returns_none_without_details(self):
        """Absent details-bin returns None."""
        assert parse_grpc_status_details([("content-type", "application/grpc")]) is None
        assert parse_grpc_status_details(None) is None

    def test_returns_none_on_garbage(self):
        """Unparseable details-bin returns None instead of raising."""
        assert parse_grpc_status_details([("grpc-status-details-bin", b"not-a-protobuf")]) is None
