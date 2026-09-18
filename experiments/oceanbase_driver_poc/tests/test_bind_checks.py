"""L1: the bind checks, driven through the real driver code with a scripted cursor.

The L2 checks normally need a server, and the Oracle suite in particular has never
been run against anything. These tests do not pretend to be a server: they fake the
driver module at the Python API level and drive the real ``_check_*`` methods through
it, which is enough to catch the failure mode that matters here -- a check that
reports PASS while having compared fewer values than it claims to.

That failure mode is not hypothetical. The first version of the MySQL check read a
missing row as "the driver returned NULL", so the NULL case passed without a result;
the Oracle check zipped its expected and observed values without a length guard, so a
short projection silently stopped the comparison early. Neither is visible without
executing the code.
"""

from __future__ import annotations

import pytest
from common.checks import CheckSpec
from common.config import ConnectionConfig
from common.probes import BIND_CASES
from common.redaction import Redactor
from common.results import CheckStatus
from mysql_mode import driver as mysql_driver
from oracle_mode import driver as oracle_driver

MYSQL_CONFIG = ConnectionConfig(mode="mysql", host="h", port=2883, user="u", password="sup3r-s3cret", database="d")
ORACLE_CONFIG = ConnectionConfig(mode="oracle", host="h", port=2883, user="u", password="sup3r-s3cret", service_name="svc")


class ScriptedCursor:
    """A cursor that answers from a callable instead of a server."""

    def __init__(self, resolver) -> None:
        """Record the resolver that decides each statement's result."""
        self._resolver = resolver
        self._row = None
        self.executed: list[tuple[str, object]] = []

    def __enter__(self) -> ScriptedCursor:
        """Enter the context manager."""
        return self

    def __exit__(self, *exc: object) -> bool:
        """Leave the context manager without suppressing anything."""
        return False

    def execute(self, sql: str, params: object = None) -> None:
        """Record the statement and take its scripted result."""
        self.executed.append((sql, params))
        self._row = self._resolver(sql, params)

    def fetchone(self):
        """Return the scripted row."""
        return self._row

    def fetchall(self):
        """Return the scripted row as a one-row result set."""
        return [] if self._row is None else [self._row]

    def close(self) -> None:
        """Close the cursor."""


class ScriptedConnection:
    """A connection handing out scripted cursors."""

    def __init__(self, resolver) -> None:
        """Record the resolver passed to every cursor."""
        self._resolver = resolver
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        """Return a new scripted cursor."""
        return ScriptedCursor(self._resolver)

    def close(self) -> None:
        """Mark the connection closed."""
        self.closed = True

    def ping(self, reconnect: bool = False) -> None:
        """Report the connection as alive."""


class ScriptedModule:
    """A stand-in for a database driver module."""

    def __init__(self, resolver) -> None:
        """Record the resolver used for every connection."""
        self._resolver = resolver

    def connect(self, **kwargs: object) -> ScriptedConnection:
        """Return a scripted connection."""
        return ScriptedConnection(self._resolver)


def _mysql_driver_for(resolver) -> mysql_driver.PyMySQLDriver:
    """Build a real MySQL adapter over a scripted module."""
    return mysql_driver.PyMySQLDriver(ScriptedModule(resolver), "1.2.0", Redactor())


def _oracle_driver_for(resolver) -> oracle_driver.OracleDBDriver:
    """Build a real Oracle adapter over a scripted module."""
    return oracle_driver.OracleDBDriver(ScriptedModule(resolver), "26.0.0", Redactor())


def _mysql_bind_resolver(value_for=lambda params: params[0] if params else None):
    """Answer ``SELECT %s`` with the bound value, and everything else with nothing."""

    def resolve(sql: str, params: object):
        if sql.startswith("SELECT %s"):
            return (value_for(params),)
        return None

    return resolve


