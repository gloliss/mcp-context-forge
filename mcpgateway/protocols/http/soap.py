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
from mcpgateway.protocols.codecs.soap import SoapFaultError
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
    soap = (protocol_config.get("request") or {}).get("soap") or {}
    version = str(soap.get("version") or "1.1")
    action = soap.get("soapAction")
    if version == "1.2":
        return {}
    if action:
        return {"SOAPAction": str(action)}
    return {}


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


__all__ = ["map_soap_fault", "soap_request_headers"]
