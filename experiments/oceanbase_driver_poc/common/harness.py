"""The check harness: turns a mode, a configuration, and a driver into a report.

The harness owns three things the driver adapters must not each reimplement:

* the decision to skip rather than fail when the machine has no target for a mode;
* the guarantee that a check never passes without having been executed;
* redaction at the boundary, so nothing a driver produces reaches a file or a
  terminal without passing through the redactor first.

Drivers are injected rather than imported, which is what lets the L1 tests drive the
whole harness with a fake that raises on demand.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from common.checks import CheckSpec, checks_for
from common.config import ConnectionConfig
from common.errors import ErrorCode
from common.evidence import (
    INSTANCE_EVIDENCE_KEY,
    SERVER_STOP_EVIDENCE_KEY,
    ServerStopEvidence,
    classify_failure,
)
from common.redaction import Redactor
from common.results import CheckResult, CheckStatus, RunReport


@dataclass
class CheckOutcome:
    """What a driver reports back for one check, before the harness records it."""

    status: CheckStatus
    summary: str
    error_code: ErrorCode | None = None
    native_code: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class Driver(Protocol):
    """The contract a compatibility-mode driver adapter satisfies."""

    name: str
    version: str

    def run_check(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """Execute one verification item.

        Args:
            check: The verification item to execute.
            config: Connection settings for this mode.

        Returns:
            The outcome to record.
        """
        ...


def run_mode(
    mode: str,
    *,
    config: ConnectionConfig | None,
    driver: Driver | None,
    redactor: Redactor,
    missing_env: Sequence[str] = (),
    runtime: str = "python",
    checks: Sequence[CheckSpec] | None = None,
    driver_info: dict[str, Any] | None = None,
) -> RunReport:
    """Run every check for one mode and collect the results.

    Args:
        mode: ``"mysql"`` or ``"oracle"``.
        config: The mode's configuration, or ``None`` when the environment lacks the
            required variables.
        driver: The driver adapter, or ``None`` when the driver package is not
            installed.
        redactor: Redactor applied to everything a driver produces.
        missing_env: Required environment variables that were absent, recorded on
            the report so the skip is actionable.
        runtime: The runtime label recorded on the report.
        checks: Override the declared check list, for tests.
        driver_info: Overrides the driver descriptor recorded on the report.

    Returns:
        The report. When ``config`` is ``None`` every check is ``SKIP_NO_ENV``; when
        only the driver is missing every check is ``UNSUPPORTED``. Neither case can
        produce a pass.
    """
    specs = tuple(checks) if checks is not None else checks_for(mode)
    started_at = _now()

    if config is None:
        results = [_skipped(mode, spec, missing_env) for spec in specs]
        resolved_driver = driver_info or {"name": None, "version": None, "installed": False}
        target: dict[str, Any] = {"configured": False}
    elif driver is None:
        results = [_unsupported(mode, spec, "驱动未安装") for spec in specs]
        resolved_driver = driver_info or {"name": None, "version": None, "installed": False}
        target = config.redacted(redactor)
    else:
        resolved_driver = driver_info or {
            "name": getattr(driver, "name", type(driver).__name__),
            "version": getattr(driver, "version", "unknown"),
            "installed": True,
        }
        target = config.redacted(redactor)
        results = [_run_one(mode, spec, driver, config, redactor) for spec in specs]

    return RunReport(
        mode=mode,
        runtime=runtime,
        driver=redactor.redact_deep(resolved_driver),
        target=target,
        checks=results,
        started_at=started_at,
        finished_at=_now(),
        missing_env=list(missing_env),
        ob_version=_instance_field(results, "version"),
        compat_mode=_instance_field(results, "compat_mode"),
    )


def _instance_field(checks: Sequence[CheckResult], field: str) -> Any | None:
    """Lift one field out of whatever instance evidence the sweep collected.

    Args:
        checks: The check results from a sweep, in declaration order.
        field: The key to read from the instance evidence payload.

    Returns:
        The first non-empty value any check recorded for ``field``, or ``None`` when
        no check reported instance facts.
    """
    for check in checks:
        payload = check.evidence.get(INSTANCE_EVIDENCE_KEY)
        if isinstance(payload, dict):
            value = payload.get(field)
            if value:
                return value
    return None


def _enforce_server_observation(spec: CheckSpec, result: CheckResult) -> CheckResult:
    """Refuse a PASS that rests on client-side evidence alone.

    The design makes the server actually stopping its query a gate for the whole
    runtime selection, not a detail of one check. Stating that in prose is not
    enough: an implementation that returns PASS without ever looking at the server
    would sail through. Enforcing it here means the rule holds no matter how the
    check is written, and an unobservable server-side outcome degrades to
    ``INDETERMINATE`` rather than to a pass.

    Args:
        spec: The check specification, carrying the observation requirement.
        result: The result the driver produced.

    Returns:
        The result, downgraded to ``INDETERMINATE`` when a PASS lacks a positive
        server-side observation.
    """
    if not spec.requires_server_observation or result.status is not CheckStatus.PASS:
        return result

    observed = result.evidence.get(SERVER_STOP_EVIDENCE_KEY)
    if observed == ServerStopEvidence.STOPPED.value:
        return result

    return CheckResult(
        check_id=result.check_id,
        mode=result.mode,
        name=result.name,
        status=CheckStatus.INDETERMINATE,
        summary=f"{result.summary}（未提供服务端停止证据，不计为通过）",
        error_code=result.error_code,
        native_code=result.native_code,
        duration_ms=result.duration_ms,
        evidence={**result.evidence, "server_stop": observed or ServerStopEvidence.UNDETERMINED.value},
    )


def _run_one(mode: str, spec: CheckSpec, driver: Driver, config: ConnectionConfig, redactor: Redactor) -> CheckResult:
    """Execute one check, converting an escaped failure into a recorded status.

    Args:
        mode: The compatibility mode, selecting the error mapping.
        spec: The check to execute.
        driver: The driver adapter.
        config: Connection settings.
        redactor: Redactor applied to the summary and evidence.

    Returns:
        The recorded result. An exception escaping the driver is never allowed to
        abort the run or to pass: it becomes ``ERROR`` with a classified code.
    """
    started = time.perf_counter()
    try:
        outcome = driver.run_check(spec, config)
    except Exception as exc:  # a driver failure must not abort the sweep
        code, native = classify_failure(mode, exc)
        return CheckResult(
            check_id=spec.check_id,
            mode=mode,
            name=spec.name,
            status=CheckStatus.ERROR,
            summary=redactor.redact(str(exc)) or exc.__class__.__name__,
            error_code=code,
            native_code=native,
            duration_ms=_elapsed_ms(started),
            evidence={"exception": exc.__class__.__name__},
        )

    return _enforce_server_observation(
        spec,
        CheckResult(
            check_id=spec.check_id,
            mode=mode,
            name=spec.name,
            status=outcome.status,
            summary=redactor.redact(outcome.summary),
            error_code=outcome.error_code,
            native_code=outcome.native_code,
            duration_ms=_elapsed_ms(started),
            evidence=redactor.redact_deep(outcome.evidence),
        ),
    )


def _skipped(mode: str, spec: CheckSpec, missing_env: Sequence[str]) -> CheckResult:
    """Build the result for a check that could not run because the target is unset."""
    if missing_env:
        summary = "未执行：缺少连接环境变量 " + ", ".join(missing_env)
    else:
        summary = "未执行：未配置连接环境变量"
    return CheckResult(
        check_id=spec.check_id,
        mode=mode,
        name=spec.name,
        status=CheckStatus.SKIP_NO_ENV,
        summary=summary,
        evidence={"missing_env": list(missing_env), "unimplemented": False},
    )


def _unsupported(mode: str, spec: CheckSpec, reason: str) -> CheckResult:
    """Build the result for a check the available driver cannot provide."""
    return CheckResult(
        check_id=spec.check_id,
        mode=mode,
        name=spec.name,
        status=CheckStatus.UNSUPPORTED,
        summary=f"无法提供该能力：{reason}",
        evidence={"unimplemented": True, "reason": reason},
    )


def _elapsed_ms(started: float) -> float:
    """Return milliseconds elapsed since ``started``."""
    return round((time.perf_counter() - started) * 1000, 3)


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()
