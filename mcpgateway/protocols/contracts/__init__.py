# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/contracts/__init__.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Contract layer public API.
"""

# First-Party
from mcpgateway.protocols.contracts.base import ContractBuilder
from mcpgateway.protocols.contracts.models import OperationDefinition

__all__ = [
    "ContractBuilder",
    "OperationDefinition",
]
