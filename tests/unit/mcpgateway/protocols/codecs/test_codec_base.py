# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/codecs/test_codec_base.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the codec SPI value objects (PR2): EncodedBody and CodecContext.
"""

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext, EncodedBody


class TestEncodedBody:
    """EncodedBody carries one of the four HTTPX body modes."""

    def test_json_mode(self):
        """A json-mode body carries the JSON value."""
        body = EncodedBody(mode="json", value={"a": 1}, content_type="application/json")

        assert body.mode == "json"
        assert body.value == {"a": 1}
        assert body.content_type == "application/json"

    def test_content_mode(self):
        """A content-mode body carries raw bytes."""
        body = EncodedBody(mode="content", value=b"raw", content_type="text/plain")

        assert body.mode == "content"
        assert body.value == b"raw"

    def test_data_mode(self):
        """A data-mode body carries a form mapping."""
        body = EncodedBody(mode="data", value={"k": "v"})

        assert body.mode == "data"
        assert body.value == {"k": "v"}
        assert body.content_type is None

    def test_files_mode(self):
        """A files-mode body carries multipart file parts."""
        body = EncodedBody(mode="files", value={"f": (None, "v")})

        assert body.mode == "files"
        assert body.value == {"f": (None, "v")}


class TestCodecContext:
    """CodecContext carries read-only per-invocation codec inputs."""

    def test_defaults(self):
        """All fields default to None."""
        context = CodecContext()

        assert context.protocol_config is None
        assert context.preferred_content_type is None
        assert context.preferred_media_types is None
        assert context.max_response_bytes is None

    def test_populated_context(self):
        """Every field can be set."""
        config = {"version": 1}
        context = CodecContext(
            protocol_config=config,
            preferred_content_type="application/json",
            preferred_media_types=("application/json",),
            max_response_bytes=4096,
        )

        assert context.protocol_config is config
        assert context.preferred_content_type == "application/json"
        assert context.preferred_media_types == ("application/json",)
        assert context.max_response_bytes == 4096
