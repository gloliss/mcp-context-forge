# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/admin/test_database_sources_admin.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the Database Sources admin UI form parsing and rendering (OB-06).

These cover the flat-form parser, the edit-form context (which must never
echo a credential), and the connection-test result renderer.
"""

# First-Party
from mcpgateway.admin import (
    _database_source_form_context,
    _database_source_raw_fields,
    _database_source_test_result_html,
)
from mcpgateway.schemas import DatabaseSourceCreate


def _form(**overrides) -> dict:
    """Build a realistic flat admin form submission."""
    data = {
        "name": "Prod DB",
        "engine": "oceanbase",
        "compatibility_mode": "mysql",
        "host": "127.0.0.1",
        "port": "2881",
        "cluster_name": "",
        "tenant_name": "tenant",
        "database_name": "test",
        "schema_name": "",
        "username": "root",
        "password": "s3cr3t",
        "ssl_mode": "",
        "charset": "",
        "timezone": "",
        "pool_size": "10",
        "max_overflow": "5",
        "acquire_timeout_seconds": "5.5",
        "recycle_seconds": "300",
        "pre_ping": "on",
        "max_rows": "500",
        "query_timeout_seconds": "30.0",
        "readonly": "on",
        "allow_multi_statement": "on",
        "allowed_statement_types": "SELECT, SHOW",
        "tool_execute_query": "on",
    }
    data.update(overrides)
    return data


def test_raw_fields_parses_flat_form():
    """Flat form values are normalized into typed, nested configs."""
    fields = _database_source_raw_fields(_form())

    assert fields["name"] == "Prod DB"
    assert fields["engine"] == "oceanbase"
    assert fields["compatibility_mode"] == "mysql"
    assert fields["port"] == 2881
    assert fields["password"] == "s3cr3t"
    assert fields["tenant_name"] == "tenant"
    assert fields["pool_config"]["pool_size"] == 10
    assert fields["pool_config"]["acquire_timeout_seconds"] == 5.5
    assert fields["pool_config"]["pre_ping"] is True
    assert fields["policy_config"]["readonly"] is True
    assert fields["policy_config"]["allow_multi_statement"] is True
    assert fields["policy_config"]["max_rows"] == 500
    assert fields["policy_config"]["allowed_statement_types"] == ["SELECT", "SHOW"]
    assert fields["tool_config"]["execute_query"] is True


def test_raw_fields_unchecked_checkboxes_default_false():
    """An absent checkbox means unchecked: pre_ping/readonly/execute_query all false."""
    fields = _database_source_raw_fields({})

    assert fields["password"] is None
    assert fields["port"] is None
    assert fields["pool_config"]["pre_ping"] is False
    assert fields["policy_config"]["readonly"] is False
    assert fields["policy_config"]["allow_multi_statement"] is False
    assert fields["tool_config"]["execute_query"] is False


def test_raw_fields_builds_mysql_create():
    """MySQL engine yields a valid create payload with no compatibility mode."""
    fields = _database_source_raw_fields(_form(engine="mysql", compatibility_mode="", port="3306"))
    data = DatabaseSourceCreate(**fields)

    assert data.engine == "mysql"
    assert data.compatibility_mode is None
    assert data.port == 3306


def test_raw_fields_builds_oracle_create():
    """Oracle engine yields a valid create payload with no compatibility mode."""
    fields = _database_source_raw_fields(_form(engine="oracle", compatibility_mode="", port="1521"))
    data = DatabaseSourceCreate(**fields)

    assert data.engine == "oracle"
    assert data.compatibility_mode is None
    assert data.port == 1521


def test_form_context_never_echoes_password():
    """The create/edit form context carries no password and defaults tools off."""
    create = _database_source_form_context()

    assert "password" not in create
    assert create["tool_execute_query"] is False
    assert create["readonly"] is True  # initial default: read-only
    assert create["pre_ping"] is True  # initial default: pre-ping on


def test_test_result_html_renders_checks_and_latency():
    """The test result renderer shows checks, status badges, and latency."""
    html = _database_source_test_result_html(
        {
            "ok": True,
            "latency_ms": 12.3,
            "checks": [
                {"name": "connection", "status": "passed", "detail": "ok"},
                {"name": "database_schema", "status": "failed", "detail": "schema missing"},
            ],
        }
    )

    assert "Connection" in html
    assert "Passed" in html
    assert "Failed" in html
    assert "12.3 ms" in html


def test_test_result_html_escapes_detail():
    """Hostile driver error text is HTML-escaped, never injected as markup."""
    html = _database_source_test_result_html(
        {"ok": False, "checks": [{"name": "connection", "status": "failed", "detail": "<script>alert(1)</script>"}]}
    )

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
