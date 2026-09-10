# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_grpc_stream_limits.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for configurable gRPC streaming limits (PR6, design §42/§44).

The limiter and its byte accounting live with the adapter that applies them
(PR7 T7.8 consolidated the two implementations); these tests target that one
implementation.
"""

# Third-Party

# First-Party
from mcpgateway.protocols.grpc.adapter import GrpcProtocolAdapter
from mcpgateway.protocols.grpc.stream import StreamLimiter, item_size, serialize_item


async def _collect(stream, *, max_items=100, max_bytes=0, stream_callback=None):
    """Run the adapter's bounded collector over a stream (design §52)."""
    endpoint = _StreamEndpoint(stream)
    adapter = GrpcProtocolAdapter(endpoint, default_limiter=StreamLimiter(max_items=max_items, max_bytes=max_bytes, serialize=item_size))
    result = await adapter.invoke_via_endpoint(_stream_operation(), {}, stream_callback=stream_callback)
    return result.data


class _StreamEndpoint:
    """Duck-typed endpoint yielding one pre-built stream."""

    def __init__(self, stream):
        """Store the stream to hand back."""
        self._stream = stream

    def invoke_streaming(self, _service, _method, _request, timeout=None):
        """Return the stored stream (a plain async generator)."""
        return self._stream


def _stream_operation():
    """Build a server-streaming operation with no explicit limits."""
    # First-Party
    from mcpgateway.protocols.contracts.models import OperationDefinition

    return OperationDefinition(key="svc.Method", protocol="grpc", request={"service_name": "svc", "method": "Method", "server_streaming": True})


async def _async_iter(items):
    """Wrap an iterable into an async generator."""
    for item in items:
        yield item


class TestCollectBoundedStream:
    """_collect_bounded_stream applies item and byte caps (§44)."""

    async def test_collects_all_when_within_limits(self):
        """Fewer items than the cap are returned untruncated."""
        result = await _collect(_async_iter([{"n": 1}, {"n": 2}, {"n": 3}]), max_items=10)

        assert result == {"items": [{"n": 1}, {"n": 2}, {"n": 3}], "truncated": False}

    async def test_truncates_at_max_items(self):
        """Reaching max_items stops collection and flags truncation."""
        result = await _collect(_async_iter([{"n": i} for i in range(10)]), max_items=3)

        assert result["items"] == [{"n": 0}, {"n": 1}, {"n": 2}]
        assert result["truncated"] is True

    async def test_truncates_at_max_bytes(self):
        """Reaching max_bytes stops collection and flags truncation."""
        # Each item serialises to about 8 bytes; a 20-byte budget admits 2.
        result = await _collect(_async_iter([{"n": i} for i in range(10)]), max_items=100, max_bytes=20)

        assert len(result["items"]) == 2
        assert result["truncated"] is True

    async def test_zero_limits_mean_no_extra_caps(self):
        """max_items=0/max_bytes=0 disables both caps."""
        result = await _collect(_async_iter([{"n": i} for i in range(5)]), max_items=0, max_bytes=0)

        assert len(result["items"]) == 5
        assert result["truncated"] is False

    async def test_stream_callback_receives_each_item(self):
        """An optional callback sees every admitted item."""
        seen = []

        async def callback(item):
            seen.append(item)

        await _collect(_async_iter([{"n": 1}, {"n": 2}]), max_items=2, stream_callback=callback)

        assert seen == [{"n": 1}, {"n": 2}]


class TestSerializeItem:
    """_serialize_item produces a byte-accounting string."""

    def test_dict_items_serialize_as_json(self):
        """Dict items serialise to JSON for byte accounting."""
        assert serialize_item({"a": 1}) == '{"a": 1}'

    def test_scalar_items_stringify(self):
        """Scalar items fall back to str()."""
        assert serialize_item("plain") == "plain"
