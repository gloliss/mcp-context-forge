# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/codecs/test_codec_registry.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the CodecRegistry resolution pipeline (PR2).
"""

# First-Party
from mcpgateway.protocols.codecs import build_default_codec_registry
from mcpgateway.protocols.codecs.binary import BinaryCodec
from mcpgateway.protocols.codecs.json import JsonCodec
from mcpgateway.protocols.codecs.multipart import MultipartCodec
from mcpgateway.protocols.codecs.registry import CodecRegistry
from mcpgateway.protocols.codecs.text import TextCodec


class _FakeCodec:
    """Minimal MessageCodec stand-in with configurable media types."""

    def __init__(self, media_types, name="fake"):
        """Initialise with claimed media types and a label."""
        self.media_types = frozenset(media_types)
        self.name = name


def _make_registry() -> CodecRegistry:
    """Build a default registry (Json/Text/Form/Multipart/Binary)."""
    return build_default_codec_registry()


class TestCodecRegistryResolution:
    """Resolution order: exact → +json suffix → main type → binary fallback."""

    def test_exact_media_type_match(self):
        """An exact media-type claim resolves to its codec."""
        registry = _make_registry()

        assert isinstance(registry.resolve("application/json"), JsonCodec)
        assert isinstance(registry.resolve("text/plain"), TextCodec)

    def test_content_type_parameters_are_stripped(self):
        """Parameter suffixes (charset, boundary) do not affect resolution."""
        registry = _make_registry()

        assert isinstance(registry.resolve("application/json; charset=utf-8"), JsonCodec)
        assert isinstance(registry.resolve("text/plain; charset=iso-8859-1"), TextCodec)

    def test_structured_json_suffix(self):
        """application/*+json resolves to the JSON codec."""
        registry = _make_registry()

        assert isinstance(registry.resolve("application/problem+json"), JsonCodec)
        assert isinstance(registry.resolve("application/vnd.api+json"), JsonCodec)

    def test_main_type_match(self):
        """An unregistered subtype of a claimed main type resolves to it."""
        registry = _make_registry()

        assert isinstance(registry.resolve("text/html"), TextCodec)
        assert isinstance(registry.resolve("text/csv"), TextCodec)

    def test_unknown_media_type_falls_back_to_binary(self):
        """Unrecognized media types fall back to the BinaryCodec."""
        registry = _make_registry()

        assert isinstance(registry.resolve("application/xml"), BinaryCodec)
        assert isinstance(registry.resolve("image/png"), BinaryCodec)

    def test_missing_media_type_falls_back_to_binary(self):
        """A None/empty media type resolves to the BinaryCodec."""
        registry = _make_registry()

        assert isinstance(registry.resolve(None), BinaryCodec)
        assert isinstance(registry.resolve(""), BinaryCodec)

    def test_multipart_and_form_resolution(self):
        """Multipart and form media types resolve to their codecs."""
        registry = _make_registry()

        assert isinstance(registry.resolve("multipart/form-data"), MultipartCodec)
        from mcpgateway.protocols.codecs.form import FormCodec

        assert isinstance(registry.resolve("application/x-www-form-urlencoded"), FormCodec)


class TestCodecRegistryRegistration:
    """Registration semantics: later claims replace earlier ones."""

    def test_later_registration_replaces_earlier_for_same_media_type(self):
        """The most recent codec claiming a media type wins."""
        registry = CodecRegistry()
        first = _FakeCodec(["application/x-thing"], name="first")
        second = _FakeCodec(["application/x-thing"], name="second")
        registry.register(first)
        registry.register(second)

        assert registry.resolve("application/x-thing") is second

    def test_fallback_is_binary_when_registered(self):
        """A registered octet-stream codec is the natural fallback."""
        registry = CodecRegistry()
        binary = _FakeCodec(["application/octet-stream"], name="binary")
        other = _FakeCodec(["text/plain"], name="text")
        registry.register(other)
        registry.register(binary)

        assert registry.resolve("application/unknown") is binary
