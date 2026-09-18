"""The result contract for a POC run.

The status vocabulary is the mechanism that keeps "we never ran this" from being
read as "this passed". Every other part of the POC -- aggregation, the generated
conclusions document, the CLI exit code -- is built on it, and
``tests/test_skip_semantics.py`` pins the behaviour down so a later refactor cannot
quietly collapse two states into one.

A run that never executed a check because the environment was missing must not read
like a run that executed it and succeeded. ``SKIP_NO_ENV`` therefore exists as its
own state and ``aggregate()`` reports it separately rather than folding it into a
pass count.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from common.errors import ErrorCode


class CheckStatus(str, Enum):
    """Outcome of a single verification item.

    ``UNSUPPORTED`` covers "this driver could not give us the capability" -- whether
    because the capability is genuinely absent or because the POC has not wired it
    up yet. The ``unimplemented`` flag in a check's evidence distinguishes the two,
    so the distinction is machine-readable rather than lost in prose.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP_NO_ENV = "SKIP_NO_ENV"
    UNSUPPORTED = "UNSUPPORTED"
    INDETERMINATE = "INDETERMINATE"
    ERROR = "ERROR"

    @property
    def counts_as_pass(self) -> bool:
        """Whether this state may be reported as a pass.

        Returns:
            ``True`` only for :attr:`PASS`. Every other state, including
            ``SKIP_NO_ENV`` and ``INDETERMINATE``, is explicitly not a pass.
        """
        return self is CheckStatus.PASS


@dataclass
class CheckResult:
    """One verification item's outcome, with the evidence behind it."""

    check_id: str
    mode: str
    name: str
    status: CheckStatus
    summary: str
    error_code: ErrorCode | None = None
    native_code: str | None = None
    duration_ms: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render the check as a JSON-safe mapping.

        Returns:
            The check's fields with enum values flattened to strings.
        """
        return {
            "check_id": self.check_id,
            "mode": self.mode,
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
            "error_code": self.error_code.value if self.error_code else None,
            "native_code": self.native_code,
            "duration_ms": self.duration_ms,
            "evidence": self.evidence,
        }


@dataclass
class RunReport:
    """The full result of running one mode against one runtime."""

    mode: str
    runtime: str
    driver: dict[str, Any]
    target: dict[str, Any]
    checks: list[CheckResult]
    started_at: str
    schema_version: int = 1
    ob_version: str | None = None
    compat_mode: str | None = None
    finished_at: str | None = None
    missing_env: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Render the whole report as a JSON-safe mapping.

        Returns:
            The report including its aggregate view of the checks.
        """
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "runtime": self.runtime,
            "driver": self.driver,
            "target": self.target,
            "ob_version": self.ob_version,
            "compat_mode": self.compat_mode,
            "missing_env": self.missing_env,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "aggregate": aggregate(self.checks),
            "checks": [check.to_dict() for check in self.checks],
        }

    @property
    def exit_ok(self) -> bool:
        """Whether the run should be treated as clean by the CLI.

        Returns:
            ``False`` when any check failed, errored, or was indeterminate.
            ``SKIP_NO_ENV`` and ``UNSUPPORTED`` do not by themselves make a run
            unclean -- the CLI decides separately whether abstentions are fatal.
        """
        return not any(check.status in UNCLEAN_STATUSES for check in self.checks)


# Ran and did not pass, or could not be judged. These are unclean by default.
UNCLEAN_STATUSES: tuple[CheckStatus, ...] = (CheckStatus.FAIL, CheckStatus.ERROR, CheckStatus.INDETERMINATE)

# Nothing was established for the item, but nothing failed either. Only fatal when
# the caller asks for abstentions to be treated as failure.
ABSTAINED_STATUSES: tuple[CheckStatus, ...] = (CheckStatus.SKIP_NO_ENV, CheckStatus.UNSUPPORTED)


def aggregate(checks: Sequence[CheckResult]) -> dict[str, Any]:
    """Summarise a set of checks without ever promoting a non-PASS state.

    Args:
        checks: The check results to summarise.

    Returns:
        Counts per status, the executed/not-executed split, and a verdict string
        that never describes a skipped or indeterminate check as passing.
    """
    counts = {status.value: 0 for status in CheckStatus}
    for check in checks:
        counts[check.status.value] += 1

    total = len(checks)
    skipped = counts[CheckStatus.SKIP_NO_ENV.value]
    passed = counts[CheckStatus.PASS.value]

    return {
        "counts": counts,
        "total": total,
        "passed": passed,
        "not_executed": skipped,
        "all_passed": total > 0 and passed == total,
        "has_skips": skipped > 0,
        "verdict": _verdict(counts, total),
    }


def _verdict(counts: Mapping[str, int], total: int) -> str:
    """Phrase a check set's outcome so no non-PASS state reads as a pass.

    Args:
        counts: Per-status counts keyed by status value.
        total: Total number of checks.

    Returns:
        A human-readable verdict. "全部通过" appears only when every check passed.
    """
    if total == 0:
        return "无检查项"
    if counts[CheckStatus.PASS.value] == total:
        return "全部通过"
    if counts[CheckStatus.SKIP_NO_ENV.value] == total:
        return "未验证（缺环境）"

    parts: list[str] = []
    for label, status in (
        ("项通过", CheckStatus.PASS),
        ("项失败", CheckStatus.FAIL),
        ("项驱动不支持", CheckStatus.UNSUPPORTED),
        ("项无法判定", CheckStatus.INDETERMINATE),
        ("项执行异常", CheckStatus.ERROR),
        ("项未执行（缺环境）", CheckStatus.SKIP_NO_ENV),
    ):
        if counts[status.value]:
            parts.append(f"{counts[status.value]} {label}")
    return "，".join(parts)
