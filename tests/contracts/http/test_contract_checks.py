# -*- coding: utf-8 -*-
"""Location: ./tests/contracts/http/test_contract_checks.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the contract-test primitives (PR8, design §58/§59).

These exercise the judgement logic directly, with no gateway involved, so
the contract suite's verdicts are trustworthy before the live suite runs.
"""

# Standard
import json

# Third-Party
import pytest

# First-Party
from tests.contracts.http.contract_checks import (
    ContractViolation,
    build_case_strategies,
    case_request,
    OperationCase,
    check_invalid_input_rejected,
    classify_response,
    collect_operations,
    load_openapi_document,
    sample_parameter_values,
    select_operations,
)

_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Demo", "version": "1.0.0"},
    "paths": {
        "/widgets": {
            "parameters": [{"name": "trace", "in": "query", "schema": {"type": "string"}}],
            "get": {
                "operationId": "listWidgets",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["items"],
                                    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
                                }
                            }
                        },
                    }
                },
            },
            "post": {
                "operationId": "createWidget",
                "responses": {"201": {"description": "created"}},
            },
        },
        "/health": {"get": {"operationId": "health"}},
        "/widgets/{widget_id}": {
            "delete": {"operationId": "deleteWidget", "responses": {"204": {"description": "gone"}}},
        },
    },
}


class TestCollectOperations:
    """Operations are enumerated from the document (§58)."""

    def test_collects_every_method_with_responses(self):
        """Each (path, method) with a responses object becomes a case."""
        cases = {case.label for case in collect_operations(_SPEC)}

        assert cases == {"GET /widgets", "POST /widgets", "DELETE /widgets/{widget_id}"}

    def test_operations_without_responses_are_skipped(self):
        """/health declares no responses, so it yields nothing to check."""
        assert "GET /health" not in {case.label for case in collect_operations(_SPEC)}

    def test_path_level_parameters_are_merged(self):
        """Path-level parameters are merged ahead of operation-level ones."""
        case = next(case for case in collect_operations(_SPEC) if case.label == "GET /widgets")

        assert [param["name"] for param in case.parameters] == ["trace", "limit"]

    def test_operation_id_is_captured(self):
        """The declared operationId is preserved for reporting."""
        case = next(case for case in collect_operations(_SPEC) if case.label == "GET /widgets")

        assert case.operation_id == "listWidgets"

    def test_empty_document_yields_nothing(self):
        """A document without paths yields no cases rather than raising."""
        assert collect_operations({}) == []


class TestSelectOperations:
    """The Activation Gate decides what the suite exercises (§59)."""

    def test_default_gate_skips_mutating_operations(self):
        """Without an opt-in, only safe methods are exercised."""
        selected, skipped = select_operations(collect_operations(_SPEC))

        assert {case.method for case in selected} == {"GET"}
        assert {case.method for case in skipped} == {"POST", "DELETE"}

    def test_allow_mutating_includes_mutating_operations(self):
        """The manifest opt-in brings mutating operations into scope."""
        selected, skipped = select_operations(collect_operations(_SPEC), allow_mutating=True)

        assert {case.label for case in selected} == {"GET /widgets", "POST /widgets", "DELETE /widgets/{widget_id}"}
        assert skipped == []

    def test_gate_off_skips_everything(self):
        """gate=off disables the suite entirely."""
        selected, skipped = select_operations(collect_operations(_SPEC), gate="off", allow_mutating=True)

        assert selected == []
        assert len(skipped) == 3

    def test_strict_refuses_mutating_without_opt_in(self):
        """strict treats an unpermitted mutating operation as a skip."""
        selected, skipped = select_operations(collect_operations(_SPEC), gate="strict")

        assert {case.method for case in selected} == {"GET"}
        assert {case.method for case in skipped} == {"POST", "DELETE"}


