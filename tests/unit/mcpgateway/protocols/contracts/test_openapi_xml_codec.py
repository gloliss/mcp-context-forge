# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/contracts/test_openapi_xml_codec.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

XML/soap media types must reach the XML codecs (PR4, design §24/§27).

Before this mapping was fixed, ``application/xml`` compiled to ``binary`` and
``text/xml`` to ``text``: an OpenAPI-declared XML operation therefore encoded
and decoded as an opaque blob and never reached ``XmlCodec`` at all, which
also made an XSD binding unreachable.
"""

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.codecs import codec_registry
from mcpgateway.protocols.contracts.openapi import _media_type_to_codec, _media_type_xsd

_XSD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'/>"


class TestMediaTypeToCodec:
    """Media types map to the codec that can actually handle them (§24)."""

    @pytest.mark.parametrize(
        "media_type,expected",
        [
            ("application/xml", "xml"),
            ("text/xml", "xml"),
            ("application/xml; charset=utf-8", "xml"),
            ("application/atom+xml", "xml"),
            ("application/soap+xml", "soap"),
            ("application/json", "json"),
            ("text/plain", "text"),
            ("application/octet-stream", "binary"),
        ],
    )
    def test_expected_codec_name(self, media_type, expected):
        """Each media type maps to its intended codec name."""
        assert _media_type_to_codec(media_type) == expected

    @pytest.mark.parametrize("media_type", ["application/xml", "text/xml", "application/soap+xml"])
    def test_the_name_resolves_to_an_xml_codec(self, media_type):
        """The produced name resolves to an XML-aware codec, not a blob codec."""
        codec = codec_registry.resolve_name(_media_type_to_codec(media_type))

        assert type(codec).__name__ in {"XmlCodec", "SoapCodec"}


class TestMediaTypeXsd:
    """The XSD vendor extension is read off the media type object (§27)."""

    def test_binding_is_returned(self):
        """A well-formed extension is surfaced as a mapping."""
        assert _media_type_xsd({"x-contextforge-xsd": {"schema": _XSD}}) == {"schema": _XSD}

    @pytest.mark.parametrize(
        "media_obj",
        [
            {},
            {"x-contextforge-xsd": "nope"},
            {"x-contextforge-xsd": {}},
            {"x-contextforge-xsd": {"schema": ""}},
            {"x-contextforge-xsd": {"schema": 42}},
        ],
    )
    def test_absent_or_malformed_extensions_yield_none(self, media_obj):
        """A missing or unusable extension is not an error, just no schema."""
        assert _media_type_xsd(media_obj) is None
