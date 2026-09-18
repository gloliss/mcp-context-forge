"""The shared error-code vocabulary, and the mapping onto native driver errors.

These codes are not an implementation detail of the POC: the downstream OceanBase
data-source design reuses them verbatim for its tool error contract, so changing one
is a contract change.

Native codes are always preserved alongside the shared code. An ``ORA-`` or errno
value this table does not recognise falls back to ``DB_DRIVER_ERROR`` *with* the
original code attached, so an unrecognised failure still tells the reader what the
database actually said.
"""

from __future__ import annotations

import re
from enum import Enum

_ORA_RE = re.compile(r"ORA-\d{5}")
_MYSQL_ERRNO_RE = re.compile(r"\((\d{3,5}),")

# PyMySQL's client-side "lost connection during query" errno. It is overloaded: the
# same code is raised both for a genuinely dropped connection and for a socket read
# timeout, so the code alone cannot decide the classification -- see
# :func:`classify_mysql_error`.
CR_SERVER_LOST = 2013

# Wording that means "the deadline expired" rather than "the endpoint is wrong".
# Used only when no native code identified the failure, and for the read-timeout
# disambiguation above.
_TIMEOUT_WORDS = ("timed out", "timeout", "call timeout")


class ErrorCode(str, Enum):
    """Stable error codes produced by the POC checks."""

    DB_AUTH_FAILED = "DB_AUTH_FAILED"
    DB_UNREACHABLE = "DB_UNREACHABLE"
    DB_TIMEOUT = "DB_TIMEOUT"
    DB_SYNTAX_ERROR = "DB_SYNTAX_ERROR"
    DB_OBJECT_NOT_FOUND = "DB_OBJECT_NOT_FOUND"
    DB_PERMISSION_DENIED = "DB_PERMISSION_DENIED"
    DB_PROTOCOL_UNSUPPORTED = "DB_PROTOCOL_UNSUPPORTED"
    DB_DRIVER_ERROR = "DB_DRIVER_ERROR"


class DriverError(Exception):
    """A driver failure already classified into the shared vocabulary.

    Drivers raise this when they can classify their own failure more precisely than
    message inspection can. Anything else that escapes a driver is caught by the
    harness and classified from its message.
    """

    def __init__(self, code: ErrorCode, message: str, *, native_code: str | None = None) -> None:
        """Record a classified driver failure.

        Args:
            code: The shared error code.
            message: Human-readable detail. Must be safe to show after redaction.
            native_code: The original ``ORA-`` or errno value, when one exists.
        """
        super().__init__(message)
        self.code = code
        self.native_code = native_code


# Oracle-mode codes, which OceanBase's Oracle compatibility mode reuses.
ORACLE_ERROR_MAP: dict[str, ErrorCode] = {
    "ORA-01017": ErrorCode.DB_AUTH_FAILED,  # invalid username/password
    "ORA-01031": ErrorCode.DB_PERMISSION_DENIED,  # insufficient privileges
    "ORA-00942": ErrorCode.DB_OBJECT_NOT_FOUND,  # table or view does not exist
    "ORA-00904": ErrorCode.DB_SYNTAX_ERROR,  # invalid identifier
    "ORA-00933": ErrorCode.DB_SYNTAX_ERROR,  # SQL command not properly ended
    "ORA-00911": ErrorCode.DB_SYNTAX_ERROR,  # invalid character
    "ORA-00936": ErrorCode.DB_SYNTAX_ERROR,  # missing expression
    "ORA-12541": ErrorCode.DB_UNREACHABLE,  # no listener
    "ORA-12514": ErrorCode.DB_UNREACHABLE,  # service not registered with the listener
    "ORA-12154": ErrorCode.DB_UNREACHABLE,  # could not resolve the connect identifier
    "ORA-12170": ErrorCode.DB_TIMEOUT,  # connect timeout occurred
    "ORA-03113": ErrorCode.DB_UNREACHABLE,  # end-of-file on communication channel
    "ORA-03114": ErrorCode.DB_UNREACHABLE,  # not connected to ORACLE
    "ORA-03135": ErrorCode.DB_UNREACHABLE,  # connection lost contact
    "ORA-01013": ErrorCode.DB_TIMEOUT,  # user requested cancel of current operation
    "ORA-00028": ErrorCode.DB_UNREACHABLE,  # your session has been killed
}