class TestClassifyResponse:
    """Observed responses are judged against the declared contract (§58)."""

    def _get_case(self) -> OperationCase:
        """Return the documented GET /widgets case."""
        return next(case for case in collect_operations(_SPEC) if case.label == "GET /widgets")

    def test_conforming_response_has_no_violations(self):
        """A 200 whose body matches the schema is clean."""
        violations = classify_response(self._get_case(), 200, {"items": ["a"]}, "application/json")

        assert violations == []

    def test_server_error_is_reported(self):
        """A 5xx is a server_error regardless of declared responses."""
        violations = classify_response(self._get_case(), 503, {"detail": "down"}, "application/json")

        assert [violation.code for violation in violations] == ["server_error"]

    def test_server_error_short_circuits_schema_check(self):
        """A 5xx body is not additionally reported as a schema mismatch."""
        violations = classify_response(self._get_case(), 500, {"unexpected": True}, "application/json")

        assert len(violations) == 1
        assert violations[0].code == "server_error"

    def test_undeclared_status_is_reported(self):
        """A status the document never declares is a contract breach."""
        violations = classify_response(self._get_case(), 418, {}, "application/json")

        assert [violation.code for violation in violations] == ["undeclared_status"]

    def test_default_response_declaration_covers_any_status(self):
        """A `default` response makes other statuses declared."""
        case = OperationCase(method="GET", path="/x", responses={"default": {"description": "any"}})

        assert classify_response(case, 418, {}, "application/json") == []

    def test_schema_mismatch_is_reported_with_location(self):
        """A body violating the declared schema reports where it diverged."""
        violations = classify_response(self._get_case(), 200, {"items": "not-a-list"}, "application/json")

        assert [violation.code for violation in violations] == ["response_schema_mismatch"]
        assert "items" in violations[0].message

    def test_missing_required_property_is_a_mismatch(self):
        """A body omitting a required property is a mismatch."""
        violations = classify_response(self._get_case(), 200, {}, "application/json")

        assert [violation.code for violation in violations] == ["response_schema_mismatch"]

    def test_body_is_not_validated_without_a_declared_schema(self):
        """A response that declares no schema is not body-checked."""
        case = OperationCase(method="POST", path="/x", responses={"201": {"description": "created"}})

        assert classify_response(case, 201, {"anything": 1}, "application/json") == []

    def test_non_json_body_is_not_schema_checked(self):
        """A non-JSON response is outside the JSON contract."""
        violations = classify_response(self._get_case(), 200, "<xml/>", "application/xml")

        assert violations == []

    def test_bodyless_response_is_not_schema_checked(self):
        """A bodyless response cannot violate a body schema."""
        assert classify_response(self._get_case(), 200, None, "application/json") == []

    def test_malformed_document_schema_is_not_a_gateway_breach(self):
        """A broken schema in the document is not reported against the gateway."""
        case = OperationCase(
            method="GET",
            path="/x",
            responses={"200": {"content": {"application/json": {"schema": {"type": "not-a-real-type"}}}}},
        )

        assert classify_response(case, 200, {"a": 1}, "application/json") == []

    def test_violation_carries_the_status_code(self):
        """Violations record the status they were observed on."""
        violations = classify_response(self._get_case(), 502, None, "application/json")

        assert isinstance(violations[0], ContractViolation)
        assert violations[0].status_code == 502


class TestCheckInvalidInputRejected:
    """Invalid input must be rejected, not accepted (§58)."""

    @pytest.mark.parametrize("status_code", [400, 401, 404, 422])
    def test_rejection_is_accepted(self, status_code):
        """A 4xx response satisfies the contract."""
        assert check_invalid_input_rejected(status_code) is None

    @pytest.mark.parametrize("status_code", [200, 201, 204])
    def test_acceptance_is_a_violation(self, status_code):
        """A 2xx on invalid input is a contract breach."""
        violation = check_invalid_input_rejected(status_code)

        assert violation is not None
        assert violation.code == "invalid_input_not_rejected"

    def test_server_error_is_left_to_the_server_error_check(self):
        """A 5xx is not double-reported as an input-validation failure."""
        assert check_invalid_input_rejected(503) is None


