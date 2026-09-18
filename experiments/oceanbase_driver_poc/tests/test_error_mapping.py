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
