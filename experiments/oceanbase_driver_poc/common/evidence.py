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
from typing import Any

from common.errors import DriverError, ErrorCode, classify_mysql_error, classify_oracle_error
from common.results import CheckStatus

# A check records what it learned about the instance under the key ``instance`` in its
# evidence. The harness lifts that into the report's top-level ``ob_version`` /
# ``compat_mode``, which is what the result JSON and the generated conclusions
# sections read. Without this handoff the drivers read the version and nothing ever
# surfaces it, so the document reports "未读取" even after a successful connection.
INSTANCE_EVIDENCE_KEY = "instance"


class ServerStopEvidence(str, Enum):
    """What an independent observation connection saw after the client gave up."""

    STOPPED = "server_stopped"
    STILL_RUNNING = "server_still_running"
    UNDETERMINED = "server_undetermined"


# The evidence key a Query Timeout check must carry for its PASS to survive the
# harness. Only a positive server-side observation counts.
SERVER_STOP_EVIDENCE_KEY = "server_stop"


def build_instance_evidence(
    *,
    version: str | None,
    compat_mode: str | None,
    compat_mode_source: str | None,
    probe_attempts: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble the instance facts a connection check learned.

    Args:
        version: The instance version, when it could be read.
        compat_mode: The self-reported compatibility mode, when it could be read.
        compat_mode_source: Which probe produced ``compat_mode``, or the reason it is
            absent. Recorded so a ``None`` mode is actionable rather than opaque --
            "we could not ask" and "the instance said nothing" are different.
        probe_attempts: Rendered attempts, for diagnosing a failed probe.

    Returns:
        The evidence payload to store under :data:`INSTANCE_EVIDENCE_KEY`.
    """
    return {
        "version": version,
        "compat_mode": compat_mode,
        "compat_mode_source": compat_mode_source,
        "probe_attempts": list(probe_attempts or []),
    }


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
