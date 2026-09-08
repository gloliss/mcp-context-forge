# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/codecs/__init__.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Codec layer public API (PR2).

Exports the SPI/value objects, the registry, the default codecs, and the
default pre-registered registry used by the HTTP runtime.
"""

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext, EncodedBody, MessageCodec
from mcpgateway.protocols.codecs.binary import BinaryCodec
from mcpgateway.protocols.codecs.form import FormCodec
from mcpgateway.protocols.codecs.json import JsonCodec
from mcpgateway.protocols.codecs.multipart import MultipartCodec
from mcpgateway.protocols.codecs.registry import CodecRegistry
from mcpgateway.protocols.codecs.text import TextCodec


def build_default_codec_registry() -> CodecRegistry:
    """Build a registry with the PR2 default codecs registered.

    BinaryCodec is registered last so it is the natural fallback for any
    unknown content type.

    Returns:
        A populated ``CodecRegistry``.
    """
    registry = CodecRegistry()
    registry.register(JsonCodec())
    registry.register(TextCodec())
    registry.register(FormCodec())
    registry.register(MultipartCodec())
    registry.register(BinaryCodec())
    return registry


# Module-level singleton used by the HTTP runtime (PR2).
codec_registry = build_default_codec_registry()

__all__ = [
    "BinaryCodec",
    "CodecContext",
    "CodecRegistry",
    "EncodedBody",
    "FormCodec",
    "JsonCodec",
    "MessageCodec",
    "MultipartCodec",
    "TextCodec",
    "build_default_codec_registry",
    "codec_registry",
]
