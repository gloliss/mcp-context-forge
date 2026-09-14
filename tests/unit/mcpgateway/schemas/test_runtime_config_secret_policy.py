# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/schemas/test_runtime_config_secret_policy.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests that runtime_config fields reject plaintext secrets (PR8, §67).
"""

# Third-Party
import pytest
from pydantic import ValidationError

# First-Party
from mcpgateway.schemas import GrpcServiceCreate, GrpcServiceUpdate, HttpServiceCreate, HttpServiceUpdate


class TestRuntimeConfigSecretPolicy:
    """Create/Update schemas enforce the §67 plaintext-secret ban."""

    def test_http_create_rejects_plaintext(self):
        """HttpServiceCreate rejects a plaintext api_key in runtime_config."""
        with pytest.raises(ValidationError, match="Plaintext secret"):
            HttpServiceCreate(name="svc", base_url="http://example.com", runtime_config={"api_key": "plaintext"})

    def test_http_create_accepts_encrypted(self):
        """HttpServiceCreate accepts encrypted runtime_config values."""
        service = HttpServiceCreate(
            name="svc",
            base_url="http://example.com",
            runtime_config={"api_key": "v2:ciphertext", "maxItems": 10},
        )

        assert service.runtime_config["api_key"] == "v2:ciphertext"

    def test_http_update_rejects_plaintext(self):
        """HttpServiceUpdate rejects a plaintext password in runtime_config."""
        with pytest.raises(ValidationError, match="Plaintext secret"):
            HttpServiceUpdate(runtime_config={"password": "hunter2"})

    def test_grpc_create_rejects_plaintext(self):
        """GrpcServiceCreate rejects a plaintext client_secret."""
        with pytest.raises(ValidationError, match="Plaintext secret"):
            GrpcServiceCreate(name="g", target="127.0.0.1:50051", runtime_config={"client_secret": "plaintext"})

    def test_grpc_create_accepts_clean(self):
        """GrpcServiceCreate accepts a secret-free runtime_config."""
        service = GrpcServiceCreate(
            name="g",
            target="127.0.0.1:50051",
            runtime_config={"streaming": {"maxItems": 100}, "keepaliveMs": 30000},
        )

        assert service.runtime_config["streaming"]["maxItems"] == 100

    def test_grpc_update_rejects_plaintext(self):
        """GrpcServiceUpdate rejects a plaintext token."""
        with pytest.raises(ValidationError, match="Plaintext secret"):
            GrpcServiceUpdate(runtime_config={"token": "plaintext"})

    def test_grpc_update_accepts_none(self):
        """GrpcServiceUpdate accepts an unset runtime_config."""
        update = GrpcServiceUpdate()

        assert update.runtime_config is None
