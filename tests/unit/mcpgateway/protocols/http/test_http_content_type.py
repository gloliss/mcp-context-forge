# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_http_content_type.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for response Content-Type handling on the PR2 protocol_config
path: the Content-Type selects the codec, and the response body is never
force-parsed as JSON.
"""

# Standard
import base64

# Third-Party
import httpx
import pytest

# First-Party
from mcpgateway.protocols.http import adapter as adapter_module
from mcpgateway.protocols.http import redirect as redirect_module
from mcpgateway.protocols.http.adapter import HttpProtocolAdapter
from tests.unit.mcpgateway.protocols.http.test_http_adapter_legacy import (  # noqa: F401
    _FakeClient,
    _make_context,
    _make_operation,
)


class _NewPathResponse:
    """Minimal httpx-like response for the protocol_config path."""

    def __init__(self, status_code=200, content_type=None, content=b""):
        """Initialise with status, content type, and raw body bytes."""
        self.status_code = status_code
        self.headers = httpx.Headers({"content-type": content_type} if content_type else {})
        self.content = content


def _make_protocol_config(path_template="/reports", **overrides) -> dict:
    """Build a protocol_config selecting the new adapter path."""
    config = {
        "version": 1,
        "operationRef": "POST /reports",
        "request": {"method": "POST", "pathTemplate": path_template, "preferredContentType": "application/json"},
        "response": {"codec": "auto", "preferredMediaTypes": ["application/json"]},
    }
    config.update(overrides)
    return config


def _make_new_context(response, protocol_config=None) -> object:
    """Build an InvocationContext on the protocol_config path."""
    return _make_context(
        http_client=_FakeClient(response=response),
        protocol_config=protocol_config if protocol_config is not None else _make_protocol_config(),
    )


@pytest.fixture(autouse=True)
def _neutralize_ssrf(monkeypatch):
    """Neutralize SSRF validation and settings for the new path."""
    async def _validate_unpinned(url, label):
        return {"resolved_ip": None, "hostname": None, "original_authority": None}

    monkeypatch.setattr(redirect_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_unpinned)
    monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", False)


class TestNewPathContentTypes:
    """Content-Type selects the codec on the protocol_config path."""

    async def test_application_json_decodes_parsed_json(self):
        """application/json bodies decode to Python values."""
        response = _NewPathResponse(content_type="application/json", content=b'{"ok": true}')
        context = _make_new_context(response)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == {"ok": True}
        assert result.metadata["content_type"] == "application/json"

    async def test_structured_json_suffix_decodes_json(self):
        """application/*+json bodies decode as JSON."""
        response = _NewPathResponse(content_type="application/problem+json", content=b'{"detail": "d"}')
        context = _make_new_context(response)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == {"detail": "d"}

    async def test_text_content_decodes_to_string(self):
        """text/* bodies decode to UTF-8 strings."""
        response = _NewPathResponse(content_type="text/plain", content=b"hello")
        context = _make_new_context(response)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == "hello"
        assert result.metadata["content_type"] == "text/plain"

    async def test_octet_stream_decodes_to_base64_envelope(self):
        """application/octet-stream produces the §72 envelope."""
        payload = b"\x00\x01binary"
        response = _NewPathResponse(content_type="application/octet-stream", content=payload)
        context = _make_new_context(response)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data["encoding"] == "base64"
        assert result.data["data"] == base64.b64encode(payload).decode("ascii")

    async def test_form_urlencoded_decodes_to_dict(self):
        """application/x-www-form-urlencoded decodes parse_qs-style."""
        response = _NewPathResponse(content_type="application/x-www-form-urlencoded", content=b"a=1&b=2")
        context = _make_new_context(response)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == {"a": "1", "b": "2"}

    async def test_multipart_response_is_returned_raw(self):
        """multipart/form-data responses decode to raw bytes (no parsing)."""
        raw = b"--boundary\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nv\r\n--boundary--"
        response = _NewPathResponse(content_type="multipart/form-data; boundary=boundary", content=raw)
        context = _make_new_context(response)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == raw

    async def test_unknown_content_type_never_force_parses_json(self):
        """An unmatched content type falls to sniffing, never response.json()."""
        payload = b"<xml>not json</xml>"
        response = _NewPathResponse(content_type="application/xml", content=payload)
        config = _make_protocol_config(response={"codec": "auto"})
        context = _make_new_context(response, protocol_config=config)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == "<xml>not json</xml>"
        assert result.metadata["content_type"] == "text/plain"

    async def test_preferred_media_types_apply_when_content_type_unknown(self):
        """Configured preferences are tried after an unmatched Content-Type."""
        response = _NewPathResponse(content_type="application/xml", content=b'{"via": "pref"}')
        context = _make_new_context(response)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == {"via": "pref"}

    async def test_missing_content_type_sniffs_text(self):
        """Without a Content-Type, a null-free body sniffs as text."""
        response = _NewPathResponse(content=b"plain body")
        config = _make_protocol_config(response={"codec": "auto"})
        context = _make_new_context(response, protocol_config=config)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == "plain body"

    async def test_missing_content_type_sniffs_binary(self):
        """Without a Content-Type, a null byte in the body sniffs as binary."""
        payload = b"\x00\x01raw"
        response = _NewPathResponse(content=payload)
        config = _make_protocol_config(response={"codec": "auto"})
        context = _make_new_context(response, protocol_config=config)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data["encoding"] == "base64"
        assert result.data["data"] == base64.b64encode(payload).decode("ascii")

    async def test_explicit_response_codec_overrides_content_type(self):
        """protocol_config response.codec wins over the Content-Type header."""
        config = _make_protocol_config(response={"codec": "json", "preferredMediaTypes": ["application/json"]})
        response = _NewPathResponse(content_type="text/plain", content=b'{"forced": 1}')
        context = _make_new_context(response, protocol_config=config)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == {"forced": 1}

    async def test_request_was_sent_without_follow_redirects(self):
        """The new path never enables HTTPX auto-redirects (§9.10)."""
        response = _NewPathResponse(content_type="application/json", content=b'{"ok": 1}')
        context = _make_new_context(response)

        await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        _, called_url, kwargs = context.http_client.calls[0][1:4]
        assert called_url == "https://api.example.com/reports"
        assert kwargs["follow_redirects"] is False
