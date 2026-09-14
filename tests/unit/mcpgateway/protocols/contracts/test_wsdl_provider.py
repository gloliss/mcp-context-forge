# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/contracts/test_wsdl_provider.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the WSDL contract provider (PR5, design §31).
"""

# Standard
import textwrap

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.contracts.models import ContractArtifact, ContractProviderError, DiscoveryContext
from mcpgateway.protocols.contracts.wsdl import WsdlContractProvider

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


def _artifact(payload: bytes = _WSDL.encode("utf-8")) -> ContractArtifact:
    """Build a WSDL ContractArtifact."""
    return ContractArtifact(payload=payload, artifact_format="wsdl", source_type="wsdl-upload")


class TestWsdlContractProvider:
    """WsdlContractProvider compiles WSDL operations (§31)."""

    async def test_discovers_operations(self):
        """One operation is discovered with the stable key and SOAP config."""
        provider = WsdlContractProvider()
        catalog = await provider.discover(_artifact(), DiscoveryContext())

        assert catalog.source_type == "wsdl"
        assert len(catalog.operations) == 1
        operation = catalog.operations[0]
        assert operation.key == "ReportService:ReportPort:{urn:report}ReportBinding:QueryReport"
        assert operation.protocol == "http"
        assert operation.source_operation_id == "QueryReport"
        # A SOAP operation now compiles to the same typed HTTP contracts an
        # OpenAPI one does (design §31), with the SOAP specifics in typed
        # fields rather than in UI-only extensions.
        assert operation.request.method == "POST"
        assert operation.request.path_template == "/soap"
        assert operation.request.bodies[0].codec == "soap"
        assert operation.request.bodies[0].media_type == "text/xml"
        assert operation.response.codec == "soap"
        assert operation.soap_binding["version"] == "1.1"
        assert operation.soap_binding["soapAction"] == "urn:report#QueryReport"
        assert operation.extensions["service"] == "ReportService"

    async def test_source_hash_is_content_hash(self):
        """The catalog source hash is the artifact payload SHA-256."""
        provider = WsdlContractProvider()
        catalog = await provider.discover(_artifact(), DiscoveryContext())

        import hashlib

        assert catalog.source_hash == hashlib.sha256(_WSDL.encode("utf-8")).hexdigest()

    async def test_unsupported_artifact_format_rejected(self):
        """A non-WSDL artifact format is rejected."""
        provider = WsdlContractProvider()
        artifact = ContractArtifact(payload=b"{}", artifact_format="openapi-json", source_type="openapi-upload")

        with pytest.raises(ContractProviderError, match="Unsupported WSDL artifact format"):
            await provider.discover(artifact, DiscoveryContext())

    async def test_invalid_wsdl_raises_provider_error(self):
        """Malformed WSDL payloads surface as ContractProviderError."""
        provider = WsdlContractProvider()
        artifact = ContractArtifact(payload=b"this is not XML at all", artifact_format="wsdl", source_type="wsdl-upload")

        with pytest.raises(ContractProviderError, match="Unable to parse WSDL|WSDL discovery failed"):
            await provider.discover(artifact, DiscoveryContext())
