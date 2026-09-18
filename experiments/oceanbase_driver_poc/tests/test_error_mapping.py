"""L1: the native-to-shared error-code mapping.

Downstream code branches on these codes, so an ORACLE table entry that quietly maps
the wrong way produces a wrong error contract. The rule that an unrecognised native
code keeps its original value is tested explicitly, because dropping it would make an
unknown failure undiagnosable in the field.
"""

from __future__ import annotations

import pytest
from common.errors import (
    MYSQL_ERRNO_MAP,
    ORACLE_ERROR_MAP,
    DriverError,
    ErrorCode,
    classify_mysql_error,
    classify_oracle_error,
)


class TestOracleMapping:
    """OceanBase's Oracle mode reuses Oracle's ORA- codes."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("ORA-01017: invalid username/password; logon denied", ErrorCode.DB_AUTH_FAILED),
            ("ORA-01031: insufficient privileges", ErrorCode.DB_PERMISSION_DENIED),
            ("ORA-00942: table or view does not exist", ErrorCode.DB_OBJECT_NOT_FOUND),
            ("ORA-00933: SQL command not properly ended", ErrorCode.DB_SYNTAX_ERROR),
            ('ORA-00904: "NOPE": invalid identifier', ErrorCode.DB_SYNTAX_ERROR),
            ("ORA-12541: TNS:no listener", ErrorCode.DB_UNREACHABLE),
            ("ORA-12170: TNS:Connect timeout occurred", ErrorCode.DB_TIMEOUT),
            ("ORA-03113: end-of-file on communication channel", ErrorCode.DB_UNREACHABLE),
        ],
    )
    def test_known_codes_map_as_documented(self, message: str, expected: ErrorCode) -> None:
        """Each documented code maps to its shared counterpart, native code kept."""
        code, native = classify_oracle_error(message)
        assert code is expected
        assert native is not None and native.startswith("ORA-")

    def test_unrecognised_ora_code_keeps_the_native_value(self) -> None:
        """An unknown ORA code must not lose the information that identifies it."""
        code, native = classify_oracle_error("ORA-56789: something new")
        assert code is ErrorCode.DB_DRIVER_ERROR
        assert native == "ORA-56789"

    def test_message_without_an_ora_code_yields_no_native_value(self) -> None:
        """A non-Oracle message has no native code to preserve."""
        assert classify_oracle_error("connection refused") == (ErrorCode.DB_DRIVER_ERROR, None)
        assert classify_oracle_error("") == (ErrorCode.DB_DRIVER_ERROR, None)

    def test_every_mapped_code_is_a_known_error_code(self) -> None:
        """The table cannot reference a code outside the shared vocabulary."""
        assert all(isinstance(value, ErrorCode) for value in ORACLE_ERROR_MAP.values())
        assert len(ORACLE_ERROR_MAP) == len(set(ORACLE_ERROR_MAP))


class TestMySQLMapping:
    """OceanBase's MySQL mode follows MySQL server errno values."""

    @pytest.mark.parametrize(
        ("errno", "expected"),
        [
            (1045, ErrorCode.DB_AUTH_FAILED),
            (1044, ErrorCode.DB_PERMISSION_DENIED),
            (1049, ErrorCode.DB_OBJECT_NOT_FOUND),
            (1064, ErrorCode.DB_SYNTAX_ERROR),
            (1146, ErrorCode.DB_OBJECT_NOT_FOUND),
            (1205, ErrorCode.DB_TIMEOUT),
            (3024, ErrorCode.DB_TIMEOUT),
            (2013, ErrorCode.DB_UNREACHABLE),
        ],
    )
    def test_explicit_errno_maps_as_documented(self, errno: int, expected: ErrorCode) -> None:
        """A supplied errno is mapped directly."""
        code, native = classify_mysql_error("boom", errno)
        assert code is expected
        assert native == str(errno)

    def test_errno_is_extracted_from_the_message(self) -> None:
        """PyMySQL embeds the errno in its message text."""
        code, native = classify_mysql_error('(1064, "You have an error in your SQL syntax")')
        assert code is ErrorCode.DB_SYNTAX_ERROR
        assert native == "1064"

    def test_unrecognised_errno_keeps_the_native_value(self) -> None:
        """An unknown server errno is preserved rather than flattened."""
        code, native = classify_mysql_error("(9999, 'unknown')")
        assert code is ErrorCode.DB_DRIVER_ERROR
        assert native == "9999"

    def test_client_side_failures_have_no_server_errno(self) -> None:
        """Connection-level failures are matched on text, since they carry no errno."""
        assert classify_mysql_error("Can't connect to MySQL server on 'ob-proxy.example.internal'")[0] is ErrorCode.DB_UNREACHABLE
        assert classify_mysql_error('(2003, "Can\'t connect to MySQL server")')[0] is ErrorCode.DB_UNREACHABLE
        assert classify_mysql_error("connect timed out")[0] is ErrorCode.DB_TIMEOUT
        assert classify_mysql_error("Access denied for user 'appuser'")[0] is ErrorCode.DB_AUTH_FAILED

    def test_empty_message_does_not_crash(self) -> None:
        """Classification is total: it always returns a code."""
        assert classify_mysql_error("") == (ErrorCode.DB_DRIVER_ERROR, None)

    def test_every_mapped_errno_is_a_known_error_code(self) -> None:
        """The errno table cannot reference a code outside the shared vocabulary."""
        assert all(isinstance(value, ErrorCode) for value in MYSQL_ERRNO_MAP.values())


