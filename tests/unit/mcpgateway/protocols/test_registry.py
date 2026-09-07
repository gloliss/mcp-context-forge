# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/test_registry.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the ProtocolRegistry (PR1).
"""

# Standard
from types import SimpleNamespace

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.http.adapter import HttpProtocolAdapter
from mcpgateway.protocols.models import ErrorCategory, ProtocolError, ProtocolResult
from mcpgateway.protocols.registry import ProtocolRegistry, build_default_protocol_registry, protocol_registry


class _RecordingAdapter:
    """Minimal adapter recording invocations for delegation assertions."""

    def __init__(self) -> None:
        """Initialise with an empty invocation record."""
        self.invocations = []

    async def invoke(self, operation, arguments, context):
        """Record and return a fixed successful result."""
        self.invocations.append((operation, arguments, context))
        return ProtocolResult(data={"ok": True})


def test_register_and_get_roundtrip():
    """A registered adapter is returned by get() under its protocol name."""
    registry = ProtocolRegistry()
    adapter = _RecordingAdapter()
    registry.register("http", adapter)
    assert registry.get("http") is adapter


def test_register_replaces_existing_adapter():
    """Registering a second adapter for a protocol replaces the first."""
    registry = ProtocolRegistry()
    registry.register("http", _RecordingAdapter())
    replacement = _RecordingAdapter()
    registry.register("http", replacement)
    assert registry.get("http") is replacement


def test_get_unknown_protocol_raises_unsupported_protocol_error():
    """get() raises a ProtocolError with UNSUPPORTED category for unknown protocols."""
    registry = ProtocolRegistry()
    with pytest.raises(ProtocolError, match="Protocol 'grpc' is not registered") as exc_info:
        registry.get("grpc")
    assert exc_info.value.category is ErrorCategory.UNSUPPORTED
    assert exc_info.value.code == "UNSUPPORTED_PROTOCOL"


async def test_invoke_delegates_to_the_registered_adapter():
    """invoke() forwards operation/arguments/context to the adapter and returns its result."""
    registry = ProtocolRegistry()
    adapter = _RecordingAdapter()
    registry.register("http", adapter)
    operation = SimpleNamespace(key="http:rest:tool-1")
    arguments = {"a": 1}
    context = SimpleNamespace(tool_name="demo")
    result = await registry.invoke("http", operation, arguments, context)
    assert result.data == {"ok": True}
    assert adapter.invocations == [(operation, arguments, context)]


async def test_invoke_unknown_protocol_raises_unsupported_protocol_error():
    """invoke() surfaces the registry's UNSUPPORTED ProtocolError."""
    registry = ProtocolRegistry()
    with pytest.raises(ProtocolError, match="not registered"):
        await registry.invoke("grpc", None, {}, SimpleNamespace(tool_name="demo"))


async def test_invoke_propagates_adapter_protocol_errors():
    """Adapter ProtocolErrors pass through invoke() unchanged."""
    registry = ProtocolRegistry()

    class _FailingAdapter:
        """Adapter that always raises a structured protocol error."""

        async def invoke(self, operation, arguments, context):
            """Raise a REST_HTTP_STATUS_ERROR-style failure."""
            raise ProtocolError(
                category=ErrorCategory.UPSTREAM_ERROR,
                code="REST_HTTP_STATUS_ERROR",
                message="HTTP 502",
                origin="http",
                retryable=False,
                protocol_status=502,
            )

    registry.register("http", _FailingAdapter())
    with pytest.raises(ProtocolError, match="HTTP 502") as exc_info:
        await registry.invoke("http", None, {}, SimpleNamespace(tool_name="demo"))
    assert exc_info.value.protocol_status == 502


def test_build_default_protocol_registry_registers_http_adapter():
    """The default registry ships with the PR1 HTTP adapter registered."""
    registry = build_default_protocol_registry()
    assert isinstance(registry.get("http"), HttpProtocolAdapter)


def test_module_level_singleton_has_http_registered():
    """The module singleton used by ToolService has 'http' registered."""
    assert isinstance(protocol_registry.get("http"), HttpProtocolAdapter)
