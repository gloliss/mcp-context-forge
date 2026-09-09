# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/http/legacy_contract.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Legacy REST tool contract compiler (PR1, extended by PR3).

Compiles the pre-existing REST tool table (``integration_type == "REST"``)
into an ``OperationDefinition`` without any database migration.  The
``tool_payload`` dict is the authoritative source because the cache-hit
invocation path resolves the tool from the in-memory payload and may pass
``tool=None``; the ORM row is only a fallback for identity fields.

PR3 adds the registry-tool branch: tools carrying a ``protocol_config``
compile to an ``http:registry:`` operation whose runtime URL comes from
``base_url`` and whose method/path come from the config itself.
"""

# Standard
from collections.abc import Mapping
from typing import Any, Optional

# First-Party
from mcpgateway.protocols.contracts.models import OperationDefinition


class LegacyRestContractBuilder:
    """Build an ``OperationDefinition`` from a legacy REST DbTool/payload.

    The runtime-required values (URL, method, mappings, response pipeline
    inputs) are copied into typed ``request``/``response`` fields; only
    identity metadata (tool id, gateway id, computed name) lands in
    ``extensions``, per the IR's extension policy.
    """

    @classmethod
    def from_tool(
        cls,
        tool: Any,
        tool_payload: Optional[Mapping[str, Any]] = None,
    ) -> OperationDefinition:
        """Compile a legacy REST tool into an operation definition.

        Args:
            tool: The legacy ``DbTool`` ORM row, or ``None`` on the
                cache-hit invocation path.
            tool_payload: The in-memory tool payload; authoritative for
                URL/request_type/mappings when provided.

        Returns:
            The compiled ``OperationDefinition`` for protocol ``"http"``.
        """
        payload = tool_payload or {}
        tool_id = payload.get("id") or (str(tool.id) if tool else "")
        request_type = payload.get("request_type")
        # Mirrors the pre-extraction method derivation verbatim:
        # method = tool_request_type.upper() if tool_request_type else "POST"
        method = request_type.upper() if request_type else "POST"
        query_mapping = payload.get("query_mapping")
        header_mapping = payload.get("header_mapping")
        protocol_config = payload.get("protocol_config")
        extensions = {
            "tool_id": tool_id,
            "gateway_id": payload.get("gateway_id"),
            "tool_name_computed": payload.get("name"),
        }
        # Registry-compiled tools (PR3) carry a protocol_config; their
        # runtime URL comes from the tool's base_url column (written by the
        # HTTP registry sync to http_service.base_url) and the request
        # method/path from the config itself.
        if isinstance(protocol_config, dict):
            request_config = protocol_config.get("request") or {}
            return OperationDefinition(
                key=f"http:registry:{tool_id}",
                protocol="http",
                source_operation_id=protocol_config.get("operationRef") or tool_id or None,
                title=payload.get("name"),
                description=payload.get("description"),
                request={
                    "base_url": payload.get("base_url") or payload.get("url"),
                    "method": str(request_config.get("method") or method).upper(),
                    "path_template": request_config.get("pathTemplate"),
                },
                response={
                    "output_schema": payload.get("output_schema"),
                },
                extensions=extensions,
            )
        return OperationDefinition(
            key=f"http:rest:{tool_id}",
            protocol="http",
            source_operation_id=tool_id or None,
            title=payload.get("name"),
            request={
                "url": payload.get("url"),
                "method": method,
                "query_mapping": query_mapping if isinstance(query_mapping, dict) else None,
                "header_mapping": header_mapping if isinstance(header_mapping, dict) else None,
            },
            response={
                "output_schema": payload.get("output_schema"),
                "jsonpath_filter": payload.get("jsonpath_filter"),
            },
            extensions=extensions,
        )
