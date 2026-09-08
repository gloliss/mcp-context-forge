# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_http_redirect_security.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for PR2 RedirectSecurity: manual per-hop SSRF re-validation,
connection pinning, and the hop budget (design-document §9.10).
"""

# Standard
from types import SimpleNamespace

# Third-Party
import httpx
import pytest

# First-Party
from mcpgateway.config import settings
from mcpgateway.protocols.http import adapter as adapter_module
from mcpgateway.protocols.http import redirect as redirect_module
from mcpgateway.protocols.http.adapter import HttpProtocolAdapter
from mcpgateway.protocols.http.redirect import (
    REST_TOO_MANY_REDIRECTS,
    REST_URL_PINNING_MISSING,
    REST_URL_VALIDATION_FAILED,
    RedirectSecurity,
    RedirectTarget,
)
from mcpgateway.protocols.models import ErrorCategory, ProtocolError
from tests.unit.mcpgateway.protocols.http.test_http_adapter_legacy import (  # noqa: F401
    _FakeClient,
    _make_context,
    _make_operation,
)


class _HopResponse:
    """Minimal httpx-like response for one redirect hop."""

    def __init__(self, status_code=200, headers=None, content=b""):
        """Initialise with status, headers, and raw body bytes."""
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self.content = content


class _SequencedClient:
    """Client answering each request with the next canned response."""

    def __init__(self, responses):
        """Initialise with an ordered response queue."""
        self._responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        """Record the call and pop the next canned response."""
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)


def _make_protocol_config(path_template="/start") -> dict:
    """Build a protocol_config selecting the new adapter path."""
    return {
        "version": 1,
        "operationRef": "POST /start",
        "request": {"method": "POST", "pathTemplate": path_template, "preferredContentType": "application/json"},
        "response": {"codec": "auto", "preferredMediaTypes": ["application/json"]},
    }


async def _validate_unpinned(url, label):
    """Return an unpinned validation result."""
    return {"resolved_ip": None, "hostname": None, "original_authority": None}


async def _validate_pinned(url, label):
    """Return a pinned validation result."""
    return {"resolved_ip": "10.0.0.1", "hostname": "api.example.com", "original_authority": "api.example.com:443"}


async def _validate_blocked(url, label):
    """Raise the SSRF policy rejection."""
    raise ValueError("blocked by policy")


class TestStatusClassification:
    """Only the five redirect statuses trigger a follow."""

    @pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
    def test_redirect_statuses_are_redirects(self, status_code):
        """301/302/303/307/308 are redirects."""
        assert RedirectSecurity.is_redirect(status_code) is True

    @pytest.mark.parametrize("status_code", [200, 201, 204, 300, 304, 400, 404, 500])
    def test_non_redirect_statuses_are_not_redirects(self, status_code):
        """Other statuses (including 304) are not redirects."""
        assert RedirectSecurity.is_redirect(status_code) is False


class TestLocationParsing:
    """Location extraction is case-insensitive and safe on absence."""

    def test_location_header_extracted_case_insensitively(self):
        """The Location header is found regardless of key casing."""
        assert RedirectSecurity.parse_location({"Location": "/next"}) == "/next"
        assert RedirectSecurity.parse_location({"location": "/next"}) == "/next"
        assert RedirectSecurity.parse_location({"LOCATION": "/next"}) == "/next"

    def test_missing_location_returns_none(self):
        """A response without Location returns None."""
        assert RedirectSecurity.parse_location({}) is None
        assert RedirectSecurity.parse_location({"Content-Type": "text/plain"}) is None


class TestUrlResolution:
    """Relative Locations resolve against the current hop URL."""

    def test_relative_location_resolves(self):
        """A relative Location resolves against the base URL."""
        assert RedirectSecurity.resolve_absolute("/next", "https://api.example.com/old") == "https://api.example.com/next"
        assert RedirectSecurity.resolve_absolute("next", "https://api.example.com/dir/old") == "https://api.example.com/dir/next"

    def test_absolute_location_passes_through(self):
        """An absolute Location is returned unchanged."""
        assert RedirectSecurity.resolve_absolute("https://other.example.com/x", "https://api.example.com/old") == "https://other.example.com/x"


class TestHopBudget:
    """The hop budget defaults to settings.gateway_max_redirects."""

    def test_default_max_hops_reads_settings(self):
        """The default budget mirrors the gateway setting."""
        assert RedirectSecurity().max_hops == settings.gateway_max_redirects

    def test_explicit_max_hops_overrides_settings(self):
        """An explicit budget wins over the setting."""
        assert RedirectSecurity(max_hops=3).max_hops == 3


class TestValidateAndPin:
    """Every hop re-runs SSRF validation and connection pinning."""

    async def test_unpinned_url_passes_through(self, monkeypatch):
        """Without pinning, the target is the URL itself with no extras."""
        monkeypatch.setattr(redirect_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_unpinned)
        monkeypatch.setattr(settings, "ssrf_protection_enabled", False)

        target = await RedirectSecurity().validate_and_pin("https://api.example.com/p", None)

        assert target == RedirectTarget(url="https://api.example.com/p")

    async def test_blocked_url_raises_validation_failed(self, monkeypatch):
        """A policy-rejected hop raises REST_URL_VALIDATION_FAILED."""
        monkeypatch.setattr(redirect_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_blocked)

        with pytest.raises(ProtocolError) as exc_info:
            await RedirectSecurity().validate_and_pin("https://blocked.example.com/p", None)

        assert exc_info.value.code == REST_URL_VALIDATION_FAILED
        assert exc_info.value.category is ErrorCategory.PERMISSION_DENIED

    async def test_pinning_missing_raises_when_ssrf_enabled(self, monkeypatch):
        """SSRF protection without a pinned target raises REST_URL_PINNING_MISSING."""
        monkeypatch.setattr(redirect_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_unpinned)
        monkeypatch.setattr(settings, "ssrf_protection_enabled", True)

        with pytest.raises(ProtocolError) as exc_info:
            await RedirectSecurity().validate_and_pin("https://api.example.com/p", None)

        assert exc_info.value.code == REST_URL_PINNING_MISSING

    async def test_pinned_target_carries_pinned_url_host_and_sni(self, monkeypatch):
        """A pinned hop returns the IP-pinned URL, Host header, and SNI."""
        monkeypatch.setattr(redirect_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_pinned)
        monkeypatch.setattr(settings, "ssrf_protection_enabled", True)

        target = await RedirectSecurity().validate_and_pin("https://api.example.com/p", None)

        assert target.url == "https://10.0.0.1/p"
        assert target.headers == {"Host": "api.example.com:443"}
        assert target.extensions == {"sni_hostname": "api.example.com"}
        assert target.resolved_ip == "10.0.0.1"

    async def test_pinning_follows_validator_result(self, monkeypatch):
        """When the validator returns pinned values, the hop is pinned."""
        monkeypatch.setattr(redirect_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_pinned)
        monkeypatch.setattr(settings, "ssrf_protection_enabled", False)

        target = await RedirectSecurity().validate_and_pin("https://api.example.com/p", None)

        assert target.url == "https://10.0.0.1/p"
        assert target.headers == {"Host": "api.example.com:443"}
        assert target.extensions == {"sni_hostname": "api.example.com"}


class TestPoolKey:
    """Pool keys exist only for pinned targets."""

    def test_pool_key_uses_factory_for_pinned_targets(self):
        """A pinned target produces the factory-computed pool key."""
        target = RedirectTarget(
            url="https://10.0.0.1/p",
            resolved_ip="10.0.0.1",
            hostname="api.example.com",
            original_authority="api.example.com:443",
        )
        recorded = {}

        def _factory(url, ip, host, authority):
            recorded["args"] = (url, ip, host, authority)
            return ("key", url)

        context = SimpleNamespace(pool_key_factory=_factory)

        assert RedirectSecurity().pool_key(target, context) == ("key", "https://10.0.0.1/p")
        assert recorded["args"] == ("https://10.0.0.1/p", "10.0.0.1", "api.example.com", "api.example.com:443")

    def test_pool_key_none_for_unpinned_targets(self):
        """An unpinned target has no pool key."""
        target = RedirectTarget(url="https://api.example.com/p")

        assert RedirectSecurity().pool_key(target, None) is None


class TestAdapterRedirectLoop:
    """The new adapter path follows redirects manually with per-hop SSRF."""

    async def _validate_and_record(self, recorded, url, label):
        """Record the hop URL and return an unpinned validation result."""
        recorded.append(url)
        return {"resolved_ip": None, "hostname": None, "original_authority": None}

    async def test_redirect_is_followed_with_per_hop_validation(self, monkeypatch):
        """Each hop re-runs SSRF validation before it is taken."""
        recorded = []
        responses = [
            _HopResponse(status_code=302, headers={"Location": "/next"}),
            _HopResponse(status_code=200, headers={"Content-Type": "application/json"}, content=b'{"done": true}'),
        ]
        client = _SequencedClient(responses)
        context = _make_context(
            http_client=client,
            protocol_config=_make_protocol_config(),
        )
        monkeypatch.setattr(
            redirect_module.SecurityValidator,
            "validate_url_for_connection_pinning",
            lambda url, label: self._validate_and_record(recorded, url, label),
        )
        monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", False)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == {"done": True}
        assert recorded == ["https://api.example.com/start", "https://api.example.com/next"]
        assert len(client.calls) == 2
        assert all(kwargs["follow_redirects"] is False for _, _, kwargs in client.calls)

    async def test_relative_location_resolves_against_current_hop(self, monkeypatch):
        """A relative Location resolves against the hop that produced it."""
        recorded = []
        responses = [
            _HopResponse(status_code=302, headers={"Location": "/dir/deeper"}),
            _HopResponse(status_code=302, headers={"Location": "../end"}),
            _HopResponse(status_code=200, headers={"Content-Type": "text/plain"}, content=b"ok"),
        ]
        client = _SequencedClient(responses)
        context = _make_context(http_client=client, protocol_config=_make_protocol_config())
        monkeypatch.setattr(
            redirect_module.SecurityValidator,
            "validate_url_for_connection_pinning",
            lambda url, label: self._validate_and_record(recorded, url, label),
        )
        monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", False)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data == "ok"
        assert recorded == [
            "https://api.example.com/start",
            "https://api.example.com/dir/deeper",
            "https://api.example.com/end",
        ]

    async def test_missing_location_stops_following(self, monkeypatch):
        """A 3xx without a Location header terminates the loop."""
        responses = [
            _HopResponse(status_code=302, headers={}),
        ]
        client = _SequencedClient(responses)
        context = _make_context(http_client=client, protocol_config=_make_protocol_config())
        monkeypatch.setattr(
            redirect_module.SecurityValidator,
            "validate_url_for_connection_pinning",
            _validate_unpinned,
        )
        monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", False)

        result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert result.data is None  # empty body → data=None
        assert len(client.calls) == 1

    async def test_hop_budget_exceeded_raises_too_many_redirects(self, monkeypatch):
        """Exceeding gateway_max_redirects raises REST_TOO_MANY_REDIRECTS."""
        redirect_response = _HopResponse(status_code=302, headers={"Location": "/next"})
        client = _SequencedClient([redirect_response] * 4)
        context = _make_context(http_client=client, protocol_config=_make_protocol_config())
        monkeypatch.setattr(
            redirect_module.SecurityValidator,
            "validate_url_for_connection_pinning",
            _validate_unpinned,
        )
        monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", False)
        monkeypatch.setattr(adapter_module.settings, "gateway_max_redirects", 2)

        with pytest.raises(ProtocolError) as exc_info:
            await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert exc_info.value.code == REST_TOO_MANY_REDIRECTS
        assert exc_info.value.protocol_status == 302
        assert len(client.calls) == 3  # first hop + 2 allowed redirects, then budget hit

    async def test_redirect_to_blocked_host_is_rejected(self, monkeypatch):
        """A redirect to a policy-blocked host raises REST_URL_VALIDATION_FAILED."""

        async def _block_evil(url, label):
            if "evil" in url:
                raise ValueError("blocked by policy")
            return {"resolved_ip": None, "hostname": None, "original_authority": None}

        responses = [
            _HopResponse(status_code=302, headers={"Location": "https://evil.example.com/steal"}),
        ]
        client = _SequencedClient(responses)
        context = _make_context(http_client=client, protocol_config=_make_protocol_config())
        monkeypatch.setattr(redirect_module.SecurityValidator, "validate_url_for_connection_pinning", _block_evil)
        monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", False)

        with pytest.raises(ProtocolError) as exc_info:
            await HttpProtocolAdapter().invoke(_make_operation(), {}, context)

        assert exc_info.value.code == REST_URL_VALIDATION_FAILED
        assert len(client.calls) == 1  # the blocked hop was never sent
