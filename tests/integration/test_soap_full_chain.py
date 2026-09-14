# -*- coding: utf-8 -*-
"""Location: ./tests/integration/test_soap_full_chain.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

SOAP full-chain integration test (PR5, design-document §63).

Exercises the whole chain against a real local upstream — no mocks:

    WSDL artifact → WsdlContractProvider → OperationToolCompiler
                 → protocol_config → HttpProtocolAdapter → SoapCodec → HTTP

A real HTTP server plays the SOAP endpoint, so the test observes the actual
bytes on the wire (a genuine ``soap:Envelope`` with the right ``SOAPAction``
and ``Content-Type``) and the actual fault handling, rather than asserting
against a stubbed transport.
"""

# Standard
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Tuple
import textwrap

# Third-Party
import httpx
import pytest

# First-Party
from mcpgateway.protocols.contracts.models import ContractArtifact, DiscoveryContext, OperationDefinition
from mcpgateway.protocols.contracts.wsdl import WsdlContractProvider
from mcpgateway.protocols.http import adapter as adapter_module
from mcpgateway.protocols.http.adapter import HttpProtocolAdapter
from mcpgateway.protocols.http.legacy_contract import LegacyRestContractBuilder
from mcpgateway.protocols.models import ErrorCategory, ProtocolError
from mcpgateway.services.operation_tool_compiler import OperationToolCompiler, ToolCompileOverrides
from tests.unit.mcpgateway.protocols.http.test_http_adapter_legacy import _make_context

_WSDL = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
      xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
      xmlns:tns="urn:report" xmlns:xsd="http://www.w3.org/2001/XMLSchema"
      targetNamespace="urn:report">
      <types>
        <xsd:schema targetNamespace="urn:report">
          <xsd:element name="QueryRequest">
            <xsd:complexType><xsd:sequence>
              <xsd:element name="factory" type="xsd:string"/>
              <xsd:element name="date" type="xsd:string"/>
            </xsd:sequence></xsd:complexType>
          </xsd:element>
          <xsd:element name="QueryResponse">
            <xsd:complexType><xsd:sequence>
              <xsd:element name="status" type="xsd:string"/>
            </xsd:sequence></xsd:complexType>
          </xsd:element>
        </xsd:schema>
      </types>
      <message name="QueryRequestMsg"><part name="parameters" element="tns:QueryRequest"/></message>
      <message name="QueryResponseMsg"><part name="parameters" element="tns:QueryResponse"/></message>
      <portType name="ReportPort">
        <operation name="QueryReport">
          <input message="tns:QueryRequestMsg"/>
          <output message="tns:QueryResponseMsg"/>
        </operation>
      </portType>
      <binding name="ReportBinding" type="tns:ReportPort">
        <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
        <operation name="QueryReport">
          <soap:operation soapAction="urn:report#QueryReport"/>
          <input><soap:body use="literal"/></input>
          <output><soap:body use="literal"/></output>
        </operation>
      </binding>
      <service name="ReportService">
        <port name="ReportPort" binding="tns:ReportBinding">
          <soap:address location="http://report.internal/soap"/>
        </port>
      </service>
    </definitions>
    """
)

_SUCCESS_ENVELOPE = (
    '<?xml version="1.0"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
    "<QueryResponse><status>OK</status></QueryResponse>"
    "</soap:Body></soap:Envelope>"
)

_FAULT_ENVELOPE = (
    '<?xml version="1.0"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
    "<soap:Fault><faultcode>soap:Client</faultcode>"
    "<faultstring>Unknown factory</faultstring>"
    "<detail><message>factory FAB9 does not exist</message></detail>"
    "</soap:Fault></soap:Body></soap:Envelope>"
)


class _SoapUpstream:
    """A real HTTP server standing in for the SOAP endpoint."""

    def __init__(self, *, fault: bool = False) -> None:
        """Record requests and answer with a success or fault envelope."""
        self._fault = fault
        self.requests: List[Dict[str, Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_SoapUpstream":
        """Start the server on a loopback port."""
        upstream = self

        class _Handler(BaseHTTPRequestHandler):
            """Answer every POST with the canned SOAP envelope."""

            def do_POST(self):  # noqa: N802 - stdlib handler name
                """Record the request and reply with the envelope."""
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                upstream.requests.append(
                    {
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": body.decode("utf-8", "replace"),
                    }
                )
                payload = (_FAULT_ENVELOPE if upstream._fault else _SUCCESS_ENVELOPE).encode()
                self.send_response(500 if upstream._fault else 200)
                self.send_header("Content-Type", "text/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                """Silence the handler's stderr access log."""

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args) -> bool:
        """Shut the server down."""
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        return False

    @property
    def base_url(self) -> str:
        """Return the upstream's base URL."""
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_port}"


