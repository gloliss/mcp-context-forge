# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/codecs/registry.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Codec registry: map a media type to a codec (PR2).

Resolution mirrors design-document §9.7 and §70: an exact media-type
match, then a ``+json`` structured-suffix match, then a main-type match
for codecs that claim their whole main type (``text/*``), then the
``BinaryCodec`` fallback so decode never fails merely because the content
type is unknown.
"""

# Standard
from typing import Optional

# First-Party
from mcpgateway.protocols.codecs.base import MessageCodec


class CodecRegistry:
    """Registry mapping media types to ``MessageCodec`` instances."""

    def __init__(self) -> None:
        """Initialise an empty registry."""
        self._codecs: list[MessageCodec] = []
        self._by_media_type: dict[str, MessageCodec] = {}

    def register(self, codec: MessageCodec) -> None:
        """Register a codec, indexing every media type it claims.

        Args:
            codec: The codec instance to register.  Later registrations
                for a media type replace earlier ones.
        """
        self._codecs.append(codec)
        for media_type in codec.media_types:
            self._by_media_type[media_type] = codec

    def resolve(self, media_type: Optional[str]) -> MessageCodec:
        """Resolve a media type to the most specific codec available.

        Args:
            media_type: A ``Content-Type`` value, possibly carrying a
                parameter suffix (e.g. ``application/json; charset=utf-8``).

        Returns:
            The best-matching codec, or the ``BinaryCodec`` fallback when
            nothing matches.
        """
        return self.match(media_type) or self._fallback()

    def match(self, media_type: Optional[str]) -> Optional[MessageCodec]:
        """Resolve a media type without the binary fallback.

        Unlike :meth:`resolve`, this returns ``None`` when no codec claims
        the media type, letting callers distinguish a real match (including
        an explicit ``application/octet-stream``) from a fall-through.

        Args:
            media_type: A ``Content-Type`` value, possibly carrying a
                parameter suffix.

        Returns:
            The matching codec, or ``None`` when nothing matches.
        """
        normalized = self._normalize(media_type)
        if normalized is None:
            return None

        if normalized in self._by_media_type:
            return self._by_media_type[normalized]

        if "+" in normalized:
            base = normalized.rsplit("+", 1)[-1]
            candidate = self._by_media_type.get(f"application/{base}")
            if candidate is not None:
                return candidate

        main_type = normalized.split("/", 1)[0]
        for codec in self._codecs:
            if not getattr(codec, "main_type_wildcard", False):
                continue
            if any(claimed.split("/", 1)[0] == main_type for claimed in codec.media_types):
                return codec

        return None

    def _normalize(self, media_type: Optional[str]) -> Optional[str]:
        """Strip parameters and lower-case a media type.

        Args:
            media_type: The raw content type.

        Returns:
            The bare lower-cased media type, or ``None`` when absent.
        """
        if not media_type:
            return None
        return media_type.split(";", 1)[0].strip().lower()

    def _fallback(self) -> MessageCodec:
        """Return the registered fallback codec (BinaryCodec).

        Returns:
            The BinaryCodec if registered, otherwise the first registered
            codec.  Callers register BinaryCodec last so it is the natural
            fallback.
        """
        for codec in reversed(self._codecs):
            if "application/octet-stream" in codec.media_types:
                return codec
        return self._codecs[-1]
