# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_grpc_metadata_resolver.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the unified gRPC metadata resolver (PR6, design §41).
"""

# Standard
from types import SimpleNamespace

# First-Party
from mcpgateway.config import settings
from mcpgateway.services.encryption_service import get_encryption_service
from mcpgateway.services.grpc_service import _resolve_grpc_metadata


def _encrypted(value: str) -> str:
    """Encrypt a value through the real EncryptionService."""
    return get_encryption_service(settings.auth_encryption_secret).encrypt_secret(value)


class TestResolveGrpcMetadata:
    """_resolve_grpc_metadata unifies reflection/health/business metadata (§41)."""

    def test_decrypts_stored_metadata(self):
        """Stored encrypted metadata is decrypted at the outbound boundary."""
        service = SimpleNamespace(grpc_metadata={"authorization": _encrypted("Bearer abc")})

        metadata = _resolve_grpc_metadata(service)

        assert metadata == {"authorization": "Bearer abc"}

    def test_merges_override_on_top(self):
        """An invocation-time override wins over stored metadata."""
        service = SimpleNamespace(grpc_metadata={"authorization": _encrypted("stored"), "x-tenant": "a"})

        metadata = _resolve_grpc_metadata(service, metadata_override={"x-tenant": "b"})

        assert metadata == {"authorization": "stored", "x-tenant": "b"}

    def test_empty_metadata_returns_empty(self):
        """No metadata yields an empty mapping."""
        service = SimpleNamespace(grpc_metadata={})

        assert _resolve_grpc_metadata(service) == {}

    def test_missing_attribute_returns_empty(self):
        """A service without the attribute is tolerated."""
        service = SimpleNamespace()

        assert _resolve_grpc_metadata(service) == {}
