"""L1: configuration-to-driver parameter mapping.

This is the part of the driver adapters that can be verified without a database, and
it is worth verifying because timeout units differ per driver and per mechanism:
OceanBase's own ``ob_query_timeout`` is in microseconds, python-oracledb's
``call_timeout`` is in milliseconds, and both drivers' socket connect timeouts are in
seconds. Getting one of those wrong produces a POC that appears to work while
measuring nothing.
"""

from __future__ import annotations

import pytest
from common.checks import CheckSpec
from common.config import ConnectionConfig
from common.redaction import MASK, Redactor
from common.results import CheckStatus
from mysql_mode import driver as mysql_driver
from oracle_mode import driver as oracle_driver

MYSQL_CONFIG = ConnectionConfig(
    mode="mysql",
    host="ob-proxy.example.internal",
    port=2883,
    user="appuser@tenant1#cluster1",
    password="sup3r-s3cret",
    database="tenant1",
    connect_timeout_s=4.0,
    query_timeout_s=12.0,
)

ORACLE_CONFIG = ConnectionConfig(
    mode="oracle",
    host="ob-proxy.example.internal",
    port=2883,
    user="appuser@tenant1#cluster1",
    password="sup3r-s3cret",
    service_name="svc1",
    connect_timeout_s=4.0,
    query_timeout_s=12.0,
)


class TestMySQLParameters:
    """PyMySQL takes seconds; OceanBase's session variable takes microseconds."""

    def test_connect_kwargs_carry_every_configured_value(self) -> None:
        """The configuration reaches the driver without being reinterpreted."""
        kwargs = mysql_driver.build_connect_kwargs(MYSQL_CONFIG)
        assert kwargs["host"] == "ob-proxy.example.internal"
        assert kwargs["port"] == 2883
        assert kwargs["user"] == "appuser@tenant1#cluster1"
        assert kwargs["database"] == "tenant1"
        assert kwargs["connect_timeout"] == 4.0
        assert kwargs["read_timeout"] == 12.0

    def test_session_timeout_is_emitted_in_microseconds(self) -> None:
        """A seconds/microseconds mix-up here would set a 12-microsecond timeout."""
        statements = mysql_driver.build_session_timeout_statements(MYSQL_CONFIG)
        assert statements == ["SET SESSION ob_query_timeout = 12000000"]

    def test_redacted_view_hides_the_password_and_the_tenant(self) -> None:
        """The describable form is safe to write into a result file."""
        described = mysql_driver.describe_connect_kwargs(MYSQL_CONFIG, Redactor([MYSQL_CONFIG.password]))
        assert described["password"] == MASK
        assert described["user"] == f"appuser@{MASK}"
        assert described["host"] == "ob-proxy.example.internal"

    def test_absent_package_is_unsupported_not_an_error(self) -> None:
        """A missing dependency must not look like a broken check."""
        driver = mysql_driver.PyMySQLDriver(None, "not-installed", Redactor())
        assert driver.installed is False
        outcome = driver.run_check(CheckSpec("M1", "建立连接", "desc"), MYSQL_CONFIG)
        assert outcome.status is CheckStatus.UNSUPPORTED
        assert outcome.evidence["reason"] == "driver-not-installed"

    def test_not_yet_wired_checks_are_marked_unimplemented(self) -> None:
        """The skeleton's L2 gaps are explicit rather than silently absent."""
        driver = mysql_driver.PyMySQLDriver(object(), "1.2.3", Redactor())
        outcome = driver.run_check(CheckSpec("M4", "Connection Pool", "desc"), MYSQL_CONFIG)
        assert outcome.status is CheckStatus.UNSUPPORTED
        assert outcome.evidence == {"unimplemented": True, "reason": "l2-pending"}

    def test_factory_returns_a_driver_even_without_the_package(self) -> None:
        """The factory never raises; absence is represented, not thrown."""
        driver = mysql_driver.driver(Redactor())
        assert driver.name == "pymysql"
        assert isinstance(driver.version, str) and driver.version


