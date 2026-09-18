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
from common.checks import MYSQL_CHECKS, ORACLE_CHECKS, CheckSpec
from common.config import ConnectionConfig
from common.errors import ErrorCode
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

    def test_every_declared_check_has_a_handler(self) -> None:
        """The suite covers every declared check, so none can regress to skipped."""
        driver = mysql_driver.PyMySQLDriver(object(), "1.2.3", Redactor())
        declared = {spec.check_id for spec in MYSQL_CHECKS}
        assert declared - driver.implemented_checks == set()

    def test_unknown_check_id_reports_unimplemented(self) -> None:
        """An id with no handler is reported explicitly rather than silently absent."""
        driver = mysql_driver.PyMySQLDriver(object(), "1.2.3", Redactor())
        outcome = driver.run_check(CheckSpec("M99", "不存在的检查", "desc"), MYSQL_CONFIG)
        assert outcome.status is CheckStatus.UNSUPPORTED
        assert outcome.evidence == {"unimplemented": True, "reason": "l2-pending"}

    def test_factory_returns_a_driver_even_without_the_package(self) -> None:
        """The factory never raises; absence is represented, not thrown."""
        driver = mysql_driver.driver(Redactor())
        assert driver.name == "pymysql"
        assert isinstance(driver.version, str) and driver.version


class TestMySQLProbeHelpers:
    """Pure pieces the L2 checks depend on, testable without a server."""

    def test_compat_mode_shapes_are_normalised(self) -> None:
        """The two candidate probes return different row shapes for the same fact."""
        assert mysql_driver.normalize_compat_mode("variable", ("oracle",)) == "oracle"
        assert mysql_driver.normalize_compat_mode("show-variables", ("ob_compatibility_mode", "oracle")) == "oracle"

    def test_absent_compat_mode_is_none_not_a_default(self) -> None:
        """A probe that returned nothing must not be reported as some mode."""
        assert mysql_driver.normalize_compat_mode("variable", None) is None
        assert mysql_driver.normalize_compat_mode("variable", (None,)) is None
        assert mysql_driver.normalize_compat_mode("show-variables", ("only_name",)) is None

    def test_pool_acquire_timeout_is_bounded(self) -> None:
        """Acquisition must fail promptly rather than inherit an unbounded wait."""
        assert mysql_driver.pool_acquire_timeout_s(ConnectionConfig(mode="mysql", host="h", port=1, user="u", password="p", connect_timeout_s=0.1)) == 1.0
        assert mysql_driver.pool_acquire_timeout_s(ConnectionConfig(mode="mysql", host="h", port=1, user="u", password="p", connect_timeout_s=2.0)) == 2.0
        assert mysql_driver.pool_acquire_timeout_s(ConnectionConfig(mode="mysql", host="h", port=1, user="u", password="p", connect_timeout_s=30.0)) == 5.0

    def test_timeout_probe_carries_a_unique_marker(self) -> None:
        """The observation query identifies one run's statement on a shared server."""
        first, second = mysql_driver.new_probe_marker(), mysql_driver.new_probe_marker()
        assert first != second
        assert first in mysql_driver.build_timeout_probe(5, first)

    def test_probe_connect_kwargs_apply_the_overrides(self) -> None:
        """Each error probe varies exactly one connect parameter."""
        base = ConnectionConfig(mode="mysql", host="h", port=2883, user="u", password="p", database="d")
        by_db = next(probe for probe in mysql_driver.ERROR_PROBES if probe.override_database)
        by_user = next(probe for probe in mysql_driver.ERROR_PROBES if probe.override_user)
        assert mysql_driver.build_probe_connect_kwargs(base, by_db)["database"] == by_db.override_database
        assert mysql_driver.build_probe_connect_kwargs(base, by_user)["user"] == by_user.override_user

    def test_error_probe_expectations_are_non_empty_code_tuples(self) -> None:
        """Every probe must declare at least one acceptable classification."""
        for probe in mysql_driver.ERROR_PROBES:
            assert probe.expect, probe.key
            assert all(isinstance(code, ErrorCode) for code in probe.expect)
            assert probe.statement or probe.override_database or probe.override_user

    def test_probe_keys_are_unique(self) -> None:
        """Duplicated keys would collapse two findings into one in the report."""
        keys = [probe.key for probe in mysql_driver.ERROR_PROBES]
        assert len(keys) == len(set(keys))


class TestOracleProbeHelpers:
    """The Oracle equivalents of the same pure pieces."""

    def test_named_bind_sql_projects_every_case(self) -> None:
        """The statement is generated from the bind cases, so it cannot drift."""
        names = oracle_driver.bind_case_names()
        statement = oracle_driver.build_bind_sql(names)
        assert statement.startswith("SELECT :p0 AS p0")
        assert statement.endswith("FROM DUAL")
        assert len(names) == len(set(names))

    def test_timeout_probe_carries_a_unique_marker(self) -> None:
        """Same marker requirement as the MySQL mode."""
        marker = oracle_driver.new_probe_marker()
        assert marker in oracle_driver.build_timeout_probe(marker)

    def test_probe_connect_kwargs_apply_the_user_override(self) -> None:
        """The auth probe replaces the account and nothing else."""
        base = ConnectionConfig(mode="oracle", host="h", port=2883, user="u", password="p", service_name="svc")
        probe = next(probe for probe in oracle_driver.ERROR_PROBES if probe.override_user)
        kwargs = oracle_driver.build_probe_connect_kwargs(base, probe)
        assert kwargs["user"] == probe.override_user
        assert kwargs["dsn"] == oracle_driver.build_dsn(base)

    def test_error_probe_expectations_are_non_empty_code_tuples(self) -> None:
        """Every probe must declare at least one acceptable classification."""
        for probe in oracle_driver.ERROR_PROBES:
            assert probe.expect, probe.key
            assert all(isinstance(code, ErrorCode) for code in probe.expect)
            assert probe.statement or probe.override_user

    def test_probe_keys_are_unique(self) -> None:
        """Duplicated keys would collapse two findings into one in the report."""
        keys = [probe.key for probe in oracle_driver.ERROR_PROBES]
        assert len(keys) == len(set(keys))
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

    def test_every_declared_check_has_a_handler(self) -> None:
        """The suite covers every declared check, so none can regress to skipped."""
        driver = oracle_driver.OracleDBDriver(object(), "3.0.0", Redactor())
        declared = {spec.check_id for spec in ORACLE_CHECKS}
        assert declared - driver.implemented_checks == set()

    def test_unknown_check_id_reports_unimplemented(self) -> None:
        """An id with no handler is reported explicitly rather than silently absent."""
        driver = oracle_driver.OracleDBDriver(object(), "3.0.0", Redactor())
        outcome = driver.run_check(CheckSpec("O99", "不存在的检查", "desc"), ORACLE_CONFIG)
        assert outcome.status is CheckStatus.UNSUPPORTED
        assert outcome.evidence == {"unimplemented": True, "reason": "l2-pending"}
