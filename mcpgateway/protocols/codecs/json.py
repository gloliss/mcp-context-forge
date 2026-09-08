# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/codecs/json.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

JSON codec (PR2): application/json and application/*+json.

Uses ``orjson`` for encoding/decoding (matching the project's existing
codec choice) and feeds the ``json=`` body keyword, which lets HTTPX set
the correct ``Content-Type`` on the wire.
"""

# Standard
from typing import Any

# Third-Party
import orjson

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext, EncodedBody, MessageCodec


class JsonCodec(MessageCodec):
    """Encode Python values as JSON and decode JSON bytes (orjson)."""

    media_types = frozenset({"application/json"})

    def encode(self, value: Any, context: CodecContext) -> EncodedBody:
        """Encode a value via the ``json=`` HTTPX keyword.

        Args:
            value: The outbound value (must be JSON-serializable).
            context: Per-invocation codec context (unused).

        Returns:
            An ``EncodedBody`` in ``json`` mode.
        """
        # Validate now so an unserializable value fails fast with a clear
        # error instead of surfacing as a transport-level failure later.
        orjson.dumps(value)
        return EncodedBody(mode="json", value=value, content_type="application/json")

    def decode(self, payload: bytes, context: CodecContext) -> Any:
        """Decode JSON bytes into a Python value.

        Args:
            payload: The raw JSON bytes.
            context: Per-invocation codec context (unused).

        Returns:
            The decoded value.

        Raises:
            orjson.JSONDecodeError: When the payload is not valid JSON.
        """
        return orjson.loads(payload)