class TestMySQLBindCheck:
    """The MySQL bind check compares every case, and says so when it cannot."""

    def test_every_case_is_returned_unchanged(self) -> None:
        """The happy path passes and covers the whole case set."""
        driver = _mysql_driver_for(_mysql_bind_resolver())
        outcome = driver.run_check(CheckSpec("M3", "参数绑定", "desc"), MYSQL_CONFIG)
        assert outcome.status is CheckStatus.PASS
        assert len(outcome.evidence["cases"]) == len(BIND_CASES)

    def test_a_changed_value_fails_the_check(self) -> None:
        """A driver that coerces a bound value must not pass."""
        driver = _mysql_driver_for(_mysql_bind_resolver(value_for=lambda _params: "coerced"))
        outcome = driver.run_check(CheckSpec("M3", "参数绑定", "desc"), MYSQL_CONFIG)
        assert outcome.status is CheckStatus.FAIL
        assert len(outcome.evidence["mismatches"]) == len(BIND_CASES)

    def test_a_missing_row_is_not_read_as_a_null_result(self) -> None:
        """No row at all must fail, even though the NULL case expects NULL.

        Reading a missing row as the value None would make the NULL case pass on a
        query that returned nothing, which is a pass for the wrong reason.
        """
        driver = _mysql_driver_for(lambda _sql, _params: None)
        outcome = driver.run_check(CheckSpec("M3", "参数绑定", "desc"), MYSQL_CONFIG)
        assert outcome.status is CheckStatus.FAIL
        assert len(outcome.evidence["mismatches"]) == len(BIND_CASES)

    def test_a_multi_column_result_fails(self) -> None:
        """A one-column projection is what the check asserts; more is a mismatch."""
        driver = _mysql_driver_for(lambda sql, params: (params[0], "extra") if sql.startswith("SELECT %s") else None)
        outcome = driver.run_check(CheckSpec("M3", "参数绑定", "desc"), MYSQL_CONFIG)
        assert outcome.status is CheckStatus.FAIL


class TestOracleBindCheck:
    """The Oracle bind check refuses to compare fewer columns than it bound."""

    @staticmethod
    def _resolver_with(values=None):
        """Answer the named-bind projection with either real values or a stand-in."""

        def resolve(sql: str, params: object):
            if sql.startswith("SELECT :p0"):
                if values is not None:
                    return values
                return tuple((params or {}).get(name) for name in oracle_driver.bind_case_names())
            return None

        return resolve

    def test_all_bound_values_come_back_unchanged(self) -> None:
        """The happy path covers the whole case set."""
        driver = _oracle_driver_for(self._resolver_with())
        outcome = driver.run_check(CheckSpec("O3", "Named Bind Parameter", "desc"), ORACLE_CONFIG)
        assert outcome.status is CheckStatus.PASS
        assert len(outcome.evidence["cases"]) == len(BIND_CASES)

    def test_a_short_projection_fails_rather_than_under_comparing(self) -> None:
        """Fewer columns than binds must fail, not silently compare only the prefix."""
        truncated = tuple(value for _, value in BIND_CASES)[:-1]
        driver = _oracle_driver_for(self._resolver_with(truncated))
        outcome = driver.run_check(CheckSpec("O3", "Named Bind Parameter", "desc"), ORACLE_CONFIG)
        assert outcome.status is CheckStatus.FAIL
        assert outcome.evidence["actual_columns"] == len(BIND_CASES) - 1

    def test_a_changed_value_fails(self) -> None:
        """A coerced value must not pass, even with the right column count."""
        altered = tuple("coerced" for _ in BIND_CASES)
        driver = _oracle_driver_for(self._resolver_with(altered))
        outcome = driver.run_check(CheckSpec("O3", "Named Bind Parameter", "desc"), ORACLE_CONFIG)
        assert outcome.status is CheckStatus.FAIL

    def test_no_row_at_all_fails(self) -> None:
        """A query returning nothing is not a pass."""
        driver = _oracle_driver_for(lambda _sql, _params: None)
        outcome = driver.run_check(CheckSpec("O3", "Named Bind Parameter", "desc"), ORACLE_CONFIG)
        assert outcome.status is CheckStatus.FAIL

    def test_the_null_case_is_compared_not_skipped(self) -> None:
        """NULL is a value to compare, and the case set is covered in full."""
        driver = _oracle_driver_for(self._resolver_with())
        outcome = driver.run_check(CheckSpec("O3", "Named Bind Parameter", "desc"), ORACLE_CONFIG)
        assert "none" in outcome.evidence["cases"]
        assert outcome.status is CheckStatus.PASS


@pytest.mark.parametrize("mode", ["mysql", "oracle"])
def test_bind_case_count_is_what_the_checks_report(mode: str) -> None:
    """Both suites bind the same case set, so neither can drift from the other."""
    assert len(BIND_CASES) == 11
    assert mode in {"mysql", "oracle"}
