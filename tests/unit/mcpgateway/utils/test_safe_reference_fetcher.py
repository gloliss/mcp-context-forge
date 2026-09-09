# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/utils/test_safe_reference_fetcher.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the SafeReferenceFetcher (PR8, design §66).
"""

# Third-Party
import pytest

# First-Party
from mcpgateway.utils.safe_reference_fetcher import SafeReferenceError, SafeReferenceFetcher


class TestSafeReferenceFetcher:
    """SafeReferenceFetcher refuses unsafe references and enforces limits."""

    async def test_remote_refused_when_disallowed(self):
        """allowRemote=false refuses before any network work."""
        fetcher = SafeReferenceFetcher()

        with pytest.raises(SafeReferenceError, match="allowRemote=false"):
            await fetcher.fetch("http://example.com/schema.xsd")

    async def test_non_http_scheme_refused(self):
        """file:// and other schemes are refused even when remote is allowed."""
        fetcher = SafeReferenceFetcher(allow_remote=True)

        with pytest.raises(SafeReferenceError, match="scheme not allowed"):
            await fetcher.fetch("file:///etc/passwd")

    async def test_ssrf_policy_blocks_metadata_ip(self, monkeypatch):
        """A blocked destination surfaces as SafeReferenceError."""
        from mcpgateway.common import validators

        async def _blocked(url, label):
            raise ValueError("blocked")

        monkeypatch.setattr(validators.SecurityValidator, "validate_url_for_connection_pinning", _blocked)
        fetcher = SafeReferenceFetcher(allow_remote=True)

        with pytest.raises(SafeReferenceError, match="SSRF policy refused"):
            await fetcher.fetch("http://169.254.169.254/latest/meta-data")

    async def test_http_error_surfaces_as_safe_reference_error(self, monkeypatch):
        """Transport failures map to SafeReferenceError."""
        import httpx

        class _FailingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url):
                raise httpx.ConnectError("boom")

        monkeypatch.setattr("mcpgateway.utils.safe_reference_fetcher.httpx.AsyncClient", lambda **kw: _FailingClient())
        fetcher = SafeReferenceFetcher(allow_remote=True)
        # Neutralize SSRF so the transport error is what surfaces.
        from mcpgateway.common import validators

        async def _pass(url, label):
            return {"resolved_ip": "127.0.0.1", "hostname": "example.com", "original_authority": "example.com"}

        monkeypatch.setattr(validators.SecurityValidator, "validate_url_for_connection_pinning", _pass)

        with pytest.raises(SafeReferenceError, match="Failed to fetch reference"):
            await fetcher.fetch("http://example.com/schema.xsd")
