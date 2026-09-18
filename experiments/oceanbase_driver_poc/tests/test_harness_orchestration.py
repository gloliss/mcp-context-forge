"""L0: harness orchestration.

The harness is what decides whether a check ran, and its two skip paths are the
difference between "this machine has no OceanBase" and "the driver is not installed".
Those are different operational problems and must not collapse into one status.
"""

from __future__ import annotations

from common.checks import MYSQL_CHECKS, ORACLE_CHECKS, CheckSpec
from common.config import ConnectionConfig
from common.errors import DriverError, ErrorCode
from common.evidence import (
    INSTANCE_EVIDENCE_KEY,
    SERVER_STOP_EVIDENCE_KEY,
    ServerStopEvidence,
    build_instance_evidence,
)
from common.harness import CheckOutcome, run_mode
from common.redaction import MASK, Redactor
from common.results import CheckStatus

PASSWORD = "sup3r-s3cret"

CONFIG = ConnectionConfig(
    mode="mysql",
    host="ob-proxy.example.internal",
    port=2883,
    user="appuser@tenant1#cluster1",
    password=PASSWORD,
    database="tenant1",
)


class FakeDriver:
    """A driver that returns canned outcomes and raises on demand."""

    name = "fake"
    version = "0.0-test"
    installed = True

    def __init__(self, outcomes: dict[str, CheckOutcome] | None = None, raises: dict[str, BaseException] | None = None) -> None:
        """Record the canned behaviour keyed by check id."""
        self.outcomes = outcomes or {}
        self.raises = raises or {}

    def run_check(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """Return or raise per the canned behaviour."""
        if check.check_id in self.raises:
            raise self.raises[check.check_id]
        return self.outcomes.get(check.check_id, CheckOutcome(status=CheckStatus.UNSUPPORTED, summary=f"{check.check_id} 未设定期望"))


def test_missing_config_skips_every_check_and_never_passes() -> None:
    """No target means an honest skip, not a failure and certainly not a pass."""
    report = run_mode(
        "mysql",
        config=None,
        driver=FakeDriver(),
        redactor=Redactor(),
        missing_env=["OB_MYSQL_HOST", "OB_MYSQL_PORT"],
    )
    assert len(report.checks) == len(MYSQL_CHECKS)
    assert all(check.status is CheckStatus.SKIP_NO_ENV for check in report.checks)
    assert not any(check.status.counts_as_pass for check in report.checks)
    assert report.missing_env == ["OB_MYSQL_HOST", "OB_MYSQL_PORT"]
    assert report.exit_ok is True


def test_missing_driver_is_unsupported_not_skipped() -> None:
    """An absent driver package is a different problem from an absent target."""
    report = run_mode("mysql", config=CONFIG, driver=None, redactor=Redactor())
    assert all(check.status is CheckStatus.UNSUPPORTED for check in report.checks)
    assert all(check.evidence["unimplemented"] is True for check in report.checks)


def test_check_outcomes_are_recorded_verbatim() -> None:
    """A driver's outcome reaches the report unchanged."""
    driver = FakeDriver({"M1": CheckOutcome(status=CheckStatus.PASS, summary="连接成功", evidence={"version": "4.3.5.6"})})
    report = run_mode("mysql", config=CONFIG, driver=driver, redactor=Redactor())
    first = report.checks[0]
    assert first.check_id == "M1"
    assert first.status is CheckStatus.PASS
    assert first.evidence["version"] == "4.3.5.6"
    assert report.checks[1].status is CheckStatus.UNSUPPORTED


def test_escaped_exception_becomes_error_and_does_not_abort_the_sweep() -> None:
    """One exploding check must not stop the others from running."""
    driver = FakeDriver(
        {"O3": CheckOutcome(status=CheckStatus.PASS, summary="ok")},
        {"O2": RuntimeError("ORA-00942: table or view does not exist")},
    )
    report = run_mode("oracle", config=CONFIG, driver=driver, redactor=Redactor())
    assert [check.check_id for check in report.checks] == [spec.check_id for spec in ORACLE_CHECKS]
    broken = report.checks[1]
    assert broken.check_id == "O2"
    assert broken.status is CheckStatus.ERROR
    assert broken.error_code is ErrorCode.DB_OBJECT_NOT_FOUND
    assert broken.native_code == "ORA-00942"
    assert broken.duration_ms is not None
    assert report.checks[2].status is CheckStatus.PASS


def test_classified_driver_error_passes_its_own_code_through() -> None:
    """A driver that already classified its failure is not second-guessed."""
    driver = FakeDriver(raises={"O1": DriverError(ErrorCode.DB_PROTOCOL_UNSUPPORTED, "protocol rejected", native_code="DPY-3010")})
    report = run_mode(
        "oracle",
        config=CONFIG,
        driver=driver,
        redactor=Redactor(),
        checks=(CheckSpec("O1", "建立连接", "desc"),),
    )
    check = report.checks[0]
    assert check.status is CheckStatus.ERROR
    assert check.error_code is ErrorCode.DB_PROTOCOL_UNSUPPORTED
    assert check.native_code == "DPY-3010"


def test_secrets_are_redacted_on_the_way_out() -> None:
    """A driver cannot leak a credential into a report, even by accident."""
    driver = FakeDriver(
        {
            "M1": CheckOutcome(
                status=CheckStatus.FAIL,
                summary=f"auth failed for {PASSWORD} at appuser@tenant1#cluster1",
                evidence={"dsn": f"mysql://appuser:{PASSWORD}@ob-proxy.example.internal:2883/tenant1"},
            )
        }
    )
    report = run_mode("mysql", config=CONFIG, driver=driver, redactor=Redactor([PASSWORD]))
    check = report.checks[0]
    assert PASSWORD not in check.summary
    assert "tenant1" not in check.summary
    assert PASSWORD not in check.evidence["dsn"]
    assert MASK in check.summary


def test_target_view_is_redacted_on_the_report() -> None:
    """The configured target is recorded without its credential."""
    report = run_mode("mysql", config=CONFIG, driver=FakeDriver(), redactor=Redactor([PASSWORD]))
    assert report.target["password"] == MASK
    assert report.target["user"] == f"appuser@{MASK}"


def test_check_list_can_be_overridden() -> None:
    """Tests can narrow the sweep without touching the declared requirement list."""
    report = run_mode(
        "mysql",
        config=CONFIG,
        driver=FakeDriver(),
        redactor=Redactor(),
        checks=(CheckSpec("M1", "建立连接", "desc"),),
    )
    assert [check.check_id for check in report.checks] == ["M1"]


def test_report_carries_run_metadata() -> None:
    """The report records enough provenance to cite it from the conclusions doc."""
    report = run_mode("oracle", config=CONFIG, driver=FakeDriver(), redactor=Redactor(), runtime="python")
    assert report.mode == "oracle"
    assert report.runtime == "python"
    assert report.driver["name"] == "fake"
    assert report.started_at and report.finished_at
    assert isinstance(report.to_dict()["aggregate"], dict)


def test_all_checks_are_declared_once_per_mode() -> None:
    """The registry has no duplicate ids, so coverage cannot double-count."""
    for specs in (MYSQL_CHECKS, ORACLE_CHECKS):
        ids = [check.check_id for check in specs]
        assert len(ids) == len(set(ids)) == 9

    assert [check.check_id for check in MYSQL_CHECKS if check.requires_server_observation] == ["M6"]
    assert [check.check_id for check in ORACLE_CHECKS if check.requires_server_observation] == ["O6"]


class TestInstanceHandoff:
    """What a check learns about the instance must reach the report.

    The drivers read the version over the connection, and the report is what the
    result JSON and the generated conclusions sections read. Without an explicit
    handoff between the two, a successful connection still produces a document that
    says the instance version was never read.
    """

    @staticmethod
    def _driver_with_instance(**instance: object) -> FakeDriver:
        return FakeDriver(
            {
                "M1": CheckOutcome(
                    status=CheckStatus.PASS,
                    summary="连接成功",
                    evidence={INSTANCE_EVIDENCE_KEY: build_instance_evidence(version="4.3.5.6", compat_mode="oracle", compat_mode_source="v$parameter", probe_attempts=[], **instance)},
                )
            }
        )

    def test_version_and_mode_reach_the_report(self) -> None:
        """The report carries what the connection check read."""
        report = run_mode("mysql", config=CONFIG, driver=self._driver_with_instance(), redactor=Redactor())
        assert report.ob_version == "4.3.5.6"
        assert report.compat_mode == "oracle"
        assert report.to_dict()["compat_mode"] == "oracle"

    def test_a_missing_instance_payload_is_not_an_error(self) -> None:
        """A check that reports nothing about the instance leaves the fields unset."""
        report = run_mode("mysql", config=CONFIG, driver=FakeDriver(), redactor=Redactor())
        assert report.ob_version is None
        assert report.compat_mode is None

    def test_an_unreadable_mode_stays_empty_rather_than_guessed(self) -> None:
        """Failing to read the mode must not be reported as some default mode."""
        driver = FakeDriver(
            {
                "M1": CheckOutcome(
                    status=CheckStatus.PASS,
                    summary="连接成功",
                    evidence={INSTANCE_EVIDENCE_KEY: build_instance_evidence(version="4.3.5.6", compat_mode=None, compat_mode_source="unavailable", probe_attempts=["variable: unknown"])},
                )
            }
        )
        report = run_mode("mysql", config=CONFIG, driver=driver, redactor=Redactor())
        assert report.ob_version == "4.3.5.6"
        assert report.compat_mode is None


class TestServerObservationGate:
    """A Query Timeout PASS must rest on a server-side observation, not on prose."""

    SPEC = CheckSpec("O6", "Query Timeout", "desc", requires_server_observation=True)

    @staticmethod
    def _run(evidence: dict[str, object], status: CheckStatus = CheckStatus.PASS):
        driver = FakeDriver({"O6": CheckOutcome(status=status, summary="客户端超时", evidence=evidence)})
        return run_mode("oracle", config=CONFIG, driver=driver, redactor=Redactor(), checks=(TestServerObservationGate.SPEC,)).checks[0]

    def test_a_pass_without_server_evidence_is_downgraded(self) -> None:
        """The rule is enforced by the harness, so a check cannot opt out of it."""
        check = self._run({})
        assert check.status is CheckStatus.INDETERMINATE
        assert "未提供服务端停止证据" in check.summary

    def test_a_pass_with_a_confirmed_stop_survives(self) -> None:
        """A positive observation is what the gate accepts."""
        check = self._run({SERVER_STOP_EVIDENCE_KEY: ServerStopEvidence.STOPPED.value})
        assert check.status is CheckStatus.PASS

    def test_a_pass_claiming_the_server_is_still_running_is_downgraded(self) -> None:
        """A contradictory PASS must not survive on the strength of its own label."""
        check = self._run({SERVER_STOP_EVIDENCE_KEY: ServerStopEvidence.STILL_RUNNING.value})
        assert check.status is CheckStatus.INDETERMINATE

    def test_checks_without_the_requirement_are_untouched(self) -> None:
        """The gate applies only where the design says it applies."""
        driver = FakeDriver({"M1": CheckOutcome(status=CheckStatus.PASS, summary="连接成功", evidence={})})
        check = run_mode("mysql", config=CONFIG, driver=driver, redactor=Redactor(), checks=(CheckSpec("M1", "建立连接", "desc"),)).checks[0]
        assert check.status is CheckStatus.PASS

    def test_the_downgrade_records_why(self) -> None:
        """The evidence explains the downgrade rather than leaving it opaque."""
        check = self._run({})
        assert check.evidence[SERVER_STOP_EVIDENCE_KEY] == ServerStopEvidence.UNDETERMINED.value
