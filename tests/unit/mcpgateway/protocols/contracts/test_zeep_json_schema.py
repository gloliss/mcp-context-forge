# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/contracts/test_zeep_json_schema.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the zeep type-tree → JSON Schema mapper (PR5, design §31).

The mapper's input is a resolved zeep type, so every fixture here parses a
real WSDL and maps the type zeep produced for it — no hand-built stand-ins.
"""

# Standard
import textwrap

# Third-Party
import pytest
from zeep import Client, Transport

# First-Party
from mcpgateway.protocols.contracts.xsd_json_schema import XSD_BUILTIN_JSON_SCHEMA
from mcpgateway.protocols.contracts.zeep_json_schema import ZeepJsonSchemaMapper

_ANY_TYPE = "{http://www.w3.org/2001/XMLSchema}anyType"

_WSDL = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
      xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
      xmlns:tns="urn:probe" xmlns:xsd="http://www.w3.org/2001/XMLSchema"
      targetNamespace="urn:probe">
      <types>
        <xsd:schema targetNamespace="urn:probe" elementFormDefault="qualified">
          <xsd:simpleType name="Severity">
            <xsd:restriction base="xsd:string">
              <xsd:enumeration value="low"/>
              <xsd:enumeration value="high"/>
            </xsd:restriction>
          </xsd:simpleType>
          <xsd:simpleType name="Codes">
            <xsd:list itemType="xsd:string"/>
          </xsd:simpleType>
          <xsd:simpleType name="Either">
            <xsd:union memberTypes="xsd:int xsd:string"/>
          </xsd:simpleType>
          <xsd:complexType name="Filter">
            <xsd:sequence>
              <xsd:element name="field" type="xsd:string"/>
              <xsd:element name="op" type="tns:Severity" minOccurs="0"/>
            </xsd:sequence>
          </xsd:complexType>
          <xsd:complexType name="Node">
            <xsd:sequence>
              <xsd:element name="label" type="xsd:string"/>
              <xsd:element name="child" type="tns:Node" minOccurs="0"/>
            </xsd:sequence>
          </xsd:complexType>
          <xsd:complexType name="Bag">
            <xsd:all>
              <xsd:element name="a" type="xsd:string"/>
              <xsd:element name="b" type="xsd:int" minOccurs="0"/>
            </xsd:all>
          </xsd:complexType>
          <xsd:element name="Shared" type="xsd:string"/>
          <xsd:element name="Probe">
            <xsd:complexType>
              <xsd:sequence>
                <xsd:element name="factory" type="xsd:string"/>
                <xsd:element name="note" type="xsd:string" minOccurs="0"/>
                <xsd:element name="count" type="xsd:int" maxOccurs="unbounded"/>
                <xsd:element name="pairs" type="xsd:string" maxOccurs="5"/>
                <xsd:element name="severity" type="tns:Severity" minOccurs="0"/>
                <xsd:element name="codes" type="tns:Codes" minOccurs="0"/>
                <xsd:element name="either" type="tns:Either" minOccurs="0"/>
                <xsd:element name="filter" type="tns:Filter" minOccurs="0"/>
                <xsd:element name="filterAgain" type="tns:Filter" minOccurs="0"/>
                <xsd:element ref="tns:Shared" minOccurs="0"/>
                <xsd:element name="payload" type="xsd:anyType" minOccurs="0"/>
                <xsd:element name="node" type="tns:Node" minOccurs="0"/>
                <xsd:element name="bag" type="tns:Bag" minOccurs="0"/>
                <xsd:element name="inner" minOccurs="0">
                  <xsd:complexType><xsd:sequence>
                    <xsd:element name="leaf" type="xsd:string"/>
                  </xsd:sequence></xsd:complexType>
                </xsd:element>
                <xsd:choice minOccurs="0">
                  <xsd:element name="byId" type="xsd:int"/>
                  <xsd:element name="byName" type="xsd:string"/>
                </xsd:choice>
              </xsd:sequence>
              <xsd:attribute name="trace" type="xsd:string" use="required"/>
              <xsd:attribute name="lang" type="xsd:string"/>
            </xsd:complexType>
          </xsd:element>
          <xsd:element name="ProbeResponse">
            <xsd:complexType><xsd:sequence>
              <xsd:element name="status" type="xsd:string"/>
            </xsd:sequence></xsd:complexType>
          </xsd:element>
        </xsd:schema>
      </types>
      <message name="ProbeIn"><part name="parameters" element="tns:Probe"/></message>
      <message name="ProbeOut"><part name="parameters" element="tns:ProbeResponse"/></message>
      <portType name="ProbePort">
        <operation name="Probe">
          <input message="tns:ProbeIn"/>
          <output message="tns:ProbeOut"/>
        </operation>
      </portType>
      <binding name="ProbeBinding" type="tns:ProbePort">
        <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
        <operation name="Probe">
          <soap:operation soapAction="urn:probe#Probe"/>
          <input><soap:body use="literal"/></input>
          <output><soap:body use="literal"/></output>
        </operation>
      </binding>
      <service name="ProbeService">
        <port name="ProbePort" binding="tns:ProbeBinding">
          <soap:address location="http://probe.internal/soap"/>
        </port>
      </service>
    </definitions>
    """
)


