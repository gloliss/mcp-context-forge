# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_http_status.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the PR2 HTTP success semantics (design-document §9.8/§9.13):
the full 200-299 range is success, 204/205/HEAD decode to data=None, and
non-2xx responses remain errors.
"""

# Third-Party
import httpx
import pytest

# First-Party
from mcpgateway.protocols.http import adapter as adapter_module
from mcpgateway.protocols.http.adapter import REST_HTTP_STATUS_ERROR, HttpProtocolAdapter
from mcpgateway.protocols.models import ProtocolError
from tests.unit.mcpgateway.protocols.http.test_http_adapter_legacy import (  # noqa: F401
    _FakeClient,
    _FakeResponse,
    _make_context,
    _make_operation,
)


@pytest.fixture(autouse=True)
def _neutralize_ssrf_and_settings(monkeypatch):
    """Neutralize SSRF validation and pool settings for all status tests."""
    async def _validate_unpinned(url, label):
        return {"resolved_ip": None, "hostname": None, "original_authority": None}

    monkeypatch.setattr(adapter_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_unpinned)
    monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", False)
    monkeypatch.setattr(adapter_module.settings, "mcpgateway_rest_client_pool_enabled", False)


class _StrictFakeResponse(_FakeResponse):
    """Fake response whose raise_for_status mimics httpx for every non-2xx."""

    def raise_for_status(self):
        """Raise httpx.HTTPStatusError for any status >= 300."""
        if self.status_code >= 300:
            request = httpx.Request("GET", "http://upstream.invalid")
            raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(self.status_code, request=request))


class TestSuccessRange:
    """200-299 is success, including the non-standard 2xx codes."""

    @pytest.mark.parametrize("status_code", [200, 201, 202, 203, 204, 205, 206, 207, 226, 299])
    async def test_all_2xx_are_success(self, status_code):
        """Every 2xx status yields a ProtocolResult, never an error."""
        response = _FakeResponse(status_code=status_code, json_data={"ok": True})
        context = _make_context(http_client=_FakeClient(response=response))

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.metadata["status_code"] == status_code

    async def test_203_non_json_body_falls_back_to_response_text(self):
        """203 with a non-JSON body is success with the raw text preserved."""
        import json as json_module

        response = _FakeResponse(status_code=203, text="partial", json_error=json_module.JSONDecodeError("x", "d", 0))
        context = _make_context(http_client=_FakeClient(response=response))

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == {"response_text": "partial"}

    async def test_207_multi_status_parses_json(self):
        """207 Multi-Status decodes its JSON body normally."""
        response = _FakeResponse(status_code=207, json_data={"results": [{"status": 200}]})
        context = _make_context(http_client=_FakeClient(response=response))

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == {"results": [{"status": 200}]}


class TestBodylessSuccesses:
    """204/205/HEAD successes carry data=None and never parse the body."""

    async def test_204_returns_none(self):
        """204 No Content returns data=None without a JSON parse attempt."""
        response = _FakeResponse(status_code=204, json_error=ValueError("must not be called"))
        context = _make_context(http_client=_FakeClient(response=response))

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data is None
        assert result.metadata == {"status_code": 204}
        assert response.json_calls == 0

    async def test_205_returns_none(self):
        """205 Reset Content returns data=None."""
        response = _FakeResponse(status_code=205, json_data={"error": "ignored"})
        context = _make_context(http_client=_FakeClient(response=response))

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data is None
        assert result.metadata == {"status_code": 205}

    async def test_head_returns_none(self):
        """A HEAD invocation returns data=None regardless of status body."""
        response = _FakeResponse(status_code=200, json_data={"should": "not parse"})
        context = _make_context(http_client=_FakeClient(response=response))

        result = await HttpProtocolAdapter().invoke(_make_operation(method="HEAD"), {}, context)

        assert result.data is None
        assert response.json_calls == 0


class TestNonSuccessStatuses:
    """Non-2xx statuses remain REST_HTTP_STATUS_ERROR errors."""

    @pytest.mark.parametrize("status_code", [300, 301, 400, 401, 403, 404, 500, 502, 503])
    async def test_non_2xx_raises_http_status_error(self, status_code):
        """Every non-2xx status raises REST_HTTP_STATUS_ERROR."""
        response = _StrictFakeResponse(status_code=status_code, json_data={"error": "boom"})
        context = _make_context(http_client=_FakeClient(response=response))

        with pytest.raises(ProtocolError) as exc_info:
            await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert exc_info.value.code == REST_HTTP_STATUS_ERROR
        assert exc_info.value.protocol_status == status_code

    async def test_redirect_statuses_are_not_success(self):
        """3xx statuses raise REST_HTTP_STATUS_ERROR (no auto-follow)."""
        response = _StrictFakeResponse(status_code=302, json_data={"error": "moved"})
        context = _make_context(http_client=_FakeClient(response=response))

        with pytest.raises(ProtocolError) as exc_info:
            await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert exc_info.value.code == REST_HTTP_STATUS_ERROR
