# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/codecs/multipart.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Multipart codec (PR2): multipart/form-data.

Feeds HTTPX's ``files=`` keyword and sets no ``Content-Type`` so HTTPX can
generate the multipart boundary itself (design-document §9.2: the boundary
must be managed by HTTPX).
"""

# Standard
from typing import Any

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext, EncodedBody, MessageCodec


class MultipartCodec(MessageCodec):
    """Encode a mapping as multipart/form-data ``files=`` entries."""

    media_types = frozenset({"multipart/form-data"})

    def encode(self, value: Any, context: CodecContext) -> EncodedBody:
        """Encode a mapping as multipart form fields.

        Args:
            value: The outbound mapping; every value becomes a
                ``(None, str)`` file part.
            context: Per-invocation codec context (unused).

        Returns:
            An ``EncodedBody`` in ``files`` mode with no content type
            (HTTPX sets the boundary).
        """
        files = {k: (None, str(v)) for k, v in dict(value).items()}
        return EncodedBody(mode="files", value=files, content_type=None)

    def decode(self, payload: bytes, context: CodecContext) -> Any:
        """Return the raw multipart bytes (no structured decode in PR2).

        Args:
            payload: The raw multipart body bytes.
            context: Per-invocation codec context (unused).

        Returns:
            The raw bytes (multipart parsing is out of PR2 scope).
        """
        return payload
