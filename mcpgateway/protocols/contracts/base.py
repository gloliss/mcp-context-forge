# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/contracts/base.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Contract SPI definitions (PR1, extended by PR3).

PR1 implements only the legacy-REST contract builder; PR3 adds the
``ContractProvider`` SPI consumed by the HTTP registry (OpenAPI provider).
"""

# Standard
from typing import Any, Protocol

# First-Party
from mcpgateway.protocols.contracts.models import (
    ContractArtifact,
    DiscoveryContext,
    OperationCatalog,
    OperationDefinition,
)


class ContractBuilder(Protocol):
    """SPI: compile a contract source into operation definitions."""

    @classmethod
    def from_tool(cls, tool: Any) -> OperationDefinition:
        """Build an ``OperationDefinition`` from a legacy DB tool.

        Args:
            tool: The legacy ``DbTool`` row (or payload-backed stand-in).

        Returns:
            The compiled operation definition.
        """
        raise NotImplementedError


class ContractProvider(Protocol):
    """SPI: discover operations from a contract artifact (design §6.2)."""

    async def discover(
        self,
        artifact: ContractArtifact,
        context: DiscoveryContext,
    ) -> OperationCatalog:
        """Compile a contract artifact into an ``OperationCatalog``.

        Args:
            artifact: The immutable artifact document (external ``$ref``
                targets already materialised by the caller).
            context: Read-only discovery context (ref fetcher, limits).

        Returns:
            The compiled catalog; partial failures surface as diagnostics,
            whole-document failures raise ``ContractProviderError``.
        """
        raise NotImplementedError
