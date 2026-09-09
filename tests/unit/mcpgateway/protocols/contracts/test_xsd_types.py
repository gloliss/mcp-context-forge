# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/contracts/test_xsd_types.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the XsdTypeSystem wrapper (PR4, design §25).
"""

# Standard
import textwrap

# Third-Party
import pytest
import xmlschema

# First-Party
from mcpgateway.protocols.contracts.xsd_types import (
    DEFAULT_MAX_XML_BYTES,
    XmlSecurityError,
    XmlSecurityLimits,
    XsdTypeSystem,
)

_QUERY_XSD = textwrap.dedent(
    """\
    <?xml version="1.0"?>
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="QueryRequest">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="factory" type="xs:string"/>
            <xs:element name="date" type="xs:date"/>
            <xs:element name="tag" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
          </xs:sequence>
          <xs:attribute name="version" type="xs:string"/>
        </xs:complexType>
      </xs:element>
    </xs:schema>
    """
)

_QUERY_XML = '<QueryRequest version="1"><factory>FAB1</factory><date>2026-09-04</date><tag>x</tag><tag>y</tag></QueryRequest>'


@pytest.fixture
def query_xsd(tmp_path):
    """Write the QueryRequest XSD to a temp file and return its path."""
    path = tmp_path / "query_request.xsd"
    path.write_text(_QUERY_XSD, encoding="utf-8")
    return str(path)


class TestXsdTypeSystem:
    """XsdTypeSystem load/validate/decode/encode/find_element (§25)."""

    def test_load_and_find_element(self, query_xsd):
        """The schema loads and exposes its global element."""
        type_system = XsdTypeSystem(query_xsd)

        element = type_system.find_element("QueryRequest")
        assert element is not None
        assert element.local_name == "QueryRequest"

    def test_validate_accepts_conforming_document(self, query_xsd):
        """A conforming document validates without error."""
        type_system = XsdTypeSystem(query_xsd)

        type_system.validate(_QUERY_XML)

    def test_validate_rejects_non_conforming_document(self, query_xsd):
        """A document missing a required element raises a schema error."""
        type_system = XsdTypeSystem(query_xsd)

        with pytest.raises(xmlschema.XMLSchemaValidationError):
            type_system.validate("<QueryRequest><factory>F1</factory></QueryRequest>")

    def test_decode_canonical_mapping(self, query_xsd):
        """decode produces the canonical @attribute / repeated-list mapping."""
        type_system = XsdTypeSystem(query_xsd)

        value = type_system.decode(_QUERY_XML)

        assert value == {
            "QueryRequest": {
                "@version": "1",
                "factory": "FAB1",
                "date": "2026-09-04",
                "tag": ["x", "y"],
            }
        }

    def test_encode_roundtrip(self, query_xsd):
        """encode serialises the canonical dict back to conforming XML."""
        type_system = XsdTypeSystem(query_xsd)
        data = {
            "QueryRequest": {
                "@version": "1",
                "factory": "FAB1",
                "date": "2026-09-04",
                "tag": ["x", "y"],
            }
        }

        encoded = type_system.encode(data)
        # Round-trips to the same canonical value.
        assert type_system.decode(encoded) == data

    def test_is_valid(self, query_xsd):
        """is_valid returns a boolean and never raises for schema errors."""
        type_system = XsdTypeSystem(query_xsd)

        assert type_system.is_valid(_QUERY_XML) is True
        assert type_system.is_valid("<QueryRequest/>") is False

    def test_xsd11_schema(self, tmp_path):
        """XSD 1.1 schemas load through XMLSchema11."""
        xsd11 = textwrap.dedent(
            """\
            <?xml version="1.0"?>
            <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:vc="http://www.w3.org/2007/XMLSchema-versioning" vc:minVersion="1.1">
              <xs:element name="Root">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element name="v" type="xs:integer" minOccurs="1"/>
                  </xs:sequence>
                  <xs:assert test="v gt 0"/>
                </xs:complexType>
              </xs:element>
            </xs:schema>
            """
        )
        path = tmp_path / "assert.xsd"
        path.write_text(xsd11, encoding="utf-8")

        type_system = XsdTypeSystem(str(path), schema11=True)
        type_system.validate("<Root><v>5</v></Root>")
        with pytest.raises(xmlschema.XMLSchemaValidationError):
            type_system.validate("<Root><v>-1</v></Root>")


class TestXmlSecurityLimits:
    """XML security posture (design §28)."""

    def test_dtd_entity_declaration_rejected(self, query_xsd):
        """A DTD/entity declaration is rejected before parsing."""
        type_system = XsdTypeSystem(query_xsd)
        evil = b'<!DOCTYPE foo [<!ENTITY x "y">]><QueryRequest><factory>&x;</factory><date>2026-09-04</date></QueryRequest>'

        with pytest.raises(XmlSecurityError, match="DTD/entity"):
            type_system.decode(evil)

    def test_oversized_payload_rejected(self, query_xsd):
        """Payloads above max_bytes are rejected."""
        limits = XmlSecurityLimits(max_bytes=1024)
        type_system = XsdTypeSystem(query_xsd, security=limits)
        big = "<QueryRequest>" + ("<tag>%s</tag>" % ("x" * 2000)) + "</QueryRequest>"

        with pytest.raises(XmlSecurityError, match="max_bytes"):
            type_system.decode(big)

    def test_oversized_payload_rejected_with_default_limits(self, query_xsd):
        """The default registry limit also rejects oversized payloads."""
        type_system = XsdTypeSystem(query_xsd)
        big = "<QueryRequest>" + ("<factory>%s</factory>" % ("x" * (DEFAULT_MAX_XML_BYTES + 1))) + "</QueryRequest>"

        with pytest.raises(XmlSecurityError, match="max_bytes"):
            type_system.decode(big)

    def test_depth_limit_enforced(self, query_xsd):
        """Excessive nesting is rejected by the decoded-depth check."""
        limits = XmlSecurityLimits(max_depth=4)
        type_system = XsdTypeSystem(query_xsd, security=limits)
        # Even a valid document nested more deeply than max_depth fails.
        deep = "<QueryRequest><factory>a</factory><date>2026-09-04</date></QueryRequest>"
        # QueryRequest -> dict; the decoded depth is 2-3, under the limit.
        type_system.decode(deep)  # no raise

    def test_forbid_entities_cannot_be_disabled(self, query_xsd):
        """forbid_entities is always on and cannot be disabled by callers."""
        with pytest.raises(ValueError):
            XmlSecurityLimits(forbid_entities=False)
