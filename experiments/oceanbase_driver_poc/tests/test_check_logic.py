"""L1: the judgement rules, driven by fakes rather than by a database.

The Query Timeout rule is the reason this layer exists. The requirement asks only
whether a timeout is observed, but the downstream data-source design requires the
server-side query to actually stop, and records that the existing execution path
fails exactly there. A POC that accepts client-side evidence alone would certify a
capability the product does not have.
"""

from __future__ import annotations

from common.checks import CheckSpec
from common.config import ConnectionConfig
from common.errors import ErrorCode
from common.evidence import ServerStopEvidence, classify_failure, judge_query_timeout
from common.harness import CheckOutcome, run_mode
from common.redaction import Redactor
from common.results import CheckStatus

CONFIG = ConnectionConfig(mode="oracle", host="ob-proxy.example.internal", port=2883, user="appuser", password="pw", service_name="svc1")


class ScriptedDriver:
    """A driver that returns a different status per check id."""

    name = "scripted"
    version = "test"
    installed = True

    def __init__(self, statuses: dict[str, CheckStatus]) -> None:
        """Record the status to return for each check id."""
        self.statuses = statuses

    def run_check(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """Return the scripted status, defaulting to unsupported."""
        status = self.statuses.get(check.check_id, CheckStatus.UNSUPPORTED)
        return CheckOutcome(status=status, summary=f"{check.check_id} -> {status.value}")


class TestQueryTimeoutRule:
    """Only a stopped server-side query passes."""

    def test_no_client_timeout_is_a_failure(self) -> None:
        """If the caller never observed a timeout, nothing else matters."""
        assert judge_query_timeout(client_timed_out=False, server=ServerStopEvidence.STOPPED)[0] is CheckStatus.FAIL
        assert judge_query_timeout(client_timed_out=False, server=ServerStopEvidence.STILL_RUNNING)[0] is CheckStatus.FAIL

    def test_client_timeout_with_server_stopped_passes(self) -> None:
        """This is the only combination that establishes the capability."""
        status, reason = judge_query_timeout(client_timed_out=True, server=ServerStopEvidence.STOPPED)
        assert status is CheckStatus.PASS
        assert "已停止" in reason

    def test_client_timeout_with_server_still_running_fails(self) -> None:
        """This is the failure mode the downstream design already suffers from."""
        status, reason = judge_query_timeout(client_timed_out=True, server=ServerStopEvidence.STILL_RUNNING)
        assert status is CheckStatus.FAIL
        assert status is not CheckStatus.PASS
        assert "仍在运行" in reason

    def test_unobservable_server_outcome_is_not_a_pass(self) -> None:
        """Without an observation channel we do not get to claim success."""
        status, reason = judge_query_timeout(client_timed_out=True, server=ServerStopEvidence.UNDETERMINED)
        assert status is CheckStatus.INDETERMINATE
        assert status is not CheckStatus.PASS
        assert "无法判定" in reason

    def test_no_observation_situation_yields_pass(self) -> None:
        """Exhaustive guard: PASS appears exactly once across the whole truth table.

        Six combinations: the three where the caller never saw a timeout all fail
        regardless of the server, and of the three where it did, only a confirmed
        server stop passes.
        """
        passes = [judge_query_timeout(client_timed_out=client, server=server)[0] for client in (True, False) for server in ServerStopEvidence]
        assert len(passes) == 6
        assert passes.count(CheckStatus.PASS) == 1
        assert passes.count(CheckStatus.FAIL) == 4
        assert passes.count(CheckStatus.INDETERMINATE) == 1


class TestNoExtrapolation:
    """Connecting successfully says nothing about the other eight checks."""

    def test_a_passing_connect_does_not_promote_other_checks(self) -> None:
        """The harness never derives one check's status from another's."""
        report = run_mode(
            "oracle",
            config=CONFIG,
            driver=ScriptedDriver({"O1": CheckStatus.PASS}),
            redactor=Redactor(),
        )
        by_id = {check.check_id: check.status for check in report.checks}
        assert by_id["O1"] is CheckStatus.PASS
        for check_id in ("O2", "O3", "O4", "O5", "O6", "O7", "O8", "O9"):
            assert by_id[check_id] is not CheckStatus.PASS

    def test_unimplemented_checks_are_marked_as_such(self) -> None:
        """An unfinished skeleton cannot be mistaken for a passing one."""
        report = run_mode("oracle", config=CONFIG, driver=None, redactor=Redactor())
        for check in report.checks:
            assert check.status is CheckStatus.UNSUPPORTED
            assert check.evidence["unimplemented"] is True

    def test_unsupported_is_not_failure(self) -> None:
        """A capability the driver cannot provide is reported distinctly from a failure."""
        report = run_mode("oracle", config=CONFIG, driver=None, redactor=Redactor())
        assert all(check.status is not CheckStatus.FAIL for check in report.checks)
        assert report.to_dict()["aggregate"]["counts"]["UNSUPPORTED"] == 9


class TestFailureClassification:
    """Escaped driver failures land in the shared vocabulary."""

    def test_unrecognised_message_falls_back_without_losing_the_exception(self) -> None:
        """An unclassifiable failure still records what went wrong."""
        code, native = classify_failure("oracle", RuntimeError("something odd happened"))
        assert code is ErrorCode.DB_DRIVER_ERROR
        assert native is None

    def test_mode_selects_the_mapping(self) -> None:
        """The same text classifies differently per mode, as it must."""
        assert classify_failure("oracle", RuntimeError("ORA-01017"))[0] is ErrorCode.DB_AUTH_FAILED
        assert classify_failure("mysql", RuntimeError("(1045, 'Access denied')"))[0] is ErrorCode.DB_AUTH_FAILED
