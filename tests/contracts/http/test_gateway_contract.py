# -*- coding: utf-8 -*-
"""Location: ./tests/contracts/http/test_gateway_contract.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Live HTTP contract suite (PR8, design-document §58).

Runs the gateway's own OpenAPI document against a live deployment: every
operation the Activation Gate (§59) permits is exercised, and each response
is judged by the same primitives the unit suite covers
(:mod:`tests.contracts.http.contract_checks`).

The suite is **opt-in**.  Without ``CONTRACT_BASE_URL`` the operation list is
empty, so every test here is reported as skipped and nothing reaches the
network.  The E2E environment supplies the target:

    CONTRACT_BASE_URL=http://127.0.0.1:4444 \
    CONTRACT_OPENAPI_PATH=/openapi.json \
    pytest tests/contracts/http/test_gateway_contract.py

``CONTRACT_ACTIVATION_GATE`` (``off``/``warn``/``strict``) and
``CONTRACT_ALLOW_MUTATING`` mirror the manifest's
``spec.validation.activationGate`` and ``spec.testing.allowMutatingOperations``,
so the suite can be pointed at a service whose manifest opted into mutating
operations.  By default only GET/HEAD/OPTIONS are exercised (§82).
"""

# Standard
import os
from typing import Any, Dict, Optional

# Third-Party
# Third-Party
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
import httpx
import pytest

# First-Party
from tests.contracts.http.contract_checks import OperationCase, case_request, check_invalid_input_rejected, classify_response, sample_parameter_values
from tests.contracts.http.conftest import contract_base_url

_TIMEOUT = float(os.environ.get("CONTRACT_TIMEOUT", "10"))
_TOKEN = os.environ.get("CONTRACT_BEARER_TOKEN")

pytestmark = pytest.mark.contracts


def _absolute(path: str) -> str:
    """Join a document path onto the configured base URL.

    Path templates keep their ``{param}`` placeholders in the document; a
    probe substitutes a benign literal so the request reaches the router
    (and its validation layer) instead of 404ing on a literal brace.

    Args:
        path: The document path, possibly carrying placeholders.

    Returns:
        The absolute request URL.
    """
    resolved = path
    while "{" in resolved and "}" in resolved:
        start = resolved.index("{")
        end = resolved.index("}", start)
        resolved = f"{resolved[:start]}contract-probe{resolved[end + 1:]}"
    return f"{(contract_base_url() or '').rstrip('/')}{resolved}"


def _headers() -> Dict[str, str]:
    """Return the request headers for the contract suite.

    Returns:
        ``Authorization`` from ``CONTRACT_BEARER_TOKEN`` when configured,
        otherwise an empty mapping.
    """
    return {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}


def _decode(response: httpx.Response) -> Optional[Any]:
    """Decode a response body, returning ``None`` for bodyless or non-JSON payloads.

    Args:
        response: The observed response.

    Returns:
        The decoded body, or ``None``.
    """
    if response.status_code in (204, 205) or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _client() -> httpx.Client:
    """Build a non-redirecting client with the suite's timeout and headers."""
    return httpx.Client(timeout=_TIMEOUT, follow_redirects=False, headers=_headers())


def _valid_request(operation_case: OperationCase) -> Dict[str, Any]:
    """Build the request kwargs for a valid call to an operation.

    Sends a value for every declared query/header/cookie parameter so the
    gateway sees a request its own contract calls valid; a rejection of this
    request is then a real contract breach rather than a harness artefact.

    Args:
        operation_case: The operation to build a request for.

    Returns:
        Keyword arguments (``params``/``headers``) for ``httpx``.
    """
    query = sample_parameter_values(operation_case, location="query")
    headers = dict(_headers())
    headers.update({name: str(value) for name, value in sample_parameter_values(operation_case, location="header").items()})
    cookies = sample_parameter_values(operation_case, location="cookie")
    kwargs: Dict[str, Any] = {"headers": headers}
    if query:
        kwargs["params"] = query
    if cookies:
        kwargs["cookies"] = {name: str(value) for name, value in cookies.items()}
    return kwargs


def test_selected_operations_satisfy_their_contract(operation_case: OperationCase) -> None:
    """Every gate-permitted operation answers within its declared contract.

    Args:
        operation_case: One operation selected by the Activation Gate.
    """
    with _client() as client:
        response = client.request(operation_case.method, _absolute(operation_case.path), **_valid_request(operation_case))

    body = _decode(response)
    violations = classify_response(operation_case, response.status_code, body, response.headers.get("content-type"))

    assert not violations, "; ".join(violation.message for violation in violations)


def test_invalid_input_is_rejected(operation_case: OperationCase) -> None:
    """An operation with a required parameter rejects a request that omits it.

    This is the "invalid input" half of §58: a gateway that answers 2xx to a
    request missing a required parameter is not enforcing its own contract.
    Path parameters are excluded — their placeholder is always substituted,
    so there is no omission to test.  Operations without required
    query/header/cookie parameters have nothing invalid to send and skip.

    Args:
        operation_case: One operation selected by the Activation Gate.
    """
    required = [param for param in operation_case.parameters if param.get("required") and param.get("in") in ("query", "header", "cookie")]
    if not required:
        pytest.skip(f"{operation_case.label} declares no required non-path parameters")

    with _client() as client:
        response = client.request(operation_case.method, _absolute(operation_case.path))

    violation = check_invalid_input_rejected(response.status_code)
    assert violation is None, violation.message if violation else ""


@given(data=st.data())
@settings(max_examples=int(os.environ.get("CONTRACT_FUZZ_EXAMPLES", "10")), deadline=None, suppress_health_check=list(HealthCheck))
def test_fuzzed_values_satisfy_their_contract(fuzz_target: Any, data: st.DataObject) -> None:
    """Schema-generated values never produce a contract breach (§58).

    This is the fuzzing half of the contract suite: schemathesis derives the
    value space from the operation's own declaration, so the requests include
    the boundary and unusual values a hand-written probe never sends.

    The strategy is driven by Hypothesis rather than sampled by hand, so a
    failure shrinks to a minimal case and is replayed on the next run instead
    of being a one-off draw.

    Args:
        fuzz_target: A ``(operation, strategy)`` pair for one gated operation.
        data: Hypothesis' drawing interface.
    """
    operation_case, strategy = fuzz_target
    case = data.draw(strategy)
    request = case_request(case)

    with _client() as client:
        response = client.request(
            request["method"],
            _absolute(request["path"]),
            params=request.get("params"),
            headers=request.get("headers"),
            cookies=request.get("cookies"),
            content=request.get("content"),
        )

    body = _decode(response)
    violations = classify_response(operation_case, response.status_code, body, response.headers.get("content-type"))
    assert not violations, "; ".join(f"{violation.message} (case: {request})" for violation in violations)


def test_gate_skipped_operations_are_reported() -> None:
    """Placeholder so a fully-skipped run still reports the suite ran.

    Without a configured gateway the parametrised tests above collect zero
    cases.  This test always collects, which keeps "the suite was skipped"
    distinguishable from "the suite was never selected".
    """
    if not contract_base_url():
        pytest.skip("CONTRACT_BASE_URL is not set (contract suite is opt-in; see module docstring)")
