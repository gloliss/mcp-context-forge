# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/grpc/schema_mapper.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Proto → JSON Schema mapper (PR6, design-document §37–§39).

``ProtoJsonSchemaMapper`` centralises the protobuf → JSON Schema conversion
that previously lived inside ``GrpcSchemaService._field_schema`` /
``_message_schema`` (design §37: "把当前 GrpcSchemaService._field_schema /
_message_schema 逐步迁入 Mapper，GrpcSchemaService 最终调用 Mapper").

Two ProtoJSON correctness fixes ship here:

* 64-bit integers (design §38) are emitted as JSON *strings* with a numeric
  ``pattern`` — ``int64/uint64/fixed64/sfixed64/sint64`` — because their
  canonical ProtoJSON form is a string, never a raw JSON number.
* the Well-Known Types are covered end to end (design §39) following
  ``[PB] ProtoJSON`` semantics.
"""

# Standard
from collections import defaultdict
from typing import Any, Callable

# Third-Party
from google.protobuf.descriptor import FieldDescriptor

# 64-bit scalar types serialise as JSON strings in ProtoJSON (design §38).
_SIGNED_64_BIT_PATTERN = r"^-?[0-9]+$"
_UNSIGNED_64_BIT_PATTERN = r"^[0-9]+$"


class ProtoJsonSchemaMapper:
    """Convert protobuf descriptors into JSON Schema fragments."""

    # Well-Known Type → JSON Schema (design §39, ProtoJSON semantics).
    _WELL_KNOWN_TYPES = {
        "google.protobuf.Timestamp": {"type": "string", "format": "date-time"},
        "google.protobuf.Duration": {"type": "string", "pattern": r"^-?[0-9]+(?:\.[0-9]+)?s$"},
        "google.protobuf.Any": {"type": "object", "additionalProperties": True},
        "google.protobuf.Struct": {"type": "object", "additionalProperties": True},
        "google.protobuf.Value": {},
        "google.protobuf.ListValue": {"type": "array"},
        "google.protobuf.FieldMask": {"type": "string"},
        "google.protobuf.Empty": {"type": "object"},
        # Wrapper types serialise as their wrapped scalar (ProtoJSON).
        "google.protobuf.DoubleValue": {"type": "number", "format": "double"},
        "google.protobuf.FloatValue": {"type": "number", "format": "float"},
        "google.protobuf.Int64Value": {"type": "string", "pattern": _SIGNED_64_BIT_PATTERN},
        "google.protobuf.UInt64Value": {"type": "string", "pattern": _UNSIGNED_64_BIT_PATTERN},
        "google.protobuf.Int32Value": {"type": "integer"},
        "google.protobuf.UInt32Value": {"type": "integer", "minimum": 0},
        "google.protobuf.BoolValue": {"type": "boolean"},
        "google.protobuf.StringValue": {"type": "string"},
        "google.protobuf.BytesValue": {"type": "string", "contentEncoding": "base64"},
    }

    _SCALAR_TYPES = {
        FieldDescriptor.TYPE_DOUBLE: {"type": "number", "format": "double"},
        FieldDescriptor.TYPE_FLOAT: {"type": "number", "format": "float"},
        # 64-bit → JSON string (design §38).
        FieldDescriptor.TYPE_INT64: {"type": "string", "pattern": _SIGNED_64_BIT_PATTERN},
        FieldDescriptor.TYPE_UINT64: {"type": "string", "pattern": _UNSIGNED_64_BIT_PATTERN},
        FieldDescriptor.TYPE_INT32: {"type": "integer", "format": "int32"},
        FieldDescriptor.TYPE_FIXED64: {"type": "string", "pattern": _UNSIGNED_64_BIT_PATTERN},
        FieldDescriptor.TYPE_FIXED32: {"type": "integer", "minimum": 0},
        FieldDescriptor.TYPE_BOOL: {"type": "boolean"},
        FieldDescriptor.TYPE_STRING: {"type": "string"},
        FieldDescriptor.TYPE_BYTES: {"type": "string", "contentEncoding": "base64"},
        FieldDescriptor.TYPE_UINT32: {"type": "integer", "minimum": 0},
        FieldDescriptor.TYPE_SFIXED32: {"type": "integer"},
        FieldDescriptor.TYPE_SFIXED64: {"type": "string", "pattern": _SIGNED_64_BIT_PATTERN},
        FieldDescriptor.TYPE_SINT32: {"type": "integer"},
        FieldDescriptor.TYPE_SINT64: {"type": "string", "pattern": _SIGNED_64_BIT_PATTERN},
    }

    def field_schema(self, field: FieldDescriptor, build_message: Callable[[Any], dict]) -> dict[str, Any]:
        """Convert a protobuf field descriptor into JSON Schema.

        Args:
            field: The field descriptor to map.
            build_message: Callback that recursively builds (or references)
                a nested message schema.

        Returns:
            The JSON Schema fragment for the field.
        """
        if field.type == FieldDescriptor.TYPE_ENUM:
            schema: dict[str, Any] = {"type": "string", "enum": [value.name for value in field.enum_type.values]}
        elif field.type == FieldDescriptor.TYPE_MESSAGE:
            schema = self._WELL_KNOWN_TYPES.get(field.message_type.full_name, build_message(field.message_type))
        else:
            schema = dict(self._SCALAR_TYPES.get(field.type, {"type": "string"}))

        if field.message_type is not None and field.message_type.GetOptions().map_entry:
            value_field = field.message_type.fields_by_name["value"]
            return {"type": "object", "additionalProperties": self.field_schema(value_field, build_message)}
        if field.is_repeated:
            return {"type": "array", "items": schema}
        return schema

    def message_schema(self, root: FieldDescriptor) -> dict[str, Any]:
        """Build recursive JSON Schema with shared definitions and oneof hints.

        Args:
            root: The message descriptor to map.

        Returns:
            A JSON Schema document with ``$defs`` for shared definitions and
            ``x-protobuf-oneof`` hints (design §40 preserves the existing
            vendor extension; real-oneof vs proto3-optional differentiation
            is left to runtime consumers).
        """
        definitions: dict[str, Any] = {}
        building: set[str] = set()

        def build(message: FieldDescriptor) -> dict[str, Any]:
            reference = {"$ref": f"#/$defs/{message.full_name}"}
            if message.full_name in definitions or message.full_name in building:
                return reference
            building.add(message.full_name)
            schema: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
            required: list[str] = []
            oneofs: dict[str, list[str]] = defaultdict(list)
            definitions[message.full_name] = schema
            for field in message.fields:
                schema["properties"][field.name] = self.field_schema(field, build)
                if field.is_required:
                    required.append(field.name)
                if field.containing_oneof is not None:
                    oneofs[field.containing_oneof.name].append(field.name)
            if required:
                schema["required"] = required
            if oneofs:
                schema["x-protobuf-oneof"] = oneofs
            building.remove(message.full_name)
            return reference

        build(root)
        result = dict(definitions[root.full_name])
        result["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        result["$defs"] = definitions
        return result


# Module-level singleton shared by GrpcSchemaService and runtime consumers.
proto_json_schema_mapper = ProtoJsonSchemaMapper()

__all__ = ["ProtoJsonSchemaMapper", "proto_json_schema_mapper"]
