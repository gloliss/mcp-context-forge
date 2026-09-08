# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/codecs/base.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Message codec SPI and shared value objects (PR2).

Defines the codec contract referenced by design-document §6.3, plus the
two value objects it needs: ``EncodedBody`` (the transport-ready payload
an HTTPX call consumes) and ``CodecContext`` (read-only per-invocation
codec configuration).  ``CodecContext`` is an addition beyond the design
document, which references it in §6.3 but never defines it; it carries the
invocation-scoped inputs a codec needs without importing the service
layer.
"""

# Standard
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, Protocol


@dataclass(frozen=True)
class EncodedBody:
    """Transport-ready payload an HTTP request sends.

    HTTPX exposes four mutually exclusive body kwargs (``json=``, ``content=``,
    ``data=``, ``files=``).  Codecs therefore do not collapse to raw ``bytes``;
    they select the mode HTTPX should use and hand over the value for it.

    Attributes:
        mode: Which HTTPX body keyword the value feeds.
        value: The body value for that keyword.
        content_type: Declared ``Content-Type`` for the encoded body
            (``None`` when the transport should infer it, e.g. multipart
            boundaries are managed by HTTPX).
    """

    mode: Literal["json", "content", "data", "files"]
    value: Any
    content_type: Optional[str] = None


@dataclass(frozen=True)
class CodecContext:
    """Read-only per-invocation inputs handed to a codec.

    Design-document §6.3 references this type without defining it.  It is
    intentionally minimal and transport-agnostic: codecs read configuration,
    never the HTTP client or the ORM.

    Attributes:
        protocol_config: The tool's ``protocol_config`` (may be ``None`` for
            legacy tools on the pre-codec path).
        preferred_content_type: The configured preferred request content
            type, if any (e.g. ``request.preferredContentType``).
        preferred_media_types: The configured preferred response media
            types, if any (e.g. ``response.preferredMediaTypes``).
        max_response_bytes: Optional cap on decoded response bytes (binary
            tools), read from settings by the caller.
    """

    protocol_config: Optional[Dict[str, Any]] = None
    preferred_content_type: Optional[str] = None
    preferred_media_types: Optional[tuple[str, ...]] = None
    max_response_bytes: Optional[int] = None


class MessageCodec(Protocol):
    """SPI: encode outbound values and decode inbound payloads (design §6.3).

    Implementations declare the media types they claim (``media_types``)
    and translate between Python values and transport-ready ``EncodedBody``
    on the outbound path, and between raw ``bytes`` and Python values on
    the inbound path.  A codec may also set ``main_type_wildcard = True``
    to claim its entire main type in the registry (e.g. ``text/*``); the
    default (absent attribute) restricts matching to the declared media
    types and the ``+json`` structured suffix.
    """

    media_types: frozenset[str]

    def encode(self, value: Any, context: CodecContext) -> EncodedBody:
        """Encode a Python value into an ``EncodedBody``.

        Args:
            value: The outbound value.
            context: Per-invocation codec context.

        Returns:
            The transport-ready encoded body.

        Raises:
            NotImplementedError: The SPI has no default implementation.
        """
        raise NotImplementedError

    def decode(self, payload: bytes, context: CodecContext) -> Any:
        """Decode a raw payload into a Python value.

        Args:
            payload: The raw inbound bytes.
            context: Per-invocation codec context.

        Returns:
            The decoded value.

        Raises:
            NotImplementedError: The SPI has no default implementation.
        """
        raise NotImplementedError
