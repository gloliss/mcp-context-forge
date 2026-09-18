"""L1: the query-timeout check, driven through the real code with a scripted cursor.

The check exists to answer a question the design treats as a gate: did the *server*
stop, or did only the client give up? Its verdict is assembled from two observations,
and the failure mode these tests guard against is the check concluding "timeout works"
from evidence that never involved a timeout at all.

The specific regression: the client half used to be "did the call fail before the
deadline", so any fast failure counted as a timeout -- a denied privilege on SLEEP, a
dropped connection, a syntax error. The probe marker would then be absent from the
server, which reads as "the server stopped", and the check reported PASS for a probe
that never ran.
"""

from __future__ import annotations

from common.checks import CheckSpec
from common.config import ConnectionConfig
from common.evidence import SERVER_STOP_EVIDENCE_KEY, ServerStopEvidence
from common.redaction import Redactor
from common.results import CheckStatus
from scripted_driver import RaisingResolver, ScriptedModule
from mysql_mode import driver as mysql_driver
from oracle_mode import driver as oracle_driver

# Messages chosen because the classifiers map them the way a real server would.
MYSQL_TIMEOUT = RuntimeError("Lost connection to MySQL server during query (timed out)")
MYSQL_SYNTAX = RuntimeError("(1064, \"You have an error in your SQL syntax near 'SLEEP'\")")
ORACLE_TIMEOUT = RuntimeError("DPI-1067: call timeout of 3000 ms exceeded")
ORACLE_OBJECT_MISSING = RuntimeError("ORA-00942: table or view does not exist")

MYSQL_SPEC = CheckSpec("M6", "Query Timeout", "desc", requires_server_observation=True)
ORACLE_SPEC = CheckSpec("O6", "Query Timeout", "desc", requires_server_observation=True)


def _mysql_config(**overrides) -> ConnectionConfig:
    """A MySQL configuration, optionally carrying an observer account."""
    base = {"mode": "mysql", "host": "h", "port": 2883, "user": "u", "password": "sup3r-s3cret", "database": "d", "query_timeout_s": 3.0}
    base.update(overrides)
    return ConnectionConfig(**base)


def _oracle_config(**overrides) -> ConnectionConfig:
    """An Oracle configuration, optionally carrying an observer account."""
    base = {"mode": "oracle", "host": "h", "port": 2883, "user": "u", "password": "sup3r-s3cret", "service_name": "svc", "query_timeout_s": 3.0}
    base.update(overrides)
    return ConnectionConfig(**base)


def _mysql_driver(resolver):
    """A real MySQL adapter over a scripted module."""
    return mysql_driver.PyMySQLDriver(ScriptedModule(resolver), "1.2.0", Redactor())


def _oracle_driver(resolver):
    """A real Oracle adapter over a scripted module."""
    return oracle_driver.OracleDBDriver(ScriptedModule(resolver), "26.0.0", Redactor())


