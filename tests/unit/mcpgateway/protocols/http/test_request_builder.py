# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/protocols/http/test_request_builder.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for the PR2 RequestBuilder: independent path/query/header/cookie/
body assembly (design-document §9.6).
"""

# First-Party
from mcpgateway.protocols.codecs import build_default_codec_registry
from mcpgateway.protocols.http.request_builder import RequestBuilder


def _make_builder() -> RequestBuilder:
    """Build a RequestBuilder over the default codec registry."""
    return RequestBuilder(build_default_codec_registry())


class TestPathTemplateSubstitution:
    """{name} placeholders are rendered from arguments."""

    def test_placeholders_are_substituted_and_popped(self):
        """Placeholder values are rendered into the path and not reused."""
        built = _make_builder().build(
            {"user_id": 42, "extra": "v"},
            {"method": "POST", "pathTemplate": "/users/{user_id}", "preferredContentType": "application/json"},
        )

        assert built.url_path == "/users/42"
        assert built.body.value == {"extra": "v"}

    def test_unresolvable_placeholders_are_left_in_place(self):
        """A placeholder without a matching argument survives for validation."""
        built = _make_builder().build({}, {"method": "POST", "pathTemplate": "/users/{user_id}"})

        assert built.url_path == "/users/{user_id}"

    def test_no_template_is_passed_through(self):
        """A template without braces is returned unchanged."""
        built = _make_builder().build({"a": 1}, {"method": "POST", "pathTemplate": "/fixed"})

        assert built.url_path == "/fixed"


class TestBodylessMethods:
    """GET/HEAD/OPTIONS/DELETE carry no body; arguments go to the query."""

    def test_get_routes_arguments_to_query(self):
        """GET arguments become query parameters, not a body."""
        built = _make_builder().build({"a": 1, "b": "x"}, {"method": "GET", "pathTemplate": "/q"})

        assert built.query_params == {"a": 1, "b": "x"}
        assert built.body is None

    def test_head_options_delete_are_bodyless(self):
        """HEAD/OPTIONS/DELETE follow the same bodyless convention."""
        for method in ("HEAD", "OPTIONS", "DELETE"):
            built = _make_builder().build({"a": 1}, {"method": method, "pathTemplate": "/q"})

            assert built.query_params == {"a": 1}
            assert built.body is None


class TestParameterSplitting:
    """An explicit parameters list routes each argument by location."""

    def test_path_query_header_cookie_are_split_independently(self):
        """Each location receives its arguments; leftovers become the body."""
        built = _make_builder().build(
            {"id": "7", "page": "2", "x-token": "t", "sid": "c", "payload": {"k": "v"}},
            {
                "method": "POST",
                "pathTemplate": "/r",
                "preferredContentType": "application/json",
                "parameters": [
                    {"name": "id", "location": "path"},
                    {"name": "page", "location": "query"},
                    {"name": "x-token", "location": "header"},
                    {"name": "sid", "location": "cookie"},
                ],
            },
        )

        assert built.url_path == "/r"
        assert built.query_params == {"page": "2"}
        assert built.headers == {"x-token": "t"}
        assert built.cookies == {"sid": "c"}
        assert built.body.value == {"payload": {"k": "v"}}

    def test_missing_parameters_are_skipped(self):
        """Parameters absent from arguments do not raise."""
        built = _make_builder().build(
            {},
            {"method": "POST", "pathTemplate": "/r", "parameters": [{"name": "nope", "location": "query"}]},
        )

        assert built.query_params == {}
        assert built.body is None

    def test_body_dimension_is_independent_of_query(self):
        """Query parameters are never folded into the body (§9.6)."""
        built = _make_builder().build(
            {"q": "1", "b": "2"},
            {
                "method": "POST",
                "pathTemplate": "/r",
                "preferredContentType": "application/json",
                "parameters": [{"name": "q", "location": "query"}],
            },
        )

        assert built.query_params == {"q": "1"}
        assert built.body.value == {"b": "2"}


class TestBodyEncoding:
    """The configured codec encodes the body into an EncodedBody."""

    def test_json_codec_by_default(self):
        """No body config falls back to the preferred content type (JSON)."""
        built = _make_builder().build(
            {"a": 1},
            {"method": "POST", "pathTemplate": "/r", "preferredContentType": "application/json"},
        )

        assert built.body.mode == "json"
        assert built.body.value == {"a": 1}
        assert built.body.content_type == "application/json"

    def test_form_codec_produces_data_mode(self):
        """application/x-www-form-urlencoded bodies use data=."""
        built = _make_builder().build(
            {"a": 1, "b": None},
            {
                "method": "POST",
                "pathTemplate": "/r",
                "body": {"codec": "form", "mediaType": "application/x-www-form-urlencoded"},
            },
        )

        assert built.body.mode == "data"
        assert built.body.value == {"a": "1", "b": ""}

    def test_multipart_codec_produces_files_mode(self):
        """multipart/form-data bodies use files= with no content type."""
        built = _make_builder().build(
            {"a": "v"},
            {"method": "POST", "pathTemplate": "/r", "body": {"codec": "multipart", "mediaType": "multipart/form-data"}},
        )

        assert built.body.mode == "files"
        assert built.body.value == {"a": (None, "v")}
        assert built.body.content_type is None

    def test_body_media_type_falls_back_to_preferred_content_type(self):
        """A missing body mediaType uses request.preferredContentType."""
        built = _make_builder().build(
            {"a": 1},
            {"method": "POST", "pathTemplate": "/r", "preferredContentType": "text/plain"},
        )

        assert built.body.mode == "content"
        assert built.body.content_type == "text/plain"

    def test_empty_request_config_defaults_to_get(self):
        """An absent request config produces a GET request."""
        built = _make_builder().build({}, None)

        assert built.method == "GET"
        assert built.url_path == ""


class TestGroupedArguments:
    """PR3 structured argument groups (§16) split into dimensions."""

    def test_path_group_renders_template(self):
        """Placeholders resolve from the path group, not flat keys."""
        built = _make_builder().build(
            {"path": {"lotId": "L1"}, "query": {"page": "2"}},
            {"method": "GET", "pathTemplate": "/v1/lots/{lotId}"},
        )

        assert built.url_path == "/v1/lots/L1"
        assert built.query_params == {"page": "2"}
        assert built.body is None

    def test_groups_land_in_independent_dimensions(self):
        """path/query/headers/cookies/body groups never cross dimensions."""
        built = _make_builder().build(
            {
                "path": {"id": "7"},
                "query": {"page": "2"},
                "headers": {"x-token": "t"},
                "cookies": {"sid": "c"},
                "body": {"payload": {"k": "v"}},
            },
            {
                "method": "POST",
                "pathTemplate": "/items/{id}",
                "preferredContentType": "application/json",
            },
        )

        assert built.url_path == "/items/7"
        assert built.query_params == {"page": "2"}
        assert built.headers == {"x-token": "t"}
        assert built.cookies == {"sid": "c"}
        assert built.body.mode == "json"
        assert built.body.value == {"payload": {"k": "v"}}

    def test_absent_groups_are_empty(self):
        """Only the provided groups participate; the rest stay empty."""
        built = _make_builder().build(
            {"query": {"q": "1"}},
            {"method": "GET", "pathTemplate": "/search"},
        )

        assert built.url_path == "/search"
        assert built.query_params == {"q": "1"}
        assert built.headers == {}
        assert built.cookies == {}
        assert built.body is None

    def test_scalar_body_group_is_allowed(self):
        """A text body group may hold a scalar (not a mapping)."""
        built = _make_builder().build(
            {"body": "plain text payload"},
            {"method": "POST", "pathTemplate": "/r", "preferredContentType": "text/plain"},
        )

        assert built.body.mode == "content"
        assert built.body.value == b"plain text payload"

    def test_empty_body_group_produces_no_body(self):
        """An empty body group behaves like the flat path (None body)."""
        built = _make_builder().build(
            {"body": {}},
            {"method": "POST", "pathTemplate": "/r", "preferredContentType": "application/json"},
        )

        assert built.body is None

    def test_flat_tools_with_group_named_args_stay_flat(self):
        """Flat legacy arguments keep the flat path even when names collide."""
        built = _make_builder().build(
            {"query": 1, "body": "x"},
            {"method": "POST", "pathTemplate": "/r", "preferredContentType": "application/json"},
        )

        assert built.query_params == {}
        assert built.body.value == {"query": 1, "body": "x"}

    def test_empty_arguments_are_flat_not_grouped(self):
        """An empty argument dict takes the flat path (GET → empty query)."""
        built = _make_builder().build({}, {"method": "GET", "pathTemplate": "/r"})

        assert built.url_path == "/r"
        assert built.query_params == {}
        assert built.body is None
