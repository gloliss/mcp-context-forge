# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_soap_runtime.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the SOAP HTTP runtime glue (PR5, design §32/§34).
"""

# Third-Party
import httpx
import pytest

# First-Party
from mcpgateway.protocols.codecs import build_default_codec_registry
from mcpgateway.protocols.codecs.soap import SoapFaultError
from mcpgateway.protocols.contracts.models import OperationDefinition
from mcpgateway.protocols.http import adapter as adapter_module
from mcpgateway.protocols.http.adapter import REST_HTTP_STATUS_ERROR, REST_UNEXPECTED_STATUS, HttpProtocolAdapter
from mcpgateway.protocols.http.request_builder import RequestBuilder
from mcpgateway.protocols.http.soap import is_soap_config, map_soap_fault, soap_content_type, soap_request_headers
from mcpgateway.protocols.models import ErrorCategory, ProtocolError
from tests.unit.mcpgateway.protocols.http.test_http_adapter_legacy import _make_context


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


class TestSoapConfigDetection:
    """``is_soap_config`` recognises SOAP tools on either side (§32)."""

    def test_detected_from_request_body_codec(self):
        """A soap request body codec marks the tool as SOAP."""
        assert is_soap_config({"request": {"body": {"codec": "soap"}}}) is True

    def test_detected_from_response_codec(self):
        """A soap response codec alone is enough."""
        assert is_soap_config({"response": {"codec": "soap"}}) is True

    def test_detected_from_extensions_binding(self):
        """A WSDL-derived tool declares the soap body codec."""
        assert is_soap_config({"request": {"body": {"codec": "soap"}}, "extensions": {"soap": {"version": "1.1"}}}) is True

    def test_non_soap_configs_are_rejected(self):
        """REST/JSON tools and empty configs are not SOAP."""
        assert is_soap_config(None) is False
        assert is_soap_config({}) is False
        assert is_soap_config({"request": {"body": {"codec": "json"}}, "response": {"codec": "auto"}}) is False


class TestSoapContentType:
    """SOAP request Content-Type (§33)."""

    def test_soap11_uses_text_xml(self):
        """SOAP 1.1 declares text/xml and carries the action in a header."""
        config = {"request": {"body": {"codec": "soap"}, "soap": {"version": "1.1", "soapAction": "urn:r#Q"}}}

        assert soap_content_type(config) == "text/xml; charset=utf-8"

    def test_soap12_carries_action_parameter(self):
        """SOAP 1.2 carries the action as a Content-Type parameter."""
        config = {"request": {"body": {"codec": "soap"}, "soap": {"version": "1.2", "soapAction": "urn:r#Q"}}}

        assert soap_content_type(config) == 'application/soap+xml; charset=utf-8; action="urn:r#Q"'

    def test_soap12_without_action_omits_the_parameter(self):
        """A SOAP 1.2 binding without an action emits no empty parameter."""
        config = {"request": {"body": {"codec": "soap"}, "soap": {"version": "1.2"}}}

        assert soap_content_type(config) == "application/soap+xml; charset=utf-8"

    def test_codec_content_type_is_the_base(self):
        """The body codec's media type is the base; binding adds parameters."""
        config = {"request": {"body": {"codec": "soap"}, "soap": {"version": "1.2", "soapAction": "urn:r#Q"}}}

        assert soap_content_type(config, "application/soap+xml") == 'application/soap+xml; charset=utf-8; action="urn:r#Q"'

    def test_non_soap_config_returns_none(self):
        """A non-SOAP tool leaves the codec's own Content-Type untouched."""
        assert soap_content_type(None) is None
        assert soap_content_type({"request": {"body": {"codec": "json"}}}) is None


class TestSoapEnvelopeEncode:
    """The request builder + SoapCodec produce a real envelope (§32/§33)."""

    def test_soap11_envelope_roundtrip(self):
        """A SOAP 1.1 payload encodes to an envelope the codec can decode."""
        config = {
            "request": {
                "method": "POST",
                "pathTemplate": "/ReportService",
                "body": {"codec": "soap", "mediaType": "text/xml"},
                "soap": {"version": "1.1", "operation": "QueryReport", "namespace": "urn:report"},
            },
            "response": {"codec": "soap"},
        }
        built = RequestBuilder(build_default_codec_registry()).build({"body": {"factory": "FAB1"}}, config["request"], config)

        assert built.body.content_type == "text/xml"
        body = built.body.value
        assert b"Soap" not in body  # never the Python repr of the envelope class
        assert b"Envelope" in body and b"QueryReport" in body and b"FAB1" in body


