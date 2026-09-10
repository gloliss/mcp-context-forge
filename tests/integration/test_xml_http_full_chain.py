# -*- coding: utf-8 -*-
"""Location: ./tests/integration/test_xml_http_full_chain.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

XML-over-HTTP full-chain integration test (PR4, design-document §27/§62).

Proves that a manually declared XML operation is actually *schema-driven*
over a real HTTP call: the XSD the tool declares governs both the bytes that
leave the gateway and how the response is decoded.  Before the XSD binding
bridge, `XsdTypeSystem` existed but was never handed to the codec, so an XML
tool's schema was decorative — these tests would have passed with the
schema-less loose codec, which is exactly what they guard against.
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
import xmlschema

# First-Party
from mcpgateway.protocols.http import adapter as adapter_module
from mcpgateway.protocols.http.adapter import HttpProtocolAdapter
from mcpgateway.protocols.http.legacy_contract import LegacyRestContractBuilder
from tests.unit.mcpgateway.protocols.http.test_http_adapter_legacy import _make_context

_XSD = textwrap.dedent(
    """\
    <?xml version="1.0"?>
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="QueryRequest">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="factory" type="xs:string"/>
            <xs:element name="count" type="xs:integer"/>
          </xs:sequence>
        </xs:complexType>
      </xs:element>
    </xs:schema>
    """
)

_CONFORMING_RESPONSE = "<QueryResponse><status>OK</status><total>7</total></QueryResponse>"
# ``total`` is a string where the (echoed) contract expects an integer.
_NON_CONFORMING_RESPONSE = "<QueryResponse><status>OK</status><total>not-a-number</total></QueryResponse>"

_RESPONSE_XSD = textwrap.dedent(
    """\
    <?xml version="1.0"?>
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="QueryResponse">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="status" type="xs:string"/>
            <xs:element name="total" type="xs:integer"/>
          </xs:sequence>
        </xs:complexType>
      </xs:element>
    </xs:schema>
    """
)


class _XmlUpstream:
    """A real HTTP server standing in for the XML endpoint."""

    def __init__(self, response: str = _CONFORMING_RESPONSE, *, status: int = 200) -> None:
        """Serve the given XML document for every POST."""
        self._response = response
        self._status = status
        self.requests: List[Dict[str, Any]] = []
        self._server: HTTPServer | None = None

    def __enter__(self) -> "_XmlUpstream":
        """Start the server on a loopback port."""
        upstream = self

        class _Handler(BaseHTTPRequestHandler):
            """Record the request and reply with the canned XML."""

            def do_POST(self):  # noqa: N802 - stdlib handler name
                """Record the request body and answer with the canned document."""
                length = int(self.headers.get("Content-Length") or 0)
                upstream.requests.append(
                    {
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": self.rfile.read(length).decode("utf-8", "replace"),
                    }
                )
                payload = upstream._response.encode()
                self.send_response(upstream._status)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                """Silence the handler's stderr access log."""

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
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


def _protocol_config(base_url: str, *, request_xsd: str | None = _XSD, response_xsd: str | None = _RESPONSE_XSD) -> Dict[str, Any]:
    """Build the protocol_config of a manually declared XML operation."""
    body: Dict[str, Any] = {"codec": "xml", "mediaType": "application/xml"}
    if request_xsd is not None:
        body["xsd"] = {"schema": request_xsd}
    config: Dict[str, Any] = {
        "version": 1,
        "request": {"method": "POST", "pathTemplate": "/query", "body": body},
        "response": {"codec": "xml"},
    }
    if response_xsd is not None:
        config["response"]["xsd"] = {"schema": response_xsd}
    return config


def _operation(base_url: str) -> Any:
    """Rebuild the runtime operation the way ToolService does."""
    return LegacyRestContractBuilder.from_tool(
        None,
        tool_payload={"name": "query-report", "base_url": base_url, "protocol_config": {"request": {"method": "POST", "pathTemplate": "/query"}}},
    )


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


def _invoke(config: Dict[str, Any], arguments: Dict[str, Any], base_url: str):
    """Invoke the adapter against the upstream with one config."""
    client = httpx.AsyncClient()
    context = _make_context(http_client=client, protocol_config=config)
    try:
        return _run(HttpProtocolAdapter().invoke(_operation(base_url), arguments, context))
    finally:
        _run(client.aclose())


class TestXmlFullChain:
    """A declared XSD governs the request bytes and the response decode (§27)."""

    def test_conforming_call_round_trips(self):
        """A schema-conforming call reaches the upstream and decodes back."""
        with _XmlUpstream() as upstream:
            config = _protocol_config(upstream.base_url)

            result = _invoke(config, {"body": {"QueryRequest": {"factory": "FAB1", "count": 3}}}, upstream.base_url)

        sent = upstream.requests[0]
        assert sent["path"] == "/query"
        assert sent["headers"]["content-type"].startswith("application/xml")
        # The XSD drove the encoding: the root element is the schema's.
        assert "<QueryRequest>" in sent["body"]
        assert "FAB1" in sent["body"]
        assert result.data == {"QueryResponse": {"status": "OK", "total": 7}}

    def test_request_body_is_validated_against_the_declared_xsd(self):
        """A non-conforming request never leaves the gateway."""
        with _XmlUpstream() as upstream:
            config = _protocol_config(upstream.base_url)

            with pytest.raises(Exception) as exc_info:
                _invoke(config, {"body": {"QueryRequest": {"factory": "FAB1"}}}, upstream.base_url)

        # ``count`` is required by the schema, so encoding it fails loudly
        # rather than sending a document the upstream would reject.
        assert "count" in str(exc_info.value)
        assert upstream.requests == []

    def test_response_is_validated_against_the_declared_xsd(self):
        """A non-conforming response is rejected rather than silently decoded."""
        with _XmlUpstream(_NON_CONFORMING_RESPONSE) as upstream:
            config = _protocol_config(upstream.base_url, response_xsd=_RESPONSE_XSD)

            with pytest.raises(xmlschema.XMLSchemaValidationError):
                _invoke(config, {"body": {"factory": "FAB1", "count": 3}}, upstream.base_url)

    def test_conforming_response_passes_the_declared_xsd(self):
        """A conforming response decodes with its types applied."""
        with _XmlUpstream() as upstream:
            config = _protocol_config(upstream.base_url, response_xsd=_RESPONSE_XSD)

            result = _invoke(config, {"body": {"QueryRequest": {"factory": "FAB1", "count": 3}}}, upstream.base_url)

        assert result.data == {"QueryResponse": {"status": "OK", "total": 7}}

    def test_without_a_binding_the_loose_codec_still_works(self):
        """A schemaless XML tool is unaffected by the binding bridge."""
        with _XmlUpstream() as upstream:
            config = _protocol_config(upstream.base_url, request_xsd=None, response_xsd=None)

            result = _invoke(config, {"body": {"QueryRequest": {"factory": "FAB1", "count": 3}}}, upstream.base_url)

        assert "<QueryRequest>" in upstream.requests[0]["body"]
        # Without a schema there is no type information, so the value stays a
        # string — the contrast with the XSD-backed assertion above is the
        # point of this test.
        assert result.data == {"QueryResponse": {"status": "OK", "total": "7"}}
