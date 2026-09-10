# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/http/activation_gate.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

HTTP contract-test activation gate (PR8, design-document §59).

The gate decides whether an operation may be exercised by contract tests
(``[ST] Schemathesis``) when an HTTP service is activated:

* ``off`` — never run contract tests;
* ``warn`` — run, but a mutating operation is skipped unless explicitly
  allowed (warnings surface as diagnostics, activation is not blocked);
* ``strict`` — run, and refuse (skip) mutating operations unless
  explicitly allowed.

Only ``GET``/``HEAD``/``OPTIONS`` are safe to invoke automatically.
``POST``/``PUT``/``PATCH``/``DELETE`` require
``testing.allowMutatingOperations: true`` (design §59).

This module owns only the *policy* (configuration parsing helpers and the
decision function); the actual Schemathesis execution lives with the
contract-test suite (design §58).
"""

# Standard
from typing import Literal

# Recognised gate modes.
ACTIVATION_GATES: tuple[str, ...] = ("off", "warn", "strict")
DEFAULT_ACTIVATION_GATE = "warn"

# HTTP methods safe to exercise automatically.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

# Decision values returned by :func:`evaluate_activation_gate`.
GateDecision = Literal["off", "run", "warn", "skip"]


def evaluate_activation_gate(gate: str, method: str, allow_mutating: bool = False) -> GateDecision:
    """Decide whether a contract test may run for one operation (design §59).

    Args:
        gate: The configured gate mode (``off``/``warn``/``strict``).
        method: The HTTP method of the operation.
        allow_mutating: Whether ``testing.allowMutatingOperations`` is set.

    Returns:
        ``"off"`` when the gate disables testing entirely; ``"run"`` when the
        operation may be exercised; ``"warn"`` when a mutating operation is
        allowed only because the gate is ``warn`` (caller should annotate);
        ``"skip"`` when a mutating operation is refused.
    """
    normalized_gate = (gate or DEFAULT_ACTIVATION_GATE).strip().lower()
    normalized_method = (method or "").strip().upper()

    if normalized_gate == "off":
        return "off"
    if normalized_method in SAFE_METHODS or allow_mutating:
        return "run"
    if normalized_gate == "warn":
        return "warn"
    # strict (or unknown treated conservatively): refuse mutating ops.
    return "skip"


__all__ = ["ACTIVATION_GATES", "DEFAULT_ACTIVATION_GATE", "SAFE_METHODS", "evaluate_activation_gate"]