async def _compile_soap_operation(base_url: str) -> Tuple[OperationDefinition, Dict[str, Any]]:
    """Run WSDL → provider → compiler and rebuild the runtime operation.

    The runtime never sees the typed contract: `ToolService` compiles the
    artifact into a tool and then rebuilds an `OperationDefinition` through
    `LegacyRestContractBuilder`, carrying the `protocol_config` on the
    invocation context.  This helper reproduces that handoff exactly, so the
    test exercises the same seam production does.
    """
    artifact = ContractArtifact(payload=_WSDL.encode("utf-8"), artifact_format="wsdl", source_type="wsdl-upload")
    catalog = await WsdlContractProvider().discover(artifact, DiscoveryContext())
    assert catalog.operations, "the WSDL must yield at least one operation"

    service = type("Service", (), {"slug": "report", "base_url": base_url})()
    compiled = OperationToolCompiler().compile(
        catalog.operations[0],
        service,
        type("Artifact", (), {"content_hash": catalog.source_hash, "source_type": "wsdl"})(),
        ToolCompileOverrides(),
    )
    config = dict(compiled.protocol_config or {})
    # The SOAP endpoint address is the upstream's base URL here; the WSDL's
    # own address is only a contract default.
    config["request"]["pathTemplate"] = "/soap"
    runtime_operation = LegacyRestContractBuilder.from_tool(
        None,
        tool_payload={
            "name": compiled.name,
            "description": compiled.description,
            "base_url": base_url,
            "protocol_config": config,
        },
    )
    return runtime_operation, config


@pytest.fixture(autouse=True)
def _neutralize_ssrf(monkeypatch):
    """Let the adapter reach the loopback upstream."""

    async def _validate_unpinned(url, label):
        return {"resolved_ip": None, "hostname": None, "original_authority": None}

    monkeypatch.setattr(adapter_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_unpinned)
    monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", False)


def _run(coro):
    """Run a coroutine from a synchronous test."""
    return asyncio.run(coro)


class TestSoapCompilation:
    """WSDL operations compile into a SOAP protocol_config (§31)."""

    def test_compiled_config_pins_the_soap_codec_and_binding(self):
        """The compiled tool carries the SOAP body/response codec and binding."""
        _operation, config = _run(_compile_soap_operation("http://127.0.0.1:1"))

        assert config["request"]["method"] == "POST"
        assert config["request"]["body"]["codec"] == "soap"
        assert config["request"]["soap"]["version"] == "1.1"
        assert config["request"]["soap"]["soapAction"] == "urn:report#QueryReport"
        # The envelope body element is the WSDL input element, not the
        # operation name — that is what a document/literal endpoint expects.
        assert config["request"]["soap"]["operation"] == "QueryRequest"
        # SOAP 1.1 replies arrive as text/xml, so the codec must be pinned.
        assert config["response"]["codec"] == "soap"
        assert config["response"]["preferredMediaTypes"] == ["text/xml"]


class TestSoapFullChain:
    """WSDL → compiler → adapter → HTTP against a real SOAP endpoint (§63)."""

    def test_successful_call_sends_an_envelope_and_decodes_the_body(self):
        """A compiled SOAP tool reaches the upstream and decodes its Body."""
        with _SoapUpstream() as upstream:
            operation, config = _run(_compile_soap_operation(upstream.base_url))
            client = httpx.AsyncClient()
            context = _make_context(http_client=client, protocol_config=config)
            try:
                result = _run(HttpProtocolAdapter().invoke(operation, {"body": {"factory": "FAB1", "date": "2026-09-10"}}, context))
            finally:
                _run(client.aclose())

        assert result.data == {"QueryResponse": {"status": "OK"}}
        assert result.metadata["status_code"] == 200

        sent = upstream.requests[0]
        assert sent["path"] == "/soap"
        assert sent["headers"]["soapaction"] == "urn:report#QueryReport"
        assert sent["headers"]["content-type"].startswith("text/xml")
        assert "soap:Envelope" in sent["body"] or "Envelope" in sent["body"]
        assert "QueryRequest" in sent["body"]
        assert "FAB1" in sent["body"]

    def test_soap_fault_becomes_a_canonical_protocol_error(self):
        """A Fault envelope maps to INVALID_ARGUMENT with its detail kept (§34)."""
        with _SoapUpstream(fault=True) as upstream:
            operation, config = _run(_compile_soap_operation(upstream.base_url))
            client = httpx.AsyncClient()
            context = _make_context(http_client=client, protocol_config=config)
            try:
                with pytest.raises(ProtocolError) as exc_info:
                    _run(HttpProtocolAdapter().invoke(operation, {"body": {"factory": "FAB9", "date": "2026-09-10"}}, context))
            finally:
                _run(client.aclose())

        error = exc_info.value
        assert error.category is ErrorCategory.INVALID_ARGUMENT
        assert error.code == "soap:Client"
        assert error.message == "Unknown factory"
        assert error.origin == "http:soap"
        # The safe detail travels; no raw stack trace is surfaced.
        assert error.details == {"message": "factory FAB9 does not exist"}
