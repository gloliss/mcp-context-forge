# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/contracts/__init__.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Contract layer public API.
"""

# First-Party
from mcpgateway.protocols.contracts.base import ContractBuilder, ContractProvider
from mcpgateway.protocols.contracts.models import (
    ContractArtifact,
    ContractDiagnostic,
    DiscoveryContext,
    OperationCatalog,
    OperationDefinition,
)

__all__ = [
    "ContractArtifact",
    "ContractBuilder",
    "ContractDiagnostic",
    "ContractProvider",
    "DiscoveryContext",
    "OperationCatalog",
    "OperationDefinition",
]