@pytest.fixture(scope="module")
def wsdl_client(tmp_path_factory):
    """A zeep client over the probe WSDL (parsed once for the module)."""
    path = tmp_path_factory.mktemp("wsdl") / "probe.wsdl"
    path.write_text(_WSDL, encoding="utf-8")
    return Client(str(path), transport=Transport())


@pytest.fixture(scope="module")
def schema_types(wsdl_client):
    """The parsed schema's global type registry."""
    return wsdl_client.wsdl.types


@pytest.fixture(scope="module")
def probe_schema(wsdl_client):
    """The JSON Schema the mapper produces for the request element."""
    operation = wsdl_client.wsdl.services["ProbeService"].ports["ProbePort"].binding._operations["Probe"]
    return ZeepJsonSchemaMapper().map_type(operation.input.body.type)


@pytest.fixture(scope="module")
def probe_properties(probe_schema):
    """The mapped request body's properties."""
    return probe_schema["properties"]


class TestZeepJsonSchemaMapper:
    """ZeepJsonSchemaMapper maps resolved zeep types to JSON Schema (§31)."""

    def test_lists_every_parameter(self, probe_properties):
        """Every attribute and element reaches the schema, under its own name."""
        assert set(probe_properties) == {
            "@trace",
            "@lang",
            "factory",
            "note",
            "count",
            "pairs",
            "severity",
            "codes",
            "either",
            "filter",
            "filterAgain",
            "Shared",
            "payload",
            "node",
            "bag",
            "inner",
            "byId",
            "byName",
        }

    def test_required_covers_attributes_and_repeating_elements(self, probe_schema):
        """Attributes are listed first, then elements in document order."""
        assert probe_schema["required"] == ["@trace", "factory", "count", "pairs"]

    def test_optional_members_are_not_required(self, probe_schema, probe_properties):
        """An element with minOccurs=0 is present but absent from required."""
        assert probe_properties["note"] == {"type": "string"}
        assert "note" not in probe_schema["required"]

    def test_attributes_use_the_at_prefix(self, probe_properties):
        """Attributes become "@name" properties (canonical mapping, §24)."""
        assert probe_properties["@trace"] == {"type": "string"}
        assert probe_properties["@lang"] == {"type": "string"}

    def test_unbounded_element_becomes_an_array(self, probe_properties):
        """maxOccurs="unbounded" wraps the item schema in an array."""
        assert probe_properties["count"] == {"type": "array", "items": {"type": "integer"}}

    def test_numeric_max_occurs_becomes_an_array(self, probe_properties):
        """A finite maxOccurs above 1 also wraps in an array."""
        assert probe_properties["pairs"] == {"type": "array", "items": {"type": "string"}}

    def test_named_simple_type_resolves_to_its_builtin_base(self, probe_properties):
        """A named restriction maps to the JSON primitive of its XSD base."""
        assert probe_properties["severity"] == {"type": "string"}

    def test_list_type_becomes_an_array_of_its_item_type(self, probe_properties):
        """xsd:list is a whitespace-separated list of the item type."""
        assert probe_properties["codes"] == {"type": "array", "items": {"type": "string"}}

    def test_union_type_accepts_any_member(self, probe_properties):
        """xsd:union maps to anyOf over its member types."""
        assert probe_properties["either"] == {"anyOf": [{"type": "integer"}, {"type": "string"}]}

    def test_nested_complex_type_is_mapped_recursively(self, probe_properties):
        """A named complexType expands to a nested object schema."""
        assert probe_properties["filter"] == {
            "type": "object",
            "properties": {"field": {"type": "string"}, "op": {"type": "string"}},
            "required": ["field"],
        }

    def test_a_reused_type_expands_at_every_occurrence(self, probe_properties):
        """The cycle guard is path-scoped: a sibling reuse still expands fully."""
        assert probe_properties["filterAgain"] == probe_properties["filter"]

    def test_referenced_global_element_is_inlined(self, probe_properties):
        """``ref`` to a global element uses the referenced element's own name."""
        assert probe_properties["Shared"] == {"type": "string"}

    def test_inline_anonymous_complex_type_is_mapped(self, probe_properties):
        """An anonymous inline complexType becomes its own object schema."""
        assert probe_properties["inner"] == {
            "type": "object",
            "properties": {"leaf": {"type": "string"}},
            "required": ["leaf"],
        }

    def test_any_type_accepts_anything(self, probe_properties):
        """xsd:anyType maps to the empty schema, which permits any value."""
        assert probe_properties["payload"] == {}

    def test_choice_members_become_optional_properties(self, probe_properties, probe_schema):
        """zeep flattens a choice; both alternatives read as optional params."""
        assert probe_properties["byId"] == {"type": "integer"}
        assert probe_properties["byName"] == {"type": "string"}
        assert "byId" not in probe_schema["required"]
        assert "byName" not in probe_schema["required"]

    def test_self_referential_type_stops_at_an_object(self, probe_properties):
        """A recursive type stops rather than recursing without bound."""
        assert probe_properties["node"] == {
            "type": "object",
            "properties": {"label": {"type": "string"}, "child": {"type": "object"}},
            "required": ["label"],
        }

    def test_xsd_all_marks_only_declared_required(self, probe_properties):
        """A group from ``xsd:all`` is mapped member by member."""
        assert probe_properties["bag"] == {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a"],
        }

    def test_response_type_maps_like_a_request_type(self, wsdl_client):
        """The output element is mapped by the same rules as the input one."""
        operation = wsdl_client.wsdl.services["ProbeService"].ports["ProbePort"].binding._operations["Probe"]

        assert ZeepJsonSchemaMapper().map_type(operation.output.body.type) == {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        }

    def test_any_type_object_maps_to_anything(self, schema_types):
        """The anyType type object itself maps to the empty schema."""
        assert ZeepJsonSchemaMapper().map_type(schema_types.get_type(_ANY_TYPE)) == {}

    def test_missing_type_maps_to_anything(self):
        """A missing type degrades rather than raising."""
        assert ZeepJsonSchemaMapper().map_type(None) == {}

    def test_returned_schemas_are_copies(self, schema_types):
        """Callers cannot mutate the shared builtin table through a result."""
        mapper = ZeepJsonSchemaMapper()
        builtin = schema_types.get_type("{http://www.w3.org/2001/XMLSchema}string")

        first = mapper.map_type(builtin)
        first["type"] = "mutated"

        assert mapper.map_type(builtin) == {"type": "string"}
        assert XSD_BUILTIN_JSON_SCHEMA["string"] == {"type": "string"}

    def test_every_shared_builtin_maps_to_its_table_entry(self, schema_types):
        """The shared table covers every XSD builtin and loses no entry to folding.

        The mapper folds builtin names to lower case to serve both the qname
        and the zeep class-name lookup; a pair of entries colliding under that
        fold would be silently dropped, so this walks the whole table.
        """
        mapper = ZeepJsonSchemaMapper()

        for name, expected in XSD_BUILTIN_JSON_SCHEMA.items():
            xsd_type = schema_types.get_type(f"{{http://www.w3.org/2001/XMLSchema}}{name}")
            assert mapper.map_type(xsd_type) == expected, name
