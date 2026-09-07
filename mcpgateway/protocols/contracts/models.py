# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/contracts/models.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Operation IR models (PR1).

``OperationDefinition`` is the long-lived intermediate representation a
protocol contract (OpenAPI, WSDL, proto, or the legacy REST tool table)
compiles into before invocation.
"""

# Standard
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class OperationDefinition:
    """Protocol-agnostic description of one invocable operation.

    Attributes:
        key: Stable operation key (e.g. ``GET /v1/lots/{lotId}`` for HTTP,
            ``package.Service.Method`` for gRPC, or the PR1 legacy
            ``http:rest:<tool_id>`` form).
        protocol: Target protocol (``"http"`` or ``"grpc"``).
        source_operation_id: Source-specific operation identifier
            (OpenAPI operationId, legacy tool id, ...).
        title: Short human-readable title.
        description: Optional longer description.
        deprecated: Whether the source contract marks this deprecated.
        tags: Source contract tags.
        request: Protocol-specific request shape (PR1 HTTP: url, method,
            query_mapping, header_mapping).
        response: Protocol-specific response shape (PR1 HTTP:
            output_schema, jsonpath_filter).
        extensions: Source/debug/UI metadata only — runtime-required
            information must live in typed fields, never here.
    """

    key: str
    protocol: Literal["http", "grpc"]
    source_operation_id: str | None = None
    title: str | None = None
    description: str | None = None
    deprecated: bool = False
    tags: tuple[str, ...] = ()
    request: Any = None
    response: Any = None
    extensions: dict[str, Any] = field(default_factory=dict)
