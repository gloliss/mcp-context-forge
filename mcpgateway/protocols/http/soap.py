# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/http/soap.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

SOAP HTTP runtime glue (PR5, design-document §32–§34).

The business chain is ``Tool → HttpProtocolAdapter → SoapCodec → HTTPX``
(design §32): zeep is only ever used to *parse* WSDLs in the contract
provider, never to place calls.  This module supplies the two glue pieces
the HTTP runtime needs:

* :func:`soap_request_headers` — the ``SOAPAction`` header required by
  SOAP 1.1 (SOAP 1.2 uses the ``action`` parameter of the Content-Type
  instead);
* :func:`map_soap_fault` — maps a :class:`SoapFaultError` onto the
  ContextForge canonical error model (design §34): client faults →
  ``INVALID_ARGUMENT``, server faults → ``UPSTREAM_ERROR``, preserving the
  fault code/string/safe detail.
"""

# Standard
from typing import Any, Dict, Optional

# First-Party
from mcpgateway.protocols.codecs.soap import SoapFaultError, resolve_soap_config
from mcpgateway.protocols.models import ErrorCategory, ProtocolError


def soap_request_headers(protocol_config: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Build SOAP-specific request headers from a protocol_config.

    Args:
        protocol_config: The tool's ``protocol_config``.  The SOAP binding
            lives under ``request.soap`` with ``version`` and
            ``soapAction``.

    Returns:
        Extra headers to merge into the HTTP request.  SOAP 1.1 emits
        ``SOAPAction``; SOAP 1.2 emits none here (the action travels in
        the ``application/soap+xml`` Content-Type parameter).
    """
    if not protocol_config:
        return {}
    soap = resolve_soap_config(protocol_config)
    version = str(soap.get("version") or "1.1")
    action = soap.get("soapAction")
    if version == "1.2":
        return {}
    if action:
        return {"SOAPAction": str(action)}
    return {}


def is_soap_config(protocol_config: Optional[Dict[str, Any]]) -> bool:
    """Return whether a ``protocol_config`` describes a SOAP call (§32).

    SOAP is not an ``integration_type`` (design §2): a tool is SOAP when
    its ``protocol_config`` names the ``soap`` codec on either side of the
    exchange.  The WSDL contract provider emits both, but either one alone
    is enough to take the SOAP runtime path.

    Args:
        protocol_config: The tool's ``protocol_config`` (may be ``None``).

    Returns:
        ``True`` when the request body codec or the response codec is
        ``soap``.
    """
    if not protocol_config:
        return False
    request = protocol_config.get("request") or {}
    response = protocol_config.get("response") or {}
    body_codec = (request.get("body") or {}).get("codec")
    return str(body_codec or "").lower() == "soap" or str(response.get("codec") or "").lower() == "soap"


def soap_content_type(protocol_config: Optional[Dict[str, Any]], codec_content_type: Optional[str] = None) -> Optional[str]:
    """Return the SOAP ``Content-Type`` for a request, or ``None`` (§33).

    SOAP 1.1 uses ``text/xml`` and carries the action in the ``SOAPAction``
    header; SOAP 1.2 uses ``application/soap+xml`` and carries the action
    as a ``Content-Type`` parameter instead.

    Args:
        protocol_config: The tool's ``protocol_config``; the binding lives
            under ``request.soap``.
        codec_content_type: The ``Content-Type`` the body codec declared.
            Used as the base when present, so the codec stays the authority
            on the media type.

    Returns:
        The ``Content-Type`` header value, or ``None`` when the config is
        not SOAP (the caller then leaves the codec's own value untouched).
    """
    if not is_soap_config(protocol_config):
        return None
    soap = resolve_soap_config(protocol_config)
    version = str(soap.get("version") or "1.1")
    base = codec_content_type or ("application/soap+xml" if version == "1.2" else "text/xml")
    base = base.split(";", 1)[0].strip()
    action = soap.get("soapAction")
    if version == "1.2" and action:
        return f'{base}; charset=utf-8; action="{action}"'
    return f"{base}; charset=utf-8"


def map_soap_fault(exc: SoapFaultError) -> ProtocolError:
    """Map a SOAP Fault onto the canonical ProtocolError (design §34).

    Args:
        exc: The raised :class:`SoapFaultError`.

    Returns:
        A ``ProtocolError`` with a mapped category, the fault code as the
        machine code, and the safe detail preserved.  Raw internal stack
        traces are never surfaced to the tool caller.
    """
    code = (exc.code or "").lower()
    if "client" in code or "sender" in code:
        category = ErrorCategory.INVALID_ARGUMENT
    else:
        category = ErrorCategory.UPSTREAM_ERROR
    detail = exc.detail
    message = exc.fault_string or "SOAP Fault"
    return ProtocolError(
        category=category,
        code=exc.code or "SOAP:Fault",
        message=message,
        origin="http:soap",
        retryable=False,
        details=detail,
    )


__all__ = ["is_soap_config", "map_soap_fault", "soap_content_type", "soap_request_headers"]
