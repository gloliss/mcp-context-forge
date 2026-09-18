"""Rules that turn raw observations into a check status.

These live apart from the driver adapters because they encode the judgements that
make the POC's verdicts trustworthy, and because they are the part a reader is most
likely to disagree with. Separating them also makes them testable without a
database, which is the whole point of the L1 layer.

The Query Timeout rule is the load-bearing one. The requirement only asks whether a
timeout is observed, but the downstream data-source design requires the database-side
query to actually stop, and separately records that the existing execution path fails
exactly there: the client gives up while the server keeps working. A POC that reports
"timeout works" on client-side evidence alone would hand the downstream design a
capability it does not have.
"""

from __future__ import annotations

from enum import Enum

from common.errors import DriverError, ErrorCode, classify_mysql_error, classify_oracle_error
from common.results import CheckStatus


class ServerStopEvidence(str, Enum):
    """What an independent observation connection saw after the client gave up."""

    STOPPED = "server_stopped"
    STILL_RUNNING = "server_still_running"
    UNDETERMINED = "server_undetermined"


def judge_query_timeout(*, client_timed_out: bool, server: ServerStopEvidence) -> tuple[CheckStatus, str]:
    """Decide the Query Timeout check from client and server observations.

    Args:
        client_timed_out: Whether the caller received a timeout within the
            configured deadline.
        server: What an independent observation connection established about the
            server-side query after the caller gave up.

    Returns:
        The status and the reason behind it. Only a stopped server-side query is a
        pass. An unobservable server-side outcome yields
        :attr:`CheckStatus.INDETERMINATE` rather than a pass, because "we could not
        tell" and "it stopped" are different findings and the downstream design
        treats the second as a gate.
    """
    if not client_timed_out:
        return CheckStatus.FAIL, "调用方未在期限内收到超时错误"

    if server is ServerStopEvidence.STOPPED:
        return CheckStatus.PASS, "客户端超时，且服务端查询已停止"
    if server is ServerStopEvidence.STILL_RUNNING:
        return CheckStatus.FAIL, "客户端超时，但服务端查询仍在运行"
    return CheckStatus.INDETERMINATE, "客户端超时，服务端是否停止无法判定"


def classify_failure(mode: str, exc: BaseException) -> tuple[ErrorCode, str | None]:
    """Classify an escaped driver failure into the shared error vocabulary.

    Args:
        mode: ``"mysql"`` or ``"oracle"``, selecting the native mapping.
        exc: The exception raised by the driver.

    Returns:
        The shared error code and the native code when one could be extracted. A
        :class:`~common.errors.DriverError` already carries its own classification
        and is passed through unchanged.
    """
    if isinstance(exc, DriverError):
        return exc.code, exc.native_code

    message = str(exc)
    if mode == "oracle":
        return classify_oracle_error(message)
    return classify_mysql_error(message)
