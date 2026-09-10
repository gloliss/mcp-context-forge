# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/registry.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Protocol adapter registry (PR1).

PR1 registers the HTTP adapter.  PR7 adds gRPC: a gRPC adapter is
constructed per invocation (it needs a live endpoint, whose lifecycle
``GrpcService`` owns), so the registry maps ``"grpc"`` to a *factory* rather
than a shared instance — see :meth:`ProtocolRegistry.register_factory`.
"""

# Standard
from typing import Any

# First-Party
from mcpgateway.protocols.base import ProtocolAdapter
from mcpgateway.protocols.grpc.adapter import GrpcProtocolAdapter
from mcpgateway.protocols.http.adapter import HttpProtocolAdapter
from mcpgateway.protocols.models import ErrorCategory, InvocationContext, ProtocolError, ProtocolResult


class ProtocolRegistry:
    """Registry mapping protocol names to their ``ProtocolAdapter``."""

    def __init__(self) -> None:
        """Initialise an empty adapter registry."""
        self._adapters: dict[str, ProtocolAdapter] = {}
        self._factories: dict[str, Any] = {}

    def register(self, protocol: str, adapter: ProtocolAdapter) -> None:
        """Register (or replace) the adapter for a protocol.

        Args:
            protocol: Protocol name (e.g. ``"http"``).
            adapter: The adapter instance handling that protocol.
        """
        self._adapters[protocol] = adapter

    def register_factory(self, protocol: str, factory: Any) -> None:
        """Register an adapter factory for a protocol needing per-call state.

        Most adapters are stateless and shareable, but a gRPC adapter wraps one
        live endpoint, and endpoints are built, cached and closed by
        ``GrpcService`` (design §47).  Such a protocol registers a factory and
        is resolved with :meth:`get_factory` instead of :meth:`get`.

        Args:
            protocol: Protocol name (e.g. ``"grpc"``).
            factory: A callable returning a ``ProtocolAdapter``.
        """
        self._factories[protocol] = factory

    def get_factory(self, protocol: str) -> Any:
        """Return the adapter factory registered for a protocol.

        Args:
            protocol: Protocol name.

        Returns:
            The registered factory.

        Raises:
            ProtocolError: UNSUPPORTED when no factory is registered.
        """
        try:
            return self._factories[protocol]
        except KeyError:
            raise ProtocolError(
                category=ErrorCategory.UNSUPPORTED,
                code="UNSUPPORTED_PROTOCOL",
                message=f"Protocol '{protocol}' has no adapter factory registered",
                origin="registry",
                retryable=False,
            ) from None

    def protocols(self) -> tuple[str, ...]:
        """Return the registered protocol names, adapters and factories alike."""
        return tuple(sorted({*self._adapters, *self._factories}))

    def get(self, protocol: str) -> ProtocolAdapter:
        """Return the adapter registered for a protocol.

        Args:
            protocol: Protocol name.

        Returns:
            The registered adapter.

        Raises:
            ProtocolError: UNSUPPORTED when the protocol is not registered.
        """
        try:
            return self._adapters[protocol]
        except KeyError:
            raise ProtocolError(
                category=ErrorCategory.UNSUPPORTED,
                code="UNSUPPORTED_PROTOCOL",
                message=f"Protocol '{protocol}' is not registered",
                origin="registry",
                retryable=False,
            ) from None

    async def invoke(
        self,
        protocol: str,
        operation: Any,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> ProtocolResult:
        """Dispatch one invocation to the protocol's adapter.

        Args:
            protocol: Protocol name selecting the adapter.
            operation: The compiled operation definition.
            arguments: Invocation arguments.
            context: ToolService-injected invocation context.

        Returns:
            The adapter's ``ProtocolResult``.

        Raises:
            ProtocolError: UNSUPPORTED for unknown protocols, or any
                protocol error raised by the adapter itself.
        """
        adapter = self.get(protocol)
        return await adapter.invoke(operation, arguments, context)


def build_default_protocol_registry() -> ProtocolRegistry:
    """Build the default registry with the HTTP and gRPC adapters registered.

    Returns:
        A new registry with ``"http"`` bound to ``HttpProtocolAdapter`` and
        ``"grpc"`` bound to a ``GrpcProtocolAdapter`` factory.
    """
    registry = ProtocolRegistry()
    registry.register("http", HttpProtocolAdapter())
    registry.register_factory("grpc", GrpcProtocolAdapter)
    return registry


# Module-level singleton used by ToolService.invoke_tool (PR1).
protocol_registry = build_default_protocol_registry()
