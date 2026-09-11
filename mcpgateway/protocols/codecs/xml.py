# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/codecs/xml.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

XML codec (PR4, design-document §23–§24).

``XmlCodec`` translates between Python/JSON values and the XML wire format
using the canonical mapping:

* attributes are prefixed with ``@`` (``attribute_prefix="@"``),
* element text lives under ``#text`` (``text_key="#text"``),
* the root element is preserved (``preserve_root=True``).

When the invocation context carries an :class:`XsdTypeSystem`
(``CodecContext.xsd_type_system``), decode/validate/encode are delegated to
the compiled XSD schema and the result is validated.  Without a schema the
codec falls back to a compact ElementTree↔dict conversion that follows the
same canonical mapping — a pragmatic path for schema-less XML, not a
replacement for ``xmlschema``'s type-aware conversion.

Security (design-document §28) is enforced on every payload: DTD/entity
declarations are rejected before parsing, payload size is bounded by
``max_bytes``, and decoded depth/node counts are bounded.  The limits come
from an :class:`XmlSecurityLimits` instance and cannot be relaxed by
callers.
"""

# Standard
from typing import Any, Optional
from xml.etree import ElementTree as ET  # nosec B405 - every parse path runs XmlSecurityLimits.check_bytes() first, which rejects DTD/entity declarations (design §28)

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext, EncodedBody, MessageCodec
from mcpgateway.protocols.contracts.xsd_types import (
    XmlSecurityLimits,
    XsdTypeSystem,
    build_xml_converter,
)

# Canonical mapping constants (design-document §24).
_ATTRIBUTE_PREFIX = "@"
_TEXT_KEY = "#text"


class XmlCodec(MessageCodec):
    """Encode Python values as XML and decode XML bytes (schema-aware)."""

    media_types = frozenset({"application/xml", "text/xml"})

    def __init__(
        self,
        security: Optional[XmlSecurityLimits] = None,
        xsd_type_system: Optional[XsdTypeSystem] = None,
    ) -> None:
        """Initialise the codec with optional security limits and schema.

        Args:
            security: Payload security limits (§28); defaults to the
                registry defaults.
            xsd_type_system: Optional default schema used when the codec
                context does not carry one.
        """
        self._security = security or XmlSecurityLimits()
        self._converter = build_xml_converter()
        self._xsd_type_system = xsd_type_system

    def encode(self, value: Any, context: CodecContext) -> EncodedBody:
        """Encode a value to XML bytes.

        Args:
            value: The outbound value.  With a schema this is the
                preserved-root dict; without one it may be any JSON-like
                value.
            context: Per-invocation codec context (may carry an
                ``xsd_type_system``).

        Returns:
            An ``EncodedBody`` in ``content`` mode carrying XML bytes.

        Raises:
            XmlSecurityError: When the encoded payload would violate the
                security limits.
        """
        xsd = context.xsd_type_system or self._xsd_type_system
        if xsd is not None:
            payload = xsd.encode(value)
        else:
            payload = self._loose_encode(value)
        self._security.check_bytes(payload)
        return EncodedBody(mode="content", value=payload, content_type="application/xml")

    def decode(self, payload: bytes, context: CodecContext) -> Any:
        """Decode XML bytes into a Python value.

        Args:
            payload: The raw XML bytes.
            context: Per-invocation codec context (may carry an
                ``xsd_type_system``).

        Returns:
            The decoded value using the canonical mapping.

        Raises:
            XmlSecurityError: On payload-security violations.
        """
        self._security.check_bytes(payload)
        xsd = context.xsd_type_system or self._xsd_type_system
        if xsd is not None:
            return xsd.decode(payload)
        value = self._loose_decode(payload)
        self._security.check_decoded(value)
        return value

    def _loose_decode(self, payload: bytes) -> Any:
        """Decode XML without a schema via the canonical ET→dict walker.

        Args:
            payload: The raw XML bytes.

        Returns:
            The decoded value.

        Raises:
            ET.ParseError: When the payload is not well-formed XML.
        """
        root = ET.fromstring(payload)  # nosec B314 - only reached from decode(), which runs check_bytes() immediately before this call (design §28)
        return {root.tag: self._element_to_value(root)}

    def _loose_encode(self, value: Any) -> bytes:
        """Encode a value to XML without a schema via the canonical walker.

        Args:
            value: The preserved-root dict (``{"Root": {...}}``) or a bare
                value under a generated root.

        Returns:
            The encoded XML bytes.
        """
        if isinstance(value, dict) and len(value) == 1:
            root_name, root_value = next(iter(value.items()))
            root = self._value_to_element(root_name, root_value)
        else:
            root = self._value_to_element("root", value)
        return ET.tostring(root, encoding="utf-8")

    def _element_to_value(self, element: ET.Element) -> Any:
        """Walk one ElementTree element into the canonical dict value."""
        value: Any = {}

        for attr_name, attr_value in element.attrib.items():
            value[f"{_ATTRIBUTE_PREFIX}{attr_name}"] = attr_value

        text = (element.text or "").strip()
        children = list(element)
        if children:
            grouped: dict[str, list] = {}
            for child in children:
                grouped.setdefault(child.tag, []).append(self._element_to_value(child))
            for tag, items in grouped.items():
                value[tag] = items[0] if len(items) == 1 else items
            # mixed content: preserve non-whitespace text under #text.
            if text:
                value[_TEXT_KEY] = text
        elif text:
            value = text if not value else {**value, _TEXT_KEY: text}

        return value

    def _value_to_element(self, name: str, value: Any) -> ET.Element:
        """Build one ElementTree element from a canonical dict value."""
        element = ET.Element(name)
        if isinstance(value, dict):
            text = value.get(_TEXT_KEY)
            for key, item in value.items():
                if key == _TEXT_KEY:
                    continue
                if key.startswith(_ATTRIBUTE_PREFIX):
                    element.set(key[len(_ATTRIBUTE_PREFIX) :], _scalar_text(item))
                elif isinstance(item, (list, tuple)):
                    # Repeated element: one sibling per item, not a wrapper.
                    for sub in item:
                        element.append(self._value_to_element(key, sub))
                else:
                    element.append(self._value_to_element(key, item))
            if text is not None:
                element.text = _scalar_text(text)
        elif isinstance(value, (list, tuple)):
            for item in value:
                element.append(self._value_to_element(name, item))
        elif value is not None:
            element.text = _scalar_text(value)
        return element


def _scalar_text(value: Any) -> str:
    """Serialise a scalar to its XML text form."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


__all__ = ["XmlCodec"]
