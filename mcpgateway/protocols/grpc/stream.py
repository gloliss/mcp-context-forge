# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/grpc/stream.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Configurable stream limiting (PR7, design-document §52).

``StreamLimiter`` centralises the four limits shared by gRPC streaming and
(forward) HTTP SSE: ``max_items``, ``max_bytes``, ``idle_timeout`` and
``deadline``.  A bounded async iterator adapts any async stream into one
that stops once any limit is reached, flagging truncation.
"""

# Standard
import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional


@dataclass(frozen=True)
class StreamLimiter:
    """Bounded-stream policy (design §52).

    Attributes:
        max_items: Maximum number of items (0 disables).
        max_bytes: Maximum accumulated serialised bytes (0 disables).
        idle_timeout: Maximum seconds between items (0 disables).
        deadline: Absolute monotonic deadline after which the stream stops
            (``None`` disables; used by callers passing a computed
            ``time.monotonic() + budget``).
        serialize: Optional per-item byte-size function; defaults to
            ``len(str(item))``.
    """

    max_items: int = 0
    max_bytes: int = 0
    idle_timeout: float = 0
    deadline: Optional[float] = None
    serialize: Optional[Callable[[Any], int]] = None

    def bounded(self, stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
        """Adapt an async stream into one bounded by this limiter.

        Args:
            stream: The underlying async iterator.

        Yields:
            Items from ``stream`` until a limit is reached.

        Raises:
            StreamLimitError: When a limit is reached before the stream
                completes.
        """
        return _BoundedAsyncIterator(self, stream)


class StreamLimitError(Exception):
    """Raised when a stream limit is reached before the stream completes.

    Deliberately *not* a ``StopAsyncIteration`` subclass: an ``async for``
    loop treats ``StopAsyncIteration`` as normal completion and would
    swallow the signal, whereas callers need to observe truncation.
    """


class _BoundedAsyncIterator:
    """Async-iterator adapter enforcing a :class:`StreamLimiter`."""

    def __init__(self, limiter: StreamLimiter, stream: AsyncIterator[Any]) -> None:
        """Initialise with the limiter and the underlying stream."""
        self._limiter = limiter
        self._stream = stream
        self._count = 0
        self._bytes = 0
        self._last_item_at = asyncio.get_event_loop().time()

    def __aiter__(self) -> "_BoundedAsyncIterator":
        """Return self as the async iterator."""
        return self

    async def __anext__(self) -> Any:
        """Return the next bounded item, enforcing the limits."""
        limiter = self._limiter
        loop = asyncio.get_event_loop()
        if limiter.max_items and self._count >= limiter.max_items:
            raise StreamLimitError("stream exceeded max_items")

        deadline = limiter.deadline
        if deadline is not None and loop.time() >= deadline:
            raise StreamLimitError("stream deadline exceeded")

        item = await self._stream.__anext__()

        # Idle timeout measures the gap between *received* items, so it is
        # evaluated after the upstream fetch returns.
        now = loop.time()
        if limiter.idle_timeout and self._count > 0 and (now - self._last_item_at) > limiter.idle_timeout:
            raise StreamLimitError("stream idle timeout exceeded")

        self._count += 1
        if limiter.max_bytes:
            size = limiter.serialize(item) if limiter.serialize else len(str(item))
            if self._bytes and self._bytes + size > limiter.max_bytes:
                raise StreamLimitError("stream exceeded max_bytes")
            self._bytes += size
        self._last_item_at = now
        return item


__all__ = ["StreamLimiter", "StreamLimitError"]
