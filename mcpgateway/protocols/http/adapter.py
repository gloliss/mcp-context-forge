# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/http/adapter.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

HTTP protocol adapter — legacy REST runtime extraction (PR1) plus the
PR2 protocol_config runtime.

PR1 extracted the invocation-time REST logic previously inlined in
``ToolService.invoke_tool`` (URL template substitution, query/header
mapping, SSRF validation + connection pinning, transport send, response
classification).  That path — the "legacy" path — is preserved
byte-for-byte for tools with no ``protocol_config`` (design §9.11: a
``NULL`` ``protocol_config`` continues through
``LegacyRestContractBuilder``).

PR2 adds the new path taken when ``InvocationContext.protocol_config`` is
set: request assembly via ``RequestBuilder`` (path/query/header/cookie/body
independent, design §9.6), per-hop SSRF re-validation and manual redirect
following via ``RedirectSecurity`` (design §9.10 — ``follow_redirects`` is
never enabled), and response decoding via ``ResponseDecoder`` (Content-Type
→ codec, design §9.7/§70).  Success is now ``200 <= status_code < 300``
(design §9.8); 204/205/HEAD return ``data=None``.

ToolService-owned infrastructure (pinned client pool, token-exchange
retry, mapping helpers, telemetry factories) is injected per invocation
via :class:`mcpgateway.protocols.models.InvocationContext`; this module
imports nothing from ``mcpgateway.services.tool_service`` so the service
layer can import the registry without a cycle, and the existing test
suite's monkeypatches on the ``tool_service`` module namespace keep
working through the invoke-time bindings.

