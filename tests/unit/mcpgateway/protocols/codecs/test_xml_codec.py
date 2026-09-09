# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/codecs/test_xml_codec.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the XmlCodec (PR4, design §23–§24).
"""

# Standard
import textwrap

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext, EncodedBody
from mcpgateway.protocols.codecs.xml import XmlCodec
from mcpgateway.protocols.contracts.xsd_types import XmlSecurityError, XmlSecurityLimits, XsdTypeSystem

_ITEM_XSD = textwrap.dedent(
    """\
    <?xml version="1.0"?>
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="Item">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="Name" type="xs:string"/>
            <xs:element name="Tag" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
          </xs:sequence>
          <xs:attribute name="id" type="xs:string"/>
        </xs:complexType>
      </xs:element>
    </xs:schema>
    """
)

_ITEM_XML = b'<Item id="100"><Name>A</Name><Tag>x</Tag><Tag>y</Tag></Item>'


@pytest.fixture
def item_xsd(tmp_path):
    """Compile an Item XSD and return an XsdTypeSystem."""
    path = tmp_path / "item.xsd"
    path.write_text(_ITEM_XSD, encoding="utf-8")
    return XsdTypeSystem(str(path))


class TestXmlCodec:
    """XmlCodec encode/decode and security posture."""

    def test_media_types(self):
        """The codec claims application/xml and text/xml."""
        codec = XmlCodec()

        assert codec.media_types == frozenset({"application/xml", "text/xml"})

    def test_decode_loose_canonical_mapping(self):
        """Without a schema, decode follows the @attribute / list mapping."""
        codec = XmlCodec()

        value = codec.decode(_ITEM_XML, CodecContext())

        assert value == {"Item": {"@id": "100", "Name": "A", "Tag": ["x", "y"]}}

    def test_encode_loose_roundtrip(self):
        """Without a schema, encode builds canonical XML from the dict."""
        codec = XmlCodec()
        data = {"Item": {"@id": "100", "Name": "A", "Tag": ["x", "y"]}}

        body = codec.encode(data, CodecContext())

        assert isinstance(body, EncodedBody)
        assert body.mode == "content"
        assert body.content_type == "application/xml"
        decoded = codec.decode(body.value, CodecContext())
        assert decoded == data

    def test_decode_with_xsd(self, item_xsd):
        """With a schema in the context, decode validates against the XSD."""
        codec = XmlCodec()
        context = CodecContext(xsd_type_system=item_xsd)

        value = codec.decode(_ITEM_XML, context)

        assert value == {"Item": {"@id": "100", "Name": "A", "Tag": ["x", "y"]}}

    def test_decode_with_xsd_rejects_invalid(self, item_xsd):
        """A non-conforming document raises a schema validation error."""
        codec = XmlCodec()
        context = CodecContext(xsd_type_system=item_xsd)

        with pytest.raises(Exception):
            codec.decode(b"<Item><Missing/></Item>", context)

    def test_encode_with_xsd_roundtrip(self, item_xsd):
        """Encoding through the schema serialises and round-trips."""
        codec = XmlCodec()
        context = CodecContext(xsd_type_system=item_xsd)
        data = {"Item": {"@id": "100", "Name": "A", "Tag": ["x", "y"]}}

        body = codec.encode(data, context)
        assert codec.decode(body.value, context) == data

    def test_xxe_declaration_rejected_loose(self):
        """A DTD/entity declaration is rejected even without a schema."""
        codec = XmlCodec()
        evil = b'<!DOCTYPE foo [<!ENTITY x "y">]><Item id="1"><Name>&x;</Name></Item>'

        with pytest.raises(XmlSecurityError, match="DTD/entity"):
            codec.decode(evil, CodecContext())

    def test_oversized_payload_rejected(self):
        """Payloads above the configured max_bytes are rejected."""
        codec = XmlCodec(security=XmlSecurityLimits(max_bytes=1024))
        big = b"<Item>" + b"<Name>" + b"x" * 4096 + b"</Name></Item>"

        with pytest.raises(XmlSecurityError, match="max_bytes"):
            codec.decode(big, CodecContext())

    def test_document_type_declaration_rejected(self):
        """A plain DOCTYPE (even without entities) is rejected."""
        codec = XmlCodec()
        doctype = b'<!DOCTYPE Item><Item id="1"><Name>A</Name></Item>'

        with pytest.raises(XmlSecurityError, match="DTD/entity"):
            codec.decode(doctype, CodecContext())
