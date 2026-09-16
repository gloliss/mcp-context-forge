# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/http/xsd_binding.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

XSD bindings for manually declared XML operations (PR4, design §27).

An XML operation is only schema-validated when a codec receives an
:class:`XsdTypeSystem` through ``CodecContext.xsd_type_system``.  PR4 built
the type system and taught :class:`XmlCodec` to honour it, but nothing in the
runtime ever populated it — so every XML tool ran the schema-less "loose"
path and an XSD was decorative.

This module is that missing link.  A tool declares its schema in
``protocol_config``::

    "request": {
        "body": {
            "codec": "xml",
            "mediaType": "application/xml",
            "xsd": {"schema": "<xsd:schema …>", "schema11": false},
        }
    }

The schema travels inline rather than as a URL so no new fetch path (and no
new SSRF surface) is introduced: the manifest author already had to supply
the document, and it is stored with the tool's own configuration.
"""

# Standard
from functools import lru_cache
from typing import Any, Dict, Optional

# First-Party
from mcpgateway.protocols.contracts.xsd_types import XMLSCHEMA_AVAILABLE, XsdTypeSystem

# Cache size: schemas are per-tool and bounded by the number of XML tools a
# gateway exposes; the LRU keeps a hot schema from re-parsing on every call.
_XSD_CACHE_SIZE = 64


@lru_cache(maxsize=_XSD_CACHE_SIZE)
def _load_schema(schema_text: str, schema11: bool) -> XsdTypeSystem:
    """Load and cache an ``XsdTypeSystem`` for one schema document.

    Args:
        schema_text: The XSD document text.
        schema11: Whether to load it as XSD 1.1.

    Returns:
        The loaded type system.

    Raises:
        Exception: Any ``xmlschema`` load failure propagates to the caller,
            which reports it as an invalid configuration rather than
            silently degrading to the schema-less path.
    """
    return XsdTypeSystem(schema_text.encode("utf-8"), schema11=schema11)


def xsd_binding(protocol_config: Optional[Dict[str, Any]], *, side: str = "request") -> Optional[Dict[str, Any]]:
    """Return the XSD binding a ``protocol_config`` declares for one side.

    The two directions carry independent schemas: a request document and the
    response document are different elements and are validated separately, so
    a tool may bind either, both, or neither.

    Args:
        protocol_config: The tool's ``protocol_config`` (may be ``None``).
        side: ``"request"`` (schema under ``request.body.xsd``) or
            ``"response"`` (schema under ``response.xsd``).

    Returns:
        The binding mapping (``{"schema": str, "schema11": bool, ...}``), or
        ``None`` when that side declares no usable XML schema.
    """
    if not protocol_config:
        return None
    if side == "response":
        section = protocol_config.get("response") or {}
    else:
        section = (protocol_config.get("request") or {}).get("body") or {}
    binding = section.get("xsd")
    if not isinstance(binding, dict):
        return None
    schema = binding.get("schema")
    if not isinstance(schema, str) or not schema.strip():
        return None
    return binding


def build_xsd_type_system(protocol_config: Optional[Dict[str, Any]], *, side: str = "request") -> Optional[XsdTypeSystem]:
    """Build (or reuse) the type system a ``protocol_config`` declares (§27).

    Args:
        protocol_config: The tool's ``protocol_config`` (may be ``None``).
        side: ``"request"`` or ``"response"`` — see :func:`xsd_binding`.

    Returns:
        The :class:`XsdTypeSystem` to hand to the XML codec, or ``None`` when
        that side declares no schema — in which case the codec keeps its
        schema-less behaviour, so non-XML and schemaless XML tools are
        unaffected.
    """
    if not XMLSCHEMA_AVAILABLE:
        return None
    binding = xsd_binding(protocol_config, side=side)
    if binding is None:
        return None
    return _load_schema(binding["schema"], bool(binding.get("schema11")))


__all__ = ["build_xsd_type_system", "xsd_binding"]
