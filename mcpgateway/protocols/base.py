# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/base.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Core Protocol Runtime SPI definitions (PR1).

Only the three SPIs from the design document exist here; PR1 implements
just ``ProtocolAdapter`` (the HTTP variant).
"""

# Standard
from typing import Any, Protocol

# First-Party
from mcpgateway.protocols.models import InvocationContext, ProtocolResult


class ProtocolAdapter(Protocol):
    """SPI: execute one protocol operation against its transport.

    Implementations receive the compiled ``OperationDefinition``, the
    invocation arguments, and the ToolService-injected
    ``InvocationContext``, and return a ``ProtocolResult`` on success or
    raise a ``ProtocolError`` on failure.
    """

    async def invoke(
        self,
        operation: Any,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> ProtocolResult:
        """Execute the operation and return the decoded upstream result.

        Args:
            operation: The compiled operation definition for the target.
            arguments: Invocation arguments (path params are popped from a
                copy; the caller's dict is never mutated).
            context: Per-invocation runtime context injected by ToolService.

        Returns:
            The decoded upstream result.

        Raises:
            ProtocolError: For any structured invocation failure.
        """
        raise NotImplementedError