# ── adapter-level wiring (§32–§34) ────────────────────────────────────────


class _SoapResponse:
    """Minimal httpx-like response for the protocol_config path."""

    def __init__(self, status_code=200, body=b"", content_type="text/xml"):
        """Initialise with a status, raw body, and Content-Type."""
        self.status_code = status_code
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self.headers = httpx.Headers({"content-type": content_type})


class _SoapClient:
    """Recording client answering every request with one canned response."""

    def __init__(self, response):
        """Initialise with the canned response."""
        self._response = response
        self.calls = []

    async def request(self, method, url, **kwargs):
        """Record the call and return the canned response."""
        self.calls.append((method, url, kwargs))
        return self._response


def _soap_protocol_config(version="1.1", action="urn:report#QueryReport", path_template="/ReportService"):
    """Build a SOAP protocol_config as the WSDL provider emits it."""
    content_type = "application/soap+xml" if version == "1.2" else "text/xml"
    return {
        "version": 1,
        "request": {
            "method": "POST",
            "pathTemplate": path_template,
            "preferredContentType": content_type,
            "body": {"codec": "soap", "mediaType": content_type},
            "soap": {"version": version, "operation": "QueryReport", "namespace": "urn:report", "soapAction": action},
        },
        "response": {"codec": "soap", "preferredMediaTypes": ["application/soap+xml", "text/xml"]},
    }


def _soap_operation(path_template="/ReportService"):
    """Build the OperationDefinition a SOAP tool compiles to."""
    return OperationDefinition(
        key="http:soap:query-report",
        protocol="http",
        request={"url": f"https://svc.example.com{path_template}", "method": "POST"},
    )


def _ok_envelope(body_xml: str) -> bytes:
    """Wrap a Body payload in a SOAP 1.1 Envelope."""
    return (
        '<?xml version="1.0"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        f"{body_xml}"
        "</soap:Body></soap:Envelope>"
    ).encode()


def _fault_envelope(code="soap:Client", message="Bad factory") -> bytes:
    """Build a SOAP 1.1 Fault envelope."""
    return (
        '<?xml version="1.0"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        f"<soap:Fault><faultcode>{code}</faultcode><faultstring>{message}</faultstring>"
        "<detail><message>invalid</message></detail></soap:Fault>"
        "</soap:Body></soap:Envelope>"
    ).encode()


@pytest.fixture(autouse=True)
def _neutralize_ssrf(monkeypatch):
    """Neutralize SSRF validation so the adapter reaches the transport."""

    async def _validate_unpinned(url, label):
        return {"resolved_ip": None, "hostname": None, "original_authority": None}

    monkeypatch.setattr(adapter_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_unpinned)
    monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", False)


class TestSoapAdapterRequestWiring:
    """The adapter emits a real SOAP request (§32/§33)."""

    async def test_soap11_request_carries_envelope_action_and_content_type(self):
        """SOAP 1.1 sends an envelope, a SOAPAction header, and text/xml."""
        client = _SoapClient(_SoapResponse(body=_ok_envelope("<QueryReportResponse><count>3</count></QueryReportResponse>")))
        context = _make_context(http_client=client, protocol_config=_soap_protocol_config("1.1"))

        await HttpProtocolAdapter().invoke(_soap_operation(), {"body": {"factory": "FAB1"}}, context)

        method, url, kwargs = client.calls[0]
        assert method == "POST" and url == "https://svc.example.com/ReportService"
        assert kwargs["headers"]["SOAPAction"] == "urn:report#QueryReport"
        assert kwargs["headers"]["Content-Type"] == "text/xml; charset=utf-8"
        assert b"Envelope" in kwargs["content"] and b"FAB1" in kwargs["content"]

    async def test_soap12_request_uses_content_type_action(self):
        """SOAP 1.2 drops the header and carries the action in the Content-Type."""
        client = _SoapClient(_SoapResponse(body=_ok_envelope("<QueryReportResponse><count>3</count></QueryReportResponse>"), content_type="application/soap+xml"))
        context = _make_context(http_client=client, protocol_config=_soap_protocol_config("1.2"))

        await HttpProtocolAdapter().invoke(_soap_operation(), {"body": {"factory": "FAB1"}}, context)

        headers = client.calls[0][2]["headers"]
        assert "SOAPAction" not in headers
        assert headers["Content-Type"] == 'application/soap+xml; charset=utf-8; action="urn:report#QueryReport"'


