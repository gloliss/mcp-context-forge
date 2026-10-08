# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/contracts/zeep_json_schema.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

zeep type tree → JSON Schema mapper (design-document §31).

``WsdlContractProvider`` parses a WSDL with zeep, so by the time an operation
is compiled its request and response element structure is already resolved
into zeep's own type objects.  ``ZeepJsonSchemaMapper`` walks that tree and
produces the JSON Schema a tool listing shows, so a SOAP operation carries
real parameter names, types, cardinality and required-ness instead of an
opaque signature string.

The sibling
:class:`~mcpgateway.protocols.contracts.xsd_json_schema.XsdJsonSchemaMapper`
does the same job for the standalone-XSD path, where the schema is compiled by
``xmlschema``.  Both share one primitive table
(``XSD_BUILTIN_JSON_SCHEMA``) and the same two conventions (design §24/§26):

* attributes are emitted as ``properties["@<name>"]``;
* an element with ``maxOccurs > 1`` is wrapped in a JSON Schema ``array``.

Walking zeep's tree — rather than compiling the WSDL's embedded
``<xsd:schema>`` a second time with ``xmlschema`` — keeps this path
offline-only: zeep's transport already refuses remote fetches, while a fresh
``xmlschema`` compile would follow ``xsd:import schemaLocation`` over the
network unless separately guarded.  The trade-off is that zeep 4.x drops
simple-type restriction facets (``enumeration``, ``pattern``, length bounds)
while resolving a type, so they cannot be emitted from here; the
standalone-XSD path does carry them.

The mapper is total: rather than raising on a shape it does not model it
degrades to ``{}`` (any JSON value), because a tool listing with a thin schema
beats a WSDL that cannot be imported at all.

Known simplifications:

* zeep flattens a ``choice`` into its members, so its alternatives read as
  independent optional properties rather than a ``oneOf``;
* a self-referential type stops at ``{"type": "object"}``, since a recursive
  type has no finite expansion;
* ``simpleContent`` text surfaces under zeep's own ``_value_1`` name instead
  of the canonical ``#text`` key, because zeep models it as a synthesized
  element.
"""

# Standard
from typing import Any, Dict, Optional

# Third-Party
# ``zeep`` is an optional dependency (pyproject ``soap`` extra, design §35).
try:
    from zeep.xsd import ComplexType, ListType, UnionType

    ZEEP_AVAILABLE = True
except ImportError:  # pragma: no cover - optional "soap" extra
    ComplexType = ListType = UnionType = None  # type: ignore[assignment]
    ZEEP_AVAILABLE = False

# First-Party
from mcpgateway.protocols.contracts.xsd_json_schema import XSD_BUILTIN_JSON_SCHEMA

# XSD builtin local names are lower-camel (``dateTime``, ``NMTOKEN``) while
# zeep names the classes matching them in CamelCase (``DateTime``,
# ``NmToken``).  Folding case lets one table answer both the qname lookup and
# the class-name fallback below.
_BUILTIN_BY_FOLDED_NAME: Dict[str, dict] = {name.lower(): schema for name, schema in XSD_BUILTIN_JSON_SCHEMA.items()}


def _local_name(name: Any) -> str:
    """Return the local part of a Clark-notation or prefixed name."""
    text = str(name)
    if text.startswith("{") and "}" in text:
        return text.split("}", 1)[1]
    if ":" in text:
        return text.rsplit(":", 1)[1]
    return text


def _type_identity(xsd_type: Any) -> str:
    """Return a stable key identifying a type, for cycle detection."""
    qname = getattr(xsd_type, "qname", None)
    if qname is not None:
        return str(qname)
    return f"id:{id(xsd_type)}"


def _builtin_schema(xsd_type: Any) -> Optional[dict]:
    """Return the JSON Schema for an XSD builtin, or ``None`` when unrecognised.

    Args:
        xsd_type: A zeep type object.

    Returns:
        The shared table entry for the type's XSD builtin base, or ``None``.
    """
    default_qname = getattr(xsd_type, "_default_qname", None)
    if default_qname is not None:
        schema = _BUILTIN_BY_FOLDED_NAME.get(_local_name(default_qname).lower())
        if schema is not None:
            return schema

    # A complexType deriving from a simple type (``simpleContent`` extension)
    # becomes a subclass of that simple type's class in zeep, but keeps
    # ``_default_qname == anyType``; the base is only reachable via the MRO.
    for klass in type(xsd_type).__mro__:
        schema = _BUILTIN_BY_FOLDED_NAME.get(klass.__name__.lower())
        if schema is not None:
            return schema
    return None


class ZeepJsonSchemaMapper:
    """Map a resolved zeep type to a JSON Schema fragment (§31)."""

    def map_type(self, xsd_type: Any) -> dict:
        """Map a zeep type (complex or simple) to JSON Schema.

        Args:
            xsd_type: A resolved zeep type object — typically
                ``operation.input.body.type`` or ``operation.output.body.type``.

        Returns:
            A JSON Schema dict.  A complex type becomes an object schema with
            ``properties`` and, when any member is required, ``required``; a
            simple type becomes the JSON Schema primitive of its XSD builtin
            base.  Anything unclassifiable — including ``xsd:anyType`` — maps
            to ``{}``, which accepts any JSON value.
        """
        return self._map_type(xsd_type, set())

    def _map_type(self, xsd_type: Any, active: set) -> dict:
        """Map one type, carrying the types already on the current path."""
        if xsd_type is None:
            return {}
        if ZEEP_AVAILABLE:
            if isinstance(xsd_type, ComplexType):
                return self._map_complex_type(xsd_type, active)
            # ``xsd:list`` is a whitespace-separated list of its item type.
            if isinstance(xsd_type, ListType):
                return {"type": "array", "items": self._map_type(xsd_type.item_type, active)}
            # ``xsd:union`` accepts any of its member types.
            if isinstance(xsd_type, UnionType):
                members = [self._map_type(member, active) for member in xsd_type.item_types]
                return {"anyOf": members} if members else {}
        schema = _builtin_schema(xsd_type)
        return dict(schema) if schema is not None else {}

    def _map_complex_type(self, xsd_type: Any, active: set) -> dict:
        """Map a complexType to an object schema, stopping at cycles."""
        identity = _type_identity(xsd_type)
        if identity in active:
            # A self-referential type (a tree, a linked list) has no finite
            # JSON Schema expansion; stop and accept any object.
            return {"type": "object"}
        active.add(identity)
        try:
            properties: Dict[str, Any] = {}
            required: list = []

            # Attributes → "@name" properties, the canonical XmlCodec mapping
            # (§24).  Attributes are always simple, but share the type mapper
            # so an unmodelled one still degrades rather than raising.
            for attribute_name, attribute in xsd_type.attributes:
                name = f"@{attribute_name}"
                properties[name] = self._map_type(attribute.type, active)
                if attribute.required:
                    required.append(name)

            for element_name, element in xsd_type.elements:
                properties[element_name] = self._map_element(element, active)
                if element.min_occurs >= 1:
                    required.append(element_name)
        finally:
            active.discard(identity)

        schema: dict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def _map_element(self, element: Any, active: set) -> dict:
        """Map one element, applying its occurrence cardinality."""
        schema = self._map_type(element.type, active)
        max_occurs = element.max_occurs
        if max_occurs == "unbounded" or (isinstance(max_occurs, int) and max_occurs > 1):
            return {"type": "array", "items": schema}
        return schema


__all__ = ["ZeepJsonSchemaMapper"]
