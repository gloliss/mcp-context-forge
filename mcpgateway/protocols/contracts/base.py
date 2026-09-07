# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/contracts/base.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Contract SPI definitions (PR1).

PR1 implements only the legacy-REST contract builder; OpenAPI/WSDL
providers arrive in later PRs.
"""

# Standard
from typing import Any, Protocol

# First-Party
from mcpgateway.protocols.contracts.models import OperationDefinition


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
