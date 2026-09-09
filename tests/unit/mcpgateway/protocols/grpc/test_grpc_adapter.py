# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/grpc/test_grpc_adapter.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the GrpcProtocolAdapter (PR7, design §47–§52).
"""

# Standard
from types import SimpleNamespace

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.contracts.models import OperationDefinition
from mcpgateway.protocols.grpc.adapter import GrpcProtocolAdapter
from mcpgateway.protocols.grpc.stream import StreamLimiter
from mcpgateway.protocols.models import ErrorCategory, ProtocolError


def _grpc_operation(**request_overrides) -> OperationDefinition:
    """Build a gRPC OperationDefinition with a configurable request."""
    request = {
        "service_name": "example.Catalog",
        "method": "Get",
        "client_streaming": False,
        "server_streaming": False,
    }
    request.update(request_overrides)
    return OperationDefinition(
        key="example.Catalog:Get",
        protocol="grpc",
        source_operation_id="Get",
        request=request,
        response={},
    )


class _FakeEndpoint:
    """Minimal duck-typed gRPC endpoint."""

    def __init__(self, unary_result=None, stream_items=None):
        """Initialise with canned results."""
        self.unary_result = unary_result or {"ok": True}
        self.stream_items = stream_items or [{"n": i} for i in range(5)]
        self.invoke_calls = []
        self.invoke_streaming_calls = []

    async def invoke(self, service, method, request, timeout=None):
        """Record and return the canned unary result."""
        self.invoke_calls.append((service, method, request, timeout))
        return self.unary_result

    async def invoke_streaming(self, service, method, request, timeout=None):
        """Record and stream the canned items."""
        self.invoke_streaming_calls.append((service, method, request, timeout))
        for item in self.stream_items:
            yield item


def _context(timeout=10.0) -> SimpleNamespace:
    """Build a minimal invocation context exposing remaining_timeout."""
    return SimpleNamespace(remaining_timeout=lambda: timeout)


class TestGrpcProtocolAdapter:
    """GrpcProtocolAdapter maps operations onto endpoint calls."""

    async def test_unary_unary_delegates_to_endpoint_invoke(self):
        """A unary call forwards to endpoint.invoke and returns the result."""
        endpoint = _FakeEndpoint(unary_result={"ok": True})
        adapter = GrpcProtocolAdapter(endpoint)

        result = await adapter.invoke(_grpc_operation(), {"id": "1"}, _context())

        assert result.data == {"ok": True}
        assert result.metadata["grpc_mode"] == "unary_unary"
        assert endpoint.invoke_calls == [("example.Catalog", "Get", {"id": "1"}, 10.0)]

    async def test_unary_stream_returns_bounded_items(self):
        """A server-stream call collects items and flags truncation."""
        endpoint = _FakeEndpoint(stream_items=[{"n": i} for i in range(200)])
        adapter = GrpcProtocolAdapter(endpoint, default_limiter=StreamLimiter(max_items=3))

        result = await adapter.invoke(_grpc_operation(server_streaming=True), {}, _context())

        assert len(result.data["items"]) == 3
        assert result.data["truncated"] is True
        assert result.metadata["grpc_mode"] == "unary_stream"

    async def test_stream_without_truncation(self):
        """A short stream completes without truncation."""
        endpoint = _FakeEndpoint(stream_items=[{"n": 1}, {"n": 2}])
        adapter = GrpcProtocolAdapter(endpoint, default_limiter=StreamLimiter(max_items=10))

        result = await adapter.invoke(_grpc_operation(server_streaming=True), {}, _context())

        assert result.data == {"items": [{"n": 1}, {"n": 2}], "truncated": False}

    async def test_client_streaming_raises_unsupported(self):
        """Client-streaming/bidi modes raise UNSUPPORTED until grpc.aio lands."""
        adapter = GrpcProtocolAdapter(_FakeEndpoint())

        with pytest.raises(ProtocolError) as exc_info:
            await adapter.invoke(_grpc_operation(client_streaming=True), {}, _context())

        assert exc_info.value.category == ErrorCategory.UNSUPPORTED

    async def test_missing_operation_fields_raise_invalid_argument(self):
        """An operation without service/method is rejected."""
        adapter = GrpcProtocolAdapter(_FakeEndpoint())
        operation = _grpc_operation()
        operation = OperationDefinition(
            key="example.Catalog:Get",
            protocol="grpc",
            source_operation_id="Get",
            request={},
            response={},
        )

        with pytest.raises(ProtocolError) as exc_info:
            await adapter.invoke(operation, {}, _context())

        assert exc_info.value.category == ErrorCategory.INVALID_ARGUMENT
