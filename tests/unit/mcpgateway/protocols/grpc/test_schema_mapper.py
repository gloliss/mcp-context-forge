# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/grpc/test_schema_mapper.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the Proto → JSON Schema mapper (PR6, design §37–§39).
"""

# Third-Party
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorProto, FileDescriptorSet
from google.protobuf import any_pb2, duration_pb2, struct_pb2, timestamp_pb2, wrappers_pb2

# First-Party
from mcpgateway.services.grpc_schema_service import GrpcSchemaService

_SIGNED_64 = {"type": "string", "pattern": r"^-?[0-9]+$"}
_UNSIGNED_64 = {"type": "string", "pattern": r"^[0-9]+$"}


def _descriptor_set(*files: FileDescriptorProto) -> bytes:
    """Pack FileDescriptorProto objects into a serialized FileDescriptorSet."""
    descriptor_set = FileDescriptorSet()
    descriptor_set.file.extend(files)
    return descriptor_set.SerializeToString()


class TestInt64Mapping:
    """64-bit scalar types serialise as JSON strings (design §38)."""

    def test_all_64bit_types_are_string_patterns(self):
        """int64/uint64/fixed64/sfixed64/sint64 map to string patterns."""
        file_proto = FileDescriptorProto(name="scalars.proto", package="s", syntax="proto3")
        message = file_proto.message_type.add(name="Scalars")
        message.field.add(name="signed", number=1, type=FieldDescriptorProto.TYPE_INT64)
        message.field.add(name="unsigned", number=2, type=FieldDescriptorProto.TYPE_UINT64)
        message.field.add(name="fixed", number=3, type=FieldDescriptorProto.TYPE_FIXED64)
        message.field.add(name="sfixed", number=4, type=FieldDescriptorProto.TYPE_SFIXED64)
        message.field.add(name="sint", number=5, type=FieldDescriptorProto.TYPE_SINT64)
        message.field.add(name="small", number=6, type=FieldDescriptorProto.TYPE_INT32)
        service = file_proto.service.add(name="ScalarService")
        service.method.add(name="Get", input_type=".s.Scalars", output_type=".s.Scalars")

        _, catalog = GrpcSchemaService.normalize_descriptor_set(_descriptor_set(file_proto))
        schema = catalog["s.ScalarService"]["methods"][0]["input_schema"]["properties"]

        assert schema["signed"] == _SIGNED_64
        assert schema["unsigned"] == _UNSIGNED_64
        assert schema["fixed"] == _UNSIGNED_64
        assert schema["sfixed"] == _SIGNED_64
        assert schema["sint"] == _SIGNED_64
        # 32-bit stays a JSON integer.
        assert schema["small"] == {"type": "integer", "format": "int32"}


class TestWellKnownTypes:
    """WKT mapping follows ProtoJSON (design §39)."""

    def _wkt_catalog(self, type_name: str, package: str = "w", message_name: str = "Wrap") -> dict:
        """Build a catalog for a single message wrapping one WKT field."""
        file_proto = FileDescriptorProto(name="wkt.proto", package=package, syntax="proto3")
        message = file_proto.message_type.add(name=message_name)
        message.field.add(
            name="value",
            number=1,
            type=FieldDescriptorProto.TYPE_MESSAGE,
            type_name=f".{type_name}",
        )
        service = file_proto.service.add(name="WrapService")
        service.method.add(name="Get", input_type=f".{package}.{message_name}", output_type=f".{package}.{message_name}")
        # Add the WKT dependency files referenced by the field.
        dependency_files = [
            timestamp_pb2.DESCRIPTOR,
            duration_pb2.DESCRIPTOR,
            struct_pb2.DESCRIPTOR,
            wrappers_pb2.DESCRIPTOR,
            any_pb2.DESCRIPTOR,
        ]
        files = [file_proto]
        for descriptor in dependency_files:
            dep = FileDescriptorProto()
            dep.ParseFromString(descriptor.serialized_pb)
            files.append(dep)
        _, catalog = GrpcSchemaService.normalize_descriptor_set(_descriptor_set(*files))
        return catalog[f"{package}.WrapService"]["methods"][0]["input_schema"]["properties"]["value"]

    def test_timestamp_maps_to_date_time_string(self):
        """google.protobuf.Timestamp → date-time string."""
        assert self._wkt_catalog("google.protobuf.Timestamp") == {"type": "string", "format": "date-time"}

    def test_duration_maps_to_seconds_string(self):
        """google.protobuf.Duration → seconds string pattern."""
        assert self._wkt_catalog("google.protobuf.Duration") == {"type": "string", "pattern": r"^-?[0-9]+(?:\.[0-9]+)?s$"}

    def test_struct_maps_to_free_object(self):
        """google.protobuf.Struct → free-form object."""
        assert self._wkt_catalog("google.protobuf.Struct") == {"type": "object", "additionalProperties": True}

    def test_list_value_maps_to_array(self):
        """google.protobuf.ListValue → array."""
        assert self._wkt_catalog("google.protobuf.ListValue") == {"type": "array"}

    def test_int64_wrapper_maps_to_string(self):
        """google.protobuf.Int64Value → string (wrapped scalar, ProtoJSON)."""
        assert self._wkt_catalog("google.protobuf.Int64Value") == _SIGNED_64

    def test_string_wrapper_maps_to_string(self):
        """google.protobuf.StringValue → string."""
        assert self._wkt_catalog("google.protobuf.StringValue") == {"type": "string"}


class TestMessageRecursionAndOneof:
    """Recursive $defs and x-protobuf-oneof hints (design §40)."""

    def test_recursive_message_uses_ref_and_oneof_preserved(self):
        """Recursive schemas reference $defs and oneof hints are preserved."""
        file_proto = FileDescriptorProto(name="node.proto", package="n", syntax="proto3")
        node = file_proto.message_type.add(name="Node")
        node.field.add(name="name", number=1, type=FieldDescriptorProto.TYPE_STRING)
        node.field.add(name="child", number=2, type=FieldDescriptorProto.TYPE_MESSAGE, type_name=".n.Node")
        node.oneof_decl.add(name="selector")
        node.field.add(name="slug", number=3, type=FieldDescriptorProto.TYPE_STRING, oneof_index=0)
        node.field.add(name="legacy", number=4, type=FieldDescriptorProto.TYPE_INT64, oneof_index=0)
        service = file_proto.service.add(name="NodeService")
        service.method.add(name="Get", input_type=".n.Node", output_type=".n.Node")

        _, catalog = GrpcSchemaService.normalize_descriptor_set(_descriptor_set(file_proto))
        schema = catalog["n.NodeService"]["methods"][0]["output_schema"]

        assert schema["properties"]["child"] == {"$ref": "#/$defs/n.Node"}
        assert "$defs" in schema and "n.Node" in schema["$defs"]
        assert schema["x-protobuf-oneof"] == {"selector": ["slug", "legacy"]}
