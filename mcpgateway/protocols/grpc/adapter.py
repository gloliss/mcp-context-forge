# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/grpc/adapter.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

gRPC protocol adapter (PR7, design-document §46–§52).

``GrpcProtocolAdapter`` adapts an :class:`OperationDefinition` (protocol
``"grpc"``) into a call against a duck-typed gRPC endpoint (the
``GrpcEndpoint`` from ``mcpgateway.translate_grpc``).  The adapter is
invoked by the protocol registry and owns the bounded-stream behaviour:

* **unary → unary** delegates to ``endpoint.invoke``;
* **unary → server stream** iterates ``endpoint.invoke_streaming`` through
  a :class:`StreamLimiter` (design §52), returning ``{"items": ...,
  "truncated": bool}``;
* **client stream → unary** sends ``{"items": [...]}`` through
  ``endpoint.invoke_client_stream`` and returns the single response;
* **bidi** sends the same items through ``endpoint.invoke_bidi_stream``
  and bounds the response with the same limiter.

Client-streaming and bidirectional calls (design §48) run on the aio channel
that landed with the grpc.aio migration (design §53).  Their request side
uses the MCP Stream input model ``{"items": [...]}`` (design §49/§50); the
bidi response side is bounded by the same :class:`StreamLimiter` as the
server-streaming path (design §52).
"""

# Standard
from typing import Any, Awaitable, Callable, Optional

# First-Party
from mcpgateway.protocols.contracts.models import OperationDefinition
from mcpgateway.protocols.grpc.stream import StreamLimitError, StreamLimiter, item_size
from mcpgateway.protocols.models import ErrorCategory, InvocationContext, ProtocolError, ProtocolResult


class GrpcProtocolAdapter:
    """Call a gRPC operation through a duck-typed endpoint (design §47)."""

    def __init__(self, endpoint: Any, default_limiter: Optional[StreamLimiter] = None) -> None:
        """Initialise with the endpoint and an optional default limiter.

        Args:
            endpoint: An object exposing ``invoke(service, method, request,
                timeout=...)`` and ``invoke_streaming(service, method,
                request, timeout=...)``.
            default_limiter: Stream limiter used when the operation does
                not configure one.
        """
        self._endpoint = endpoint
        self._default_limiter = default_limiter or StreamLimiter(max_items=100, serialize=item_size)

    async def invoke(self, operation: OperationDefinition, arguments: dict[str, Any], context: InvocationContext) -> ProtocolResult:
        """Invoke a gRPC operation.

        Args:
            operation: The compiled gRPC operation.  ``operation.request``
                carries ``service_name``, ``method``, ``client_streaming``
                and ``server_streaming`` flags.
            arguments: The MCP tool arguments (the request payload).
            context: The invocation context (provides the timeout budget).

        Returns:
            A ``ProtocolResult``.  Server-streaming calls produce
            ``{"items": [...], "truncated": bool}``.

        Raises:
            ProtocolError: For unsupported streaming modes or endpoint
                failures.
        """
        timeout = context.remaining_timeout() if callable(getattr(context, "remaining_timeout", None)) else None
        return await self.invoke_via_endpoint(operation, arguments, timeout=timeout)

    async def invoke_via_endpoint(
        self,
        operation: OperationDefinition,
        arguments: dict[str, Any],
        *,
        timeout: Optional[float] = None,
        stream_callback: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> ProtocolResult:
        """Invoke an operation against this adapter's endpoint (design §47).

        This is the entry point for callers that already own the endpoint
        lifecycle (``GrpcService.invoke_method`` builds, caches and closes
        endpoints) and only need the *call shape* chosen correctly.  Keeping
        the four-mode dispatch here means there is exactly one implementation
        of "which of the four RPC classes is this", regardless of whether the
        call arrives through the protocol registry or through the gRPC
        service.

        Args:
            operation: The compiled gRPC operation.  ``operation.request``
                carries ``service_name``, ``method``, ``client_streaming``
                and ``server_streaming``.
            arguments: The MCP tool arguments (the request payload).
            timeout: The deadline in seconds, or ``None`` for no deadline.
            stream_callback: Optional async per-item callback, awaited as
                each streamed item is collected.  ToolService uses it to
                forward items to SSE consumers.

        Returns:
            A ``ProtocolResult``.

        Raises:
            ProtocolError: For incomplete operations, a missing message list,
                or endpoint failures.
        """
        request = operation.request or {}
        service_name = request.get("service_name")
        method = request.get("method")
        if not service_name or not method:
            raise ProtocolError(
                category=ErrorCategory.INVALID_ARGUMENT,
                code="grpc-operation-incomplete",
                message="gRPC operation missing service_name or method",
                origin="grpc",
                retryable=False,
            )

        client_streaming = bool(request.get("client_streaming"))
        server_streaming = bool(request.get("server_streaming"))

        if not client_streaming and not server_streaming:
            response = await self._endpoint.invoke(service_name, method, arguments, timeout=timeout)
            return ProtocolResult(data=response, metadata={"grpc_mode": "unary_unary"})

        if server_streaming and not client_streaming:
            return await self._invoke_server_stream(service_name, method, arguments, timeout, operation, stream_callback)

        items = self._request_items(arguments)
        if client_streaming and not server_streaming:
            response = await self._endpoint.invoke_client_stream(service_name, method, items, timeout=timeout)
            return ProtocolResult(data=response, metadata={"grpc_mode": "stream_unary"})

        return await self._invoke_bidi_stream(service_name, method, items, timeout, operation, stream_callback)

    @staticmethod
    def _request_items(arguments: dict[str, Any]) -> list[Any]:
        """Extract the client-side message list from MCP arguments (§49/§50).

        The MCP Stream input model is ``{"items": [...]}``: a tool call that
        feeds a client-streaming or bidi RPC supplies the message list under
        ``items``.  A bare list is accepted too, so a caller that already has
        the sequence does not have to wrap it.

        Args:
            arguments: The MCP tool arguments.

        Returns:
            The list of request messages to send upstream.

        Raises:
            ProtocolError: When the arguments carry no message list.
        """
        items = arguments.get("items") if isinstance(arguments, dict) else None
        if items is None and isinstance(arguments, list):
            items = arguments
        if not isinstance(items, list) or not items:
            raise ProtocolError(
                category=ErrorCategory.INVALID_ARGUMENT,
                code="grpc-stream-items-required",
                message='Client-streaming and bidirectional calls require a non-empty {"items": [...]} argument',
                origin="grpc",
                retryable=False,
            )
        return list(items)

    async def _invoke_bidi_stream(
        self,
        service_name: str,
        method: str,
        items: list[Any],
        timeout: Optional[float],
        operation: OperationDefinition,
        stream_callback: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> ProtocolResult:
        """Invoke a bidirectional operation with bounded collection (§48/§52)."""
        stream = self._endpoint.invoke_bidi_stream(service_name, method, items, timeout=timeout)
        collected, truncated = await self._collect(operation, stream, stream_callback)
        return ProtocolResult(data={"items": collected, "truncated": truncated}, metadata={"grpc_mode": "stream_stream"})

    async def _invoke_server_stream(
        self,
        service_name: str,
        method: str,
        arguments: dict[str, Any],
        timeout: Optional[float],
        operation: OperationDefinition,
        stream_callback: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> ProtocolResult:
        """Invoke a unary→server-stream operation with bounded collection."""
        stream = self._endpoint.invoke_streaming(service_name, method, arguments, timeout=timeout)
        items, truncated = await self._collect(operation, stream, stream_callback)
        return ProtocolResult(data={"items": items, "truncated": truncated}, metadata={"grpc_mode": "unary_stream"})

    async def _collect(
        self,
        operation: OperationDefinition,
        stream: Any,
        stream_callback: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> tuple[list[Any], bool]:
        """Collect a bounded stream, notifying the callback per item.

        Args:
            operation: The operation (supplies the stream limits, §52).
            stream: The async iterator to consume.
            stream_callback: Optional async per-item callback, awaited as each
                item is accepted.  It is deliberately *not* called for an item
                the limiter rejects, so a consumer never sees an item the
                result does not contain.

        Returns:
            A ``(items, truncated)`` pair.
        """
        limiter = self._limiter_for(operation)
        items: list[Any] = []
        truncated = False
        try:
            async for item in limiter.bounded(stream):
                items.append(item)
                if stream_callback is not None:
                    await stream_callback(item)
        except StreamLimitError:
            truncated = True
        return items, truncated

    def _limiter_for(self, operation: OperationDefinition) -> StreamLimiter:
        """Resolve the stream limiter from operation extensions, if any."""
        streaming = (operation.extensions or {}).get("streaming") or {}
        if not streaming:
            return self._default_limiter
        return StreamLimiter(
            max_items=int(streaming.get("maxItems") or 0),
            max_bytes=int(streaming.get("maxBytes") or 0),
            idle_timeout=float(streaming.get("idleTimeoutMs") or 0) / 1000.0,
            serialize=item_size,
        )


__all__ = ["GrpcProtocolAdapter"]
