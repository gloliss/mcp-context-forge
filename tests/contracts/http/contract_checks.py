# -*- coding: utf-8 -*-
"""Location: ./tests/contracts/http/contract_checks.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

HTTP contract-test primitives (PR8, design-document §58).

This module holds the gateway-independent half of the contract suite: it
turns an OpenAPI document into a flat list of operations, narrows that list
through the Activation Gate (§59 — mutating operations are only exercised
when the manifest explicitly opts in), and classifies an observed response
against the contract the document declares.

Keeping these as plain functions (rather than inside the pytest fixtures
that drive a live gateway) is what makes the contract *judgement* testable
without a running deployment: the live suite supplies real responses, the
unit suite supplies synthetic ones, and both go through the same code.
"""

# Standard
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Third-Party
from jsonschema import exceptions as jsonschema_exceptions
from jsonschema import validators

# First-Party
from mcpgateway.protocols.http.activation_gate import DEFAULT_ACTIVATION_GATE, evaluate_activation_gate

# HTTP methods an OpenAPI ``paths`` entry may carry (design §58).
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


@dataclass(frozen=True)
class OperationCase:
    """One operation derived from an OpenAPI document (design §58).

    Attributes:
        method: Upper-case HTTP method.
        path: The path template as written in the document.
        operation_id: The declared ``operationId``, when present.
        responses: The declared ``responses`` object, keyed by status code
            (``"default"`` is kept as-is — it declares "any status").
        parameters: The merged path-level and operation-level parameter
            list, used by the live suite to drive requests.
    """

    method: str
    path: str
    operation_id: Optional[str] = None
    responses: Dict[str, Any] = field(default_factory=dict)
    parameters: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        """Return a stable ``METHOD path`` label for reporting."""
        return f"{self.method} {self.path}"


@dataclass(frozen=True)
class ContractViolation:
    """One contract breach observed on a response (design §58).

    Attributes:
        code: Machine-readable breach kind (``server_error``,
            ``undeclared_status``, ``response_schema_mismatch``).
        message: Human-readable explanation for the test report.
        status_code: The status code that was observed.
    """

    code: str
    message: str
    status_code: int


