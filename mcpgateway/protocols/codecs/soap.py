# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/codecs/soap.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

SOAP codec (PR5, design-document §33).

``SoapCodec`` encodes Python values into a SOAP envelope and decodes SOAP
responses back into Python values, covering SOAP 1.1 (``text/xml`` +
``SOAPAction``) and SOAP 1.2 (``application/soap+xml``), Envelope/Header/
Body, namespaces, and Fault handling.

The codec is transport-agnostic: it builds/parses XML with the stdlib
``ElementTree`` and the canonical XmlCodec mapping; the HTTP runtime
(``protocols.http.soap``) turns SOAP configuration into headers and maps
:class:`SoapFaultError` onto the ContextForge error model (§34).
"""

# Standard
from typing import Any, Optional
from xml.etree import ElementTree as ET

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext, EncodedBody, MessageCodec
from mcpgateway.protocols.codecs.xml import XmlCodec
from mcpgateway.protocols.contracts.xsd_types import XmlSecurityLimits

# SOAP envelope namespaces (design §33).
SOAP11_ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP12_ENVELOPE_NS = "http://www.w3.org/2003/05/soap-envelope"

# Envelope prefix used on wire elements.
_ENVELOPE_PREFIX = "soap"


def resolve_soap_config(protocol_config: Optional[dict]) -> dict:
    """Return the SOAP binding block carried by a ``protocol_config``.

    The binding travels on two different keys depending on how the tool was
    produced: ``WsdlContractProvider`` attaches it to the operation's
    ``extensions.soap``, while a registry-compiled tool carries it inline at
    ``request.soap``.  Both are accepted here so the codec and the HTTP glue
    (``protocols.http.soap``) always read the same block.

    Args:
        protocol_config: The tool's ``protocol_config`` (may be ``None``).

    Returns:
        The SOAP binding mapping, or an empty dict when absent.
    """
    if not protocol_config:
        return {}
    inline = (protocol_config.get("request") or {}).get("soap")
    if inline:
        return dict(inline)
    return dict(protocol_config.get("extensions", {}).get("soap") or {})


class SoapFaultError(ValueError):
    """A SOAP Fault was returned by the upstream (design §34).

    Attributes:
        code: The SOAP fault code (e.g. ``soap:Client`` / ``soap:Server``).
        fault_string: The fault string.
        detail: The safe fault detail (may be empty).
    """

    def __init__(self, code: str, fault_string: str, detail: Any = None) -> None:
        """Initialise with fault code, string, and optional detail."""
        super().__init__(fault_string)
        self.code = code
        self.fault_string = fault_string
        self.detail = detail


class SoapCodec(MessageCodec):
    """Encode Python values as SOAP envelopes and decode SOAP responses."""

    media_types = frozenset({"application/soap+xml"})

    def __init__(self, security: Optional[XmlSecurityLimits] = None) -> None:
        """Initialise with an internal XmlCodec for body conversion.

        Args:
            security: XML payload security limits (§28); defaults apply.
        """
        self._xml = XmlCodec(security=security)

    def encode(self, value: Any, context: CodecContext) -> EncodedBody:
        """Encode a value into a SOAP envelope.

        Args:
            value: The operation payload (a dict of the operation's
                child elements/attributes).
            context: Carries the SOAP binding configuration under
                ``protocol_config.request.soap``: ``version`` (``"1.1"`` or
                ``"1.2"``), ``operation`` (Clark notation), ``namespace``
                and ``soapAction``.

        Returns:
            An ``EncodedBody`` carrying the envelope XML bytes and the
            correct SOAP ``Content-Type``.
        """
        config = (context.protocol_config or {}).get("request") or {}
        soap = config.get("soap") or resolve_soap_config(context.protocol_config)
        version = str(soap.get("version") or "1.1")
        operation = soap.get("operation")
        namespace = soap.get("namespace")
        payload = self._strip_body_wrapper(value)

        envelope = self._build_envelope(payload, version=version, operation=operation, namespace=namespace)
        xml_bytes = ET.tostring(envelope, encoding="utf-8")
        content_type = "application/soap+xml" if version == "1.2" else "text/xml"
        return EncodedBody(mode="content", value=xml_bytes, content_type=content_type)

    def decode(self, payload: bytes, context: CodecContext) -> Any:
        """Decode a SOAP response into a Python value.

        Args:
            payload: The raw SOAP response XML bytes.
            context: Unused for decoding.

        Returns:
            The decoded Body content (the first child of ``soap:Body``) as
            a dict using the canonical XML mapping.

        Raises:
            SoapFaultError: When the response carries a SOAP Fault.
            XmlSecurityError: On payload-security violations.
        """
        self._xml._security.check_bytes(payload)  # pylint: disable=protected-access
        root = ET.fromstring(payload)
        body = self._find_body(root)
        if body is None:
            raise ValueError("SOAP response is missing a Body element")

        fault = self._find_fault(body)
        if fault is not None:
            raise SoapFaultError(
                code=self._fault_code(fault),
                fault_string=self._fault_string(fault),
                detail=self._fault_detail(fault),
            )

        children = [child for child in body if not (child.tag.endswith("}Header") or child.tag == "Header")]
        if not children:
            return None
        return {children[0].tag.split("}")[-1]: self._element_to_value(children[0])}

    # -- envelope construction ------------------------------------------------

    def _build_envelope(self, payload: Any, *, version: str, operation: Optional[str], namespace: Optional[str]) -> ET.Element:
        """Build a SOAP Envelope ElementTree element."""
        env_ns = SOAP12_ENVELOPE_NS if version == "1.2" else SOAP11_ENVELOPE_NS
        # Force the SOAP namespace to serialize with the canonical prefix.
        ET.register_namespace(_ENVELOPE_PREFIX, env_ns)
        envelope = ET.Element(
            f"{{{env_ns}}}Envelope",
            {f"xmlns:{_ENVELOPE_PREFIX}": env_ns, "xmlns:xsd": "http://www.w3.org/2001/XMLSchema", "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        )
        body = ET.SubElement(envelope, f"{{{env_ns}}}Body")
        if operation:
            operation_element = ET.SubElement(body, operation)
            if namespace and not operation.startswith("{"):
                operation_element.set("xmlns", namespace)
            self._append_payload(operation_element, payload)
        else:
            self._append_payload(body, payload)
        return envelope

    def _append_payload(self, parent: ET.Element, payload: Any) -> None:
        """Append a canonical payload dict to a parent element."""
        if isinstance(payload, dict):
            for key, item in payload.items():
                if key.startswith("@"):
                    parent.set(key[1:], str(item))
                elif isinstance(item, (list, tuple)):
                    for sub in item:
                        parent.append(self._xml._value_to_element(key, sub))  # pylint: disable=protected-access
                else:
                    parent.append(self._xml._value_to_element(key, item))  # pylint: disable=protected-access
        else:
            parent.append(self._xml._value_to_element("value", payload))  # pylint: disable=protected-access

    # -- response handling ----------------------------------------------------

    @staticmethod
    def _find_body(root: ET.Element) -> Optional[ET.Element]:
        """Return the first soap:Body element in a parsed envelope."""
        for element in root.iter():
            if element.tag.endswith("}Body") or element.tag == "Body":
                return element
        return None

    @staticmethod
    def _find_fault(body: ET.Element) -> Optional[ET.Element]:
        """Return the first Fault element inside a Body, if any."""
        for element in body:
            if element.tag.endswith("}Fault") or element.tag == "Fault":
                return element
        return None

    @staticmethod
    def _fault_code(fault: ET.Element) -> str:
        """Extract the fault code (faultcode/faultcode@value for SOAP 1.2)."""
        code = fault.find("faultcode")
        if code is not None:
            value = code.get("value")
            return value or (code.text or "").strip()
        return "SOAP:Fault"

    @staticmethod
    def _fault_string(fault: ET.Element) -> str:
        """Extract the fault string."""
        fault_string = fault.find("faultstring")
        return (fault_string.text or "").strip() if fault_string is not None else "SOAP Fault"

    @staticmethod
    def _fault_detail(fault: ET.Element) -> Any:
        """Extract a safe fault detail, if any."""
        detail = fault.find("detail")
        if detail is None or not list(detail):
            return None
        return {child.tag.split("}")[-1]: (child.text or "").strip() for child in detail}

    def _element_to_value(self, element: ET.Element) -> Any:
        """Convert an ET element to the canonical dict value."""
        return self._xml._element_to_value(element)  # pylint: disable=protected-access

    @staticmethod
    def _strip_body_wrapper(value: Any) -> Any:
        """Unwrap a ``{"body": {...}}`` MCP argument into the payload itself."""
        if isinstance(value, dict) and set(value) == {"body"} and isinstance(value.get("body"), dict):
            return value["body"]
        return value


__all__ = ["SOAP11_ENVELOPE_NS", "SOAP12_ENVELOPE_NS", "SoapCodec", "SoapFaultError", "resolve_soap_config"]
