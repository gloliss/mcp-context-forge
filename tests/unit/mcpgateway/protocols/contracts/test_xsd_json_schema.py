# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/contracts/test_xsd_json_schema.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the XSD → JSON Schema mapper (PR4, design §26).
"""

# Standard
import textwrap

# Third-Party
import pytest
import xmlschema

# First-Party
from mcpgateway.protocols.contracts.xsd_json_schema import XsdJsonSchemaMapper

_REPORT_XSD = textwrap.dedent(
    """\
    <?xml version="1.0"?>
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
               targetNamespace="urn:report" xmlns:r="urn:report"
               elementFormDefault="qualified">
      <xs:simpleType name="CodeType">
        <xs:restriction base="xs:string">
          <xs:enumeration value="A"/>
          <xs:enumeration value="B"/>
        </xs:restriction>
      </xs:simpleType>
      <xs:complexType name="QueryRequestType">
        <xs:sequence>
          <xs:element name="factory" type="xs:string"/>
          <xs:element name="date" type="xs:date"/>
          <xs:element name="code" type="r:CodeType" minOccurs="0" maxOccurs="unbounded"/>
        </xs:sequence>
        <xs:attribute name="version" type="xs:string" use="required"/>
      </xs:complexType>
      <xs:element name="QueryRequest" type="r:QueryRequestType"/>
    </xs:schema>
    """
)


@pytest.fixture
def report_schema(tmp_path):
    """Compile the report XSD and return the xmlschema object."""
    path = tmp_path / "report.xsd"
    path.write_text(_REPORT_XSD, encoding="utf-8")
    return xmlschema.XMLSchema(str(path))


class TestXsdJsonSchemaMapper:
    """XsdJsonSchemaMapper maps XSD elements/types to JSON Schema (§26)."""

    def test_map_element_primitive_string(self, tmp_path):
        """xs:string maps to a JSON string."""
        path = tmp_path / "simple.xsd"
        path.write_text(
            textwrap.dedent(
                """\
                <?xml version="1.0"?>
                <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
                  <xs:element name="Name" type="xs:string"/>
                </xs:schema>
                """
            ),
            encoding="utf-8",
        )
        schema = xmlschema.XMLSchema(str(path))
        mapper = XsdJsonSchemaMapper(schema)

        assert mapper.map_element("Name") == {"type": "string"}

    def test_map_element_integer_and_date_types(self, tmp_path):
        """xs:integer and xs:date map to their JSON Schema forms."""
        path = tmp_path / "types.xsd"
        path.write_text(
            textwrap.dedent(
                """\
                <?xml version="1.0"?>
                <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
                  <xs:element name="Count" type="xs:integer"/>
                  <xs:element name="Day" type="xs:date"/>
                  <xs:element name="Stamp" type="xs:dateTime"/>
                  <xs:element name="Ratio" type="xs:decimal"/>
                </xs:schema>
                """
            ),
            encoding="utf-8",
        )
        schema = xmlschema.XMLSchema(str(path))
        mapper = XsdJsonSchemaMapper(schema)

        assert mapper.map_element("Count") == {"type": "integer"}
        assert mapper.map_element("Day") == {"type": "string", "format": "date"}
        assert mapper.map_element("Stamp") == {"type": "string", "format": "date-time"}
        assert mapper.map_element("Ratio") == {"type": "number"}

    def test_map_complex_type_object(self, report_schema):
        """A complexType maps to an object with @attributes and properties."""
        mapper = XsdJsonSchemaMapper(report_schema)

        schema = mapper.map_element("QueryRequest")

        assert schema["type"] == "object"
        properties = schema["properties"]
        assert properties["@version"] == {"type": "string"}
        assert properties["factory"] == {"type": "string"}
        assert properties["date"] == {"type": "string", "format": "date"}
        # maxOccurs=unbounded wraps in an array; required attributes listed.
        assert properties["code"] == {"type": "array", "items": {"type": "string", "enum": ["A", "B"]}}
        assert "@version" in schema["required"]
        assert "factory" in schema["required"]
        assert "code" not in schema["required"]

    def test_enumeration_and_pattern_facets(self, tmp_path):
        """Enumeration and pattern facets are carried into JSON Schema."""
        path = tmp_path / "facets.xsd"
        path.write_text(
            textwrap.dedent(
                """\
                <?xml version="1.0"?>
                <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
                  <xs:simpleType name="CodeType">
                    <xs:restriction base="xs:string">
                      <xs:enumeration value="A"/>
                      <xs:enumeration value="B"/>
                    </xs:restriction>
                  </xs:simpleType>
                  <xs:simpleType name="RefType">
                    <xs:restriction base="xs:string">
                      <xs:pattern value="[A-Z]{3}"/>
                    </xs:restriction>
                  </xs:simpleType>
                  <xs:element name="Code" type="CodeType"/>
                  <xs:element name="Ref" type="RefType"/>
                </xs:schema>
                """
            ),
            encoding="utf-8",
        )
        schema = xmlschema.XMLSchema(str(path))
        mapper = XsdJsonSchemaMapper(schema)

        assert mapper.map_element("Code") == {"type": "string", "enum": ["A", "B"]}
        assert mapper.map_element("Ref") == {"type": "string", "pattern": "[A-Z]{3}"}

    def test_numeric_facets(self, tmp_path):
        """minInclusive/maxInclusive map to minimum/maximum."""
        path = tmp_path / "nums.xsd"
        path.write_text(
            textwrap.dedent(
                """\
                <?xml version="1.0"?>
                <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
                  <xs:simpleType name="Bounded">
                    <xs:restriction base="xs:integer">
                      <xs:minInclusive value="1"/>
                      <xs:maxInclusive value="10"/>
                    </xs:restriction>
                  </xs:simpleType>
                  <xs:element name="N" type="Bounded"/>
                </xs:schema>
                """
            ),
            encoding="utf-8",
        )
        schema = xmlschema.XMLSchema(str(path))
        mapper = XsdJsonSchemaMapper(schema)

        mapped = mapper.map_element("N")
        assert mapped["type"] == "integer"
        assert mapped["minimum"] == 1
        assert mapped["maximum"] == 10

    def test_choice_maps_to_oneof(self, tmp_path):
        """A choice content model maps to a oneOf fragment."""
        path = tmp_path / "choice.xsd"
        path.write_text(
            textwrap.dedent(
                """\
                <?xml version="1.0"?>
                <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
                  <xs:complexType name="T">
                    <xs:choice>
                      <xs:element name="a" type="xs:string"/>
                      <xs:element name="b" type="xs:integer"/>
                    </xs:choice>
                  </xs:complexType>
                  <xs:element name="Root" type="T"/>
                </xs:schema>
                """
            ),
            encoding="utf-8",
        )
        schema = xmlschema.XMLSchema(str(path))
        mapper = XsdJsonSchemaMapper(schema)

        mapped = mapper.map_element("Root")
        one_of = mapped["properties"]["#oneOf"]
        assert {"type": "string"} in one_of["oneOf"]
        assert {"type": "integer"} in one_of["oneOf"]

    def test_missing_element_raises(self, report_schema):
        """An unknown element name raises KeyError."""
        mapper = XsdJsonSchemaMapper(report_schema)

        with pytest.raises(KeyError):
            mapper.map_element("DoesNotExist")
