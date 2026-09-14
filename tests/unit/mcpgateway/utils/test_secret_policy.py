# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/utils/test_secret_policy.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the plaintext-secret policy (PR8, design §67).
"""

# Third-Party
import pytest

# First-Party
from mcpgateway.utils.secret_policy import check_no_plaintext_secrets, find_plaintext_secret_paths


class TestFindPlaintextSecretPaths:
    """find_plaintext_secret_paths locates offending keys."""

    def test_detects_plaintext_password(self):
        """A plaintext password key is reported."""
        hits = find_plaintext_secret_paths({"password": "hunter2"})

        assert hits == ["password"]

    def test_detects_nested_token(self):
        """Nested sensitive keys are reported with their dotted path."""
        hits = find_plaintext_secret_paths({"streaming": {"maxItems": 10}, "auth": {"token": "abc"}})

        assert "auth.token" in hits
        assert "streaming.maxItems" not in hits

    def test_encrypted_value_is_safe(self):
        """v2:-encrypted values are not reported."""
        assert find_plaintext_secret_paths({"api_key": "v2:eyJhbGciOiJIUzI1NiJ9"}) == []

    def test_non_string_values_are_safe(self):
        """Numbers, bools, None, and nested mappings are structural, not secrets."""
        config = {"maxItems": 100, "enabled": True, "nested": {"k": "v"}, "null_key": None}
        assert find_plaintext_secret_paths(config) == []

    def test_authorization_header_flagged(self):
        """authorization-style keys are flagged."""
        hits = find_plaintext_secret_paths({"headers": {"authorization": "Bearer abc"}})

        assert "headers.authorization" in hits


class TestCheckNoPlaintextSecrets:
    """check_no_plaintext_secrets raises on plaintext secrets (§67)."""

    def test_raises_on_plaintext(self):
        """A plaintext secret raises ValueError naming the key."""
        with pytest.raises(ValueError, match="password"):
            check_no_plaintext_secrets({"password": "hunter2"}, label="runtime_config")

    def test_accepts_clean_config(self):
        """A config without secrets passes silently."""
        check_no_plaintext_secrets({"maxItems": 10, "timeout": {"readMs": 3000}}, label="runtime_config")

    def test_accepts_encrypted_values(self):
        """Encrypted markers are accepted."""
        check_no_plaintext_secrets({"client_secret": "v2:ciphertext", "token": "enc:xxx"}, label="runtime_config")

    def test_accepts_none_value(self):
        """None values are accepted."""
        check_no_plaintext_secrets({"runtime_config": None}, label="runtime_config")
