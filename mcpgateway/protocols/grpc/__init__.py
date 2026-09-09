# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/grpc/__init__.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

gRPC protocol layer public API (PR6).

PR6 ships the Proto→JSON Schema mapper extracted from
``GrpcSchemaService``; the runtime adapter (GrpcProtocolAdapter) arrives in
PR7 (§46–§55).
"""

# First-Party
from mcpgateway.protocols.grpc.schema_mapper import ProtoJsonSchemaMapper, proto_json_schema_mapper

__all__ = ["ProtoJsonSchemaMapper", "proto_json_schema_mapper"]