class TestQueryTimeoutDisambiguation:
    """CR_SERVER_LOST is overloaded, and misreading it corrupts the timeout contract.

    PyMySQL raises errno 2013 both for a dropped connection and for a socket read
    timeout -- which is the query timeout this POC itself configured. Reporting a
    read timeout as "unreachable" would hand the downstream design the wrong error
    for the one check that exists to detect timeouts.
    """

    def test_read_timeout_is_a_timeout_not_an_unreachable_endpoint(self) -> None:
        """The wording PyMySQL embeds is what separates the two meanings."""
        code, native = classify_mysql_error("Lost connection to MySQL server during query (timed out)", 2013)
        assert code is ErrorCode.DB_TIMEOUT
        assert native == "2013"

    def test_a_genuine_drop_keeps_the_unreachable_meaning(self) -> None:
        """Without timeout wording the same errno still means the connection died."""
        code, native = classify_mysql_error("Lost connection to MySQL server during query (Connection reset by peer)", 2013)
        assert code is ErrorCode.DB_UNREACHABLE
        assert native == "2013"

    def test_unreachable_endpoint_is_not_reported_as_a_timeout(self) -> None:
        """A connect failure that mentions a timeout is still a reachability failure.

        "Can't connect ... (timed out)" says the endpoint never answered. Grading it
        DB_TIMEOUT would make the connect-timeout check and the query-timeout check
        indistinguishable in the result file.
        """
        for message in (
            "Can't connect to MySQL server on 'h' (timed out)",
            'connection refused',
        ):
            assert classify_mysql_error(message)[0] is ErrorCode.DB_UNREACHABLE


class TestUnmappedErrnoFallsBackToTheMessage:
    """The errno table is not exhaustive, and the message still carries signal."""

    def test_an_unmapped_errno_still_uses_its_message(self) -> None:
        """MariaDB reports access-denied under 1698; the wording must still classify.

        Returning the catch-all the moment an errno is absent from the table would
        discard a perfectly clear "Access denied for user" message. The native code is
        kept either way, so no diagnostic information is lost by looking first.
        """
        code, native = classify_mysql_error("(1698, \"Access denied for user 'x'@'localhost'\")")
        assert code is ErrorCode.DB_AUTH_FAILED
        assert native == "1698"

    def test_a_mapped_errno_still_wins_over_its_message(self) -> None:
        """The table is authoritative when it has an entry."""
        code, _ = classify_mysql_error("(1146, \"Table 'x' doesn't exist\")")
        assert code is ErrorCode.DB_OBJECT_NOT_FOUND

    def test_an_unmapped_errno_with_no_signal_stays_a_driver_error(self) -> None:
        """Nothing recognisable means nothing is claimed."""
        code, native = classify_mysql_error("(9999, 'something entirely new')")
        assert code is ErrorCode.DB_DRIVER_ERROR
        assert native == "9999"

    def test_1698_is_also_mapped_directly(self) -> None:
        """The socket-auth access-denied code is worth an explicit entry."""
        assert MYSQL_ERRNO_MAP[1698] is ErrorCode.DB_AUTH_FAILED


class TestOracleClientSideTimeouts:
    """python-oracledb reports its own failures under DPI-/DPY-, not ORA-."""

    def test_a_client_side_timeout_classifies_as_a_timeout(self) -> None:
        """A call timeout is a timeout even without an ORA code.

        The specific DPI number is deliberately not pinned: python-oracledb could
        not be installed here, so the wording is matched rather than a code this POC
        has never been able to observe.
        """
        code, native = classify_oracle_error("DPI-1067: call timeout of 30000 ms exceeded")
        assert code is ErrorCode.DB_TIMEOUT
        assert native is None

    def test_a_non_timeout_driver_failure_stays_a_driver_error(self) -> None:
        """Not every DPI- message is a timeout."""
        code, _ = classify_oracle_error("DPY-6005: cannot connect to database")
        assert code is ErrorCode.DB_DRIVER_ERROR


class TestDriverError:
    """A driver that classifies its own failure is trusted."""

    def test_carries_code_and_native_code(self) -> None:
        """Both values survive construction."""
        error = DriverError(ErrorCode.DB_PROTOCOL_UNSUPPORTED, "thin mode rejected by server", native_code="DPY-3010")
        assert error.code is ErrorCode.DB_PROTOCOL_UNSUPPORTED
        assert error.native_code == "DPY-3010"
        assert str(error) == "thin mode rejected by server"

    def test_native_code_is_optional(self) -> None:
        """A failure with no native code is still representable."""
        assert DriverError(ErrorCode.DB_DRIVER_ERROR, "boom").native_code is None
