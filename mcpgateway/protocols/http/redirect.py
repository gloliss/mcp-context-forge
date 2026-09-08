# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/http/redirect.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Redirect security (PR2): manual, per-hop SSRF re-validation.

The configured HTTP runtime never lets HTTPX follow redirects
(``follow_redirects=False``).  Instead, every 3xx hop re-runs the SSRF
validation and connection-pinning pipeline against the resolved
``Location`` before the hop is taken (design-document §9.10).  The hop
budget is capped by ``settings.gateway_max_redirects``; the default policy
is ``followRedirects=false``, so a redirect is only followed when the
caller drives this loop.
"""

# Standard
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

# First-Party
from mcpgateway.common.validators import pin_url_to_resolved_ip, SecurityValidator
from mcpgateway.config import settings
from mcpgateway.protocols.models import ErrorCategory, ProtocolError

# Error codes mirroring the adapter's SSRF-failure codes; ToolService matches
# these by string value, so re-declaring the literals here (rather than
# importing from ``adapter``, which would create a cycle) is intentional.
REST_URL_VALIDATION_FAILED = "REST_URL_VALIDATION_FAILED"
REST_URL_PINNING_MISSING = "REST_URL_PINNING_MISSING"
REST_TOO_MANY_REDIRECTS = "REST_TOO_MANY_REDIRECTS"

# Status codes that request a redirect.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class RedirectTarget:
    """A validated, connection-pinned target for one hop.

    Attributes:
        url: The URL to request (pinned to the resolved IP when SSRF
            pinning is active).
        headers: Headers to attach (``Host`` when pinned).
        extensions: HTTPX connection extensions (``sni_hostname`` when
            pinned).
        resolved_ip: The resolved IP, or ``None`` when not pinned.
        hostname: The validated hostname, or ``None`` when not pinned.
        original_authority: The validated authority, or ``None``.
    """

    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    extensions: Dict[str, str] = field(default_factory=dict)
    resolved_ip: Optional[str] = None
    hostname: Optional[str] = None
    original_authority: Optional[str] = None


class RedirectSecurity:
    """Manual redirect policy with per-hop SSRF re-validation (§9.10)."""

    def __init__(self, max_hops: Optional[int] = None) -> None:
        """Initialise with an explicit hop budget or the settings default.

        Args:
            max_hops: Maximum redirect hops to follow; defaults to
                ``settings.gateway_max_redirects``.
        """
        self.max_hops = max_hops if max_hops is not None else settings.gateway_max_redirects

    @staticmethod
    def is_redirect(status_code: int) -> bool:
        """Whether a status code requests a redirect.

        Args:
            status_code: The upstream status code.

        Returns:
            ``True`` for 301/302/303/307/308.
        """
        return status_code in _REDIRECT_STATUSES

    @staticmethod
    def parse_location(headers: Any) -> Optional[str]:
        """Extract the ``Location`` header value, if present.

        Args:
            headers: A mapping of response headers.

        Returns:
            The raw ``Location`` value, or ``None`` when absent.
        """
        for key, value in headers.items():
            if str(key).lower() == "location":
                return value
        return None

    @staticmethod
    def resolve_absolute(location: str, base_url: str) -> str:
        """Resolve a possibly-relative ``Location`` against the current URL.

        Args:
            location: The raw ``Location`` value.
            base_url: The URL that produced the redirect.

        Returns:
            The absolute redirect URL.
        """
        return urljoin(base_url, location)

    async def validate_and_pin(self, url: str, context: Any) -> RedirectTarget:  # pylint: disable=unused-argument
        """Re-run SSRF validation + connection pinning for one hop.

        This is the security core of §9.10: no redirect hop is taken
        without re-validating the resolved target and pinning the
        connection to the validated IP, exactly like the first hop.

        Args:
            url: The hop URL (already rendered).
            context: The invocation context (supplies ``pool_key_factory``
                inputs, not used directly here).

        Returns:
            A ``RedirectTarget`` describing the validated/pinned URL.

        Raises:
            ProtocolError: ``REST_URL_VALIDATION_FAILED`` when the URL is
                blocked by policy, or ``REST_URL_PINNING_MISSING`` when
                SSRF protection is enabled but no pinned target resolved.
        """
        try:
            validated_target = await SecurityValidator.validate_url_for_connection_pinning(url, "Tool URL")
        except ValueError as validation_error:
            raise ProtocolError(
                category=ErrorCategory.PERMISSION_DENIED,
                code=REST_URL_VALIDATION_FAILED,
                message="Outbound URL blocked by URL policy",
                origin="http",
                retryable=False,
                details={"raw_url": url, "validation_error": str(validation_error)},
            ) from validation_error

        resolved_ip = validated_target.get("resolved_ip")
        original_hostname = validated_target.get("hostname")
        original_authority = validated_target.get("original_authority")

        if settings.ssrf_protection_enabled and not (resolved_ip and original_hostname and original_authority):
            raise ProtocolError(
                category=ErrorCategory.PERMISSION_DENIED,
                code=REST_URL_PINNING_MISSING,
                message="Outbound URL blocked by URL policy",
                origin="http",
                retryable=False,
                details={"raw_url": url},
            )

        if resolved_ip and original_hostname and original_authority:
            pinned_url = pin_url_to_resolved_ip(url, resolved_ip)
            return RedirectTarget(
                url=pinned_url,
                headers={"Host": original_authority},
                extensions={"sni_hostname": original_hostname},
                resolved_ip=resolved_ip,
                hostname=original_hostname,
                original_authority=original_authority,
            )

        return RedirectTarget(url=url)

    def pool_key(self, target: RedirectTarget, context: Any) -> Optional[Tuple]:
        """Compute the pinned-pool isolation key for a validated target.

        Args:
            target: The validated target.
            context: The invocation context (supplies ``pool_key_factory``).

        Returns:
            The pool key, or ``None`` when the target is not pinned.
        """
        if target.resolved_ip and target.hostname and target.original_authority:
            return context.pool_key_factory(target.url, target.resolved_ip, target.hostname, target.original_authority)
        return None
