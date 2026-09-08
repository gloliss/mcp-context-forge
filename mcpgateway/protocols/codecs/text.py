# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/codecs/text.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Text codec (PR2): text/* bodies as UTF-8 ``content=``.

Text bodies are passed to HTTPX as ``content=`` (raw bytes) so the exact
``Content-Type`` survives; encoding is always UTF-8.
"""

# Standard
from typing import Any

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext, EncodedBody, MessageCodec


class TextCodec(MessageCodec):
    """Encode/decode plain text as UTF-8 ``content=`` bytes."""

    media_types = frozenset({"text/plain"})
    # Design-document §9.7: the codec claims the whole ``text/*`` family.
    main_type_wildcard = True

    def encode(self, value: Any, context: CodecContext) -> EncodedBody:
        """Encode a value as UTF-8 text.

        Args:
            value: The outbound value; coerced to ``str`` when it is not
                already a string.
            context: Per-invocation codec context (unused).

        Returns:
            An ``EncodedBody`` in ``content`` mode.
        """
        text = value if isinstance(value, str) else str(value)
        return EncodedBody(mode="content", value=text.encode("utf-8"), content_type="text/plain")

    def decode(self, payload: bytes, context: CodecContext) -> str:
        """Decode UTF-8 bytes to text.

        Args:
            payload: The raw text bytes.
            context: Per-invocation codec context (unused).

        Returns:
            The decoded string.
        """
        return payload.decode("utf-8")
