# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/http/__init__.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

HTTP protocol layer public API.

NOTE: this package ``__init__`` is an addition beyond the design document
file list (PR1), required for ``mcpgateway.protocols.http`` to be a
regular package alongside the planned PR2 modules.
"""

# First-Party
from mcpgateway.protocols.http.adapter import HttpProtocolAdapter
from mcpgateway.protocols.http.legacy_contract import LegacyRestContractBuilder
from mcpgateway.protocols.http.models import (
    HttpBodyVariant,
    HttpParameter,
    HttpRequestContract,
    HttpResponseContract,
    HttpResponseVariant,
)

__all__ = [
    "HttpBodyVariant",
    "HttpParameter",
    "HttpProtocolAdapter",
    "HttpRequestContract",
    "HttpResponseContract",
    "HttpResponseVariant",
    "LegacyRestContractBuilder",
]