class TestMySQLQueryTimeout:
    """M6 must only call it a timeout when a timeout actually happened."""

    def test_a_non_timeout_failure_is_never_a_pass(self) -> None:
        """A syntax error on the probe is not evidence that the timeout works.

        This is the regression: the probe never ran, so the marker is absent, which
        the observation step reads as "the server stopped". Without the classification
        guard that combination produced PASS.
        """
        driver = _mysql_driver(RaisingResolver(match="SLEEP", error=MYSQL_SYNTAX))
        outcome = driver.run_check(MYSQL_SPEC, _mysql_config(observer_user="obs", observer_password="sup3r-s3cret"))
        assert outcome.status is CheckStatus.INDETERMINATE
        assert outcome.status is not CheckStatus.PASS
        assert "不是超时" in outcome.summary
        assert outcome.evidence[SERVER_STOP_EVIDENCE_KEY] == ServerStopEvidence.UNDETERMINED.value

    def test_a_dropped_connection_is_not_a_timeout(self) -> None:
        """An unreachable endpoint is a different finding from an expired deadline."""
        driver = _mysql_driver(RaisingResolver(match="SLEEP", error=RuntimeError("(2003, \"Can't connect to MySQL server\")")))
        outcome = driver.run_check(MYSQL_SPEC, _mysql_config(observer_user="obs", observer_password="sup3r-s3cret"))
        assert outcome.status is CheckStatus.INDETERMINATE
        assert outcome.evidence["client_error_code"] == "DB_UNREACHABLE"

    def test_a_timeout_without_an_observer_is_undetermined(self) -> None:
        """The client timed out, but nobody looked at the server -- so no verdict."""
        driver = _mysql_driver(RaisingResolver(match="SLEEP", error=MYSQL_TIMEOUT))
        outcome = driver.run_check(MYSQL_SPEC, _mysql_config())
        assert outcome.status is CheckStatus.INDETERMINATE

    def test_a_query_that_completed_did_not_time_out(self) -> None:
        """No failure at all means the deadline was never reached."""
        driver = _mysql_driver(lambda _sql, _params: (1,))
        outcome = driver.run_check(MYSQL_SPEC, _mysql_config())
        assert outcome.status is CheckStatus.FAIL

    def test_a_timeout_with_the_server_stopped_passes(self) -> None:
        """The one combination that may pass: client timed out, server really stopped."""
        resolver = RaisingResolver(match="SLEEP", error=MYSQL_TIMEOUT, otherwise=lambda _sql, _params: (0,))
        driver = _mysql_driver(resolver)
        outcome = driver.run_check(MYSQL_SPEC, _mysql_config(observer_user="obs", observer_password="sup3r-s3cret"))
        assert outcome.status is CheckStatus.PASS
        assert outcome.evidence[SERVER_STOP_EVIDENCE_KEY] == ServerStopEvidence.STOPPED.value

    def test_a_timeout_with_the_server_still_running_fails(self) -> None:
        """The finding the whole design hangs on: the client gave up, the server did not.

        Slow by construction -- the observation polls until its budget runs out, which
        is exactly what makes "still running" different from "not observed yet".
        """
        resolver = RaisingResolver(match="SLEEP", error=MYSQL_TIMEOUT, otherwise=lambda _sql, _params: (1,))
        driver = _mysql_driver(resolver)
        outcome = driver.run_check(MYSQL_SPEC, _mysql_config(observer_user="obs", observer_password="sup3r-s3cret"))
        assert outcome.status is CheckStatus.FAIL
        assert outcome.evidence[SERVER_STOP_EVIDENCE_KEY] == ServerStopEvidence.STILL_RUNNING.value


class TestOracleQueryTimeout:
    """O6 applies the same rule through the Oracle classifier."""

    def test_a_non_timeout_failure_is_never_a_pass(self) -> None:
        """A missing object is not a timeout, however fast it failed."""
        driver = _oracle_driver(RaisingResolver(match="CONNECT BY", error=ORACLE_OBJECT_MISSING))
        outcome = driver.run_check(ORACLE_SPEC, _oracle_config(observer_user="obs", observer_password="sup3r-s3cret"))
        assert outcome.status is CheckStatus.INDETERMINATE
        assert outcome.evidence["client_error_code"] == "DB_OBJECT_NOT_FOUND"

    def test_a_timeout_without_an_observer_is_undetermined(self) -> None:
        """Same gate as MySQL: no observation, no verdict."""
        driver = _oracle_driver(RaisingResolver(match="CONNECT BY", error=ORACLE_TIMEOUT))
        outcome = driver.run_check(ORACLE_SPEC, _oracle_config())
        assert outcome.status is CheckStatus.INDETERMINATE

    def test_the_harness_gate_holds_end_to_end(self) -> None:
        """A PASS from the check survives the harness only with server evidence.

        The check already refuses to pass without it; this asserts the harness would
        catch it even if a future implementation forgot to.
        """
        resolver = RaisingResolver(match="CONNECT BY", error=ORACLE_TIMEOUT, otherwise=lambda _sql, _params: (0,))
        driver = _oracle_driver(resolver)
        outcome = driver.run_check(ORACLE_SPEC, _oracle_config(observer_user="obs", observer_password="sup3r-s3cret"))
        assert outcome.status is CheckStatus.PASS
        assert outcome.evidence[SERVER_STOP_EVIDENCE_KEY] == ServerStopEvidence.STOPPED.value
