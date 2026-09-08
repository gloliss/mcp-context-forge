# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_response_decoder.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the PR2 ResponseDecoder: the §70 codec resolution order and
the absence of a default ``response.json()``.
"""

# Standard
import base64

# First-Party
from mcpgateway.protocols.codecs import build_default_codec_registry
from mcpgateway.protocols.codecs.base import CodecContext
from mcpgateway.protocols.http.response_decoder import ResponseDecoder


def _make_decoder() -> ResponseDecoder:
    """Build a ResponseDecoder over the default codec registry."""
    return ResponseDecoder(build_default_codec_registry())


def _make_context(**overrides) -> CodecContext:
    """Build a CodecContext with overridable fields."""
    kwargs = {"protocol_config": None, "preferred_media_types": None, "max_response_bytes": None}
    kwargs.update(overrides)
    return CodecContext(**kwargs)


class TestBodylessStatuses:
    """204/205 and empty bodies decode to None without touching a codec."""

    def test_204_decodes_to_none(self):
        """204 No Content returns data=None even with a payload."""
        decoded = _make_decoder().decode(204, "application/json", b'{"a":1}', _make_context())

        assert decoded.data is None

    def test_205_decodes_to_none(self):
        """205 Reset Content returns data=None."""
        decoded = _make_decoder().decode(205, None, b"", _make_context())

        assert decoded.data is None

    def test_empty_payload_decodes_to_none(self):
        """An empty body on a 200 returns data=None."""
        decoded = _make_decoder().decode(200, "application/json", b"", _make_context())

        assert decoded.data is None


class TestContentTypeResolution:
    """The response Content-Type drives codec selection (§70.2)."""

    def test_application_json_parses_json(self):
        """application/json bodies decode to parsed values."""
        decoded = _make_decoder().decode(200, "application/json", b'{"ok": true}', _make_context())

        assert decoded.data == {"ok": True}
        assert decoded.codec_media_type == "application/json"

    def test_json_parameters_are_ignored(self):
        """A charset parameter does not defeat JSON resolution."""
        decoded = _make_decoder().decode(200, "application/json; charset=utf-8", b"[1, 2]", _make_context())

        assert decoded.data == [1, 2]

    def test_structured_json_suffix(self):
        """application/*+json decodes as JSON."""
        decoded = _make_decoder().decode(200, "application/problem+json", b'{"detail": "d"}', _make_context())

        assert decoded.data == {"detail": "d"}

    def test_text_plain_decodes_utf8(self):
        """text/plain bodies decode to strings."""
        decoded = _make_decoder().decode(200, "text/plain", "héllo".encode("utf-8"), _make_context())

        assert decoded.data == "héllo"

    def test_text_main_type_decodes_utf8(self):
        """Any text/* content type decodes as UTF-8 text."""
        decoded = _make_decoder().decode(200, "text/html", b"<p>hi</p>", _make_context())

        assert decoded.data == "<p>hi</p>"

    def test_binary_content_type_produces_envelope(self):
        """application/octet-stream produces the §72 base64 envelope."""
        payload = b"\x00\x01\x02"
        decoded = _make_decoder().decode(200, "application/octet-stream", payload, _make_context())

        assert decoded.data == {
            "encoding": "base64",
            "contentType": "application/octet-stream",
            "size": 3,
            "data": base64.b64encode(payload).decode("ascii"),
        }

    def test_unknown_content_type_sniffs_text(self):
        """An unmatched content type falls to the sniff step, not a JSON probe."""
        payload = b"<xml>text</xml>"
        decoded = _make_decoder().decode(200, "application/xml", payload, _make_context())

        assert decoded.data == "<xml>text</xml>"
        assert decoded.codec_media_type == "text/plain"

    def test_unknown_content_type_with_null_bytes_decodes_binary(self):
        """An unmatched content type with binary data produces the envelope."""
        payload = b"\x89PNG\r\n\x1a\n\x00\x00"
        decoded = _make_decoder().decode(200, "image/png", payload, _make_context())

        assert decoded.data["encoding"] == "base64"
        assert decoded.codec_media_type == "application/octet-stream"

    def test_form_content_type_decodes_to_dict(self):
        """application/x-www-form-urlencoded decodes parse_qs-style."""
        decoded = _make_decoder().decode(200, "application/x-www-form-urlencoded", b"a=1&b=2", _make_context())

        assert decoded.data == {"a": "1", "b": "2"}


class TestResolutionPrecedence:
    """Explicit config and preferred media types outrank Content-Type (§70)."""

    def test_explicit_codec_wins_over_content_type(self):
        """protocol_config response.codec outranks the response header."""
        config = {"response": {"codec": "json", "preferredMediaTypes": ["application/json"]}}
        decoded = _make_decoder().decode(
            200,
            "text/plain",
            b'{"n": 1}',
            _make_context(protocol_config=config),
        )

        assert decoded.data == {"n": 1}
        assert decoded.codec_media_type == "application/json"

    def test_explicit_auto_codec_falls_through(self):
        """A codec of "auto" defers to the remaining resolution steps."""
        config = {"response": {"codec": "auto"}}
        decoded = _make_decoder().decode(200, "text/plain", b"plain", _make_context(protocol_config=config))

        assert decoded.data == "plain"

    def test_preferred_media_types_used_when_content_type_missing(self):
        """Without a Content-Type, configured preferences are tried (§70.3)."""
        context = _make_context(preferred_media_types=("application/json",))
        decoded = _make_decoder().decode(200, None, b'{"p": 1}', context)

        assert decoded.data == {"p": 1}

    def test_preferred_media_types_skipped_when_content_type_matches(self):
        """A resolvable Content-Type wins over preferences."""
        context = _make_context(preferred_media_types=("text/plain",))
        decoded = _make_decoder().decode(200, "application/json", b'{"j": 1}', context)

        assert decoded.data == {"j": 1}


class TestSniffing:
    """The bounded sniff step (text vs binary) applies only as a last resort."""

    def test_text_sniffed_without_content_type_or_preferences(self):
        """A null-free payload without any hints decodes as text."""
        decoded = _make_decoder().decode(200, None, b"plain text", _make_context())

        assert decoded.data == "plain text"
        assert decoded.codec_media_type == "text/plain"

    def test_binary_sniffed_via_null_byte(self):
        """A null byte in the sniff window selects the binary codec."""
        payload = b"\x00\x01binary"
        decoded = _make_decoder().decode(200, None, payload, _make_context())

        assert decoded.data["encoding"] == "base64"
        assert decoded.codec_media_type == "application/octet-stream"


class TestBinaryCap:
    """Binary decoding honors max_response_bytes."""

    def test_binary_payload_is_capped(self):
        """Payloads beyond max_response_bytes are truncated before base64."""
        payload = b"x" * 100
        context = _make_context(max_response_bytes=10)
        decoded = _make_decoder().decode(200, "application/octet-stream", payload, context)

        assert decoded.data["size"] == 10

    def test_binary_payload_under_cap_is_untouched(self):
        """Payloads within the cap pass through whole."""
        payload = b"y" * 10
        context = _make_context(max_response_bytes=100)
        decoded = _make_decoder().decode(200, "application/octet-stream", payload, context)

        assert decoded.data["size"] == 10
