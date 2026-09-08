# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/http/response_decoder.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

HTTP response decoder (PR2).

Replaces the legacy ``response.json()`` default with a codec resolution
pipeline (design-document §9.7 and §70): the explicit ``protocol_config``
codec wins, then the response ``Content-Type``, then the configured
preferred media types, then a bounded safe sniff, then the ``BinaryCodec``
fallback.
"""

# Standard
from dataclasses import dataclass
from typing import Any, Optional, Tuple

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext
from mcpgateway.protocols.codecs.registry import CodecRegistry

# Status codes whose success carries no body (design-document §9.8).
_BODYLESS_STATUSES = frozenset({204, 205})

# A bounded first window used only to sniff text vs binary when nothing
# else matched; never a full-body JSON probe (design-document §70.4).
_SNIFF_WINDOW = 512

# protocol_config response.codec names mapped to their media types (§70.1).
_CODEC_NAME_MEDIA_TYPES = {
    "json": "application/json",
    "text": "text/plain",
    "binary": "application/octet-stream",
    "form": "application/x-www-form-urlencoded",
    "multipart": "multipart/form-data",
}


@dataclass(frozen=True)
class DecodedResponse:
    """The decoded upstream response.

    Attributes:
        data: The decoded body, or ``None`` for bodyless statuses.
        codec_media_type: The media type the winning codec claims, for
            diagnostics.
    """

    data: Any
    codec_media_type: Optional[str] = None


class ResponseDecoder:
    """Decode an HTTP response via the codec resolution pipeline."""

    def __init__(self, codecs: CodecRegistry) -> None:
        """Initialise with the codec registry.

        Args:
            codecs: The codec registry used for resolution.
        """
        self._codecs = codecs

    def decode(
        self,
        status_code: int,
        content_type: Optional[str],
        payload: bytes,
        context: CodecContext,
    ) -> DecodedResponse:
        """Decode a response body per the §70 resolution order.

        Args:
            status_code: The response status code.
            content_type: The response ``Content-Type`` header.
            payload: The raw response body bytes.
            context: The per-invocation codec context (carries
                ``protocol_config`` and preferred media types).

        Returns:
            A ``DecodedResponse`` with ``data=None`` for bodyless statuses.
        """
        if status_code in _BODYLESS_STATUSES:
            return DecodedResponse(data=None, codec_media_type=None)

        codec, media_type = self._resolve(content_type, payload, context)
        if not payload:
            return DecodedResponse(data=None, codec_media_type=media_type)
        return DecodedResponse(data=codec.decode(payload, context), codec_media_type=media_type)

    def _resolve(
        self,
        content_type: Optional[str],
        payload: bytes,
        context: CodecContext,
    ) -> Tuple[Any, Optional[str]]:
        """Resolve the codec per design-document §70 order.

        Args:
            content_type: The response ``Content-Type``.
            payload: The raw response body (used only by the sniff step).
            context: The codec context.

        Returns:
            A ``(codec, media_type)`` tuple.
        """
        # 1. protocol_config explicit codec (design §70.1).
        explicit = self._explicit_codec(context)
        if explicit is not None:
            return explicit

        # 2. response Content-Type (design §70.2).  A real match — including
        #    an explicit ``application/octet-stream`` — is honoured; an
        #    unmatched content type falls through to the next steps.
        if content_type:
            codec = self._codecs.match(content_type)
            if codec is not None:
                return codec, content_type.split(";", 1)[0].strip().lower()

        # 3. configured preferred content types (design §70.3).
        for preferred in context.preferred_media_types or ():
            codec = self._codecs.match(preferred)
            if codec is not None:
                return codec, preferred.split(";", 1)[0].strip().lower()

        # 4. bounded safe sniff (design §70.4): text vs binary on the first
        #    window only — never a full-body JSON probe.
        media_type = "application/octet-stream" if self._looks_binary(payload) else "text/plain"
        return self._codecs.resolve(media_type), media_type

    def _explicit_codec(self, context: CodecContext) -> Optional[Tuple[Any, Optional[str]]]:
        """Resolve the explicit codec named by ``protocol_config``.

        Args:
            context: The codec context.

        Returns:
            A ``(codec, media_type)`` tuple when configured, else ``None``.
        """
        config = context.protocol_config or {}
        response_config = config.get("response") or {}
        codec_name = response_config.get("codec")
        if not codec_name or codec_name == "auto":
            return None
        name_media_type = _CODEC_NAME_MEDIA_TYPES.get(str(codec_name).lower())
        if name_media_type is not None:
            return self._codecs.resolve(name_media_type), name_media_type
        preferred = response_config.get("preferredMediaTypes") or []
        media_type = preferred[0] if preferred else "application/json"
        return self._codecs.resolve(media_type), media_type

    @staticmethod
    def _looks_binary(payload: bytes) -> bool:
        """Detect binary payloads by a null byte in the sniff window.

        Args:
            payload: The raw response body.

        Returns:
            ``True`` when a null byte appears in the bounded window, which
            is a reliable signal of non-textual (binary) content.
        """
        return b"\x00" in payload[:_SNIFF_WINDOW]
