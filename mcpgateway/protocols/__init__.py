# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/__init__.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Protocol Runtime public API (PR1).

The protocol layer owns the transport-agnostic operation IR, the
adapter SPI, and the per-protocol adapters.  ToolService dispatches
legacy REST invocations through :data:`protocol_registry`.
"""

# First-Party
from mcpgateway.protocols.base import ProtocolAdapter
from mcpgateway.protocols.contracts import ContractBuilder, OperationDefinition
from mcpgateway.protocols.http import HttpProtocolAdapter, LegacyRestContractBuilder
from mcpgateway.protocols.models import ErrorCategory, InvocationContext, ProtocolError, ProtocolResult
from mcpgateway.protocols.registry import ProtocolRegistry, protocol_registry

__all__ = [
    "ContractBuilder",
    "ErrorCategory",
    "HttpProtocolAdapter",
    "InvocationContext",
    "LegacyRestContractBuilder",
    "OperationDefinition",
    "ProtocolAdapter",
    "ProtocolError",
    "ProtocolRegistry",
    "ProtocolResult",
    "protocol_registry",
]
