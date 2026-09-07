# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/registry.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Protocol adapter registry (PR1).

PR1 registers the single HTTP adapter; gRPC joins in PR6/PR7.
"""

# Standard
from typing import Any

# First-Party
from mcpgateway.protocols.base import ProtocolAdapter
from mcpgateway.protocols.http.adapter import HttpProtocolAdapter
from mcpgateway.protocols.models import ErrorCategory, InvocationContext, ProtocolError, ProtocolResult


class ProtocolRegistry:
    """Registry mapping protocol names to their ``ProtocolAdapter``."""

    def __init__(self) -> None:
        """Initialise an empty adapter registry."""
        self._adapters: dict[str, ProtocolAdapter] = {}

    def register(self, protocol: str, adapter: ProtocolAdapter) -> None:
        """Register (or replace) the adapter for a protocol.

        Args:
            protocol: Protocol name (e.g. ``"http"``).
            adapter: The adapter instance handling that protocol.
        """
        self._adapters[protocol] = adapter

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
    """Build the default registry with the PR1 adapters registered.

    Returns:
        A new registry with ``"http"`` bound to ``HttpProtocolAdapter``.
    """
    registry = ProtocolRegistry()
    registry.register("http", HttpProtocolAdapter())
    return registry


# Module-level singleton used by ToolService.invoke_tool (PR1).
protocol_registry = build_default_protocol_registry()
