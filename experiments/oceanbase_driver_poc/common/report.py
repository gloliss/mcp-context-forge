"""Rendering of run reports for terminals and for the conclusions document.

The design requires every claim in ``docs/oceanbase-driver-poc.md`` to be traceable
to a real run's result JSON. The cheapest way to make that true is to generate the
result sections from the JSON rather than typing them by hand, so that is what
:func:`render_markdown` does.

The same rule as everywhere else applies: an unexecuted check is written as
unverified, never as a pass.
"""

from __future__ import annotations

from collections.abc import Sequence

from common.results import CheckStatus, RunReport, aggregate

_STATUS_LABEL: dict[CheckStatus, str] = {
    CheckStatus.PASS: "通过",
    CheckStatus.FAIL: "失败",
    CheckStatus.SKIP_NO_ENV: "未验证（缺环境）",
    CheckStatus.UNSUPPORTED: "驱动不支持",
    CheckStatus.INDETERMINATE: "无法判定",
    CheckStatus.ERROR: "执行异常",
}


def render_table(reports: Sequence[RunReport]) -> str:
    """Render a compact terminal table of every check across several reports.

    Args:
        reports: The reports to render.

    Returns:
        A plain-text table with one row per check.
    """
    lines = [f"{'模式':<7} {'检查':<5} {'状态':<16} 说明"]
    for report in reports:
        for check in report.checks:
            lines.append(f"{report.mode:<7} {check.check_id:<5} {_STATUS_LABEL[check.status]:<16} {_oneline(check.summary)}")
    return "\n".join(lines)


def render_summary(reports: Sequence[RunReport]) -> str:
    """Render the per-mode aggregate verdicts.

    Args:
        reports: The reports to summarise.

    Returns:
        One line per report, plus a note when nothing was executed.
    """
    lines: list[str] = []
    for report in reports:
        agg = aggregate(report.checks)
        lines.append(f"[{report.mode}] 驱动 {report.driver.get('name')} {report.driver.get('version')} | {agg['verdict']}（{agg['passed']}/{agg['total']} 通过，{agg['not_executed']} 未执行）")
        if report.missing_env:
            lines.append(f"  缺少环境变量：{', '.join(report.missing_env)}")
    return "\n".join(lines)


def render_markdown(reports: Sequence[RunReport]) -> str:
    """Render the measured sections of the conclusions document from run reports.

    Args:
        reports: The reports to render, typically one per mode.

    Returns:
        A Markdown fragment covering driver, version, per-mode results, and known
        issues. Claims about pool and timeout support are left to the reader's
        judgement of the individual checks rather than restated, so the fragment
        cannot assert more than the checks actually established.
    """
    lines: list[str] = ["<!-- 本段由 run_poc.py --md-out 生成，结论取自当次 results/*.json -->", ""]

    for report in reports:
        agg = aggregate(report.checks)
        lines.append(f"## {report.mode} Mode 验证结果")
        lines.append("")
        lines.append(f"- Driver：`{report.driver.get('name')}`")
        lines.append(f"- Driver 版本：`{report.driver.get('version')}`")
        lines.append(f"- Runtime：`{report.runtime}`")
        lines.append(f"- 实例版本：`{report.ob_version or '未读取'}`")
        lines.append(f"- 汇总结论：**{agg['verdict']}**")
        lines.append("")
        lines.append("| 检查 | 项目 | 状态 | 说明 |")
        lines.append("|---|---|---|---|")
        for check in report.checks:
            lines.append(f"| {check.check_id} | {check.name} | {_STATUS_LABEL[check.status]} | {_oneline(check.summary)} |")
        lines.append("")

        pending = [check.check_id for check in report.checks if check.status is CheckStatus.SKIP_NO_ENV]
        if pending:
            lines.append(f"> 未验证项：{', '.join(pending)}（缺环境，不得据本表宣称通过）")
            lines.append("")

    return "\n".join(lines)


def _oneline(text: str) -> str:
    """Collapse a summary to a single table-safe line."""
    return " ".join((text or "").split()).replace("|", "\\|")
