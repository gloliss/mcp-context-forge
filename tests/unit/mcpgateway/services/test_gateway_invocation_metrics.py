# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_gateway_invocation_metrics.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Unit tests for per-gateway invocation metrics aggregation.
"""

# Standard
from datetime import datetime, timedelta, timezone
import uuid

# First-Party
from mcpgateway.db import Gateway, Prompt, PromptMetric, Resource, ResourceMetric, Tool, ToolMetric, ToolMetricsDaily, ToolMetricsHourly
from mcpgateway.services import metrics_query_service as mqs


def _gateway(gateway_id: str) -> Gateway:
    """Build a mounted gateway row."""
    return Gateway(
        id=gateway_id,
        name=f"gateway-{gateway_id}",
        slug=f"gateway-{gateway_id}",
        url="http://127.0.0.1:9/mcp",
        capabilities={},
    )


def _tool(tool_id: str, gateway_id: str) -> Tool:
    """Build a tool owned by a gateway."""
    return Tool(
        id=tool_id,
        original_name=f"tool-{tool_id}",
        custom_name=f"tool-{tool_id}",
        custom_name_slug=f"tool-{tool_id}",
        input_schema={},
        gateway_id=gateway_id,
    )


def test_aggregate_gateway_invocation_metrics_sums_children_and_keeps_error_text(test_db, monkeypatch):
    """Counts, duration, and exact failure text stay scoped to one gateway."""
    monkeypatch.setattr(mqs.settings, "metrics_delete_raw_after_rollup", False)
    monkeypatch.setattr(mqs.settings, "metrics_retention_days", 30)
    monkeypatch.setattr(mqs.settings, "metrics_daily_rollup_enabled", False)

    gateway_id = uuid.uuid4().hex
    other_id = uuid.uuid4().hex
    tool_id = uuid.uuid4().hex
    resource_id = uuid.uuid4().hex
    prompt_id = uuid.uuid4().hex
    other_tool_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    db_error = "UNIQUE constraint failed: tools.original_name"

    test_db.add_all([_gateway(gateway_id), _gateway(other_id)])
    test_db.add_all(
        [
            _tool(tool_id, gateway_id),
            _tool(other_tool_id, other_id),
            Resource(id=resource_id, uri=f"res://{resource_id}", name="resource", gateway_id=gateway_id, text_content="body"),
            Prompt(
                id=prompt_id,
                original_name=f"prompt-{prompt_id}",
                custom_name=f"prompt-{prompt_id}",
                custom_name_slug=f"prompt-{prompt_id}",
                name=f"prompt-{prompt_id}",
                template="hello",
                argument_schema={},
                gateway_id=gateway_id,
            ),
        ]
    )
    test_db.add_all(
        [
            ToolMetric(tool_id=tool_id, response_time=1.5, is_success=True, timestamp=now),
            ToolMetric(tool_id=tool_id, response_time=2.5, is_success=False, error_message=db_error, timestamp=now),
            ToolMetric(tool_id=tool_id, response_time=0.5, is_success=False, error_message=None, timestamp=now),
            ResourceMetric(resource_id=resource_id, response_time=0.25, is_success=False, error_message=db_error, timestamp=now),
            PromptMetric(prompt_id=prompt_id, response_time=0.75, is_success=True, timestamp=now),
            ToolMetric(tool_id=other_tool_id, response_time=9.0, is_success=False, error_message="other gateway", timestamp=now),
        ]
    )
    test_db.commit()

    result = mqs.aggregate_gateway_invocation_metrics(test_db, gateway_id)

    assert result["gateway_id"] == gateway_id
    assert result["total_executions"] == 5
    assert result["failed_executions"] == 3
    assert abs(result["total_response_time"] - 5.5) < 0.001
    assert result["failure_reasons"] == [{"error_message": db_error, "count": 2}]


def test_aggregate_gateway_invocation_metrics_uses_rollup_without_double_counting(test_db, monkeypatch):
    """Older raw rows inside a rolled-up window contribute the message, not a second count."""
    monkeypatch.setattr(mqs.settings, "metrics_delete_raw_after_rollup", False)
    monkeypatch.setattr(mqs.settings, "metrics_retention_days", 1)
    monkeypatch.setattr(mqs.settings, "metrics_daily_rollup_enabled", False)

    gateway_id = uuid.uuid4().hex
    tool_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=3)
    test_db.add(_gateway(gateway_id))
    test_db.add(_tool(tool_id, gateway_id))
    test_db.add(
        ToolMetricsHourly(
            tool_id=tool_id,
            tool_name="echo",
            hour_start=old_time.replace(minute=0, second=0, microsecond=0),
            total_count=5,
            success_count=3,
            failure_count=2,
            avg_response_time=1.0,
        )
    )
    test_db.add_all(
        [
            ToolMetric(tool_id=tool_id, response_time=9.0, is_success=False, error_message="rolled up driver error", timestamp=old_time),
            ToolMetric(tool_id=tool_id, response_time=0.5, is_success=True, timestamp=now),
            ToolMetric(tool_id=tool_id, response_time=1.5, is_success=False, error_message="live driver error", timestamp=now),
        ]
    )
    test_db.commit()

    result = mqs.aggregate_gateway_invocation_metrics(test_db, gateway_id)

    assert result["total_executions"] == 7
    assert result["failed_executions"] == 3
    assert abs(result["total_response_time"] - 7.0) < 0.001
    assert result["failure_reasons"] == [
        {"error_message": "live driver error", "count": 1},
        {"error_message": "rolled up driver error", "count": 1},
    ]


def test_aggregate_gateway_invocation_metrics_limits_failure_reasons(test_db, monkeypatch):
    """The reason list keeps the most frequent stored messages."""
    monkeypatch.setattr(mqs.settings, "metrics_delete_raw_after_rollup", False)
    monkeypatch.setattr(mqs.settings, "metrics_retention_days", 30)
    monkeypatch.setattr(mqs.settings, "metrics_daily_rollup_enabled", False)

    gateway_id = uuid.uuid4().hex
    tool_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    test_db.add(_gateway(gateway_id))
    test_db.add(_tool(tool_id, gateway_id))
    test_db.add_all(
        [
            ToolMetric(tool_id=tool_id, response_time=0.1, is_success=False, error_message="aaa", timestamp=now),
            ToolMetric(tool_id=tool_id, response_time=0.1, is_success=False, error_message="bbb", timestamp=now),
            ToolMetric(tool_id=tool_id, response_time=0.1, is_success=False, error_message="bbb", timestamp=now),
            ToolMetric(tool_id=tool_id, response_time=0.1, is_success=False, error_message="ccc", timestamp=now),
            ToolMetric(tool_id=tool_id, response_time=0.1, is_success=False, error_message="ccc", timestamp=now),
            ToolMetric(tool_id=tool_id, response_time=0.1, is_success=False, error_message="ccc", timestamp=now),
        ]
    )
    test_db.commit()

    result = mqs.aggregate_gateway_invocation_metrics(test_db, gateway_id, failure_reason_limit=2)

    assert result["failure_reasons"] == [
        {"error_message": "ccc", "count": 3},
        {"error_message": "bbb", "count": 2},
    ]


def test_aggregate_gateway_invocation_metrics_partitions_daily_hourly_and_raw(test_db, monkeypatch):
    """Daily, hourly, and raw windows contribute once each."""
    monkeypatch.setattr(mqs.settings, "metrics_delete_raw_after_rollup", False)
    monkeypatch.setattr(mqs.settings, "metrics_retention_days", 2)
    monkeypatch.setattr(mqs.settings, "metrics_daily_rollup_enabled", True)
    monkeypatch.setattr(mqs.settings, "metrics_daily_rollup_cutoff_days", 10)

    cutoff = mqs.get_retention_cutoff()
    daily_cutoff = mqs.get_daily_retention_cutoff()
    gateway_id = uuid.uuid4().hex
    tool_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    test_db.add(_gateway(gateway_id))
    test_db.add(_tool(tool_id, gateway_id))
    test_db.add_all(
        [
            ToolMetricsDaily(
                tool_id=tool_id,
                tool_name="echo",
                day_start=daily_cutoff - timedelta(days=1),
                total_count=4,
                success_count=3,
                failure_count=1,
                avg_response_time=2.0,
            ),
            ToolMetricsHourly(
                tool_id=tool_id,
                tool_name="echo",
                hour_start=daily_cutoff - timedelta(hours=3),
                total_count=100,
                success_count=100,
                failure_count=0,
                avg_response_time=9.0,
            ),
            ToolMetricsHourly(
                tool_id=tool_id,
                tool_name="echo",
                hour_start=cutoff - timedelta(hours=2),
                total_count=3,
                success_count=2,
                failure_count=1,
                avg_response_time=1.0,
            ),
            ToolMetric(tool_id=tool_id, response_time=9.0, is_success=False, error_message="stale raw error", timestamp=cutoff - timedelta(hours=3)),
            ToolMetric(tool_id=tool_id, response_time=0.5, is_success=True, timestamp=now),
        ]
    )
    test_db.commit()

    result = mqs.aggregate_gateway_invocation_metrics(test_db, gateway_id)

    assert result["total_executions"] == 8
    assert result["failed_executions"] == 2
    assert abs(result["total_response_time"] - 11.5) < 0.001
    assert result["failure_reasons"] == [{"error_message": "stale raw error", "count": 1}]


def test_aggregate_gateway_invocation_metrics_empty_gateway(test_db):
    """A gateway with no invocations returns zeros."""
    gateway_id = uuid.uuid4().hex
    test_db.add(_gateway(gateway_id))
    test_db.commit()

    result = mqs.aggregate_gateway_invocation_metrics(test_db, gateway_id)

    assert result["total_executions"] == 0
    assert result["failed_executions"] == 0
    assert result["total_response_time"] == 0.0
    assert result["failure_reasons"] == []
