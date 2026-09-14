# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/codecs/test_soap_codec.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the SoapCodec (PR5, design §33).
"""

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext
from mcpgateway.protocols.codecs.soap import SOAP11_ENVELOPE_NS, SOAP12_ENVELOPE_NS, SoapCodec, SoapFaultError


def _soap_context(version="1.1", operation="{urn:report}QueryRequest", action="urn:report#QueryReport"):
    """Build a CodecContext carrying a SOAP binding configuration."""
    return CodecContext(
        protocol_config={
            "version": 1,
            "request": {
                "method": "POST",
                "pathTemplate": "/soap",
                "soap": {"version": version, "operation": operation, "soapAction": action},
            },
            "response": {"codec": "soap"},
        }
    )


class TestSoapCodec:
    """SoapCodec envelope construction and parsing."""

    def test_media_types(self):
        """The codec claims application/soap+xml only."""
        codec = SoapCodec()

        assert codec.media_types == frozenset({"application/soap+xml"})

    def test_encode_soap11(self):
        """SOAP 1.1 envelopes carry text/xml and a soap:Envelope root."""
        codec = SoapCodec()
        body = codec.encode({"factory": "F1"}, _soap_context(version="1.1"))

        assert body.mode == "content"
        assert body.content_type == "text/xml"
        payload = body.value.decode("utf-8")
        assert f'xmlns:soap="{SOAP11_ENVELOPE_NS}"' in payload
        assert "<soap:Body>" in payload
        # The operation element may be namespaced (nsN prefix); the payload follows.
        assert "QueryRequest" in payload
        assert "<factory>F1</factory>" in payload

    def test_encode_soap12(self):
        """SOAP 1.2 envelopes carry application/soap+xml."""
        codec = SoapCodec()
        body = codec.encode({"factory": "F1"}, _soap_context(version="1.2"))

        assert body.content_type == "application/soap+xml"
        assert f'xmlns:soap="{SOAP12_ENVELOPE_NS}"' in body.value.decode("utf-8")

    def test_decode_soap_response(self):
        """A SOAP response Body decodes to the canonical dict."""
        codec = SoapCodec()
        response = (f'<?xml version="1.0"?><soap:Envelope xmlns:soap="{SOAP11_ENVELOPE_NS}"><soap:Body><QueryResponse><status>OK</status></QueryResponse></soap:Body></soap:Envelope>').encode("utf-8")

        value = codec.decode(response, CodecContext())

        assert value == {"QueryResponse": {"status": "OK"}}

    def test_decode_soap_fault_raises(self):
        """A SOAP Fault surfaces as SoapFaultError with code/string/detail."""
        codec = SoapCodec()
        fault = (
            '<?xml version="1.0"?>'
            f'<soap:Envelope xmlns:soap="{SOAP11_ENVELOPE_NS}">'
            "<soap:Body><soap:Fault>"
            "<faultcode>soap:Client</faultcode>"
            "<faultstring>Bad request</faultstring>"
            "<detail><message>invalid factory</message></detail>"
            "</soap:Fault></soap:Body>"
            "</soap:Envelope>"
        ).encode("utf-8")

        with pytest.raises(SoapFaultError) as exc_info:
            codec.decode(fault, CodecContext())

        assert exc_info.value.code == "soap:Client"
        assert exc_info.value.fault_string == "Bad request"
        assert exc_info.value.detail == {"message": "invalid factory"}

    def test_decode_rejects_entity_declaration(self):
        """XML security (§28) applies to SOAP payloads too."""
        codec = SoapCodec()
        evil = (
            f'<!DOCTYPE soap:Envelope [<!ENTITY x "y">]><soap:Envelope xmlns:soap="{SOAP11_ENVELOPE_NS}"><soap:Body><QueryResponse><status>&x;</status></QueryResponse></soap:Body></soap:Envelope>'
        ).encode("utf-8")

        with pytest.raises(Exception):
            codec.decode(evil, CodecContext())
