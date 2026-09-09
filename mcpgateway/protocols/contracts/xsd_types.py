# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/contracts/xsd_types.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

XSD type system wrapper (PR4, design-document §25).

``XsdTypeSystem`` owns a compiled ``xmlschema`` schema (XSD 1.0 or 1.1) and
exposes the five operations the design document requires — ``load``,
``validate``, ``decode``, ``encode`` and ``find_element`` — over a stable
canonical mapping:

* attributes are prefixed with ``@`` (``attribute_prefix="@"``),
* element text lives under ``#text`` (``text_key="#text"``),
* the root element is preserved (``preserve_root=True``).

The wrapper centralises the security posture (design-document §28): DTD /
entity declarations are rejected before the payload ever reaches a parser,
payload size is bounded by ``max_bytes``, and decoded nesting depth is
bounded by ``max_depth``.  These limits cannot be switched off by callers.
"""

# Standard
import re
from dataclasses import dataclass
from typing import Any, Optional
from xml.etree import ElementTree as ET

# Third-Party
import xmlschema

# The XML ``py.typed`` markers this project uses for converter options are
# passed through ``xmlschema.converters.XMLSchemaConverter`` instances.
from xmlschema.converters import UnorderedConverter

_DTD_ENTITY_RE = re.compile(rb"<!DOCTYPE|<!ENTITY", re.IGNORECASE)

# Registry-wide default security limits (design-document §28).
DEFAULT_MAX_XML_BYTES = 4_194_304  # 4 MiB
DEFAULT_MAX_XML_DEPTH = 64
DEFAULT_MAX_XML_NODES = 100_000


class XmlSecurityError(ValueError):
    """Raised when an XML payload violates the security posture (§28)."""


@dataclass(frozen=True)
class XmlSecurityLimits:
    """Numeric and structural limits applied to every XML payload (§28).

    Attributes:
        max_bytes: Maximum encoded payload size in bytes.
        max_depth: Maximum decoded document nesting depth (root = 1).
        max_nodes: Maximum number of decoded nodes (keys across the tree).
        forbid_entities: Whether ``<!DOCTYPE``/``<!ENTITY`` declarations are
            rejected outright.  Always ``True``; cannot be disabled.
    """

    max_bytes: int = DEFAULT_MAX_XML_BYTES
    max_depth: int = DEFAULT_MAX_XML_DEPTH
    max_nodes: int = DEFAULT_MAX_XML_NODES
    forbid_entities: bool = True

    def __post_init__(self) -> None:
        """Lock the security posture: entity blocking cannot be disabled.

        Raises:
            ValueError: When ``forbid_entities=False`` is requested.
        """
        if not self.forbid_entities:
            raise ValueError("forbid_entities cannot be disabled (design §28)")

    def check_bytes(self, payload: bytes) -> None:
        """Reject oversized or DTD-bearing payloads before parsing.

        Args:
            payload: The raw XML bytes.

        Raises:
            XmlSecurityError: When the payload exceeds ``max_bytes`` or
                contains a DTD/entity declaration.
        """
        if self.forbid_entities and _DTD_ENTITY_RE.search(payload[: self.max_bytes]):
            raise XmlSecurityError("XML DTD/entity declarations are forbidden (design §28)")
        if len(payload) > self.max_bytes:
            raise XmlSecurityError(f"XML payload exceeds max_bytes={self.max_bytes}")

    def check_decoded(self, value: Any, depth: int = 1) -> int:
        """Recursively enforce depth/node limits over a decoded value.

        Args:
            value: A decoded XML value (dict/list/str/...).
            depth: Current nesting depth (root call passes 1).

        Returns:
            The number of nodes visited in the subtree.

        Raises:
            XmlSecurityError: When depth or node counts exceed the limits.
        """
        if depth > self.max_depth:
            raise XmlSecurityError(f"XML nesting exceeds max_depth={self.max_depth}")

        if isinstance(value, dict):
            count = 1
            for item in value.values():
                count += self.check_decoded(item, depth=depth + 1)
            if count > self.max_nodes:
                raise XmlSecurityError(f"XML node count exceeds max_nodes={self.max_nodes}")
            return count

        if isinstance(value, (list, tuple)):
            count = 1
            for item in value:
                count += self.check_decoded(item, depth=depth + 1)
            if count > self.max_nodes:
                raise XmlSecurityError(f"XML node count exceeds max_nodes={self.max_nodes}")
            return count

        return 1


def build_xml_converter() -> UnorderedConverter:
    """Build the canonical converter (design-document §24).

    Returns:
        An ``UnorderedConverter`` configured with ``attribute_prefix="@"``,
        ``text_key="#text"`` and ``preserve_root=True``.
    """
    return UnorderedConverter(attribute_prefix="@", text_key="#text", preserve_root=True)


class XsdTypeSystem:
    """Compile-time XSD wrapper providing validate/decode/encode (§25)."""

    def __init__(
        self,
        schema_source: Any,
        *,
        schema11: bool = False,
        converter: Optional[UnorderedConverter] = None,
        security: Optional[XmlSecurityLimits] = None,
    ) -> None:
        """Initialise and load an XSD schema.

        Args:
            schema_source: Path, file object, ``bytes`` or ``URL`` accepted
                by ``xmlschema.XMLSchema``.
            schema11: Load the schema as XSD 1.1 (``XMLSchema11``).
            converter: Optional converter; defaults to the canonical one.
            security: Optional security limits; defaults to the registry
                defaults.
        """
        self._converter = converter or build_xml_converter()
        self._security = security or XmlSecurityLimits()
        self._schema11 = schema11
        self._schema: Optional[Any] = None
        self.load(schema_source)

    @property
    def schema(self) -> Any:
        """The underlying ``xmlschema`` schema object."""
        return self._schema

    def load(self, schema_source: Any) -> None:
        """(Re)load the XSD schema.

        Args:
            schema_source: Path, file object, ``bytes`` or ``URL``.
        """
        schema_cls = xmlschema.XMLSchema11 if self._schema11 else xmlschema.XMLSchema
        self._schema = schema_cls(schema_source)

    def validate(self, xml_data: Any) -> None:
        """Validate an XML payload against the schema.

        Args:
            xml_data: XML text/bytes or an ElementTree element.

        Raises:
            XmlSecurityError: On payload-security violations.
            xmlschema.XMLSchemaValidationError: When the document does not
                conform to the schema.
        """
        payload = self._coerce_bytes(xml_data)
        self._security.check_bytes(payload)
        self._schema.validate(payload)

    def is_valid(self, xml_data: Any) -> bool:
        """Best-effort validity check (never raises for schema errors)."""
        try:
            self.validate(xml_data)
            return True
        except (XmlSecurityError, xmlschema.XMLSchemaValidationError):
            return False

    def decode(self, xml_data: Any) -> Any:
        """Decode XML to a Python value via the canonical mapping.

        Args:
            xml_data: XML text/bytes.

        Returns:
            The decoded dict/str with ``@``-prefixed attributes and
            ``#text`` text keys.

        Raises:
            XmlSecurityError: On payload-security violations.
            xmlschema.XMLSchemaValidationError: When the document does not
                conform to the schema.
        """
        payload = self._coerce_bytes(xml_data)
        self._security.check_bytes(payload)
        value = self._schema.to_dict(payload, converter=self._converter)
        self._security.check_decoded(value)
        return value

    def encode(self, data: Any, *, element: Optional[str] = None, xml_declaration: bool = True) -> bytes:
        """Encode a Python value to XML per the canonical mapping.

        Args:
            data: The value to serialise (dict/list/scalar matching the
                schema's root element).
            element: The root element name (XPath) to encode into.  When
                omitted, it is derived from ``data``: the single top-level
                key when ``data`` is a preserved-root dict, else the
                schema's first global element.
            xml_declaration: Include the ``<?xml ...?>`` declaration.

        Returns:
            The encoded XML bytes.
        """
        path = element or self._derive_root_path(data)
        result = self._schema.encode(data, path=path, converter=self._converter)
        if isinstance(result, bytes):
            return result
        return ET.tostring(result, encoding="utf-8", xml_declaration=xml_declaration)

    def _derive_root_path(self, data: Any) -> str:
        """Derive the root element path from the value being encoded."""
        if isinstance(data, dict) and len(data) == 1:
            return next(iter(data))
        root_elements = list(self._schema.elements)
        if root_elements:
            return root_elements[0]
        raise ValueError("cannot determine the root XML element for encoding")

    def find_element(self, name: str) -> Optional[Any]:
        """Return the schema element with ``name`` (``None`` if absent).

        Args:
            name: The element's expanded name (e.g. ``{urn:report}QueryRequest``)
                or a plain local name.

        Returns:
            The ``XsdElement`` when found, else ``None``.
        """
        return self._schema.elements.get(name)

    @staticmethod
    def _coerce_bytes(xml_data: Any) -> bytes:
        """Normalise XML input to bytes for the security guard."""
        if isinstance(xml_data, bytes):
            return xml_data
        if isinstance(xml_data, str):
            return xml_data.encode("utf-8")
        return xml_data