The two ``logger.warning`` statements of the legacy SSRF paths are
replayed by ToolService (not logged here) so their exact log identity is
kept; the raw URL is carried in ``ProtocolError.details`` for that
replay.
"""

# Standard
import asyncio
import json  # NOTE: httpx uses stdlib json, not orjson, so response.json() raises json.JSONDecodeError
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

# Third-Party
import httpx
import orjson

# First-Party
from mcpgateway.common.validators import pin_url_to_resolved_ip, SecurityValidator
from mcpgateway.config import settings
from mcpgateway.protocols.base import ProtocolAdapter
from mcpgateway.protocols.codecs import codec_registry
from mcpgateway.protocols.codecs.base import CodecContext
from mcpgateway.protocols.http.redirect import REST_TOO_MANY_REDIRECTS, RedirectSecurity
from mcpgateway.protocols.http.request_builder import RequestBuilder
from mcpgateway.protocols.http.response_decoder import ResponseDecoder
from mcpgateway.protocols.models import ErrorCategory, InvocationContext, ProtocolError, ProtocolResult
from mcpgateway.utils.retry_manager import ResilientHttpClient

logger = logging.getLogger(__name__)

# ── [CF] Error Model codes for the legacy REST paths ───────────────────────
REST_MISSING_URL_PARAM = "REST_MISSING_URL_PARAM"
REST_QUERY_MAPPING_NON_SCALAR = "REST_QUERY_MAPPING_NON_SCALAR"
REST_HEADER_MAPPING_ILLEGAL_CHARS = "REST_HEADER_MAPPING_ILLEGAL_CHARS"
REST_URL_VALIDATION_FAILED = "REST_URL_VALIDATION_FAILED"
REST_URL_PINNING_MISSING = "REST_URL_PINNING_MISSING"
REST_URL_VALIDATION_TIMEOUT = "REST_URL_VALIDATION_TIMEOUT"
REST_SEND_TIMEOUT = "REST_SEND_TIMEOUT"
REST_HTTP_STATUS_ERROR = "REST_HTTP_STATUS_ERROR"
REST_UNEXPECTED_STATUS = "REST_UNEXPECTED_STATUS"


def _handle_json_parse_error(response: Any, error: Any, is_error_response: bool = False) -> dict:
    """Handle JSON parsing failures with graceful fallback to raw text.

    Args:
        response: The HTTP response object with .text attribute
        error: The exception that was raised during JSON parsing
        is_error_response: If True, logs as "error response", else "response"

    Returns:
        Dictionary with response_text key containing the raw response text
        (truncated to REST_RESPONSE_TEXT_MAX_LENGTH if longer to avoid exposing sensitive data),
        or error details if response body is empty/None
    """
    msg = "error response" if is_error_response else "response"
    if not response.text:
        logger.warning("Failed to parse JSON %s: %s. Response body was empty.", msg, error)
        return {"error": "Empty response body"}

    max_length = settings.rest_response_text_max_length
    text = response.text[:max_length] if len(response.text) > max_length else response.text
    if len(response.text) > max_length:
        logger.warning("Failed to parse JSON %s: %s. Response truncated from %s to %s characters.", msg, error, len(response.text), max_length)
    else:
        logger.warning("Failed to parse JSON %s: %s", msg, error)
    return {"response_text": text}


def _form_value_to_str(v: Any) -> str:
    """Coerce a payload value to string for form/multipart encoding."""
    if v is None:
        return ""
    if isinstance(v, (dict, list, bool)):
        return orjson.dumps(v).decode()
    return str(v)


class HttpProtocolAdapter(ProtocolAdapter):
    """Execute HTTP operations over the legacy and protocol_config paths.

    When ``InvocationContext.protocol_config`` is ``None`` (legacy REST
    tools) the adapter runs the extracted PR1 body of the old
    ``if tool_integration_type == "REST":`` branch unchanged.  When
    ``protocol_config`` is set (PR2) it assembles the request with
    ``RequestBuilder``, follows redirects manually with per-hop SSRF
    re-validation (``RedirectSecurity``), and decodes the response with
    ``ResponseDecoder``.
    """

    async def invoke(
        self,
        operation: Any,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> ProtocolResult:
        """Execute an HTTP operation.

        Args:
            operation: ``OperationDefinition`` whose ``request`` carries
                url, method, query_mapping, and header_mapping.
            arguments: Invocation arguments; URL-template parameters are
                popped from a copy, never from the caller's dict.
            context: ToolService-injected runtime context.

        Returns:
            ``ProtocolResult`` for ``200 <= status_code < 300`` (data =
            decoded body) and ``data=None`` for 204/205/HEAD successes.

        Raises:
            ProtocolError: For argument, URL-policy, redirect-budget,
                timeout, and upstream status failures.  Timeout and
                URL-policy codes carry ``details`` consumed by ToolService
                to replay the legacy logs/metrics exactly.
        """
        if context.protocol_config is not None:
            return await self._invoke_protocol_config(operation, arguments, context)
        return await self._invoke_legacy(operation, arguments, context)

    async def _invoke_protocol_config(
        self,
        operation: Any,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> ProtocolResult:
        """Run the PR2 protocol_config path (§9.6/§9.7/§9.10).

        Args:
            operation: ``OperationDefinition`` (request carries the base
                ``url``/``base_url`` used to resolve the request path).
            arguments: Invocation arguments.
            context: Runtime context with a non-``None``
                ``protocol_config``.

        Returns:
            ``ProtocolResult`` decoded via ``ResponseDecoder``.

        Raises:
            ProtocolError: ``REST_TOO_MANY_REDIRECTS`` when the hop budget
                is exhausted, plus the shared URL-policy/timeout codes.
        """
        config = context.protocol_config or {}
        request_config = config.get("request") or {}

        # Resolve the full request URL from the tool base URL + rendered path.
        base_url = operation.request.get("url") or operation.request.get("base_url") or ""
        built = RequestBuilder(codec_registry).build(arguments, request_config)
        final_url = RedirectSecurity.resolve_absolute(built.url_path, base_url)

        redirect_security = RedirectSecurity()
        response = None
        rest_start_time = time.time()
        current_url = final_url
        hops = 0

        # Validate + pin the first hop, then send, following redirects
        # manually so every hop re-runs SSRF validation (§9.10).
        while True:
            target = await redirect_security.validate_and_pin(current_url, context)
            hop_headers = {hk: hv for hk, hv in context.headers.items() if hk.lower() != "host"}
            hop_headers.update(target.headers)
            hop_headers.update(built.headers)

            request_options: dict[str, Any] = {
                "cookies": built.cookies or None,
                "follow_redirects": False,
            }
            if target.extensions:
                request_options["extensions"] = target.extensions
            if built.query_params:
                request_options["params"] = built.query_params

            body_kwargs = self._body_kwargs(built.body)
            try:
                response = await asyncio.wait_for(
                    context.send_with_retry(
                        lambda call_headers, _url=target.url: self._send_new(
                            context, built.method, _url, call_headers, request_options, body_kwargs
                        ),
                        hop_headers,
                    ),
                    timeout=context.remaining_timeout(),
                )
            except (asyncio.TimeoutError, httpx.TimeoutException):
                rest_elapsed_ms = (time.time() - rest_start_time) * 1000
                raise ProtocolError(
                    category=ErrorCategory.UNAVAILABLE,
                    code=REST_SEND_TIMEOUT,
                    message=f"Tool invocation timed out after {context.effective_timeout}s",
                    origin="http",
                    retryable=True,
                    details={"elapsed_ms": rest_elapsed_ms},
                )

            if not redirect_security.is_redirect(response.status_code):
                break

            # A redirect hop: resolve Location and re-validate on the next
            # iteration, bounded by gateway_max_redirects (§9.10).
            location = redirect_security.parse_location(response.headers)
            if not location:
                break
            current_url = redirect_security.resolve_absolute(location, current_url)
            hops += 1
            if hops > redirect_security.max_hops:
                raise ProtocolError(
                    category=ErrorCategory.UPSTREAM_ERROR,
                    code=REST_TOO_MANY_REDIRECTS,
                    message=f"Too many redirects (exceeded {redirect_security.max_hops} hops)",
                    origin="http",
                    retryable=False,
                    protocol_status=response.status_code,
                )

        decoder = ResponseDecoder(codec_registry)
        content_type = response.headers.get("content-type")
        payload = response.content
        codec_context = CodecContext(
            protocol_config=config,
            preferred_media_types=tuple((config.get("response") or {}).get("preferredMediaTypes") or ()),
            max_response_bytes=settings.rest_response_text_max_length,
        )
        decoded = decoder.decode(response.status_code, content_type, payload, codec_context)
        return ProtocolResult(
            data=decoded.data,
            metadata={"status_code": response.status_code, "content_type": decoded.codec_media_type},
            duration_ms=(time.time() - rest_start_time) * 1000,
        )

    @staticmethod
    def _body_kwargs(body: Any) -> dict[str, Any]:
        """Map an ``EncodedBody`` to HTTPX body kwargs.

        Args:
            body: The encoded body, or ``None`` for bodyless methods.

        Returns:
            A dict of HTTPX body keyword arguments (may be empty).
        """
        if body is None:
            return {}
        return {body.mode: body.value}

    async def _send_new(
        self,
        context: InvocationContext,
        method: str,
        url: str,
        call_headers: dict,
        options: dict[str, Any],
        body_kwargs: dict[str, Any],
    ) -> Any:
        """Issue one protocol_config-path hop with the given headers.

        Args:
            context: The invocation context (supplies the HTTP client).
            method: Uppercase HTTP method.
            url: The validated/pinned hop URL.
            call_headers: The merged outbound headers (B2 retry hook input).
            options: Request options (extensions, cookies, params, headers).
            body_kwargs: HTTPX body keyword arguments.

        Returns:
            The HTTPX response for this hop.
        """
        client = context.http_client
        # The B2 retry hook may replace ``call_headers`` (token exchange), so
        # the headers are applied here rather than baked into ``options``.
        request_options = dict(options)
        request_options["headers"] = call_headers
        return await client.request(method, url, **body_kwargs, **request_options)

    async def _invoke_legacy(
        self,
        operation: Any,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> ProtocolResult:
        """Run the extracted PR1 legacy path (no ``protocol_config``).

        Args:
            operation: ``OperationDefinition`` whose ``request`` carries
                url, method, query_mapping, and header_mapping.
            arguments: Invocation arguments; URL-template parameters are
                popped from a copy, never from the caller's dict.
            context: ToolService-injected runtime context.

        Returns:
            ``ProtocolResult`` for ``200 <= status_code < 300`` (data =
            parsed body) and ``data=None`` for 204/205/HEAD successes.

        Raises:
            ProtocolError: For argument, URL-policy, timeout, and upstream
                status failures.  Timeout and URL-policy codes carry
                ``details`` consumed by ToolService to replay the legacy
                logs/metrics exactly.
        """
        payload = arguments.copy()
        headers = context.headers

        # Handle URL path and query parameter substitution (using local variable)
        final_url = operation.request["url"]
        if "{" in final_url and "}" in final_url:
            # Extract ALL parameters (path and query) from URL template
            url_params = re.findall(r"\{(\w+)\}", final_url)
            url_substitutions = {}

            for param in url_params:
                if param in payload:
                    url_substitutions[param] = payload.pop(param)  # Remove from payload
                    final_url = final_url.replace(f"{{{param}}}", str(url_substitutions[param]))
                else:
                    raise ProtocolError(
                        category=ErrorCategory.INVALID_ARGUMENT,
                        code=REST_MISSING_URL_PARAM,
                        message=f"Required URL parameter '{param}' not found in arguments",
                        origin="http",
                        retryable=False,
                    )

        tool_query_mapping = operation.request.get("query_mapping")
        tool_header_mapping = operation.request.get("header_mapping")

        # --- Extract query params from URL if query_mapping or header_mapping is used ---
        # When mappings are present (not None/empty), we strip query params from URL and apply transformations.
        # When mappings are absent (None/empty), preserve query params in URL for signed URLs.
        query_params = {}
        # Treat empty dict same as None (no mapping configured)
        has_query_mapping = tool_query_mapping is not None and tool_query_mapping != {}
        has_header_mapping = tool_header_mapping is not None and tool_header_mapping != {}

        if has_query_mapping or has_header_mapping:
            parsed = urlparse(final_url)
            final_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            query_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

            if tool_query_mapping:
                # Only mapped payload keys (renamed) are kept, merged on top of URL query params.
                # Unmapped payload keys are intentionally dropped (mapping acts as an allowlist).
                payload = context.apply_mapping(payload, tool_query_mapping, query_params)
                # Reject non-scalar values that would be inappropriate as query parameters.
                for qk, qv in payload.items():
                    if isinstance(qv, (dict, list)):
                        raise ProtocolError(
                            category=ErrorCategory.INVALID_ARGUMENT,
                            code=REST_QUERY_MAPPING_NON_SCALAR,
                            message=f"Tool '{context.tool_name}': query_mapping produced non-scalar value for parameter '{qk}'",
                            origin="http",
                            retryable=False,
                        )

            # Headers are mapped from the original arguments (not the path-param-reduced payload)
            # to preserve all available data for header injection.
            if tool_header_mapping:
                context.validate_header_mapping_targets(tool_header_mapping, context.tool_name)
                headers = context.apply_mapping(arguments.copy(), tool_header_mapping, headers)
                # Reject header values containing CRLF or null bytes to prevent header injection.
                for hdr_name, hdr_val in headers.items():
                    if isinstance(hdr_val, str) and context.invalid_header_value_chars.search(hdr_val):
                        raise ProtocolError(
                            category=ErrorCategory.INVALID_ARGUMENT,
                            code=REST_HEADER_MAPPING_ILLEGAL_CHARS,
                            message=f"Tool '{context.tool_name}': header_mapping produced value with illegal characters for header '{hdr_name}'",
                            origin="http",
                            retryable=False,
                        )

        # Use the tool's request_type rather than defaulting to POST (using local variable)
        method = operation.request.get("method") or "POST"
        _url_query_params = query_params if not tool_query_mapping else None

        # Detect body encoding from the final Content-Type header (after auth/plugin/mapping modifications).
        # Supports application/x-www-form-urlencoded and multipart/form-data in addition to the default JSON.
        _ct_base = next((v for k, v in headers.items() if k.lower() == "content-type"), "").lower().split(";")[0].strip()

        # For non-GET form-urlencoded and multipart requests without mappings,
        # extract URL query params so they are forwarded via params= (query string)
        # rather than being silently embedded in the URL or lost.  GET has its own
        # extraction below; JSON POST intentionally preserves query params in the URL
        # for signed-URL support.
        if method != "GET" and not has_query_mapping and not has_header_mapping and _ct_base in ("application/x-www-form-urlencoded", "multipart/form-data"):
            parsed = urlparse(final_url)
            if parsed.query:
                final_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                _url_query_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        rest_request_extensions: dict[str, str] = {}
        rest_http_client = context.http_client
        pinned_rest_http_client: Optional[ResilientHttpClient] = None
        pinned_rest_pool_entry: Optional[Any] = None
        pinned_rest_key: Optional[tuple] = None
        try:
            validated_target = await asyncio.wait_for(
                SecurityValidator.validate_url_for_connection_pinning(final_url, "Tool URL"),
                timeout=context.remaining_timeout(),
            )
        except asyncio.TimeoutError as timeout_error:
            # NOTE: a ToolTimeoutError raised by remaining_timeout() itself
            # (budget exhaustion) propagates unchanged, exactly as before the
            # extraction — the message is already the legacy timeout text.
            raise ProtocolError(
                category=ErrorCategory.UNAVAILABLE,
                code=REST_URL_VALIDATION_TIMEOUT,
                message=f"Tool invocation timed out after {context.effective_timeout}s",
                origin="http",
                retryable=True,
            ) from timeout_error
        except ValueError as validation_error:
            # The legacy warning log is replayed by ToolService, which owns
            # the sanitized logging pipeline (raw_url is sanitized there).
            raise ProtocolError(
                category=ErrorCategory.PERMISSION_DENIED,
                code=REST_URL_VALIDATION_FAILED,
                message="Outbound URL blocked by URL policy",
                origin="http",
                retryable=False,
                details={"raw_url": final_url, "validation_error": str(validation_error)},
            ) from validation_error

        resolved_ip = validated_target.get("resolved_ip")
        original_hostname = validated_target.get("hostname")
        original_authority = validated_target.get("original_authority")
        if settings.ssrf_protection_enabled and not (resolved_ip and original_hostname and original_authority):
            raise ProtocolError(
                category=ErrorCategory.PERMISSION_DENIED,
                code=REST_URL_PINNING_MISSING,
                message="Outbound URL blocked by URL policy",
                origin="http",
                retryable=False,
                details={"raw_url": final_url},
            )
        if resolved_ip and original_hostname and original_authority:
            final_url = pin_url_to_resolved_ip(final_url, resolved_ip)
            headers = {hk: hv for hk, hv in headers.items() if hk.lower() != "host"}
            headers["Host"] = original_authority
            rest_request_extensions["sni_hostname"] = original_hostname
            pinned_rest_key = context.pool_key_factory(final_url, resolved_ip, original_hostname, original_authority)

        with context.child_span_factory("tool.gateway_call", {"tool.name": context.tool_name, "tool.id": context.tool_id, "tool.integration_type": "REST"}):
            rest_start_time = time.time()

            async def _send(call_headers: dict) -> Any:
                """Issue the REST upstream call with the given headers (B2 retry hook)."""
                request_options = {"headers": call_headers}
                if rest_request_extensions:
                    request_options["extensions"] = rest_request_extensions
                if method == "GET":
                    return await asyncio.wait_for(
                        rest_http_client.get(final_url, params=payload, **request_options),
                        timeout=context.remaining_timeout(),
                    )
                if _ct_base == "application/x-www-form-urlencoded":
                    # NOTE: Intentional asymmetry with the JSON/default path below.
                    # Form-encoded bodies use params= to keep URL query params on the
                    # query string (semantically correct for form encoding), whereas
                    # the JSON path merges them into the body via payload.update() for
                    # backward compatibility and signed-URL support.
                    form_payload = {k: _form_value_to_str(v) for k, v in payload.items()}
                    return await asyncio.wait_for(
                        rest_http_client.request(
                            method,
                            final_url,
                            data=form_payload,
                            params=_url_query_params,
                            **request_options,
                        ),
                        timeout=context.remaining_timeout(),
                    )
                if _ct_base == "multipart/form-data":
                    # Strip Content-Type so httpx can set it with the correct boundary parameter.
                    # URL query params forwarded via params= (same asymmetry as form-urlencoded above).
                    headers_mp = {k: v for k, v in call_headers.items() if k.lower() != "content-type"}
                    multipart_request_options = {"headers": headers_mp}
                    if rest_request_extensions:
                        multipart_request_options["extensions"] = rest_request_extensions
                    files_payload = {k: (None, _form_value_to_str(v)) for k, v in payload.items()}
                    return await asyncio.wait_for(
                        rest_http_client.request(
                            method,
                            final_url,
                            files=files_payload,
                            params=_url_query_params,
                            **multipart_request_options,
                        ),
                        timeout=context.remaining_timeout(),
                    )
                # For POST/PUT/PATCH/DELETE (JSON body, default path)
                return await asyncio.wait_for(
                    rest_http_client.request(method, final_url, json=payload, **request_options),
                    timeout=context.remaining_timeout(),
                )

            try:
                if method == "GET":
                    # For GET: Extract and merge URL query params with input arguments
                    if not has_query_mapping and not has_header_mapping:
                        # When no mappings (None or empty), extract query params from URL
                        parsed = urlparse(final_url)
                        final_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                        query_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

                        conflicts = set(payload.keys()) & set(query_params.keys())
                        if conflicts:
                            logger.warning(
                                "REST tool GET request has conflicting parameters between URL and input arguments. URL query params will take precedence for: %s. Tool: %s",
                                ", ".join(sorted(conflicts)),
                                context.tool_name,
                            )

                    payload.update(query_params)
                elif has_query_mapping or has_header_mapping:
                    # When mappings are used (not None/empty), query params were already extracted and mapped
                    # Merge them into the JSON body for backward compatibility with mapped tools
                    payload.update(query_params)
                # else: No mappings (None or empty) - preserve query params in URL for signed URL support
                # (Azure SAS, AWS presigned URLs, webhook signatures, etc.)

                if pinned_rest_key is not None:
                    if settings.mcpgateway_rest_client_pool_enabled:
                        pinned_rest_pool_entry = await context.pinned_rest_pool.acquire(pinned_rest_key)
                        rest_http_client = pinned_rest_pool_entry.client
                    else:
                        pinned_rest_http_client = context.pinned_client_builder()
                        rest_http_client = pinned_rest_http_client

                try:
                    # Bound the complete send/re-exchange/retry sequence,
                    # rather than granting each retry a fresh deadline.
                    response = await asyncio.wait_for(
                        context.send_with_retry(_send, headers),
                        timeout=context.remaining_timeout(),
                    )
                finally:
                    if pinned_rest_pool_entry is not None:
                        await context.pinned_rest_pool.release(pinned_rest_pool_entry)
                    elif pinned_rest_http_client is not None:
                        await pinned_rest_http_client.aclose()
            except (asyncio.TimeoutError, httpx.TimeoutException):
                rest_elapsed_ms = (time.time() - rest_start_time) * 1000
                # NOTE: the structured timeout log, timeout counter, and
                # post-invoke plugin hook are replayed by ToolService, which
                # owns them; elapsed_ms is carried here for that replay.
                raise ProtocolError(
                    category=ErrorCategory.UNAVAILABLE,
                    code=REST_SEND_TIMEOUT,
                    message=f"Tool invocation timed out after {context.effective_timeout}s",
                    origin="http",
                    retryable=True,
                    details={"elapsed_ms": rest_elapsed_ms},
                )

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                # Non-2xx response — parse body (may be HTML, plain text, XML, etc.)
                try:
                    result = response.json()
                except (json.JSONDecodeError, orjson.JSONDecodeError, UnicodeDecodeError, AttributeError) as e:
                    result = _handle_json_parse_error(response, e, is_error_response=True)
                if "error" in result:
                    error_val = result["error"]
                elif "response_text" in result:
                    error_val = f"HTTP {response.status_code}: {result['response_text']}"
                else:
                    error_val = f"HTTP {response.status_code}"
                raise ProtocolError(
                    category=ErrorCategory.UPSTREAM_ERROR,
                    code=REST_HTTP_STATUS_ERROR,
                    message=error_val if isinstance(error_val, str) else orjson.dumps(error_val).decode(),
                    origin="http",
                    retryable=False,
                    protocol_status=response.status_code,
                )

            # 204/205/HEAD successes carry no body; do not parse the empty
            # body — that would emit a spurious "Failed to parse JSON response"
            # warning (design §9.8).
            if response.status_code in (204, 205) or method == "HEAD":
                return ProtocolResult(data=None, metadata={"status_code": response.status_code}, duration_ms=(time.time() - rest_start_time) * 1000)

            try:
                result = response.json()
            except (json.JSONDecodeError, orjson.JSONDecodeError, UnicodeDecodeError, AttributeError) as e:
                result = _handle_json_parse_error(response, e, is_error_response=False)
            return ProtocolResult(
                data=result,
                metadata={"status_code": response.status_code},
                duration_ms=(time.time() - rest_start_time) * 1000,
            )
