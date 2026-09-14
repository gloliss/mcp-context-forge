# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/contracts/xsd_json_schema.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

XSD → JSON Schema mapper (PR4, design-document §26).

``XsdJsonSchemaMapper`` translates an ``xmlschema`` schema into JSON Schema
fragments.  The mapping covers the surface the design document lists:
``xs:string``/``xs:boolean``/``xs:integer``/``xs:decimal``/``xs:date``/
``xs:dateTime``, ``minOccurs``/``maxOccurs``, ``enumeration``, ``pattern``,
``minInclusive``/``maxInclusive``, ``complexType`` with ``sequence``/
``choice``/``attribute``, and namespaces.

Two conventions are baked in:

* attributes are emitted as ``properties["@<name>"]`` (matching the
  canonical XmlCodec mapping, design §24);
* an element with ``maxOccurs > 1`` is wrapped in a JSON Schema ``array``.

XSD does not produce operations (design §26) — this mapper produces *type
contracts* only.
"""

# Standard
from typing import Any, Dict, Optional

# Third-Party
from xmlschema.validators import XsdAtomicBuiltin, XsdComplexType, XsdElement

# xs: builtin local name → JSON Schema primitive.
_BUILTIN_MAP: Dict[str, dict] = {
    "string": {"type": "string"},
    "normalizedString": {"type": "string"},
    "token": {"type": "string"},
    "language": {"type": "string"},
    "Name": {"type": "string"},
    "NCName": {"type": "string"},
    "NMTOKEN": {"type": "string"},
    "ID": {"type": "string"},
    "IDREF": {"type": "string"},
    "anyURI": {"type": "string", "format": "uri"},
    "QName": {"type": "string"},
    "boolean": {"type": "boolean"},
    "base64Binary": {"type": "string", "contentEncoding": "base64"},
    "hexBinary": {"type": "string", "contentEncoding": "hex"},
    "integer": {"type": "integer"},
    "int": {"type": "integer"},
    "long": {"type": "integer"},
    "short": {"type": "integer"},
    "byte": {"type": "integer"},
    "nonNegativeInteger": {"type": "integer", "minimum": 0},
    "positiveInteger": {"type": "integer", "minimum": 1},
    "nonPositiveInteger": {"type": "integer", "maximum": 0},
    "negativeInteger": {"type": "integer", "maximum": -1},
    "unsignedLong": {"type": "integer", "minimum": 0},
    "unsignedInt": {"type": "integer", "minimum": 0},
    "unsignedShort": {"type": "integer", "minimum": 0},
    "unsignedByte": {"type": "integer", "minimum": 0},
    "decimal": {"type": "number"},
    "float": {"type": "number"},
    "double": {"type": "number"},
    "date": {"type": "string", "format": "date"},
    "dateTime": {"type": "string", "format": "date-time"},
    "time": {"type": "string", "format": "time"},
    "duration": {"type": "string"},
    "gYear": {"type": "string", "format": "date"},
    "gYearMonth": {"type": "string"},
    "gMonth": {"type": "string"},
    "gDay": {"type": "string"},
    "gMonthDay": {"type": "string"},
}

# Facet local name → JSON Schema keyword for facets that map directly.
_FACET_KEYWORDS = {
    "minInclusive": "minimum",
    "maxInclusive": "maximum",
    "minExclusive": "exclusiveMinimum",
    "maxExclusive": "exclusiveMaximum",
    "minLength": "minLength",
    "maxLength": "maxLength",
}


class XsdJsonSchemaMapper:
    """Map an XSD schema (or element/type) to JSON Schema fragments."""

    def __init__(self, schema: Any) -> None:
        """Initialise the mapper with a compiled ``xmlschema`` schema.

        Args:
            schema: An ``xmlschema.XMLSchema`` instance.
        """
        self._schema = schema

    def map_element(self, element_name: str) -> dict:
        """Map a named global element to a JSON Schema fragment.

        Args:
            element_name: The element's local name or Clark notation
                (``{urn}LocalName``).

        Returns:
            A JSON Schema dict.  When the element repeats
            (``maxOccurs > 1``) the schema is wrapped in an ``array``.
        """
        element = self._resolve_element(element_name)
        if element is None:
            raise KeyError(f"XSD element {element_name!r} not found in schema")
        schema = self._map_element(element)
        return schema

    def map_type(self, type_name: str) -> dict:
        """Map a named (global or builtin) type to a JSON Schema fragment.

        Args:
            type_name: The type's local name or Clark notation.

        Returns:
            A JSON Schema dict.
        """
        xsd_type = self._schema.types.get(self._local_name(type_name))
        if xsd_type is None:
            for name, candidate in self._schema.types.items():
                if name == type_name or name.endswith(type_name):
                    xsd_type = candidate
                    break
        if xsd_type is None:
            raise KeyError(f"XSD type {type_name!r} not found in schema")
        return self._map_type(xsd_type)

    def _resolve_element(self, element_name: str) -> Optional[XsdElement]:
        """Resolve an element by local name or Clark notation."""
        local = self._local_name(element_name)
        element = self._schema.elements.get(local)
        if element is not None:
            return element
        for name, candidate in self._schema.elements.items():
            if name == element_name:
                return candidate
        return None

    def _map_element(self, element: XsdElement) -> dict:
        """Map one XsdElement, applying occurrence cardinality."""
        schema = self._map_type(element.type)
        max_occurs = element.max_occurs
        if max_occurs is None or max_occurs != 1:
            return {"type": "array", "items": schema}
        return schema

    def _map_type(self, xsd_type: Any) -> dict:
        """Map an xsd type (atomic builtin, simple or complex) to JSON Schema."""
        if isinstance(xsd_type, XsdComplexType):
            return self._map_complex_type(xsd_type)
        return self._map_simple_type(xsd_type)

    def _map_complex_type(self, xsd_type: XsdComplexType) -> dict:
        """Map a complexType to an object JSON Schema."""
        properties: Dict[str, Any] = {}
        required: list[str] = []

        # Attributes → "@name" properties.
        for attr_name, attr in xsd_type.attributes.items():
            prop = self._map_type(attr.type)
            properties[f"@{self._local_name(attr_name)}"] = prop
            if attr.is_required():
                required.append(f"@{self._local_name(attr_name)}")

        # Content model.
        content = xsd_type.content
        if content is not None:
            model = getattr(content, "model", None)
            if model in ("sequence", "all"):
                for particle in content.content:
                    if not isinstance(particle, XsdElement):
                        continue
                    prop = self._map_element(particle)
                    properties[particle.local_name] = prop
                    if particle.min_occurs and particle.min_occurs >= 1:
                        required.append(particle.local_name)
            elif model == "choice":
                alternatives = [self._map_element(particle) for particle in content.content if isinstance(particle, XsdElement)]
                if alternatives:
                    properties["#oneOf"] = {"oneOf": alternatives}
            else:
                # simple content (text): carry the text key.
                base = self._simple_content_schema(xsd_type)
                if base is not None:
                    properties["#text"] = base

        schema: dict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        self._apply_namespace_annotation(schema, xsd_type)
        return schema

    def _simple_content_schema(self, xsd_type: XsdComplexType) -> Optional[dict]:
        """Return the JSON Schema for complex content of simple type, if any."""
        # A complexType deriving from a simple type exposes `content` as a
        # group whose model is None and whose own `simple_type` carries the
        # restriction.  Fall back to mapping that simple type when present.
        simple_type = getattr(xsd_type.content, "simple_type", None)
        if simple_type is not None:
            return self._map_simple_type(simple_type)
        return None

    def _map_simple_type(self, xsd_type: Any) -> dict:
        """Map an atomic builtin or a simpleType restriction to JSON Schema."""
        schema = self._primitive_schema(xsd_type)
        if schema is None:
            return {"type": "string"}

        # Facets from the restriction chain (enumeration/pattern/limits).
        self._apply_facets(schema, xsd_type)
        return schema

    def _primitive_schema(self, xsd_type: Any) -> Optional[dict]:
        """Resolve the primitive JSON Schema for an xsd type."""
        local = self._builtin_local_name(xsd_type)
        if local is None:
            return None
        base = _BUILTIN_MAP.get(local)
        return dict(base) if base is not None else None

    def _apply_facets(self, schema: dict, xsd_type: Any) -> None:
        """Merge restriction facets into a JSON Schema fragment.

        xmlschema exposes facets as a ``{qname: facet}`` dict on each simple
        type in the restriction chain; each facet is a validator carrying
        either a ``value`` (numeric/length bounds) or an enumerable set of
        XSD source elements (enumeration/pattern).
        """
        seen = set()
        current = xsd_type
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            for qname, facet in (getattr(current, "facets", None) or {}).items():
                local = self._local_name(qname)
                if local == "enumeration":
                    values = getattr(facet, "enumeration", None) or [element.get("value") for element in facet if element.get("value")]
                    if values:
                        schema["enum"] = list(values)
                elif local == "pattern":
                    patterns = [element.get("value") for element in facet if element.get("value")]
                    if len(patterns) == 1:
                        schema["pattern"] = patterns[0]
                    elif len(patterns) > 1:
                        schema["pattern"] = "|".join(f"(?:{pattern})" for pattern in patterns)
                else:
                    keyword = _FACET_KEYWORDS.get(local)
                    if keyword:
                        value = getattr(facet, "value", None)
                        if value is not None:
                            schema[keyword] = value
            current = getattr(current, "base_type", None)

    def _builtin_local_name(self, xsd_type: Any) -> Optional[str]:
        """Walk the base-type chain to the builtin's local name."""
        seen = set()
        current = xsd_type
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, XsdAtomicBuiltin) and hasattr(current, "name"):
                return self._local_name(current.name)
            current = getattr(current, "base_type", None)
        return None

    def _apply_namespace_annotation(self, schema: dict, xsd_type: Any) -> None:
        """Attach a lightweight XML namespace annotation when present."""
        namespace = getattr(xsd_type, "target_namespace", None) or getattr(self._schema, "target_namespace", None)
        if namespace:
            schema["xml"] = {"namespace": namespace}

    @staticmethod
    def _local_name(name: Any) -> str:
        """Return the local part of a Clark-notation or prefixed name."""
        text = str(name)
        if text.startswith("{") and "}" in text:
            return text.split("}", 1)[1]
        if ":" in text:
            return text.rsplit(":", 1)[1]
        return text
