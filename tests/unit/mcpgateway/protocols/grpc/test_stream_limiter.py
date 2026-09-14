# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/grpc/test_stream_limiter.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the bounded StreamLimiter (PR7, design §52).
"""

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.grpc.stream import StreamLimitError, StreamLimiter


async def _async_iter(items):
    """Wrap an iterable into an async generator."""
    for item in items:
        yield item


class TestStreamLimiter:
    """StreamLimiter caps items/bytes/idle/deadline (§52)."""

    async def test_passes_all_items_within_limits(self):
        """Items under the caps are all yielded."""
        limiter = StreamLimiter(max_items=10)
        collected = [item async for item in limiter.bounded(_async_iter([1, 2, 3]))]

        assert collected == [1, 2, 3]

    async def test_truncates_at_max_items(self):
        """max_items stops the stream and raises StreamLimitError."""
        limiter = StreamLimiter(max_items=2)
        bounded = limiter.bounded(_async_iter([1, 2, 3, 4]))
        collected = []
        with pytest.raises(StreamLimitError):
            async for item in bounded:
                collected.append(item)

        assert collected == [1, 2]

    async def test_truncates_at_max_bytes(self):
        """max_bytes stops the stream once the byte budget is exceeded."""
        limiter = StreamLimiter(max_items=0, max_bytes=6, serialize=len)
        bounded = limiter.bounded(_async_iter(["aa", "bb", "cc", "dd"]))
        collected = []
        with pytest.raises(StreamLimitError):
            async for item in bounded:
                collected.append(item)

        # Three 2-byte items fit within a 6-byte budget; the fourth exceeds it.
        assert collected == ["aa", "bb", "cc"]

    async def test_zero_max_items_disables_item_cap(self):
        """max_items=0 means no item cap."""
        limiter = StreamLimiter(max_items=0)
        collected = [item async for item in limiter.bounded(_async_iter([1, 2, 3]))]

        assert collected == [1, 2, 3]

    async def test_idle_timeout_stops_slow_streams(self):
        """idle_timeout raises once the gap between items is too large."""
        import asyncio

        async def slow():
            yield 1
            await asyncio.sleep(0.05)
            yield 2

        limiter = StreamLimiter(idle_timeout=0.01)
        with pytest.raises(StreamLimitError):
            async for _ in limiter.bounded(slow()):
                pass

    async def test_deadline_stops_stream(self):
        """A passed deadline stops the stream immediately."""
        import time

        limiter = StreamLimiter(deadline=time.monotonic() - 1)
        with pytest.raises(StreamLimitError):
            async for _ in limiter.bounded(_async_iter([1, 2, 3])):
                pass
