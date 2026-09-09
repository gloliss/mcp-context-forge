# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/utils/safe_reference_fetcher.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Safe external-reference fetcher (PR8, design-document §66).

``SafeReferenceFetcher`` materialises external references (OpenAPI ``$ref``,
WSDL imports, XSD ``include``/``import``) that survived artifact bundling.
Every fetch passes through the existing SSRF policy, is restricted to
``http(s)``, and is bounded by a byte limit.  Contract parsers (Zeep,
xmlschema, OpenAPI) must never reach the network on their own — they get
pre-fetched bundles, or a diagnostic when a reference is refused.
"""

# Standard
from dataclasses import dataclass
from urllib.parse import urlparse

# Third-Party
import httpx

# First-Party
from mcpgateway.common.validators import SecurityValidator
from mcpgateway.config import settings


class SafeReferenceError(ValueError):
    """Raised when an external reference cannot be fetched safely (§66)."""


@dataclass(frozen=True)
class SafeReferenceFetcher:
    """Fetch one external reference under SSRF policy and size limits.

    Attributes:
        max_bytes: Maximum reference payload size.
        connect_timeout: HTTP connect timeout seconds.
        read_timeout: HTTP read timeout seconds.
        allow_remote: Whether remote http(s) references are permitted at
            all; ``False`` (default) refuses them before any DNS/network
            work.
    """

    max_bytes: int = 8 * 1024 * 1024
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    allow_remote: bool = False

    async def fetch(self, url: str) -> bytes:
        """Fetch ``url`` and return its bytes.

        Args:
            url: The external reference URL (http/https only).

        Returns:
            The fetched payload, bounded by ``max_bytes``.

        Raises:
            SafeReferenceError: On scheme violations, SSRF-policy refusals,
                size-limit breaches, or transport failures.
        """
        if not self.allow_remote:
            raise SafeReferenceError(f"Remote reference fetch refused (allowRemote=false): {url}")

        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise SafeReferenceError(f"Reference URL scheme not allowed: {url}")

        try:
            if settings.ssrf_protection_enabled:
                await SecurityValidator.validate_url_for_connection_pinning(url, "External reference")
        except Exception as exc:  # SecurityValidator raises on blocked destinations.
            raise SafeReferenceError(f"SSRF policy refused reference: {url}") from exc

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.connect_timeout, read=self.read_timeout), follow_redirects=False) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.content
        except httpx.HTTPError as exc:
            raise SafeReferenceError(f"Failed to fetch reference {url}: {exc}") from exc

        if len(payload) > self.max_bytes:
            raise SafeReferenceError(f"Reference exceeds max_bytes={self.max_bytes}: {url}")
        return payload


__all__ = ["SafeReferenceError", "SafeReferenceFetcher"]
