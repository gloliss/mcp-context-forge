# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/codecs/binary.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Binary codec (PR2): application/octet-stream and the decode fallback.

Binary responses are standardized per design-document §72: base64 text
with ``contentType``, ``size``, and ``data``, capped by
``max_response_bytes``.  Encoding passes raw bytes through as ``content=``.
"""

# Standard
import base64
from typing import Any

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext, EncodedBody, MessageCodec


class BinaryCodec(MessageCodec):
    """Encode raw bytes and decode into the §72 standardized envelope."""

    media_types = frozenset({"application/octet-stream"})

    def encode(self, value: Any, context: CodecContext) -> EncodedBody:
        """Encode a value as raw bytes.

        Args:
            value: The outbound value; ``str``/``bytes``/``bytearray`` are
                passed through, anything else is encoded as UTF-8.
            context: Per-invocation codec context (unused).

        Returns:
            An ``EncodedBody`` in ``content`` mode.
        """
        if isinstance(value, bytes):
            payload = value
        elif isinstance(value, bytearray):
            payload = bytes(value)
        elif isinstance(value, str):
            payload = value.encode("utf-8")
        else:
            payload = str(value).encode("utf-8")
        return EncodedBody(mode="content", value=payload, content_type="application/octet-stream")

    def decode(self, payload: bytes, context: CodecContext) -> dict[str, Any]:
        """Decode raw bytes into the §72 binary-output envelope.

        Args:
            payload: The raw response bytes.
            context: Per-invocation codec context; ``max_response_bytes``
                caps the payload before base64 encoding.

        Returns:
            A dict with ``encoding``, ``contentType``, ``size``, and
            ``data`` (base64) keys.
        """
        capped = self._cap(payload, context)
        return {
            "encoding": "base64",
            "contentType": "application/octet-stream",
            "size": len(capped),
            "data": base64.b64encode(capped).decode("ascii"),
        }

    def _cap(self, payload: bytes, context: CodecContext) -> bytes:
        """Apply the ``max_response_bytes`` cap to a payload.

        Args:
            payload: The raw response bytes.
            context: Per-invocation codec context.

        Returns:
            The payload, truncated when ``max_response_bytes`` is set and
            exceeded.
        """
        limit = context.max_response_bytes if context is not None else None
        if limit is not None and len(payload) > limit:
            return payload[:limit]
        return payload
