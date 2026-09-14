# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/grpc/__init__.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

gRPC protocol layer public API (PR6/PR7).

PR6 ships the Proto→JSON Schema mapper extracted from
``GrpcSchemaService``; PR7 adds the protocol adapter and the bounded
``StreamLimiter`` (design §52).  The grpc.aio channel migration and the
client-stream/bidi modes land with the remainder of PR7 (§53–§55).
"""

# First-Party
from mcpgateway.protocols.grpc.adapter import GrpcProtocolAdapter
from mcpgateway.protocols.grpc.schema_mapper import ProtoJsonSchemaMapper, proto_json_schema_mapper
from mcpgateway.protocols.grpc.stream import StreamLimitError, StreamLimiter

__all__ = [
    "GrpcProtocolAdapter",
    "ProtoJsonSchemaMapper",
    "StreamLimitError",
    "StreamLimiter",
    "proto_json_schema_mapper",
]
