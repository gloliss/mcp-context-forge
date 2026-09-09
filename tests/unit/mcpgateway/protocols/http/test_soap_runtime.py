# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_soap_runtime.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the SOAP HTTP runtime glue (PR5, design §32/§34).
"""

# First-Party
from mcpgateway.protocols.codecs.soap import SoapFaultError
from mcpgateway.protocols.http.soap import map_soap_fault, soap_request_headers
from mcpgateway.protocols.models import ErrorCategory


class TestSoapRequestHeaders:
    """SOAP request headers (§33)."""

    def test_soap11_adds_soapaction_header(self):
        """SOAP 1.1 requests carry a SOAPAction header."""
        config = {"request": {"soap": {"version": "1.1", "soapAction": "urn:report#QueryReport"}}}

        assert soap_request_headers(config) == {"SOAPAction": "urn:report#QueryReport"}

    def test_soap12_emits_no_header(self):
        """SOAP 1.2 requests rely on the Content-Type action parameter."""
        config = {"request": {"soap": {"version": "1.2", "soapAction": "urn:report#QueryReport"}}}

        assert soap_request_headers(config) == {}

    def test_no_config_returns_empty(self):
        """Absent protocol_config yields no extra headers."""
        assert soap_request_headers(None) == {}
        assert soap_request_headers({}) == {}


class TestMapSoapFault:
    """SOAP Fault → canonical ProtocolError mapping (§34)."""

    def test_client_fault_maps_to_invalid_argument(self):
        """A Client fault maps to INVALID_ARGUMENT and keeps code/detail."""
        error = map_soap_fault(SoapFaultError("soap:Client", "Bad request", {"message": "invalid"}))

        assert error.category == ErrorCategory.INVALID_ARGUMENT
        assert error.code == "soap:Client"
        assert error.message == "Bad request"
        assert error.details == {"message": "invalid"}
        assert error.origin == "http:soap"

    def test_server_fault_maps_to_upstream_error(self):
        """A Server fault maps to UPSTREAM_ERROR."""
        error = map_soap_fault(SoapFaultError("soap:Server", "Upstream failed", None))

        assert error.category == ErrorCategory.UPSTREAM_ERROR
