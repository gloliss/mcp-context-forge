# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_admin_ids_search.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for admin catalog search query parameters.

This module tests that the tool partial/search endpoint and the
``/admin/tools/ids``, ``/admin/resources/ids``, and ``/admin/prompts/ids``
endpoints correctly filter results when a search query is provided via the
``q`` parameter.
"""

# Standard
from unittest.mock import MagicMock

# Third-Party
import pytest
from sqlalchemy.dialects import sqlite as sqlite_dialect

# First-Party
from mcpgateway.admin import (
    admin_get_all_prompt_ids,
    admin_get_all_resource_ids,
    admin_get_all_tool_ids,
    admin_search_tools,
    admin_tools_partial_html,
)


@pytest.fixture
def mock_db():
    """Create a mock database session."""
    db = MagicMock()
    db.execute.return_value.all.return_value = []
    return db


def setup_team_service(monkeypatch, team_ids):
    """Helper to mock team service for tests."""

    async def mock_get_user_team_ids(user, db):
        return team_ids

    # First-Party
    from mcpgateway import admin

    monkeypatch.setattr(admin, "_get_user_team_ids", mock_get_user_team_ids)


def _compile_sql(statement) -> str:
    """Compile a SQLAlchemy statement with literal values for regression assertions."""
    return " ".join(str(statement.compile(dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True})).split()).lower()


def _assert_canonical_name_contains(sql: str, search_term: str) -> None:
    """Assert that a query searches the stored canonical ``tools.name`` column."""
    assert f"lower(tools.name) like '%{search_term.lower()}%'" in sql


@pytest.mark.asyncio
async def test_admin_tools_partial_searches_canonical_name(monkeypatch, mock_db):
    """The Tools table query must search the same canonical name it renders."""
    setup_team_service(monkeypatch, [])
    captured_query = None

    async def capture_paginated_query(**kwargs):
        nonlocal captured_query
        captured_query = kwargs["query"]
        pagination = MagicMock()
        pagination.model_dump.return_value = {}
        return {"data": [], "pagination": pagination, "links": None}

    monkeypatch.setattr("mcpgateway.admin.paginate_query", capture_paginated_query)

    request = MagicMock()
    request.headers = {}
    request.scope = {"root_path": ""}
    request.app.state.templates.TemplateResponse.return_value = MagicMock()

    await admin_tools_partial_html(
        request=request,
        page=1,
        per_page=10,
        q="Gateway-Weather",
        tags=None,
        include_inactive=False,
        render="controls",
        gateway_id=None,
        team_id=None,
        include_public=False,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db, "is_admin": True},
    )

    assert captured_query is not None
    _assert_canonical_name_contains(_compile_sql(captured_query), "gateway-weather")


@pytest.mark.asyncio
async def test_admin_get_all_tool_ids_searches_canonical_name(monkeypatch, mock_db):
    """Select-All IDs must use the same canonical-name search as the table."""
    setup_team_service(monkeypatch, [])
    mock_db.execute.return_value.all.return_value = []

    await admin_get_all_tool_ids(
        q="Gateway-Weather",
        include_inactive=False,
        gateway_id=None,
        team_id=None,
        include_public=False,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    executed_query = mock_db.execute.call_args.args[0]
    _assert_canonical_name_contains(_compile_sql(executed_query), "gateway-weather")


@pytest.mark.asyncio
async def test_admin_search_tools_prioritizes_exact_canonical_name_and_keeps_contains(monkeypatch, mock_db):
    """Exact canonical matches rank first while shorter substring search remains available."""
    setup_team_service(monkeypatch, [])
    mock_db.execute.return_value.all.return_value = []

    await admin_search_tools(
        q="Gateway-Weather",
        tags=None,
        include_inactive=False,
        limit=10,
        gateway_id=None,
        team_id=None,
        include_public=False,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    executed_query = mock_db.execute.call_args.args[0]
    sql = _compile_sql(executed_query)
    _assert_canonical_name_contains(sql, "gateway-weather")

    exact = "lower(tools.name) = 'gateway-weather'"
    prefix = "lower(tools.name) like 'gateway-weather%'"
    contains = "lower(tools.name) like '%gateway-weather%'"
    assert f"when ({exact}) then 0" in sql
    assert f"when ({prefix} escape '\\') then 1" in sql
    assert contains in sql
    assert sql.index(exact) < sql.index(prefix)

    mock_db.execute.reset_mock()
    mock_db.execute.return_value.all.return_value = []
    await admin_search_tools(
        q="Weather",
        tags=None,
        include_inactive=False,
        limit=10,
        gateway_id=None,
        team_id=None,
        include_public=False,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )
    short_query_sql = _compile_sql(mock_db.execute.call_args.args[0])
    _assert_canonical_name_contains(short_query_sql, "weather")


@pytest.mark.asyncio
async def test_admin_get_all_tool_ids_with_search_query(monkeypatch, mock_db):
    """Test that admin_get_all_tool_ids filters by search query when q parameter is provided."""
    setup_team_service(monkeypatch, [])

    # Mock database to return filtered results
    mock_db.execute.return_value.all.return_value = [("tool-git-1",), ("tool-git-2",)]

    result = await admin_get_all_tool_ids(
        q="git",
        include_inactive=False,
        gateway_id=None,
        team_id=None,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 2
    assert result["tool_ids"] == ["tool-git-1", "tool-git-2"]


@pytest.mark.asyncio
async def test_admin_get_all_tool_ids_empty_search_query(monkeypatch, mock_db):
    """Test that admin_get_all_tool_ids returns all tools when q parameter is empty."""
    setup_team_service(monkeypatch, [])

    # Mock database to return all results
    mock_db.execute.return_value.all.return_value = [("tool-1",), ("tool-2",), ("tool-3",)]

    result = await admin_get_all_tool_ids(
        q="",
        include_inactive=False,
        gateway_id=None,
        team_id=None,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 3
    assert result["tool_ids"] == ["tool-1", "tool-2", "tool-3"]


@pytest.mark.asyncio
async def test_admin_get_all_tool_ids_search_with_gateway_filter(monkeypatch, mock_db):
    """Test that search query works together with gateway filter."""
    setup_team_service(monkeypatch, [])

    # Mock database to return filtered results
    mock_db.execute.return_value.all.return_value = [("tool-git-gw1",)]

    result = await admin_get_all_tool_ids(
        q="git",
        include_inactive=False,
        gateway_id="gw-1",
        team_id=None,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 1
    assert result["tool_ids"] == ["tool-git-gw1"]


@pytest.mark.asyncio
async def test_admin_get_all_resource_ids_with_search_query(monkeypatch, mock_db):
    """Test that admin_get_all_resource_ids filters by search query when q parameter is provided."""
    setup_team_service(monkeypatch, [])

    # Mock database to return filtered results
    mock_db.execute.return_value.all.return_value = [("resource-file-1",), ("resource-file-2",)]

    result = await admin_get_all_resource_ids(
        q="file",
        include_inactive=False,
        gateway_id=None,
        team_id=None,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 2
    assert result["resource_ids"] == ["resource-file-1", "resource-file-2"]


@pytest.mark.asyncio
async def test_admin_get_all_resource_ids_empty_search_query(monkeypatch, mock_db):
    """Test that admin_get_all_resource_ids returns all resources when q parameter is empty."""
    setup_team_service(monkeypatch, [])

    # Mock database to return all results
    mock_db.execute.return_value.all.return_value = [("res-1",), ("res-2",), ("res-3",)]

    result = await admin_get_all_resource_ids(
        q="",
        include_inactive=False,
        gateway_id=None,
        team_id=None,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 3
    assert result["resource_ids"] == ["res-1", "res-2", "res-3"]


@pytest.mark.asyncio
async def test_admin_get_all_resource_ids_search_with_team_filter(monkeypatch, mock_db):
    """Test that search query works together with team filter."""
    setup_team_service(monkeypatch, ["team-1"])

    # Mock database to return filtered results
    mock_db.execute.return_value.all.return_value = [("resource-file-team1",)]

    result = await admin_get_all_resource_ids(
        q="file",
        include_inactive=False,
        gateway_id=None,
        team_id="team-1",
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 1
    assert result["resource_ids"] == ["resource-file-team1"]


@pytest.mark.asyncio
async def test_admin_get_all_prompt_ids_with_search_query(monkeypatch, mock_db):
    """Test that admin_get_all_prompt_ids filters by search query when q parameter is provided."""
    setup_team_service(monkeypatch, [])

    # Mock database to return filtered results
    mock_db.execute.return_value.all.return_value = [("prompt-code-1",), ("prompt-code-2",)]

    result = await admin_get_all_prompt_ids(
        q="code",
        include_inactive=False,
        gateway_id=None,
        team_id=None,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 2
    assert result["prompt_ids"] == ["prompt-code-1", "prompt-code-2"]


@pytest.mark.asyncio
async def test_admin_get_all_prompt_ids_empty_search_query(monkeypatch, mock_db):
    """Test that admin_get_all_prompt_ids returns all prompts when q parameter is empty."""
    setup_team_service(monkeypatch, [])

    # Mock database to return all results
    mock_db.execute.return_value.all.return_value = [("prompt-1",), ("prompt-2",), ("prompt-3",)]

    result = await admin_get_all_prompt_ids(
        q="",
        include_inactive=False,
        gateway_id=None,
        team_id=None,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 3
    assert result["prompt_ids"] == ["prompt-1", "prompt-2", "prompt-3"]


@pytest.mark.asyncio
async def test_admin_get_all_prompt_ids_search_no_results(monkeypatch, mock_db):
    """Test that search query returns empty list when no matches found."""
    setup_team_service(monkeypatch, [])

    # Mock database to return no results
    mock_db.execute.return_value.all.return_value = []

    result = await admin_get_all_prompt_ids(
        q="nonexistent",
        include_inactive=False,
        gateway_id=None,
        team_id=None,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 0
    assert result["prompt_ids"] == []


@pytest.mark.asyncio
async def test_admin_get_all_tool_ids_search_case_insensitive(monkeypatch, mock_db):
    """Test that search query is case-insensitive."""
    setup_team_service(monkeypatch, [])

    # Mock database to return results regardless of case
    mock_db.execute.return_value.all.return_value = [("tool-GIT-1",), ("tool-Git-2",)]

    result = await admin_get_all_tool_ids(
        q="GIT",
        include_inactive=False,
        gateway_id=None,
        team_id=None,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 2
    assert "tool-GIT-1" in result["tool_ids"]
    assert "tool-Git-2" in result["tool_ids"]


@pytest.mark.asyncio
async def test_admin_get_all_tool_ids_search_with_all_filters(monkeypatch, mock_db):
    """Test that search query works with gateway, team, and inactive filters combined."""
    setup_team_service(monkeypatch, ["team-1"])

    # Mock database to return filtered results
    mock_db.execute.return_value.all.return_value = [("tool-git-gw1-team1",)]

    result = await admin_get_all_tool_ids(
        q="git",
        include_inactive=True,
        gateway_id="gw-1",
        team_id="team-1",
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 1
    assert result["tool_ids"] == ["tool-git-gw1-team1"]


@pytest.mark.asyncio
async def test_admin_get_all_tool_ids_include_public_with_team(monkeypatch, mock_db):
    """Test that include_public adds platform-public tools when filtering by team."""
    setup_team_service(monkeypatch, ["team-1"])

    mock_db.execute.return_value.all.return_value = [("tool-team-1",), ("tool-public-1",)]

    result = await admin_get_all_tool_ids(
        q="",
        include_inactive=False,
        gateway_id=None,
        team_id="team-1",
        include_public=True,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 2
    assert "tool-team-1" in result["tool_ids"]
    assert "tool-public-1" in result["tool_ids"]


@pytest.mark.asyncio
async def test_admin_get_all_resource_ids_include_public_with_team(monkeypatch, mock_db):
    """Test that include_public adds platform-public resources when filtering by team."""
    setup_team_service(monkeypatch, ["team-1"])

    mock_db.execute.return_value.all.return_value = [("res-team-1",), ("res-public-1",)]

    result = await admin_get_all_resource_ids(
        q="",
        include_inactive=False,
        gateway_id=None,
        team_id="team-1",
        include_public=True,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 2
    assert "res-team-1" in result["resource_ids"]
    assert "res-public-1" in result["resource_ids"]


@pytest.mark.asyncio
async def test_admin_get_all_prompt_ids_include_public_with_team(monkeypatch, mock_db):
    """Test that include_public adds platform-public prompts when filtering by team."""
    setup_team_service(monkeypatch, ["team-1"])

    mock_db.execute.return_value.all.return_value = [("prompt-team-1",), ("prompt-public-1",)]

    result = await admin_get_all_prompt_ids(
        q="",
        include_inactive=False,
        gateway_id=None,
        team_id="team-1",
        include_public=True,
        db=mock_db,
        user={"email": "user@example.com", "db": mock_db},
    )

    assert result["count"] == 2
    assert "prompt-team-1" in result["prompt_ids"]
    assert "prompt-public-1" in result["prompt_ids"]