def collect_operations(spec: Dict[str, Any]) -> List[OperationCase]:
    """Enumerate the operations declared by an OpenAPI document.

    Args:
        spec: The parsed OpenAPI document.

    Returns:
        One :class:`OperationCase` per (path, method) pair, in document
        order.  Entries without a ``responses`` object are skipped: a
        document that declares no response contract gives the suite
        nothing to check, and inventing one would produce false breaches.
    """
    operations: List[OperationCase] = []
    paths = spec.get("paths") or {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        shared_parameters = path_item.get("parameters") or []
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            if not isinstance(responses, dict) or not responses:
                continue
            parameters = list(shared_parameters) + list(operation.get("parameters") or [])
            operations.append(
                OperationCase(
                    method=method.upper(),
                    path=path,
                    operation_id=operation.get("operationId"),
                    responses=responses,
                    parameters=parameters,
                )
            )
    return operations


def select_operations(
    cases: List[OperationCase],
    *,
    gate: Optional[str] = None,
    allow_mutating: bool = False,
) -> Tuple[List[OperationCase], List[OperationCase]]:
    """Split operations into those to exercise and those the gate refuses.

    Delegates the decision to the production Activation Gate (§59) so the
    contract suite and the tool runtime cannot drift apart: a method the
    gate refuses to fuzz is the same method the runtime refuses to exercise.

    Args:
        cases: The operations collected from the document.
        gate: Activation Gate mode (``off`` / ``warn`` / ``strict``);
            defaults to the production default.
        allow_mutating: Whether the manifest opted into mutating
            operations (``spec.testing.allowMutatingOperations``).

    Returns:
        A ``(selected, skipped)`` pair.  ``selected`` holds only the
        operations whose gate decision is ``run``.  A ``warn`` decision
        (a mutating operation under the default ``warn`` gate) lands in
        ``skipped``: design §82 requires the suite to *not* fuzz mutating
        operations by default, so warning and proceeding would contradict
        the gate it delegates to.
    """
    mode = gate or DEFAULT_ACTIVATION_GATE
    selected: List[OperationCase] = []
    skipped: List[OperationCase] = []
    for case in cases:
        decision = evaluate_activation_gate(mode, case.method, allow_mutating=allow_mutating)
        (selected if decision == "run" else skipped).append(case)
    return selected, skipped


def classify_response(
    case: OperationCase,
    status_code: int,
    body: Any,
    content_type: Optional[str] = None,
) -> List[ContractViolation]:
    """Classify one observed response against the declared contract (§58).

    Args:
        case: The operation the response came from.
        status_code: The observed status code.
        body: The decoded response body (``None`` for bodyless responses).
        content_type: The response ``Content-Type`` header, used to decide
            whether the body is subject to schema validation.

    Returns:
        Every breach the response exhibits, most severe first.  A 5xx is
        reported as ``server_error`` and short-circuits: the body of a
        server error is not expected to match the success schema, and
        reporting both would bury the real finding.
    """
    if status_code >= 500:
        return [ContractViolation(code="server_error", message=f"{case.label} returned {status_code}", status_code=status_code)]

    declared = case.responses or {}
    if "default" not in declared and str(status_code) not in declared:
        return [
            ContractViolation(
                code="undeclared_status",
                message=f"{case.label} returned undeclared status {status_code}",
                status_code=status_code,
            )
        ]

    schema = _response_schema(declared.get(str(status_code)))
    if schema is None or body is None or not _is_json(content_type):
        return []
    return _schema_violations(case, status_code, schema, body)


def check_invalid_input_rejected(status_code: int) -> Optional[ContractViolation]:
    """Judge whether an invalid-input case was rejected as expected (§58).

    Args:
        status_code: The status code the gateway returned for input that
            violates the operation's declared request schema.

    Returns:
        A ``invalid_input_not_rejected`` violation when the gateway
        answered 2xx (i.e. accepted input the contract calls invalid), or
        ``None`` when the request was rejected or the gateway itself
        reported a server error (already covered by ``server_error``).
    """
    if 200 <= status_code < 300:
        return ContractViolation(
            code="invalid_input_not_rejected",
            message=f"Invalid input was accepted with {status_code}",
            status_code=status_code,
        )
    return None


def sample_parameter_values(case: OperationCase, *, location: str) -> Dict[str, Any]:
    """Build a minimal valid parameter set for one location of an operation.

    The happy-path contract test must send a request the operation declares
    valid — otherwise a strict gateway's correct rejection would be
    misreported as an undeclared status.  Values are derived from each
    parameter's declared schema, honouring ``enum`` where present, and fall
    back to a string for schemas this deliberately-simple generator does not
    model (a wrong-but-well-formed value still exercises the contract, which
    is the point; schemathesis-driven value fuzzing is the E2E extension).

    Args:
        case: The operation to build parameters for.
        location: The parameter location to include (``query``, ``header``
            or ``cookie``); path parameters are handled by the caller,
            which substitutes the template placeholder.

    Returns:
        A mapping of parameter name to a valid value for that location.
    """
    values: Dict[str, Any] = {}
    for param in case.parameters:
        if param.get("in") != location:
            continue
        name = param.get("name")
        if not name:
            continue
        values[name] = _sample_value(param.get("schema") or {})
    return values


def _sample_value(schema: Dict[str, Any]) -> Any:
    """Return one value satisfying a parameter's declared schema.

    Args:
        schema: The parameter's JSON Schema (possibly empty).

    Returns:
        A value appropriate to the declared ``type``, preferring a declared
        ``enum`` member when one exists.
    """
    if not isinstance(schema, dict):
        return "contract-probe"
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    schema_type = schema.get("type")
    if schema_type == "integer":
        return int(schema.get("minimum") or 1)
    if schema_type == "number":
        return float(schema.get("minimum") or 1.0)
    if schema_type == "boolean":
        return True
    if schema_type == "array":
        return [_sample_value(schema.get("items") or {})]
    return "contract-probe"


def _response_schema(response: Any) -> Optional[Dict[str, Any]]:
    """Extract the JSON Schema a response declares, if any.

    Args:
        response: The ``responses`` entry for one status code.

    Returns:
        The declared schema, or ``None`` when the entry declares none (a
        response may legitimately be described by description only).
    """
    if not isinstance(response, dict):
        return None
    content = response.get("content")
    if not isinstance(content, dict):
        return None
    for media_type, media in content.items():
        if media_type == "application/json" or media_type.endswith("+json"):
            schema = (media or {}).get("schema")
            if isinstance(schema, dict):
                return schema
    return None


def _is_json(content_type: Optional[str]) -> bool:
    """Return whether a content type carries a JSON body.

    Args:
        content_type: The raw ``Content-Type`` header value.

    Returns:
        ``True`` for ``application/json`` and ``+json`` structured suffixes.
        An absent content type is treated as JSON so a gateway that omits
        the header still gets its body validated.
    """
    if not content_type:
        return True
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _schema_violations(case: OperationCase, status_code: int, schema: Dict[str, Any], body: Any) -> List[ContractViolation]:
    """Validate a body against a declared schema, reporting breaches.

    Args:
        case: The operation under test.
        status_code: The observed status code.
        schema: The declared JSON Schema.
        body: The decoded response body.

    Returns:
        A single ``response_schema_mismatch`` violation when the body does
        not validate, otherwise an empty list.  The violation is reported
        once (not per schema error) so a single contract breach produces a
        single test failure.
    """
    validator_class = validators.validator_for(schema)
    try:
        validator_class.check_schema(schema)
        errors = list(validator_class(schema).iter_errors(body))
    except (jsonschema_exceptions.SchemaError, jsonschema_exceptions.UnknownType):
        # The document's own schema is malformed (an invalid keyword, or a
        # ``type`` the validator does not define).  That is a defect in the
        # contract document, not a breach by the gateway, so it is reported
        # by document linting rather than as a contract violation.
        return []
    if not errors:
        return []
    first = errors[0]
    location = "/".join(str(part) for part in first.absolute_path) or "<root>"
    return [
        ContractViolation(
            code="response_schema_mismatch",
            message=f"{case.label} {status_code} body violates schema at {location}: {first.message}",
            status_code=status_code,
        )
    ]


def load_openapi_document(url: str) -> Dict[str, Any]:
    """Load and validate an OpenAPI document served over HTTP (§58).

    schemathesis owns schema acquisition: it resolves ``$ref``s, detects the
    specification version, and surfaces a load error rather than letting a
    malformed document masquerade as an empty one.  The raw document is then
    handed to this module's own primitives, which is what makes the
    contract judgement independently testable.

    Args:
        url: The absolute URL of the OpenAPI document.

    Returns:
        The parsed OpenAPI document.

    Raises:
        RuntimeError: When the document cannot be loaded or is not a valid
            OpenAPI schema.
    """
    # Third-Party
    from schemathesis import openapi  # pylint: disable=import-outside-toplevel

    try:
        schema = openapi.from_url(url)
    except Exception as exc:  # pylint: disable=broad-except
        raise RuntimeError(f"Unable to load OpenAPI document from {url}: {exc}") from exc
    raw = schema.raw_schema
    if not isinstance(raw, dict):
        raise RuntimeError(f"OpenAPI document at {url} did not parse into an object")
    return raw


__all__ = [
    "ContractViolation",
    "OperationCase",
    "check_invalid_input_rejected",
    "classify_response",
    "collect_operations",
    "load_openapi_document",
    "sample_parameter_values",
    "select_operations",
]