class TestLoadOpenApiDocument:
    """Schema acquisition via schemathesis (§58)."""

    @pytest.fixture
    def served_spec_url(self):
        """Serve a minimal OpenAPI document on a loopback port."""
        # Standard
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        payload = json.dumps(_SPEC).encode()

        class _Handler(BaseHTTPRequestHandler):
            """Serve the document at /openapi.json for any other path."""

            def do_GET(self):  # noqa: N802 - stdlib handler name
                """Answer with the document, or 404 for other paths."""
                if self.path != "/openapi.json":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                """Silence the handler's stderr access log."""

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/openapi.json"
        finally:
            server.shutdown()
            server.server_close()

    def test_document_is_loaded_and_usable(self, served_spec_url):
        """A served document loads and feeds the collection primitives."""
        spec = load_openapi_document(served_spec_url)

        assert collect_operations(spec)

    def test_unreachable_document_raises_runtime_error(self):
        """An unreachable URL surfaces a RuntimeError, not a silent empty spec."""
        with pytest.raises(RuntimeError, match="Unable to load OpenAPI document"):
            load_openapi_document("http://127.0.0.1:1/openapi.json")


class TestSampleParameterValues:
    """Valid-parameter generation keeps the happy-path probe valid (§58)."""

    def _case(self) -> OperationCase:
        """Return an operation declaring one parameter per location and type."""
        return OperationCase(
            method="GET",
            path="/x/{widget_id}",
            parameters=[
                {"name": "widget_id", "in": "path", "required": True, "schema": {"type": "string"}},
                {"name": "limit", "in": "query", "required": True, "schema": {"type": "integer"}},
                {"name": "mode", "in": "query", "schema": {"type": "string", "enum": ["fast", "slow"]}},
                {"name": "flag", "in": "query", "schema": {"type": "boolean"}},
                {"name": "tags", "in": "query", "schema": {"type": "array", "items": {"type": "string"}}},
                {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                {"name": "session", "in": "cookie", "schema": {"type": "string"}},
            ],
        )

    def test_query_values_are_typed(self):
        """Query values follow each parameter's declared type."""
        values = sample_parameter_values(self._case(), location="query")

        assert values == {"limit": 1, "mode": "fast", "flag": True, "tags": ["contract-probe"]}

    def test_path_parameters_are_excluded(self):
        """Path parameters are not emitted — the caller substitutes placeholders."""
        assert "widget_id" not in sample_parameter_values(self._case(), location="query")

    def test_header_and_cookie_locations_are_separated(self):
        """Each location yields only its own parameters."""
        assert sample_parameter_values(self._case(), location="header") == {"X-Trace": "contract-probe"}
        assert sample_parameter_values(self._case(), location="cookie") == {"session": "contract-probe"}

    def test_enum_member_is_preferred(self):
        """A declared enum value is used verbatim."""
        assert sample_parameter_values(self._case(), location="query")["mode"] == "fast"

    def test_unknown_schema_falls_back_to_a_string(self):
        """An unmodelled schema still yields a well-formed value."""
        case = OperationCase(method="GET", path="/y", parameters=[{"name": "q", "in": "query", "schema": {"type": "object"}}])

        assert sample_parameter_values(case, location="query") == {"q": "contract-probe"}

    def test_missing_schema_is_tolerated(self):
        """A parameter without a schema yields the string fallback."""
        case = OperationCase(method="GET", path="/y", parameters=[{"name": "q", "in": "query"}])

        assert sample_parameter_values(case, location="query") == {"q": "contract-probe"}

    def test_unnamed_parameters_are_skipped(self):
        """A malformed parameter entry cannot inject an empty key."""
        case = OperationCase(method="GET", path="/y", parameters=[{"in": "query", "schema": {"type": "integer"}}])

        assert sample_parameter_values(case, location="query") == {}


class TestCaseRequest:
    """schemathesis cases translate into httpx arguments (§58)."""

    def _case(self, **overrides):
        """Build a minimal schemathesis-like case object."""
        # Standard
        from types import SimpleNamespace

        fields = {"method": "get", "path": "/things", "query": None, "headers": None, "cookies": None, "body": None, "media_type": None}
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def test_method_is_upper_cased(self):
        """The method is normalised for httpx."""
        assert case_request(self._case())["method"] == "GET"

    def test_path_is_preserved(self):
        """The path is passed through untouched."""
        assert case_request(self._case(path="/a/{b}"))["path"] == "/a/{b}"

    def test_empty_optionals_are_omitted(self):
        """Absent query/headers/cookies produce no empty httpx kwargs."""
        request = case_request(self._case())

        assert set(request) == {"method", "path"}

    def test_populated_optionals_are_included(self):
        """Query, headers and cookies are carried through."""
        request = case_request(self._case(query={"a": 1}, headers={"X-T": "v"}, cookies={"s": "1"}))

        assert request["params"] == {"a": 1}
        assert request["headers"] == {"X-T": "v"}
        assert request["cookies"] == {"s": "1"}

    def test_structured_body_is_serialised_as_json(self):
        """A dict body becomes JSON content."""
        request = case_request(self._case(body={"a": 1}))

        assert json.loads(request["content"]) == {"a": 1}

    def test_bytes_body_passes_through(self):
        """A bytes body is sent unchanged."""
        assert case_request(self._case(body=b"raw"))["content"] == b"raw"

    def test_media_type_sets_the_content_type(self):
        """A declared media type becomes the request Content-Type."""
        request = case_request(self._case(body={"a": 1}, media_type="application/json"))

        assert request["headers"]["Content-Type"] == "application/json"

    def test_not_set_body_is_not_sent(self):
        """schemathesis' "no body" sentinel must never reach the wire.

        Sending it would put the sentinel's repr in the request body; that
        bug shipped once and was caught by the live suite's failure output.
        """
        # Third-Party
        from schemathesis.core import NotSet

        request = case_request(self._case(body=NotSet()))

        assert "content" not in request

    def test_a_lookalike_not_set_is_still_detected(self):
        """Detection is not fooled by a different class of the same name."""
        class NotSet:  # noqa: N801 - deliberate name collision
            """A look-alike that is not schemathesis' sentinel type."""

        request = case_request(self._case(body=NotSet()))

        assert "content" not in request


class TestBuildCaseStrategies:
    """Strategies are paired with the operations they belong to (§58)."""

    @pytest.fixture
    def schema_url(self):
        """Serve the shared spec on a loopback port."""
        # Standard
        import json as json_module
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        payload = json_module.dumps(_SPEC).encode()

        class _Handler(BaseHTTPRequestHandler):
            """Serve the document at /openapi.json."""

            def do_GET(self):  # noqa: N802 - stdlib handler name
                """Answer with the document, or 404 elsewhere."""
                if self.path != "/openapi.json":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                """Silence the access log."""

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()

    def test_strategies_are_paired_with_their_operations(self, schema_url):
        """Each gated operation gets its own strategy, in input order."""
        cases = collect_operations(_SPEC)
        selected, _skipped = select_operations(cases)

        pairs = build_case_strategies(selected, url=schema_url)

        assert [operation.label for operation, _strategy in pairs] == [case.label for case in selected]

    def test_mutating_operations_are_absent_unless_selected(self, schema_url):
        """Only the operations handed in get a strategy — the gate still rules."""
        selected, _skipped = select_operations(collect_operations(_SPEC))

        pairs = build_case_strategies(selected, url=schema_url)

        assert all(operation.method == "GET" for operation, _strategy in pairs)

    def test_a_strategy_produces_a_case(self, schema_url):
        """The paired strategy really generates cases for its operation."""
        selected, _skipped = select_operations(collect_operations(_SPEC))
        pairs = build_case_strategies(selected, url=schema_url)

        operation, strategy = pairs[0]
        case = strategy.example()

        assert case.method.upper() == operation.method
        assert case.path == operation.path
