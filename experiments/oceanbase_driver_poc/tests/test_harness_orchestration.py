"""L0: harness orchestration.

The harness is what decides whether a check ran, and its two skip paths are the
difference between "this machine has no OceanBase" and "the driver is not installed".
Those are different operational problems and must not collapse into one status.
"""

from __future__ import annotations

from common.checks import MYSQL_CHECKS, ORACLE_CHECKS, CheckSpec
from common.config import ConnectionConfig
from common.errors import DriverError, ErrorCode
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
