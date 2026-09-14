# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_error_mapping.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for HTTP status→error-category mapping and retry policy
(PR8, design §68/§73).
"""

# First-Party
from mcpgateway.protocols.http.adapter import is_retryable_http_method, map_http_status_to_category
from mcpgateway.protocols.models import ErrorCategory


class TestMapHttpStatusToCategory:
    """HTTP status → canonical error category (§73)."""

    def test_4xx_mapping(self):
        """Client errors map to their canonical categories."""
        assert map_http_status_to_category(400) == ErrorCategory.INVALID_ARGUMENT
        assert map_http_status_to_category(401) == ErrorCategory.UNAUTHENTICATED
        assert map_http_status_to_category(403) == ErrorCategory.PERMISSION_DENIED
        assert map_http_status_to_category(404) == ErrorCategory.NOT_FOUND
        assert map_http_status_to_category(409) == ErrorCategory.CONFLICT
        assert map_http_status_to_category(429) == ErrorCategory.RATE_LIMITED

    def test_503_maps_to_unavailable(self):
        """503 maps to UNAVAILABLE."""
        assert map_http_status_to_category(503) == ErrorCategory.UNAVAILABLE

    def test_other_statuses_map_to_upstream_error(self):
        """Unlisted statuses fall back to UPSTREAM_ERROR."""
        assert map_http_status_to_category(502) == ErrorCategory.UPSTREAM_ERROR
        assert map_http_status_to_category(418) == ErrorCategory.UPSTREAM_ERROR
        assert map_http_status_to_category(None) == ErrorCategory.UPSTREAM_ERROR


class TestIsRetryableHttpMethod:
    """Retry policy (§68)."""

    def test_read_methods_always_retryable(self):
        """GET/HEAD/OPTIONS are always safe to retry."""
        assert is_retryable_http_method("GET") is True
        assert is_retryable_http_method("HEAD") is True
        assert is_retryable_http_method("OPTIONS") is True

    def test_put_delete_retryable_on_transient(self):
        """Idempotent PUT/DELETE retry on transient statuses only."""
        assert is_retryable_http_method("PUT", status_code=503) is True
        assert is_retryable_http_method("DELETE", status_code=429) is True
        assert is_retryable_http_method("PUT", status_code=200) is False

    def test_post_patch_never_retryable(self):
        """POST/PATCH are never auto-retried."""
        assert is_retryable_http_method("POST", status_code=503) is False
        assert is_retryable_http_method("PATCH", status_code=429) is False

    def test_unknown_method_not_retryable(self):
        """Unknown methods default to not retryable."""
        assert is_retryable_http_method("TRACE") is False