class TestOracleParameters:
    """python-oracledb mixes seconds and milliseconds across two settings."""

    def test_dsn_uses_the_service_name(self) -> None:
        """OceanBase is reached by service name, not by SID."""
        assert oracle_driver.build_dsn(ORACLE_CONFIG) == "ob-proxy.example.internal:2883/svc1"

    def test_dsn_falls_back_to_database(self) -> None:
        """A target configured only with a database still yields a usable DSN."""
        config = ConnectionConfig(mode="oracle", host="h", port=1, user="u", password="p", database="svc")
        assert oracle_driver.build_dsn(config) == "h:1/svc"

    def test_dsn_requires_a_service(self) -> None:
        """Silently defaulting the service would target the wrong database."""
        config = ConnectionConfig(mode="oracle", host="h", port=1, user="u", password="p")
        with pytest.raises(ValueError):
            oracle_driver.build_dsn(config)

    def test_connect_timeout_is_in_seconds(self) -> None:
        """The TCP connect timeout follows the configured seconds."""
        kwargs = oracle_driver.build_connect_kwargs(ORACLE_CONFIG)
        assert kwargs["tcp_connect_timeout"] == 4.0
        assert kwargs["dsn"] == "ob-proxy.example.internal:2883/svc1"

    def test_call_timeout_is_in_milliseconds(self) -> None:
        """A seconds/milliseconds mix-up here would set a 12-millisecond timeout."""
        assert oracle_driver.call_timeout_ms(ORACLE_CONFIG) == 12000

    def test_session_timeout_is_emitted_in_microseconds(self) -> None:
        """The session variable is the server-side counterpart to call_timeout."""
        assert oracle_driver.build_session_timeout_statements(ORACLE_CONFIG) == ["ALTER SESSION SET ob_query_timeout = 12000000"]

    def test_redacted_view_hides_the_password_and_the_tenant(self) -> None:
        """The describable form is safe to write into a result file."""
        described = oracle_driver.describe_connect_kwargs(ORACLE_CONFIG, Redactor([ORACLE_CONFIG.password]))
        assert described["password"] == MASK
        assert described["user"] == f"appuser@{MASK}"

    def test_absent_module_reports_unknown_client_mode(self) -> None:
        """Without the driver there is no thin/thick answer to give."""
        assert oracle_driver.describe_client_mode(None) == "unknown"

    def test_thin_and_thick_are_distinguished(self) -> None:
        """The client mode is recorded, because thin and thick carry different costs."""

        class ThinModule:
            """Stands in for python-oracledb running in thin mode."""

            @staticmethod
            def is_thin_mode() -> bool:
                """Report thin mode."""
                return True

        class ThickModule:
            """Stands in for python-oracledb running in thick mode."""

            @staticmethod
            def is_thin_mode() -> bool:
                """Report thick mode."""
                return False

        assert oracle_driver.describe_client_mode(ThinModule()) == "thin"
        assert oracle_driver.describe_client_mode(ThickModule()) == "thick"

    def test_client_mode_probe_that_raises_is_not_fatal(self) -> None:
        """A probe that refuses before client initialisation yields 'unknown'."""

        class BrokenModule:
            """Stands in for a probe that raises before initialisation."""

            @staticmethod
            def is_thin_mode() -> bool:
                """Fail the way an uninitialised client would."""
                raise RuntimeError("client not initialised")

        assert oracle_driver.describe_client_mode(BrokenModule()) == "unknown"

    def test_absent_package_is_unsupported_not_an_error(self) -> None:
        """A missing dependency must not look like a broken check."""
        driver = oracle_driver.OracleDBDriver(None, "not-installed", Redactor())
        outcome = driver.run_check(CheckSpec("O1", "建立连接", "desc"), ORACLE_CONFIG)
        assert outcome.status is CheckStatus.UNSUPPORTED
        assert outcome.evidence["reason"] == "driver-not-installed"

    def test_not_yet_wired_checks_are_marked_unimplemented(self) -> None:
        """The skeleton's L2 gaps are explicit rather than silently absent."""
        driver = oracle_driver.OracleDBDriver(object(), "3.0.0", Redactor())
        outcome = driver.run_check(CheckSpec("O6", "Query Timeout", "desc"), ORACLE_CONFIG)
        assert outcome.status is CheckStatus.UNSUPPORTED
        assert outcome.evidence == {"unimplemented": True, "reason": "l2-pending"}
