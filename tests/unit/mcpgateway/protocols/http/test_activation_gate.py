# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_activation_gate.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the contract-test Activation Gate (PR8, design §59).
"""

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.http.activation_gate import (
    ACTIVATION_GATES,
    DEFAULT_ACTIVATION_GATE,
    SAFE_METHODS,
    evaluate_activation_gate,
)


class TestActivationGateConstants:
    """Gate modes and safe methods (design §59)."""

    def test_gate_modes(self):
        """off/warn/strict are the recognised modes; default is warn."""
        assert ACTIVATION_GATES == ("off", "warn", "strict")
        assert DEFAULT_ACTIVATION_GATE == "warn"

    def test_safe_methods(self):
        """GET/HEAD/OPTIONS are safe; mutating methods are not."""
        assert SAFE_METHODS == {"GET", "HEAD", "OPTIONS"}


class TestEvaluateActivationGate:
    """Decision function for one operation (design §59)."""

    def test_off_disables_all(self):
        """gate=off returns off regardless of method."""
        assert evaluate_activation_gate("off", "GET") == "off"
        assert evaluate_activation_gate("off", "POST", allow_mutating=True) == "off"

    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    def test_safe_methods_run_under_warn_and_strict(self, method):
        """Safe methods run under warn and strict."""
        assert evaluate_activation_gate("warn", method) == "run"
        assert evaluate_activation_gate("strict", method) == "run"

    def test_mutating_skipped_when_not_allowed(self):
        """A mutating op is refused without allowMutatingOperations."""
        assert evaluate_activation_gate("strict", "POST") == "skip"
        # warn surfaces a warning decision instead of a hard skip.
        assert evaluate_activation_gate("warn", "POST") == "warn"

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_mutating_runs_when_explicitly_allowed(self, method):
        """allowMutatingOperations=true lets mutating ops run."""
        assert evaluate_activation_gate("warn", method, allow_mutating=True) == "run"
        assert evaluate_activation_gate("strict", method, allow_mutating=True) == "run"

    def test_method_and_gate_are_normalized(self):
        """Lowercase method / mixed-case gate are normalized."""
        assert evaluate_activation_gate("WARN", "get") == "run"
        assert evaluate_activation_gate("", "GET") == "run"

    def test_unknown_gate_is_conservative_for_mutating(self):
        """An unrecognised gate refuses mutating ops (fail-closed)."""
        assert evaluate_activation_gate("bogus", "POST") == "skip"