class TestSoapAdapterFaultMapping:
    """SOAP Faults map onto the canonical error model (§34)."""

    async def test_client_fault_maps_to_invalid_argument(self):
        """A soap:Client fault becomes INVALID_ARGUMENT with code and detail."""
        client = _SoapClient(_SoapResponse(status_code=500, body=_fault_envelope("soap:Client", "Bad factory")))
        context = _make_context(http_client=client, protocol_config=_soap_protocol_config())

        with pytest.raises(ProtocolError) as exc_info:
            await HttpProtocolAdapter().invoke(_soap_operation(), {"body": {"factory": "FAB1"}}, context)

        error = exc_info.value
        assert error.category is ErrorCategory.INVALID_ARGUMENT
        assert error.code == "soap:Client"
        assert error.message == "Bad factory"
        assert error.details == {"message": "invalid"}
        assert error.origin == "http:soap"

    async def test_server_fault_maps_to_upstream_error(self):
        """A soap:Server fault becomes UPSTREAM_ERROR."""
        client = _SoapClient(_SoapResponse(status_code=500, body=_fault_envelope("soap:Server", "Backend down")))
        context = _make_context(http_client=client, protocol_config=_soap_protocol_config())

        with pytest.raises(ProtocolError) as exc_info:
            await HttpProtocolAdapter().invoke(_soap_operation(), {"body": {"factory": "FAB1"}}, context)

        assert exc_info.value.category is ErrorCategory.UPSTREAM_ERROR

    async def test_non_fault_error_page_becomes_status_error(self):
        """A non-2xx SOAP response that is not a Fault is a status error."""
        client = _SoapClient(_SoapResponse(status_code=502, body=b"<html>Bad Gateway</html>"))
        context = _make_context(http_client=client, protocol_config=_soap_protocol_config())

        with pytest.raises(ProtocolError) as exc_info:
            await HttpProtocolAdapter().invoke(_soap_operation(), {"body": {"factory": "FAB1"}}, context)

        error = exc_info.value
        assert error.code == REST_HTTP_STATUS_ERROR
        assert error.protocol_status == 502
        assert error.category is ErrorCategory.UPSTREAM_ERROR

    async def test_soap_success_is_returned_unwrapped(self):
        """A 2xx SOAP response decodes to the Body child, not a Fault."""
        body = (
            '<?xml version="1.0"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
            "<QueryReportResponse><count>3</count></QueryReportResponse>"
            "</soap:Body></soap:Envelope>"
        ).encode()
        client = _SoapClient(_SoapResponse(body=body))
        context = _make_context(http_client=client, protocol_config=_soap_protocol_config())

        result = await HttpProtocolAdapter().invoke(_soap_operation(), {"body": {"factory": "FAB1"}}, context)

        assert result.data == {"QueryReportResponse": {"count": "3"}}
        assert result.metadata["status_code"] == 200

    async def test_malformed_2xx_body_is_an_error_not_success(self):
        """A 2xx SOAP response that is not an envelope is an upstream error."""
        client = _SoapClient(_SoapResponse(body=b"<html>not soap</html>"))
        context = _make_context(http_client=client, protocol_config=_soap_protocol_config())

        with pytest.raises(ProtocolError) as exc_info:
            await HttpProtocolAdapter().invoke(_soap_operation(), {"body": {"factory": "FAB1"}}, context)

        error = exc_info.value
        assert error.code == REST_UNEXPECTED_STATUS
        assert error.category is ErrorCategory.UPSTREAM_ERROR
        assert error.protocol_status == 200
