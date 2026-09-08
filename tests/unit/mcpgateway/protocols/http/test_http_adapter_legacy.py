# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_http_adapter_legacy.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the PR1 HttpProtocolAdapter (legacy REST runtime extraction).

These tests pin down the pre-extraction REST branch semantics: URL template
substitution, mapping allowlist behaviour, the three encoding-path
asymmetries, SSRF validation/pinning, the pinned client pool lifecycle,
token-exchange retry wiring, and response classification.
"""

# Standard
import asyncio
import json
import re
from contextlib import nullcontext
from types import SimpleNamespace

# Third-Party
import httpx
import pytest

# First-Party
from mcpgateway.protocols.http import adapter as adapter_module
from mcpgateway.protocols.http.adapter import (
    REST_HEADER_MAPPING_ILLEGAL_CHARS,
    REST_HTTP_STATUS_ERROR,
    REST_MISSING_URL_PARAM,
    REST_QUERY_MAPPING_NON_SCALAR,
    REST_SEND_TIMEOUT,
    REST_URL_PINNING_MISSING,
    REST_URL_VALIDATION_FAILED,
    REST_URL_VALIDATION_TIMEOUT,
    HttpProtocolAdapter,
    _form_value_to_str,
    _handle_json_parse_error,
)
from mcpgateway.protocols.contracts.models import OperationDefinition
from mcpgateway.protocols.models import ErrorCategory, InvocationContext, ProtocolError


class _FakeResponse:
    """Minimal httpx-like response with configurable JSON behaviour."""

    def __init__(self, status_code=200, json_data=None, text="", json_error=None):
        """Initialise with a status code, payload, raw text, and parse error."""
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._json_error = json_error
        self.json_calls = 0

    def json(self):
        """Return the canned payload or raise the canned parse error."""
        self.json_calls += 1
        if self._json_error is not None:
            raise self._json_error
        return self._json_data

    def raise_for_status(self):
        """Raise httpx.HTTPStatusError for >= 400 statuses."""
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://upstream.invalid")
            raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(self.status_code, request=request))


class _FakeClient:
    """Recording stand-in for httpx.AsyncClient."""

    def __init__(self, response=None):
        """Initialise with a canned response and an empty call record."""
        self._response = response if response is not None else _FakeResponse()
        self.calls = []
        self.aclosed = False

    async def get(self, url, params=None, **kwargs):
        """Record and answer a GET call."""
        self.calls.append(("get", url, params, kwargs))
        return self._response

    async def request(self, method, url, **kwargs):
        """Record and answer an arbitrary method call."""
        self.calls.append(("request", method, url, kwargs))
        return self._response

    async def aclose(self):
        """Mark the client as closed."""
        self.aclosed = True


class _FakePool:
    """Recording stand-in for _PinnedRestClientPool."""

    def __init__(self, client=None):
        """Initialise with a canned client and empty acquire/release records."""
        self.client = client if client is not None else _FakeClient()
        self.acquired = []
        self.released = []

    async def acquire(self, key):
        """Record the key and return an entry holding the canned client."""
        entry = SimpleNamespace(client=self.client)
        self.acquired.append((key, entry))
        return entry

    async def release(self, entry):
        """Record the released entry."""
        self.released.append(entry)


class _BudgetExceeded(Exception):
    """Sentinel for the remaining_timeout budget-exhaustion path."""


def _raise_budget_exceeded():
    """Raise the budget-exhaustion sentinel (stand-in for ToolTimeoutError)."""
    raise _BudgetExceeded("budget gone")


async def _default_send_with_retry(send, call_headers):
    """Default context.send_with_retry: single attempt, no retry."""
    return await send(call_headers)


def _simple_apply_mapping(source, mapping, target):
    """Apply {target_key: source_key} mappings onto target (allowlist)."""
    for target_key, source_key in mapping.items():
        if source_key in source:
            target[target_key] = source[source_key]
    return target


async def _validate_unpinned(url, label):
    """Default SSRF validator result: nothing to pin."""
    return {"resolved_ip": None, "hostname": None, "original_authority": None}


def _make_operation(url="https://api.example.com/data", method="POST", query_mapping=None, header_mapping=None):
    """Build an OperationDefinition with a legacy-shaped request dict."""
    return OperationDefinition(
        key="http:rest:tool-1",
        protocol="http",
        request={
            "url": url,
            "method": method,
            "query_mapping": query_mapping,
            "header_mapping": header_mapping,
        },
    )


def _make_context(**overrides) -> InvocationContext:
    """Build an InvocationContext with realistic defaults for one test."""
    kwargs = {
        "tool_name": "demo_tool",
        "tool_name_computed": "demo_tool",
        "tool_id": "tool-1",
        "effective_timeout": 30.0,
        "remaining_timeout": lambda: 30.0,
        "http_client": _FakeClient(),
        "headers": {"Authorization": "Bearer tok"},
        "send_with_retry": _default_send_with_retry,
        "pinned_rest_pool": None,
        "pinned_client_builder": None,
        "pool_key_factory": lambda url, ip, host, authority: (url, ip),
        "apply_mapping": _simple_apply_mapping,
        "validate_header_mapping_targets": lambda mapping, tool_name: None,
        "invalid_header_value_chars": re.compile(r"[\r\n\x00]"),
        "child_span_factory": lambda *args, **kwargs: nullcontext(),
    }
    kwargs.update(overrides)
    return InvocationContext(**kwargs)


@pytest.fixture(autouse=True)
def _neutralize_ssrf_and_settings(monkeypatch):
    """Neutralize SSRF validation and pool settings for all adapter tests."""
    monkeypatch.setattr(adapter_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_unpinned)
    monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", False)
    monkeypatch.setattr(adapter_module.settings, "mcpgateway_rest_client_pool_enabled", False)


# ── URL template substitution ──────────────────────────────────────────────


async def test_missing_url_parameter_raises_invalid_argument():
    """A template parameter absent from arguments raises REST_MISSING_URL_PARAM."""
    context = _make_context()
    operation = _make_operation(url="https://api.example.com/users/{user_id}")
    with pytest.raises(ProtocolError, match="Required URL parameter 'user_id' not found in arguments") as exc_info:
        await HttpProtocolAdapter().invoke(operation, {}, context)
    error = exc_info.value
    assert error.category is ErrorCategory.INVALID_ARGUMENT
    assert error.code == REST_MISSING_URL_PARAM
    assert context.http_client.calls == []


async def test_url_substitution_pops_params_from_copy_only():
    """URL params are popped from a payload copy; caller arguments unchanged."""
    context = _make_context()
    operation = _make_operation(url="https://api.example.com/users/{user_id}")
    arguments = {"user_id": 42, "keep": "v"}
    await HttpProtocolAdapter().invoke(operation, arguments, context)
    assert arguments == {"user_id": 42, "keep": "v"}
    method, url, kwargs = context.http_client.calls[0][1:4]
    assert url == "https://api.example.com/users/42"
    assert kwargs["json"] == {"keep": "v"}


async def test_signed_url_query_preserved_for_json_post_without_mappings():
    """Without mappings, JSON POST keeps the query string (signed-URL support)."""
    context = _make_context()
    url = "https://api.example.com/data?sig=abc123"
    await HttpProtocolAdapter().invoke(_make_operation(url=url), {"a": 1}, context)
    _, called_url, _ = context.http_client.calls[0][1:4]
    assert called_url == url


# ── Query/header mapping ───────────────────────────────────────────────────


async def test_query_mapping_acts_as_allowlist_and_strips_url_query():
    """Mapped-only keys survive; URL query is extracted and merged; URL stripped."""
    context = _make_context()
    operation = _make_operation(url="https://api.example.com/data?fixed=1", query_mapping={"renamed": "orig"})
    await HttpProtocolAdapter().invoke(operation, {"orig": "x", "dropped": "y"}, context)
    _, called_url, kwargs = context.http_client.calls[0][1:4]
    assert called_url == "https://api.example.com/data"
    assert kwargs["json"] == {"fixed": "1", "renamed": "x"}


async def test_query_mapping_non_scalar_value_rejected():
    """Non-scalar mapped query values raise REST_QUERY_MAPPING_NON_SCALAR."""
    context = _make_context()
    operation = _make_operation(query_mapping={"a": "b"})
    with pytest.raises(ProtocolError, match="query_mapping produced non-scalar value for parameter 'a'") as exc_info:
        await HttpProtocolAdapter().invoke(operation, {"b": [1, 2]}, context)
    error = exc_info.value
    assert error.code == REST_QUERY_MAPPING_NON_SCALAR
    assert "demo_tool" in error.message


async def test_header_mapping_reads_original_arguments_not_reduced_payload():
    """Header mapping sources are taken from arguments.copy(), URL params included."""
    recorded_sources = []
    context = _make_context(
        headers={"Authorization": "Bearer tok"},
        apply_mapping=lambda source, mapping, target: recorded_sources.append(source) or _simple_apply_mapping(source, mapping, target),
    )
    operation = _make_operation(url="https://api.example.com/{p}", header_mapping={"X-Token": "secret"})
    await HttpProtocolAdapter().invoke(operation, {"p": 1, "secret": "s"}, context)
    assert recorded_sources == [{"p": 1, "secret": "s"}]
    assert context.headers["X-Token"] == "s"


async def test_header_mapping_illegal_characters_rejected():
    """CRLF in a mapped header value raises REST_HEADER_MAPPING_ILLEGAL_CHARS."""
    context = _make_context()
    operation = _make_operation(header_mapping={"X-Bad": "v"})
    with pytest.raises(ProtocolError, match="illegal characters for header 'X-Bad'") as exc_info:
        await HttpProtocolAdapter().invoke(operation, {"v": "a\r\nb"}, context)
    assert exc_info.value.code == REST_HEADER_MAPPING_ILLEGAL_CHARS


async def test_header_mapping_targets_validated_with_tool_name():
    """validate_header_mapping_targets receives the mapping and tool name."""
    recorded = []
    operation = _make_operation(header_mapping={"X-K": "k"})
    context = _make_context(
        validate_header_mapping_targets=lambda mapping, tool_name: recorded.append((mapping, tool_name)),
    )
    await HttpProtocolAdapter().invoke(operation, {"k": "v"}, context)
    assert recorded == [({"X-K": "k"}, "demo_tool")]


# ── Encoding paths (the three intentional asymmetries) ─────────────────────


async def test_get_uses_params_and_merges_url_query():
    """GET sends params=payload and merges URL query params into it."""
    context = _make_context()
    operation = _make_operation(url="https://api.example.com/data?page=2", method="GET")
    await HttpProtocolAdapter().invoke(operation, {"q": "x"}, context)
    assert context.http_client.calls[0][0] == "get"
    _, called_url, params, _ = context.http_client.calls[0]
    assert called_url == "https://api.example.com/data"
    assert params == {"q": "x", "page": "2"}


async def test_get_conflicting_params_warned_and_url_wins(caplog):
    """URL query params win over arguments; a warning lists the conflicts."""
    caplog.set_level("WARNING", logger="mcpgateway.protocols.http.adapter")
    context = _make_context()
    operation = _make_operation(url="https://api.example.com/data?q=urlval", method="GET")
    await HttpProtocolAdapter().invoke(operation, {"q": "argval"}, context)
    _, _, params, _ = context.http_client.calls[0]
    assert params == {"q": "urlval"}
    assert "conflicting parameters between URL and input arguments" in caplog.text
    assert "q" in caplog.text
    assert "demo_tool" in caplog.text


async def test_get_with_mapping_skips_query_extraction_warning_path():
    """With mappings, GET merges mapped URL query into params without re-extraction."""
    context = _make_context()
    operation = _make_operation(url="https://api.example.com/data?fixed=1", method="GET", query_mapping={"renamed": "orig"})
    await HttpProtocolAdapter().invoke(operation, {"orig": "x"}, context)
    _, _, params, _ = context.http_client.calls[0]
    assert params == {"fixed": "1", "renamed": "x"}


async def test_form_urlencoded_uses_data_and_params():
    """Form bodies use data= with stringified values and params= for URL query."""
    context = _make_context(headers={"Content-Type": "application/x-www-form-urlencoded"})
    operation = _make_operation(url="https://api.example.com/form?a=1")
    await HttpProtocolAdapter().invoke(operation, {"f": 1, "n": None, "b": True}, context)
    assert context.http_client.calls[0][0] == "request"
    _, called_url, kwargs = context.http_client.calls[0][1:4]
    assert called_url == "https://api.example.com/form"
    assert kwargs["data"] == {"f": "1", "n": "", "b": "true"}
    assert kwargs["params"] == {"a": "1"}
    assert "json" not in kwargs


async def test_multipart_strips_content_type_and_sends_files():
    """Multipart sends files= and strips Content-Type so httpx sets the boundary."""
    context = _make_context(headers={"Content-Type": "multipart/form-data"})
    await HttpProtocolAdapter().invoke(_make_operation(url="https://api.example.com/up"), {"f": "v"}, context)
    _, _, kwargs = context.http_client.calls[0][1:4]
    assert kwargs["files"] == {"f": (None, "v")}
    sent_headers = kwargs["headers"]
    assert not any(k.lower() == "content-type" for k in sent_headers)


async def test_json_default_path_sends_json_body():
    """The default encoding path sends json= and no data=/files=."""
    context = _make_context()
    await HttpProtocolAdapter().invoke(_make_operation(), {"a": 1}, context)
    assert context.http_client.calls[0][0] == "request"
    _, _, kwargs = context.http_client.calls[0][1:4]
    assert kwargs["json"] == {"a": 1}
    assert "data" not in kwargs
    assert "files" not in kwargs


async def test_json_merges_url_query_into_body_when_mappings_present():
    """With mappings, URL query params are merged into the JSON body."""
    context = _make_context()
    operation = _make_operation(url="https://api.example.com/data?fixed=1", query_mapping={"renamed": "orig"})
    await HttpProtocolAdapter().invoke(operation, {"orig": "x"}, context)
    _, _, kwargs = context.http_client.calls[0][1:4]
    assert kwargs["json"] == {"fixed": "1", "renamed": "x"}


async def test_content_type_parameterized_still_detected():
    """Content-Type detection strips parameters (e.g. charset) before matching."""
    context = _make_context(headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
    await HttpProtocolAdapter().invoke(_make_operation(), {"a": 1}, context)
    _, _, kwargs = context.http_client.calls[0][1:4]
    assert kwargs["data"] == {"a": "1"}


# ── SSRF validation + connection pinning ───────────────────────────────────


async def test_ssrf_value_error_raises_url_validation_failed(monkeypatch):
    """A ValueError from validation raises REST_URL_VALIDATION_FAILED with details."""

    def _blocked(url, label):
        raise ValueError("blocked: localhost")

    monkeypatch.setattr(adapter_module.SecurityValidator, "validate_url_for_connection_pinning", _blocked)
    context = _make_context()
    with pytest.raises(ProtocolError, match="Outbound URL blocked by URL policy") as exc_info:
        await HttpProtocolAdapter().invoke(_make_operation(url="https://127.0.0.1/x"), {}, context)
    error = exc_info.value
    assert error.code == REST_URL_VALIDATION_FAILED
    assert error.category is ErrorCategory.PERMISSION_DENIED
    assert isinstance(error.__cause__, ValueError)
    assert error.details["raw_url"] == "https://127.0.0.1/x"
    assert error.details["validation_error"] == "blocked: localhost"


async def test_ssrf_validation_timeout_raises_url_validation_timeout(monkeypatch):
    """An asyncio timeout during validation raises REST_URL_VALIDATION_TIMEOUT."""

    async def _slow(url, label):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(adapter_module.SecurityValidator, "validate_url_for_connection_pinning", _slow)
    context = _make_context(effective_timeout=12.5)
    with pytest.raises(ProtocolError, match="Tool invocation timed out after 12.5s") as exc_info:
        await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    error = exc_info.value
    assert error.code == REST_URL_VALIDATION_TIMEOUT
    assert error.category is ErrorCategory.UNAVAILABLE
    assert isinstance(error.__cause__, asyncio.TimeoutError)


async def test_pinning_missing_raises_when_ssrf_enabled(monkeypatch):
    """With SSRF on, a validator result missing pinning data is rejected."""
    monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", True)
    context = _make_context()
    with pytest.raises(ProtocolError, match="Outbound URL blocked by URL policy") as exc_info:
        await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    error = exc_info.value
    assert error.code == REST_URL_PINNING_MISSING
    assert error.details["raw_url"] == "https://api.example.com/data"


async def test_pinning_rewrites_url_host_headers_and_pool_key(monkeypatch):
    """Pinning rewrites the URL to the IP, sets Host/sni, and isolates the pool."""
    recorded_keys = []

    async def _validate_pinned(url, label):
        return {"resolved_ip": "10.0.0.5", "hostname": "api.example.com", "original_authority": "api.example.com:8443"}

    monkeypatch.setattr(adapter_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_pinned)
    monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", True)
    recorded_keys = []
    built_client = _FakeClient()
    context = _make_context(
        pool_key_factory=lambda url, ip, host, authority: recorded_keys.append((url, ip, host, authority)) or (url, ip),
        pinned_client_builder=lambda: built_client,
    )
    await HttpProtocolAdapter().invoke(_make_operation(), {"a": 1}, context)
    _, called_url, kwargs = built_client.calls[0][1:4]
    assert called_url == "https://10.0.0.5/data"
    assert kwargs["headers"]["Host"] == "api.example.com:8443"
    assert kwargs["extensions"]["sni_hostname"] == "api.example.com"
    assert recorded_keys == [("https://10.0.0.5/data", "10.0.0.5", "api.example.com", "api.example.com:8443")]


async def test_pinning_replaces_existing_host_header(monkeypatch):
    """A pre-existing Host header is dropped before the pinned one is set."""

    async def _validate_pinned(url, label):
        return {"resolved_ip": "10.0.0.5", "hostname": "api.example.com", "original_authority": "api.example.com:8443"}

    monkeypatch.setattr(adapter_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_pinned)
    monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", True)
    built_client = _FakeClient()
    context = _make_context(headers={"Host": "evil.example.com"}, pinned_client_builder=lambda: built_client)
    await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    _, _, kwargs = built_client.calls[0][1:4]
    assert kwargs["headers"]["Host"] == "api.example.com:8443"
    assert not any(k.lower() == "host" and v == "evil.example.com" for k, v in kwargs["headers"].items())


async def test_pinned_pool_acquire_and_release(monkeypatch):
    """With the pool enabled, a pinned request acquires and releases an entry."""

    async def _validate_pinned(url, label):
        return {"resolved_ip": "10.0.0.5", "hostname": "api.example.com", "original_authority": "api.example.com:8443"}

    monkeypatch.setattr(adapter_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_pinned)
    monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", True)
    monkeypatch.setattr(adapter_module.settings, "mcpgateway_rest_client_pool_enabled", True)
    pool = _FakePool()
    context = _make_context(pinned_rest_pool=pool)
    await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    assert len(pool.acquired) == 1
    key, entry = pool.acquired[0]
    assert key[0] == "https://10.0.0.5/data"
    assert pool.released == [entry]
    assert pool.client.calls  # the pooled client performed the request


async def test_pinned_client_builder_used_and_closed_when_pool_disabled(monkeypatch):
    """With the pool disabled, a fresh pinned client is built and closed."""

    async def _validate_pinned(url, label):
        return {"resolved_ip": "10.0.0.5", "hostname": "api.example.com", "original_authority": "api.example.com:8443"}

    monkeypatch.setattr(adapter_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_pinned)
    monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", True)
    built_client = _FakeClient()
    context = _make_context(pinned_client_builder=lambda: built_client)
    await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    assert built_client.calls
    assert built_client.aclosed is True


async def test_pool_release_happens_even_on_send_timeout(monkeypatch):
    """A send timeout still releases the pinned pool entry."""

    async def _validate_pinned(url, label):
        return {"resolved_ip": "10.0.0.5", "hostname": "api.example.com", "original_authority": "api.example.com:8443"}

    monkeypatch.setattr(adapter_module.SecurityValidator, "validate_url_for_connection_pinning", _validate_pinned)
    monkeypatch.setattr(adapter_module.settings, "ssrf_protection_enabled", True)
    monkeypatch.setattr(adapter_module.settings, "mcpgateway_rest_client_pool_enabled", True)
    pool = _FakePool()
    context = _make_context(pinned_rest_pool=pool)

    async def _fail_send(send, call_headers):
        raise asyncio.TimeoutError()

    context.send_with_retry = _fail_send
    with pytest.raises(ProtocolError) as exc_info:
        await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    assert exc_info.value.code == REST_SEND_TIMEOUT
    assert len(pool.released) == 1
    assert exc_info.value.details["elapsed_ms"] >= 0


# ── Timeout / retry wiring ─────────────────────────────────────────────────


async def test_send_asyncio_timeout_raises_send_timeout_with_elapsed_ms():
    """An asyncio timeout during send raises REST_SEND_TIMEOUT with elapsed_ms."""
    context = _make_context()

    async def _fail_send(send, call_headers):
        raise asyncio.TimeoutError()

    context.send_with_retry = _fail_send
    with pytest.raises(ProtocolError, match="Tool invocation timed out after 30.0s") as exc_info:
        await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    error = exc_info.value
    assert error.code == REST_SEND_TIMEOUT
    assert error.category is ErrorCategory.UNAVAILABLE
    assert error.details["elapsed_ms"] >= 0


async def test_send_httpx_timeout_raises_send_timeout():
    """httpx.TimeoutException maps to the same REST_SEND_TIMEOUT code."""
    context = _make_context()

    async def _fail_send(send, call_headers):
        raise httpx.TimeoutException("read timeout")

    context.send_with_retry = _fail_send
    with pytest.raises(ProtocolError) as exc_info:
        await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    assert exc_info.value.code == REST_SEND_TIMEOUT


async def test_budget_exhaustion_propagates_unconverted():
    """A ToolTimeoutError from remaining_timeout passes through untouched."""
    context = _make_context(remaining_timeout=_raise_budget_exceeded)
    with pytest.raises(_BudgetExceeded, match="budget gone"):
        await HttpProtocolAdapter().invoke(_make_operation(), {}, context)


async def test_token_exchange_retry_receives_send_closure_and_headers():
    """send_with_retry is wired with the _send closure and final headers."""
    recorded = []
    context = _make_context()

    async def _recorder(send, call_headers):
        recorded.append((callable(send), call_headers))
        return await send(call_headers)

    context.send_with_retry = _recorder
    await HttpProtocolAdapter().invoke(_make_operation(), {"a": 1}, context)
    assert recorded[0][0] is True
    assert recorded[0][1] is context.headers
    assert context.http_client.calls[0][3]["json"] == {"a": 1}


# ── Response classification ────────────────────────────────────────────────


async def test_error_status_uses_error_field_message():
    """A 400 with {"error": "boom"} yields message 'boom' and status 400."""
    context = _make_context(http_client=_FakeClient(response=_FakeResponse(status_code=400, json_data={"error": "boom"})))
    with pytest.raises(ProtocolError, match="boom") as exc_info:
        await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    error = exc_info.value
    assert error.code == REST_HTTP_STATUS_ERROR
    assert error.category is ErrorCategory.UPSTREAM_ERROR
    assert error.protocol_status == 400


async def test_error_status_uses_response_text_when_not_json():
    """A 500 HTML body yields 'HTTP 500: <text>' via the parse fallback."""
    context = _make_context(http_client=_FakeClient(response=_FakeResponse(status_code=500, text="gateway down", json_error=json.JSONDecodeError("x", "d", 0))))
    with pytest.raises(ProtocolError, match="HTTP 500: gateway down") as exc_info:
        await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    assert exc_info.value.code == REST_HTTP_STATUS_ERROR


async def test_error_status_with_non_string_error_is_serialized():
    """A non-string error payload is JSON-serialized into the message."""
    context = _make_context(http_client=_FakeClient(response=_FakeResponse(status_code=400, json_data={"error": {"a": 1}})))
    with pytest.raises(ProtocolError, match='{"a":1}') as exc_info:
        await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    assert exc_info.value.message == '{"a":1}'


async def test_204_success_skips_body_parsing():
    """204 returns data=None without touching the (empty) body."""
    response = _FakeResponse(status_code=204, json_error=json.JSONDecodeError("x", "d", 0))
    context = _make_context(http_client=_FakeClient(response=response))
    result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    assert result.data is None
    assert result.metadata == {"status_code": 204}
    assert result.duration_ms >= 0
    assert response.json_calls == 0


async def test_203_success_falls_back_to_response_text():
    """203 (non-standard 2xx) is success; a non-JSON body falls back to text."""
    response = _FakeResponse(status_code=203, text="partial content", json_error=json.JSONDecodeError("x", "d", 0))
    context = _make_context(http_client=_FakeClient(response=response))
    result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    assert result.data == {"response_text": "partial content"}
    assert result.metadata == {"status_code": 203}


async def test_205_success_returns_none():
    """205 Reset Content is success with data=None (no body, design §9.8)."""
    response = _FakeResponse(status_code=205, json_data={"error": "partial"})
    context = _make_context(http_client=_FakeClient(response=response))
    result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    assert result.data is None
    assert result.metadata == {"status_code": 205}


@pytest.mark.parametrize("status_code", [200, 201, 202, 203, 206, 207])
async def test_success_statuses_return_parsed_data(status_code):
    """200/201/202/203/206/207 decode JSON into ProtocolResult.data (§9.8)."""
    response = _FakeResponse(status_code=status_code, json_data={"ok": True, "n": status_code})
    context = _make_context(http_client=_FakeClient(response=response))
    result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    assert result.data == {"ok": True, "n": status_code}
    assert result.metadata == {"status_code": status_code}
    assert result.duration_ms >= 0


async def test_success_json_parse_fallback_returns_response_text():
    """A 200 non-JSON body falls back to {'response_text': <text>}."""
    response = _FakeResponse(status_code=200, text="plain text", json_error=json.JSONDecodeError("x", "d", 0))
    context = _make_context(http_client=_FakeClient(response=response))
    result = await HttpProtocolAdapter().invoke(_make_operation(), {}, context)
    assert result.data == {"response_text": "plain text"}
    assert result.metadata == {"status_code": 200}


# ── Moved helpers ──────────────────────────────────────────────────────────


async def test_handle_json_parse_error_empty_body():
    """An empty body yields {'error': 'Empty response body'} with a warning."""
    response = _FakeResponse(status_code=200, text="")
    result = _handle_json_parse_error(response, json.JSONDecodeError("x", "d", 0))
    assert result == {"error": "Empty response body"}


async def test_handle_json_parse_error_truncates_long_text(monkeypatch):
    """Long bodies are truncated to the configured max length."""
    monkeypatch.setattr(adapter_module.settings, "rest_response_text_max_length", 5)
    response = _FakeResponse(status_code=200, text="abcdefgh")
    result = _handle_json_parse_error(response, json.JSONDecodeError("x", "d", 0))
    assert result == {"response_text": "abcde"}


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, ""),
        (True, "true"),
        (False, "false"),
        ({"a": 1}, '{"a":1}'),
        ([1, 2], "[1,2]"),
        (3.14, "3.14"),
        ("x", "x"),
    ],
)
def test_form_value_to_str_coercions(value, expected):
    """_form_value_to_str mirrors the legacy form-value serialization."""
    assert _form_value_to_str(value) == expected
