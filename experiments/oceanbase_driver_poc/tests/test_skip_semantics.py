"""L0: the result vocabulary, and the rule that a skip is never a pass.

This is the mechanism behind the requirement's "never fake a success". If these
assertions stop holding, every downstream claim in the conclusions document becomes
untrustworthy, so they are written against the aggregation and the document renderer
rather than against any one caller.
"""

from __future__ import annotations

import json

from common.results import ABSTAINED_STATUSES, UNCLEAN_STATUSES, CheckResult, CheckStatus, RunReport, aggregate
from common.report import render_markdown, render_summary, render_table

MODE = "mysql"


def result(check_id: str, status: CheckStatus, summary: str = "") -> CheckResult:
    """Build a check result for aggregation tests."""
    return CheckResult(check_id=check_id, mode=MODE, name=f"检查 {check_id}", status=status, summary=summary or check_id)


def report(checks: list[CheckResult]) -> RunReport:
    """Build a run report for aggregation tests."""
    return RunReport(
        mode=MODE,
        runtime="python",
        driver={"name": "fake", "version": "0"},
        target={},
        checks=checks,
        started_at="2026-09-17T00:00:00+00:00",
    )


def test_only_pass_counts_as_pass() -> None:
    """Every other state explicitly declines to be a pass."""
    assert CheckStatus.PASS.counts_as_pass
    for status in CheckStatus:
        if status is not CheckStatus.PASS:
            assert not status.counts_as_pass, status


def test_all_skipped_is_never_reported_as_passing() -> None:
    """A run that executed nothing reads as unverified."""
    agg = aggregate([result("M1", CheckStatus.SKIP_NO_ENV), result("M2", CheckStatus.SKIP_NO_ENV)])
    assert agg["all_passed"] is False
    assert agg["passed"] == 0
    assert agg["not_executed"] == 2
    assert "通过" not in agg["verdict"]
    assert agg["verdict"] == "未验证（缺环境）"


def test_mixed_pass_and_skip_does_not_read_as_all_passed() -> None:
    """A partial run must not be summarised as a clean pass."""
    agg = aggregate([result("M1", CheckStatus.PASS), result("M2", CheckStatus.SKIP_NO_ENV)])
    assert agg["all_passed"] is False
    assert agg["has_skips"] is True
    assert agg["verdict"] != "全部通过"
    assert "1 项未执行（缺环境）" in agg["verdict"]


def test_indeterminate_is_not_a_pass() -> None:
    """An unobservable outcome is reported as such, not as success."""
    agg = aggregate([result("O6", CheckStatus.INDETERMINATE)])
    assert agg["all_passed"] is False
    assert "无法判定" in agg["verdict"]
    assert "全部通过" not in agg["verdict"]


def test_all_executed_and_passed_is_the_only_clean_verdict() -> None:
    """Only a fully executed, fully passing set earns the clean wording."""
    agg = aggregate([result("M1", CheckStatus.PASS), result("M2", CheckStatus.PASS)])
    assert agg["all_passed"] is True
    assert agg["verdict"] == "全部通过"


def test_skips_do_not_make_a_run_unclean_but_failures_do() -> None:
    """The CLI distinguishes "nothing failed" from "nothing ran"."""
    assert report([result("M1", CheckStatus.SKIP_NO_ENV)]).exit_ok is True
    assert report([result("M1", CheckStatus.PASS)]).exit_ok is True
    assert report([result("M1", CheckStatus.FAIL)]).exit_ok is False
    assert report([result("M1", CheckStatus.ERROR)]).exit_ok is False
    assert report([result("M1", CheckStatus.INDETERMINATE)]).exit_ok is False


def test_unsupported_alone_is_not_unclean() -> None:
    """An uninstalled driver is an operational gap, not a failed capability."""
    assert report([result("M1", CheckStatus.UNSUPPORTED)]).exit_ok is True


def test_abstentions_and_failures_are_disjoint_sets() -> None:
    """Every status is classed exactly once, so the CLI cannot double-count."""
    assert set(UNCLEAN_STATUSES).isdisjoint(ABSTAINED_STATUSES)
    assert set(UNCLEAN_STATUSES) | set(ABSTAINED_STATUSES) | {CheckStatus.PASS} == set(CheckStatus)


def test_fail_on_skip_promotes_abstentions_but_a_pass_is_never_downgraded() -> None:
    """The two classes together explain every status; neither includes PASS."""
    assert CheckStatus.PASS not in UNCLEAN_STATUSES
    assert CheckStatus.PASS not in ABSTAINED_STATUSES
    assert set(ABSTAINED_STATUSES) == {CheckStatus.SKIP_NO_ENV, CheckStatus.UNSUPPORTED}


def test_document_renderer_marks_skips_as_unverified() -> None:
    """The generated document section cannot claim a skipped check passed."""
    md = render_markdown([report([result("M1", CheckStatus.PASS, "连接成功"), result("M2", CheckStatus.SKIP_NO_ENV, "未执行")])])
    assert "未验证（缺环境）" in md
    assert "未验证项：M2" in md
    assert "不得据本表宣称通过" in md


def test_terminal_summary_separates_not_executed_from_passed() -> None:
    """The terminal summary always states how many checks never ran."""
    summary = render_summary([report([result("M1", CheckStatus.PASS), result("M2", CheckStatus.SKIP_NO_ENV)])])
    assert "1/2 通过" in summary
    assert "1 未执行" in summary


def test_report_serialises_to_json_including_aggregate() -> None:
    """A report round-trips through JSON so a conclusion can cite a real file."""
    payload = report([result("M1", CheckStatus.PASS)]).to_dict()
    restored = json.loads(json.dumps(payload, ensure_ascii=False))
    assert restored["checks"][0]["check_id"] == "M1"
    assert restored["checks"][0]["status"] == "PASS"
    assert restored["aggregate"]["all_passed"] is True


def test_terminal_table_renders_every_check() -> None:
    """The table has one row per check, with a readable status label."""
    table = render_table([report([result("M1", CheckStatus.PASS), result("M2", CheckStatus.UNSUPPORTED)])])
    assert "M1" in table
    assert "M2" in table
    assert "驱动不支持" in table
