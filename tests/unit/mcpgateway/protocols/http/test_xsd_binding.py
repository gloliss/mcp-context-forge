# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_xsd_binding.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the XSD binding bridge (PR4, design §27).

`XsdTypeSystem` and the XSD-aware `XmlCodec` existed before this bridge, but
nothing in the runtime ever populated `CodecContext.xsd_type_system`, so an
XML tool's schema was decorative. These tests pin the bridge itself.
"""

# Standard
import textwrap

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.contracts.xsd_types import XsdTypeSystem
from mcpgateway.protocols.http.xsd_binding import build_xsd_type_system, xsd_binding

_XSD = textwrap.dedent(
    """\
    <?xml version="1.0"?>
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="QueryRequest">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="factory" type="xs:string"/>
          </xs:sequence>
        </xs:complexType>
      </xs:element>
    </xs:schema>
    """
)


def _config(**xsd_overrides):
    """Build a protocol_config declaring an XML body with an XSD binding."""
    binding = {"schema": _XSD}
    binding.update(xsd_overrides)
    return {
        "request": {
            "method": "POST",
            "pathTemplate": "/query",
            "body": {"codec": "xml", "mediaType": "application/xml", "xsd": binding},
        },
        "response": {"codec": "xml"},
    }


class TestXsdBinding:
    """The binding is read from the request body config (§27)."""

    def test_binding_is_returned(self):
        """A declared schema is surfaced as a binding."""
        assert xsd_binding(_config())["schema"] == _XSD

    @pytest.mark.parametrize(
        "protocol_config",
        [
            None,
            {},
            {"request": {}},
            {"request": {"body": {}}},
            {"request": {"body": {"xsd": "not-a-mapping"}}},
            {"request": {"body": {"xsd": {}}}},
            {"request": {"body": {"xsd": {"schema": ""}}}},
            {"request": {"body": {"xsd": {"schema": "   "}}}},
            {"request": {"body": {"xsd": {"schema": 42}}}},
        ],
    )
    def test_absent_or_malformed_bindings_yield_none(self, protocol_config):
        """A missing or unusable binding is not an error, just no schema."""
        assert xsd_binding(protocol_config) is None


class TestBuildXsdTypeSystem:
    """The bridge produces a usable type system, or None when undeclared."""

    def test_builds_a_loaded_type_system(self):
        """A declared schema yields a loaded XsdTypeSystem."""
        system = build_xsd_type_system(_config())

        assert isinstance(system, XsdTypeSystem)
        assert system.find_element("QueryRequest") is not None

    def test_the_type_system_validates(self):
        """The produced type system actually validates against the schema."""
        system = build_xsd_type_system(_config())

        system.validate("<QueryRequest><factory>F1</factory></QueryRequest>")

    def test_the_type_system_rejects_non_conforming_xml(self):
        """A document missing a required element is rejected."""
        # Third-Party
        import xmlschema

        system = build_xsd_type_system(_config())

        with pytest.raises(xmlschema.XMLSchemaValidationError):
            system.validate("<QueryRequest/>")

    def test_no_binding_yields_none(self):
        """A tool without an XSD keeps the schema-less codec path."""
        assert build_xsd_type_system(None) is None
        assert build_xsd_type_system({"request": {"body": {"codec": "xml"}}}) is None

    def test_repeated_calls_reuse_the_loaded_schema(self):
        """The same schema document is loaded once (the bridge caches it)."""
        first = build_xsd_type_system(_config())
        second = build_xsd_type_system(_config())

        assert first is second

    def test_a_malformed_schema_raises_rather_than_degrading_silently(self):
        """A configured-but-broken schema is an error, not a silent fallback."""
        # A tool that declares a schema and silently loses validation is worse
        # than one that fails loudly at configuration time.
        with pytest.raises(Exception):
            build_xsd_type_system(_config(schema="<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'><xs:element name='X' type='xs:notAType'/></xs:schema>"))