# MySQL server errno values, which OceanBase's MySQL compatibility mode follows.
MYSQL_ERRNO_MAP: dict[int, ErrorCode] = {
    1044: ErrorCode.DB_PERMISSION_DENIED,  # access denied for database
    1045: ErrorCode.DB_AUTH_FAILED,  # access denied for user
    1049: ErrorCode.DB_OBJECT_NOT_FOUND,  # unknown database
    1064: ErrorCode.DB_SYNTAX_ERROR,  # SQL syntax error
    1142: ErrorCode.DB_PERMISSION_DENIED,  # command denied to user
    1146: ErrorCode.DB_OBJECT_NOT_FOUND,  # table doesn't exist
    1205: ErrorCode.DB_TIMEOUT,  # lock wait timeout exceeded
    1317: ErrorCode.DB_TIMEOUT,  # query execution was interrupted
    1698: ErrorCode.DB_AUTH_FAILED,  # access denied, no password / socket auth
    2003: ErrorCode.DB_UNREACHABLE,  # can't connect to server
    2005: ErrorCode.DB_UNREACHABLE,  # unknown host
    2013: ErrorCode.DB_UNREACHABLE,  # lost connection during query
    3024: ErrorCode.DB_TIMEOUT,  # query execution was interrupted (max_statement_time)
}


def classify_oracle_error(message: str) -> tuple[ErrorCode, str | None]:
    """Classify an Oracle-mode failure from its message text.

    Args:
        message: The driver or server message.

    Returns:
        The shared error code, and the native ``ORA-`` code when one was found.
        An unrecognised ``ORA-`` code yields ``DB_DRIVER_ERROR`` rather than a
        guess, while still returning the native code.
    """
    match = _ORA_RE.search(message or "")
    if match:
        native = match.group(0)
        return ORACLE_ERROR_MAP.get(native, ErrorCode.DB_DRIVER_ERROR), native

    # python-oracledb reports its own client-side failures under DPI-/DPY- codes
    # rather than ORA-, and a call timeout is one of them. Matching the wording
    # rather than pinning a specific DPI number avoids asserting a code this POC
    # has not been able to observe (python-oracledb is not installable here).
    lowered = (message or "").lower()
    if any(word in lowered for word in _TIMEOUT_WORDS):
        return ErrorCode.DB_TIMEOUT, None
    return ErrorCode.DB_DRIVER_ERROR, None


def _classify_mysql_message(lowered: str) -> ErrorCode | None:
    """Classify a MySQL failure from its wording alone.

    Args:
        lowered: The message, lower-cased by the caller.

    Returns:
        The matching code, or ``None`` when the wording identifies nothing. Order
        matters: "Can't connect to MySQL server on 'h' (timed out)" describes an
        unreachable endpoint that timed out, not a query that exceeded its deadline,
        so the reachability wording is tested first.
    """
    if "can't connect" in lowered or "connection refused" in lowered or "unknown host" in lowered:
        return ErrorCode.DB_UNREACHABLE
    if "access denied" in lowered:
        return ErrorCode.DB_AUTH_FAILED
    if any(word in lowered for word in _TIMEOUT_WORDS):
        return ErrorCode.DB_TIMEOUT
    return None


def classify_mysql_error(message: str, errno: int | None = None) -> tuple[ErrorCode, str | None]:
    """Classify a MySQL-mode failure from its errno or message text.

    Args:
        message: The driver or server message.
        errno: A known server errno, when the caller already extracted one.

    Returns:
        The shared error code, and the native errno as text when one was found.
        Connection-level failures raised by the client library carry no server
        errno, so their message text is matched as a fallback.
    """
    text = message or ""
    lowered = text.lower()

    code = errno
    if code is None:
        match = _MYSQL_ERRNO_RE.search(text)
        code = int(match.group(1)) if match else None

    if code is None:
        return _classify_mysql_message(lowered) or ErrorCode.DB_DRIVER_ERROR, None

    # CR_SERVER_LOST is raised both for a dropped connection and for a socket read
    # timeout -- PyMySQL embeds the underlying OSError in the message ("Lost
    # connection to MySQL server during query (timed out)"), which is what tells the
    # two apart. A read timeout is the query timeout this POC configured, so it must
    # not be reported as an unreachable endpoint.
    if code == CR_SERVER_LOST and any(word in lowered for word in _TIMEOUT_WORDS):
        return ErrorCode.DB_TIMEOUT, str(code)

    mapped = MYSQL_ERRNO_MAP.get(code)
    if mapped is not None:
        return mapped, str(code)

    # An errno outside the table still arrives with a message. Returning the
    # catch-all immediately would discard wording the table happens not to cover --
    # MariaDB's 1698 "Access denied for user" is exactly such a case -- so the
    # message heuristics get a turn first. The native code is preserved either way.
    return _classify_mysql_message(lowered) or ErrorCode.DB_DRIVER_ERROR, str(code)
