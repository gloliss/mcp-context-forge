# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/codecs/form.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Form codec (PR2): application/x-www-form-urlencoded.

Feeds HTTPX's ``data=`` keyword; HTTPX then URL-encodes the mapping and
sets ``Content-Type: application/x-www-form-urlencoded``.  Values are
coerced to strings the way the legacy form path did (reusing the same
coercion so behaviour matches).
"""

# Standard
from typing import Any

# Third-Party
import orjson

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext, EncodedBody, MessageCodec


def _form_value_to_str(value: Any) -> str:
    """Coerce a form value to string (mirrors the legacy adapter)."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, bool)):
        return orjson.dumps(value).decode()
    return str(value)


class FormCodec(MessageCodec):
    """Encode a mapping as form-urlencoded ``data=`` and decode its body."""

    media_types = frozenset({"application/x-www-form-urlencoded"})

    def encode(self, value: Any, context: CodecContext) -> EncodedBody:
        """Encode a mapping as form-urlencoded data.

        Args:
            value: The outbound mapping; every value is coerced to a string.
            context: Per-invocation codec context (unused).

        Returns:
            An ``EncodedBody`` in ``data`` mode.
        """
        data = {k: _form_value_to_str(v) for k, v in dict(value).items()}
        return EncodedBody(mode="data", value=data, content_type="application/x-www-form-urlencoded")

    def decode(self, payload: bytes, context: CodecContext) -> Any:
        """Decode a form-urlencoded body into a dict.

        Args:
            payload: The raw form body bytes.
            context: Per-invocation codec context (unused).

        Returns:
            A dict of ``{key: first_value}`` pairs (legacy parse_qs shape).
        """
        from urllib.parse import parse_qs  # pylint: disable=import-outside-toplevel

        parsed = parse_qs(payload.decode("utf-8"))
        return {k: v[0] for k, v in parsed.items()}
