# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/test_models.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the Protocol Runtime data models (PR1).
"""

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.contracts.models import OperationDefinition
from mcpgateway.protocols.models import ErrorCategory, InvocationContext, ProtocolError, ProtocolResult


def test_protocol_result_defaults_are_zero():
    """ProtocolResult transport counters default to zero/False."""
    result = ProtocolResult(data={"ok": True}, metadata={"status_code": 200})
    assert result.data == {"ok": True}
    assert result.metadata == {"status_code": 200}
    assert result.bytes_in == 0
    assert result.bytes_out == 0
    assert result.duration_ms == 0
    assert result.truncated is False


def test_error_category_has_the_eleven_cf_categories():
    """ErrorCategory enumerates the [CF] Error Model's eleven categories."""
    expected = {
        "INVALID_ARGUMENT",
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "NOT_FOUND",
        "CONFLICT",
        "FAILED_PRECONDITION",
        "RATE_LIMITED",
        "UNSUPPORTED",
        "UNAVAILABLE",
        "UPSTREAM_ERROR",
        "INTERNAL",
    }
    assert {member.value for member in ErrorCategory} == expected


def test_protocol_error_is_an_exception_with_the_message_text():
    """ProtocolError is raiseable and str() returns the legacy message text."""
    error = ProtocolError(
        category=ErrorCategory.PERMISSION_DENIED,
        code="REST_URL_VALIDATION_FAILED",
        message="Outbound URL blocked by URL policy",
        origin="http",
        retryable=False,
    )
    with pytest.raises(ProtocolError, match="Outbound URL blocked by URL policy"):
        raise error
    assert str(error) == "Outbound URL blocked by URL policy"
    assert error.category is ErrorCategory.PERMISSION_DENIED
    assert error.protocol_status is None
    assert error.trace_id is None
    assert error.details is None


def test_protocol_error_carries_status_trace_and_details():
    """ProtocolError optional fields (status, trace, details) round-trip."""
    error = ProtocolError(
        category=ErrorCategory.UPSTREAM_ERROR,
        code="REST_HTTP_STATUS_ERROR",
        message="HTTP 502",
        origin="http",
        retryable=False,
        protocol_status=502,
        trace_id="trace-1",
        details={"elapsed_ms": 12.5},
    )
    assert error.protocol_status == 502
    assert error.trace_id == "trace-1"
    assert error.details == {"elapsed_ms": 12.5}


def test_invocation_context_defaults():
    """InvocationContext provides empty defaults for injected infrastructure."""
    context = InvocationContext(
        tool_name="demo",
        tool_name_computed="demo",
        tool_id="tool-1",
        effective_timeout=30.0,
        remaining_timeout=lambda: 30.0,
        http_client=None,
    )
    assert context.headers == {}
    assert context.send_with_retry is None
    assert context.pinned_rest_pool is None
    assert context.pinned_client_builder is None
    assert context.pool_key_factory is None
    assert context.apply_mapping is None
    assert context.validate_header_mapping_targets is None
    assert context.invalid_header_value_chars is None
    assert context.child_span_factory is None
    assert context.remaining_timeout() == 30.0


def test_operation_definition_is_frozen_with_defaults():
    """OperationDefinition is frozen and metadata fields default to empty."""
    operation = OperationDefinition(key="http:rest:tool-1", protocol="http")
    assert operation.source_operation_id is None
    assert operation.title is None
    assert operation.description is None
    assert operation.deprecated is False
    assert operation.tags == ()
    assert operation.request is None
    assert operation.response is None
    assert operation.extensions == {}
    with pytest.raises(AttributeError):
        operation.key = "changed"
