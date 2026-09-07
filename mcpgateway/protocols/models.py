# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/models.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Protocol Runtime data models (PR1).

These are the transport-agnostic value objects shared by ToolService and
every protocol adapter: the invocation result, the structured error model,
and the per-invocation context carrying ToolService-owned infrastructure
into the adapter without importing the service layer.
"""

# Standard
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional


class ErrorCategory(str, Enum):
    """[CF] Error Model categories shared by all protocol adapters."""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    FAILED_PRECONDITION = "FAILED_PRECONDITION"
    RATE_LIMITED = "RATE_LIMITED"
    UNSUPPORTED = "UNSUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INTERNAL = "INTERNAL"


@dataclass
class ProtocolResult:
    """Successful outcome of one protocol adapter invocation.

    Attributes:
        data: The decoded response payload (e.g. parsed JSON, or ``None``
            for a 204-style empty success).
        metadata: Protocol-specific response metadata (e.g. HTTP status code).
        bytes_in: Bytes received from the upstream transport (0 when not tracked).
        bytes_out: Bytes sent to the upstream transport (0 when not tracked).
        duration_ms: Wall-clock duration of the transport exchange.
        truncated: Whether the decoded payload was truncated by the transport layer.
    """

    data: Any
    metadata: Dict[str, Any] = field(default_factory=dict)
    bytes_in: int = 0
    bytes_out: int = 0
    duration_ms: float = 0
    truncated: bool = False


@dataclass
class ProtocolError(Exception):
    """Structured error raised by protocol adapters ([CF] Error Model).

    Attributes:
        category: One of the eleven [CF] Error Model categories.
        code: Protocol-specific machine-readable error code.
        message: Human-readable message; used verbatim by ToolService when
            converting back to ``ToolInvocationError``/``ToolResult`` so
            pre-existing error texts survive the extraction unchanged.
        origin: Name of the raising protocol (e.g. ``"http"``).
        retryable: Whether a same-argument retry could plausibly succeed.
        protocol_status: Upstream transport status (e.g. HTTP status code).
        trace_id: Optional trace identifier (unused in PR1).
        details: Free-form context needed by ToolService to restore
            pre-existing side effects (log replays, elapsed durations).
            PR1 addition beyond the design-document field list, required
            for byte-for-byte behaviour preservation.
    """

    category: ErrorCategory
    code: str
    message: str
    origin: str
    retryable: bool
    protocol_status: Optional[str | int] = None
    trace_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        """Initialise the Exception base so tracebacks display the message."""
        Exception.__init__(self, self.message)


@dataclass
class InvocationContext:
    """Per-invocation runtime context injected by ToolService.

    Everything the service layer owns — HTTP clients, the SSRF pinned
    client pool, mapping helpers, telemetry factories — is passed here at
    invoke time instead of being imported by the protocol layer.  This
    keeps ``mcpgateway.protocols`` free of any dependency on
    ``mcpgateway.services.tool_service`` and preserves monkeypatch
    compatibility for the existing test suite, which patches names in the
    ``tool_service`` module namespace.

    Attributes:
        tool_name: Current tool name (post TOOL_PRE_INVOKE modification).
        tool_name_computed: Original computed tool name (log identity).
        tool_id: Tool database identifier (may be an empty string).
        effective_timeout: Resolved end-to-end invocation deadline, seconds.
        remaining_timeout: Callable returning the remaining budget seconds;
            raises ``ToolTimeoutError`` when the budget is exhausted.
        http_client: Shared outbound HTTP client (non-pinned requests).
        headers: The in-flight request headers; the HTTP adapter mutates
            this dict in place (mapping merge, pinned ``Host``) exactly as
            the legacy REST branch did with its local ``headers`` variable.
        send_with_retry: ``(send, call_headers) -> response`` wrapper that
            applies the ToolService token-exchange single-retry policy (B2).
        pinned_rest_pool: Reference-counted LRU pool for SSRF-pinned clients.
        pinned_client_builder: Factory for a fresh pinned client when the
            pool is disabled.
        pool_key_factory: Computes the pinned pool isolation key.
        apply_mapping: ``apply_mapping_into_target`` from the service layer.
        validate_header_mapping_targets: Header mapping target validator.
        invalid_header_value_chars: Compiled regex of illegal header chars.
        child_span_factory: ``create_child_span`` context-manager factory.
    """

    tool_name: str
    tool_name_computed: str
    tool_id: str
    effective_timeout: float
    remaining_timeout: Callable[[], float]
    http_client: Any
    headers: Dict[str, Any] = field(default_factory=dict)
    send_with_retry: Optional[Callable[[Callable[[dict], Awaitable[Any]], dict], Awaitable[Any]]] = None
    pinned_rest_pool: Any = None
    pinned_client_builder: Optional[Callable[[], Any]] = None
    pool_key_factory: Optional[Callable[..., Any]] = None
    apply_mapping: Optional[Callable[..., dict]] = None
    validate_header_mapping_targets: Optional[Callable[..., None]] = None
    invalid_header_value_chars: Any = None
    child_span_factory: Optional[Callable[..., Any]] = None
