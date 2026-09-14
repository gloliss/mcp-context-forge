# -*- coding: utf-8 -*-
"""Location: ./tests/contracts/http/conftest.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Collection-time wiring for the live HTTP contract suite (§58).

The operations under test only exist once the gateway's OpenAPI document is
read, so the suite is parametrised at collection time from that document.
When ``CONTRACT_BASE_URL`` is unset the parametrisation is empty: pytest
reports the tests as skipped and **no network call is made during
collection**, which keeps the default test run offline and instant.
"""

# Standard
import os
from typing import Any, Dict, List, Optional

# Third-Party
import pytest

# First-Party
from mcpgateway.protocols.http.activation_gate import DEFAULT_ACTIVATION_GATE
from tests.contracts.http.contract_checks import OperationCase, build_case_strategies, collect_operations, load_openapi_document, select_operations

_BASE_URL = os.environ.get("CONTRACT_BASE_URL")
_OPENAPI_PATH = os.environ.get("CONTRACT_OPENAPI_PATH", "/openapi.json")
_GATE = os.environ.get("CONTRACT_ACTIVATION_GATE") or DEFAULT_ACTIVATION_GATE
_ALLOW_MUTATING = os.environ.get("CONTRACT_ALLOW_MUTATING", "").lower() in ("1", "true", "yes")

# One document load per pytest session; collection touches it once.
_document_cache: Optional[Dict[str, Any]] = None
_selection_cache: Optional[List[OperationCase]] = None
_fuzz_cache: Optional[List[object]] = None


def contract_base_url() -> Optional[str]:
    """Return the configured gateway base URL, if any."""
    return _BASE_URL


def selected_operations() -> List[OperationCase]:
    """Return the gate-permitted operations of the live gateway document.

    Returns:
        The selected operations, or an empty list when no gateway is
        configured (so the suite skips rather than reaching the network).
    """
    global _document_cache, _selection_cache  # pylint: disable=global-statement
    if _selection_cache is not None:
        return _selection_cache
    if not _BASE_URL:
        _selection_cache = []
        return _selection_cache
    _document_cache = load_openapi_document(f"{_BASE_URL.rstrip('/')}{_OPENAPI_PATH}")
    selected, _skipped = select_operations(collect_operations(_document_cache), gate=_GATE, allow_mutating=_ALLOW_MUTATING)
    _selection_cache = selected
    return _selection_cache


def fuzz_targets() -> List[object]:
    """Return ``(operation, strategy)`` pairs for the gate-permitted operations.

    Built once per session.  Returns an empty list when no gateway is
    configured, so the fuzzing test collects nothing rather than reaching the
    network during collection.

    Returns:
        The paired operations and their Hypothesis strategies.
    """
    global _fuzz_cache  # pylint: disable=global-statement
    if _fuzz_cache is not None:
        return _fuzz_cache
    base_url = contract_base_url()
    if not base_url:
        _fuzz_cache = []
        return _fuzz_cache
    _fuzz_cache = build_case_strategies(selected_operations(), url=base_url, schema_path=_OPENAPI_PATH)
    return _fuzz_cache


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrise operation-scoped tests from the live document.

    Args:
        metafunc: The collecting test function's metafunc.
    """
    if "fuzz_target" in metafunc.fixturenames:
        targets = fuzz_targets()
        metafunc.parametrize(
            "fuzz_target",
            targets,
            ids=[case.operation_id or case.label for case, _strategy in targets],
        )
        return
    if "operation_case" not in metafunc.fixturenames:
        return
    cases = selected_operations()
    metafunc.parametrize(
        "operation_case",
        cases,
        ids=[case.operation_id or case.label for case in cases],
    )



