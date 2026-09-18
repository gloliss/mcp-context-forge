"""Oracle compatibility mode over python-oracledb.

This is the mode that carries the unresolved risk. OceanBase documents its Oracle
compatibility mode against OCI-based clients (cx_Oracle with libobclient/OBCI), but
``cx_Oracle`` stopped at 8.3.0 and does not install on Python 3.12. ``python-oracledb``
is the maintained successor and has two modes:

* **thin** -- pure Python, needs no client library. Whether it is compatible with
  OceanBase's Oracle mode is the open question this POC exists to answer.
* **thick** -- wraps OCI, so it needs ``libobclient``/OBCI on the machine and
  ``LD_LIBRARY_PATH`` pointing at it.

Both paths are wired here. Which one a run actually used is recorded on the report,
because "thin worked" and "thick worked" are different findings with very different
deployment consequences -- thick drags a native client library into the image.

As in the MySQL suite, configuration mapping and verdict rules are pure and tested
now, while anything that opens a socket is an L2 seam that has never executed against
an OceanBase instance.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from common.checks import CheckSpec
from common.config import ConnectionConfig
from common.errors import ErrorCode, classify_oracle_error
from common.evidence import (
    INSTANCE_EVIDENCE_KEY,
    SERVER_STOP_EVIDENCE_KEY,
    ServerStopEvidence,
    build_instance_evidence,
    judge_query_timeout,
)
from common.harness import CheckOutcome
from common.probes import BIND_CASES, Deadline, resolve_identifier
from common.redaction import Redactor
from common.results import CheckStatus
from common.versions import resolve_version

DRIVER_PACKAGE = "oracledb"
INSTALL_HINT = "安装 POC 依赖：pip install -r requirements-poc.txt"

# OceanBase's own statement-level timeout, in microseconds.
OB_QUERY_TIMEOUT_UNIT = 1_000_000

MILLISECONDS_PER_SECOND = 1000

L2_PENDING = "需要真实 OceanBase 实例，属 L2 工作项"

# How long a pool acquisition may wait before it is called exhaustion, in seconds.
POOL_WAIT_TIMEOUT_S = 3

# Candidate statements for reading the instance's compatibility mode. These are
# *candidates* -- no OceanBase instance was reachable while writing them, and the
# dictionary view may not be readable by a least-privilege account. A probe that the
# server refuses is recorded rather than treated as a connection failure.
COMPAT_MODE_QUERIES: tuple[tuple[str, str], ...] = (
    ("SELECT value FROM v$parameter WHERE name = 'ob_compatibility_mode'", "v$parameter"),
    ("SELECT value FROM nls_database_parameters WHERE parameter = 'ob_compatibility_mode'", "nls_database_parameters"),
)

INSTANCE_VERSION_SQL = "SELECT banner FROM v$version WHERE ROWNUM = 1"

TABLES_SQL = "SELECT table_name FROM all_tables WHERE owner = :owner ORDER BY table_name"
COLUMNS_SQL = (
    "SELECT column_name, data_type, nullable, data_length, data_precision, data_scale "
    "FROM all_tab_columns WHERE owner = :owner AND table_name = :table ORDER BY column_id"
)
TABLE_COMMENT_SQL = "SELECT comments FROM all_tab_comments WHERE owner = :owner AND table_name = :table"

# Result-set metadata for the same table. This is the second, independent metadata
# path: the column list comes from the server's description of a real statement
# rather than from the catalog, so the two agreeing is actual corroboration.
DESCRIBE_SQL_TEMPLATE = "SELECT * FROM {table} WHERE 1 = 0"

# Server-side view of running statements, for the query-timeout observation. A
# least-privilege account usually cannot read v$session, which is precisely why the
# observer account exists.
SERVER_QUERY_PROBE_SQL = "SELECT COUNT(*) FROM v$session WHERE status = 'ACTIVE' AND sql_text LIKE :pattern"

# Initial value for the CPU-burning probe. A CONNECT BY over DUAL is used rather than
# DBMS_LOCK.SLEEP because it needs no EXECUTE grant; the exact level that reliably
# outlives the configured timeout depends on the instance and is expected to be tuned
# during L2.
TIMEOUT_PROBE_LEVEL = 100_000_000


@dataclass(frozen=True)
class ErrorProbe:
    """One deliberately-failing operation and the codes it may be classified as.

    ``expect`` is a tuple rather than a single code for the same reason as in the
    MySQL suite: a failure can have more than one correct classification depending on
    which layer reports it first, and pinning one would turn a correct
    classification into a failed check.
    """

    key: str
    description: str
    expect: tuple[ErrorCode, ...]
    statement: str | None = None
    override_user: str | None = None


# Authentication is probed with a *non-existent account* rather than a wrong password
# for a real one: the classification is the same (ORA-01017) and it cannot lock out
# the account the POC needs.
ERROR_PROBES: tuple[ErrorProbe, ...] = (
    ErrorProbe("unknown-table", "查询一个不存在的表", (ErrorCode.DB_OBJECT_NOT_FOUND,), statement="SELECT 1 FROM OB_POC_NO_SUCH_TABLE_XYZ"),
    ErrorProbe("invalid-identifier", "引用一个不存在的列", (ErrorCode.DB_SYNTAX_ERROR,), statement="SELECT no_such_column_xyz FROM DUAL"),
    ErrorProbe("syntax-error", "语法不完整的语句", (ErrorCode.DB_SYNTAX_ERROR,), statement="SELECT 1 FROM"),
    ErrorProbe("auth-failure", "使用不存在的账号连接", (ErrorCode.DB_AUTH_FAILED,), override_user="OB_POC_NO_SUCH_USER_XYZ"),
)


def load() -> tuple[Any | None, str]:
    """Import python-oracledb if it is installed.

    Returns:
        ``(module, version)``. The module is ``None`` when the package is absent,
        which the harness reports as ``UNSUPPORTED`` rather than as a failure. The
        version comes from distribution metadata, which is the value a reader can
        actually install.
    """
    try:
        import oracledb  # noqa: PLC0415 - optional POC dependency
    except ImportError:
        return None, "not-installed"
    return oracledb, resolve_version(DRIVER_PACKAGE, oracledb)


def build_dsn(config: ConnectionConfig) -> str:
    """Build an Easy Connect DSN for the configured target.

    OceanBase is reached either through OBProxy or directly, and the identifier used
    is the service name rather than a SID.

    Args:
        config: The Oracle-mode connection configuration.

    Returns:
        A ``host:port/service`` DSN.

    Raises:
        ValueError: If neither a service name nor a database is configured, since a
            DSN without one would silently target the wrong service.
    """
    service = config.service_name or config.database
    if not service:
        raise ValueError("Oracle 模式需要配置 service name（或 database）以构造 DSN")
    return f"{config.host}:{config.port}/{service}"


def build_connect_kwargs(config: ConnectionConfig) -> dict[str, Any]:
    """Map a connection configuration onto python-oracledb connect parameters.

    Args:
        config: The Oracle-mode connection configuration.

    Returns:
        Keyword arguments for ``oracledb.connect``. The TCP connect timeout is in
        seconds; the per-call timeout is applied separately on the connection and is
        in milliseconds, so it is deliberately not built here.
    """
    return {
        "user": config.user,
        "password": config.password,
        "dsn": build_dsn(config),
        "tcp_connect_timeout": config.connect_timeout_s,
    }


def build_probe_connect_kwargs(config: ConnectionConfig, probe: ErrorProbe) -> dict[str, Any]:
    """Build connect parameters for a deliberately-failing connection probe.

    Args:
        config: The base configuration.
        probe: The probe, carrying any user override.

    Returns:
        Connect parameters with the probe's overrides applied.
    """
    kwargs = build_connect_kwargs(config)
    if probe.override_user is not None:
        kwargs["user"] = probe.override_user
    return kwargs


def describe_connect_kwargs(config: ConnectionConfig, redactor: Redactor) -> dict[str, Any]:
    """Render connect parameters for a result file with credentials removed.

    Args:
        config: The Oracle-mode connection configuration.
        redactor: The redactor carrying this run's secret values.

    Returns:
        The same mapping with the password masked and the username scope stripped.
    """
    return redactor.redact_deep(build_connect_kwargs(config))


def call_timeout_ms(config: ConnectionConfig) -> int:
    """Return the per-call timeout in milliseconds, as python-oracledb expects.

    Args:
        config: The Oracle-mode connection configuration.

    Returns:
        The configured query timeout converted from seconds to milliseconds.
    """
    return int(config.query_timeout_s * MILLISECONDS_PER_SECOND)


def build_session_timeout_statements(config: ConnectionConfig) -> list[str]:
    """Build the session statements that apply the configured query timeout.

    Args:
        config: The Oracle-mode connection configuration.

    Returns:
        Statements to run on a freshly opened session.
    """
    microseconds = int(config.query_timeout_s * OB_QUERY_TIMEOUT_UNIT)
    return [f"ALTER SESSION SET ob_query_timeout = {microseconds}"]


def build_bind_sql(names: tuple[str, ...]) -> str:
    """Build a SELECT projecting one named bind per identifier.

    Args:
        names: The bind placeholder names, without the leading colon.

    Returns:
        A statement of the form ``SELECT :a AS a, :b AS b FROM DUAL``. Named binds
        are what the Oracle mode must be verified for, so the SQL is generated from
        names rather than hand-written.
    """
    projections = ", ".join(f":{name} AS {name}" for name in names)
    return f"SELECT {projections} FROM DUAL"


def bind_case_names() -> tuple[str, ...]:
    """Return the placeholder names used by the parameter-binding check."""
    return tuple(f"p{index}" for index, _ in enumerate(BIND_CASES))


def build_timeout_probe(marker: str) -> str:
    """Build the long-running statement the query-timeout check cancels.

    Args:
        marker: A unique token the observation connection searches for.

    Returns:
        A CONNECT BY statement burning CPU on the server, carrying the marker in a
        comment so the observation query can identify this run's statement.
    """
    return f"SELECT COUNT(*) FROM DUAL CONNECT BY LEVEL <= {TIMEOUT_PROBE_LEVEL} /* {marker} */"


def new_probe_marker() -> str:
    """Return a fresh, SQL-safe token identifying one timeout probe."""
    return f"ob-poc-timeout-{uuid.uuid4().hex}"


def describe_client_mode(module: Any | None) -> str:
    """Report whether the driver is running in thin or thick mode.

    Args:
        module: The imported ``oracledb`` module, or ``None``.

    Returns:
        ``"thin"``, ``"thick"``, or ``"unknown"`` when the module does not expose
        the probe. The distinction matters because thick mode needs a native client
        library that thin mode does not.
    """
    if module is None:
        return "unknown"
    probe = getattr(module, "is_thin_mode", None)
    if probe is None:
        return "unknown"
    try:
        return "thin" if probe() else "thick"
    except Exception:  # the probe may refuse before a client is initialised
        return "unknown"


class OracleDBDriver:
    """Driver adapter for OceanBase's Oracle compatibility mode."""

    name = DRIVER_PACKAGE

    def __init__(self, module: Any | None, version: str, redactor: Redactor) -> None:
        """Wrap an already-imported python-oracledb module.

        Args:
            module: The imported ``oracledb`` module, or ``None`` when absent.
            version: The driver version, for the result record.
            redactor: The redactor carrying this run's secret values.
        """
        self._module = module
        self._redactor = redactor
        self.version = version
        self._handlers = {
            "O1": self._check_connect,
            "O2": self._check_simple_select,
            "O3": self._check_named_bind,
            "O4": self._check_pool,
            "O5": self._check_connect_timeout,
            "O6": self._check_query_timeout,
            "O7": self._check_metadata,
            "O8": self._check_close_reconnect,
            "O9": self._check_error_handling,
        }

    @property
    def installed(self) -> bool:
        """Whether the driver package is available for use."""
        return self._module is not None

    @property
    def implemented_checks(self) -> frozenset[str]:
        """The check ids this driver can actually execute.

        Exposed so the L1 layer can assert the suite still covers every declared
        check: a check that silently loses its handler would otherwise report
        ``UNSUPPORTED`` and read as "not applicable" rather than as a regression.
        """
        return frozenset(self._handlers)

    def run_check(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """Execute one verification item.

        Args:
            check: The verification item to execute.
            config: The Oracle-mode connection configuration.

        Returns:
            The outcome. A missing driver package reports ``UNSUPPORTED``, and so does
            a check id with no handler.
        """
        if not self.installed:
            return CheckOutcome(
                status=CheckStatus.UNSUPPORTED,
                summary=f"驱动未安装：{INSTALL_HINT}",
                evidence={"unimplemented": True, "reason": "driver-not-installed"},
            )

        handler = self._handlers.get(check.check_id)
        if handler is None:
            return CheckOutcome(
                status=CheckStatus.UNSUPPORTED,
                summary=f"{check.check_id} 尚未实现（{L2_PENDING}）",
                evidence={"unimplemented": True, "reason": "l2-pending"},
            )
        return handler(check, config)

    # ---------------------------------------------------------------- connections

    def _open(self, config: ConnectionConfig, **overrides: Any) -> Any:
        """Open a raw connection with the per-call and session timeouts applied."""
        kwargs = build_connect_kwargs(config)
        kwargs.update(overrides)
        connection = self._module.connect(**kwargs)
        connection.call_timeout = call_timeout_ms(config)
        with connection.cursor() as cursor:
            for statement in build_session_timeout_statements(config):
                cursor.execute(statement)
        return connection

    def _connect(self, config: ConnectionConfig) -> Any:
        """Open a connection to the configured target."""
        return self._open(config)

    def _close(self, connection: Any) -> None:
        """Close a connection, ignoring a failure on an already-dead one."""
        try:
            connection.close()
        except Exception:  # closing an already-broken connection is not an error
            pass

    def _probe_instance(self, cursor: Any) -> dict[str, Any]:
        """Read the instance version and compatibility mode over a live cursor."""
        version: str | None = None
        try:
            cursor.execute(INSTANCE_VERSION_SQL)
            row = cursor.fetchone()
            version = str(row[0]) if row and row[0] is not None else None
        except Exception as exc:  # v$version may not be readable by a narrow account
            version = None
            version_error = str(exc)
        else:
            version_error = None

        compat_mode: str | None = None
        source: str | None = None
        attempts: list[str] = []
        if version_error:
            attempts.append(f"version: {version_error}")
        for statement, label in COMPAT_MODE_QUERIES:
            try:
                cursor.execute(statement)
                row = cursor.fetchone()
            except Exception as exc:  # a probe the server refuses is expected
                attempts.append(f"{label}: {exc}")
                continue
            if row and row[0] is not None:
                compat_mode = str(row[0])
                source = label
                break
            attempts.append(f"{label}: 未返回兼容模式")
        if compat_mode is None and not attempts:
            attempts.append("no-candidate-answered")
        return build_instance_evidence(version=version, compat_mode=compat_mode, compat_mode_source=source or "unavailable", probe_attempts=attempts)

    # --------------------------------------------------------------------- checks

    def _check_connect(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """O1: establish a connection and read back the instance version."""
        connection = self._connect(config)
        try:
            with connection.cursor() as cursor:
                instance = self._probe_instance(cursor)
        finally:
            self._close(connection)

        version = instance.get("version") or "unknown"
        mode = instance.get("compat_mode")
        client_mode = describe_client_mode(self._module)
        mode_note = f"，兼容模式 {mode}" if mode else "，兼容模式未读到"
        return CheckOutcome(
            status=CheckStatus.PASS,
            summary=f"连接成功（{client_mode} 模式），实例版本 {version}{mode_note}",
            evidence={
                INSTANCE_EVIDENCE_KEY: instance,
                "client_mode": client_mode,
                "connect_kwargs": describe_connect_kwargs(config, self._redactor),
            },
        )

    def _check_simple_select(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """O2: run a small multi-type SELECT."""
        connection = self._connect(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM DUAL")
                scalar = cursor.fetchone()
                cursor.execute("SELECT 1 AS n, 'text' AS s, NULL AS n_null FROM DUAL")
                row = cursor.fetchone()
        finally:
            self._close(connection)

        if scalar is None or row is None:
            return CheckOutcome(status=CheckStatus.FAIL, summary="简单 SELECT 未返回结果")
        if tuple(scalar)[0] != 1 or tuple(row)[0] != 1:
            return CheckOutcome(status=CheckStatus.FAIL, summary=f"简单 SELECT 返回了非预期结果：{scalar!r} / {row!r}")
        return CheckOutcome(
            status=CheckStatus.PASS,
            summary="简单 SELECT 返回预期结果",
            evidence={"select_1": list(scalar), "row": list(row)},
        )

    def _check_named_bind(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """O3: bind values to named parameters and require them back unchanged."""
        names = bind_case_names()
        statement = build_bind_sql(names)
        params = dict(zip(names, (value for _, value in BIND_CASES), strict=True))

        connection = self._connect(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement, params)
                row = cursor.fetchone()
        finally:
            self._close(connection)

        if row is None:
            return CheckOutcome(status=CheckStatus.FAIL, summary="命名绑定查询未返回结果")

        values = tuple(row)
        mismatches: list[str] = []
        for (label, expected), got in zip(BIND_CASES, values, strict=False):
            if got != expected:
                mismatches.append(f"{label}: 期望 {expected!r}，得到 {got!r}")

        if mismatches:
            return CheckOutcome(
                status=CheckStatus.FAIL,
                summary=f"命名绑定改变了取值（{len(mismatches)}/{len(BIND_CASES)} 项不符）",
                evidence={"mismatches": mismatches, "cases": len(BIND_CASES)},
            )
        return CheckOutcome(
            status=CheckStatus.PASS,
            summary=f"{len(BIND_CASES)} 类参数经 :name 命名绑定后取值不变",
            evidence={"cases": [label for label, _ in BIND_CASES], "statement_shape": statement},
        )

    def _check_pool(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """O4: exercise the driver's own connection pool against the real server."""
        ceiling = max(1, config.pool_max)
        evidence: dict[str, Any] = {"pool_max": ceiling, "driver_provides_pool": True}
        pool = None
        try:
            pool = self._create_pool(config)
            borrowed = [pool.acquire() for _ in range(ceiling)]
            evidence["live_after_borrow"] = ceiling

            deadline = Deadline.start(POOL_WAIT_TIMEOUT_S)
            exhausted: BaseException | None = None
            try:
                extra = pool.acquire()
            except Exception as exc:
                exhausted = exc
                extra = None
            if extra is not None:
                pool.release(extra)
                return CheckOutcome(
                    status=CheckStatus.FAIL,
                    summary=f"池上限 {ceiling} 已满，但第 {ceiling + 1} 次获取仍然成功",
                    evidence=evidence,
                )
            evidence["exhaustion_wait_s"] = round(deadline.elapsed_s, 3)
            evidence["exhaustion_exception"] = type(exhausted).__name__
            evidence["exhaustion_message"] = str(exhausted)
            if deadline.overran:
                return CheckOutcome(
                    status=CheckStatus.FAIL,
                    summary=f"池满时的等待未被有效限制（{deadline.describe()}）",
                    evidence=evidence,
                )

            # A released connection must come back rather than be replaced; identity
            # is the assertion, not merely that acquire returned something.
            returned = borrowed.pop()
            pool.release(returned)
            reused = pool.acquire()
            evidence["reused_same_connection"] = reused is returned
            pool.release(reused)
            if reused is not returned:
                return CheckOutcome(
                    status=CheckStatus.FAIL,
                    summary="归还的连接未被驱动池复用，池另建了一条",
                    evidence=evidence,
                )
            return CheckOutcome(
                status=CheckStatus.PASS,
                summary=f"驱动内置池可借出/归还/复用，池满时在 {evidence['exhaustion_wait_s']}s 内以 {evidence['exhaustion_exception']} 结束",
                evidence=evidence,
            )
        finally:
            if pool is not None:
                try:
                    pool.close(force=True)
                except Exception:  # a pool that cannot be closed is already gone
                    pass

    def _create_pool(self, config: ConnectionConfig) -> Any:
        """Create the driver's pool for the configured target."""
        return self._module.create_pool(
            user=config.user,
            password=config.password,
            dsn=build_dsn(config),
            min=max(0, config.pool_min),
            max=max(1, config.pool_max),
            timeout=POOL_WAIT_TIMEOUT_S,
            wait_timeout=POOL_WAIT_TIMEOUT_S,
        )

    def _check_connect_timeout(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """O5: require the connect timeout to be honoured against a dead endpoint."""
        deadline = Deadline.start(config.connect_timeout_s)
        try:
            connection = self._open(
                config,
                dsn=f"{config.unreachable_host}:{config.unreachable_port}/{config.service_name or config.database or ''}",
                tcp_connect_timeout=config.connect_timeout_s,
            )
        except Exception as exc:
            elapsed = deadline.elapsed_s
            code, native = classify_oracle_error(str(exc))
            evidence = {
                "target": f"{config.unreachable_host}:{config.unreachable_port}",
                "elapsed_s": round(elapsed, 3),
                "allowed_s": deadline.allowed_s,
                "error_code": code.value,
                "native_code": native,
            }
            if code not in (ErrorCode.DB_UNREACHABLE, ErrorCode.DB_TIMEOUT, ErrorCode.DB_DRIVER_ERROR):
                # DB_DRIVER_ERROR is tolerated here because python-oracledb reports a
                # refused connection under its own DPY- codes rather than an ORA- one,
                # and those classify as a driver error. The deadline assertion below
                # is what the check actually rests on.
                return CheckOutcome(status=CheckStatus.FAIL, summary=f"不可达目标返回了非预期错误：{code.value}", error_code=code, native_code=native, evidence=evidence)
            if deadline.overran:
                return CheckOutcome(
                    status=CheckStatus.FAIL,
                    summary=f"连接超时未被遵守：{deadline.describe()}",
                    error_code=code,
                    native_code=native,
                    evidence=evidence,
                )
            # A closed port refuses immediately, which says nothing about whether the
            # configured connect timeout is honoured. Only a target that actually
            # stalls exercises the deadline, so a prompt failure is reported as
            # undetermined rather than as a pass.
            exercised = deadline.exercised()
            evidence["deadline_exercised"] = exercised
            if not exercised:
                return CheckOutcome(
                    status=CheckStatus.INDETERMINATE,
                    summary=f"目标在 {elapsed:.2f}s 内即失败（疑似立即拒绝），未触及配置的 {config.connect_timeout_s:g}s 连接超时，无法据此判断超时是否生效",
                    error_code=code,
                    native_code=native,
                    evidence=evidence,
                )
            return CheckOutcome(
                status=CheckStatus.PASS,
                summary=f"不可达目标在期限内失败（{deadline.describe()}）",
                error_code=code,
                native_code=native,
                evidence=evidence,
            )
        else:
            self._close(connection)
            return CheckOutcome(
                status=CheckStatus.FAIL,
                summary=f"配置的不可达目标 {config.unreachable_host}:{config.unreachable_port} 竟然连接成功",
                evidence={"target": f"{config.unreachable_host}:{config.unreachable_port}"},
            )

    def _check_query_timeout(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """O6: the client must time out, and the server must actually stop."""
        marker = new_probe_marker()
        deadline = Deadline.start(config.query_timeout_s)

        client_timed_out = False
        client_error: str | None = None
        connection = self._connect(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute(build_timeout_probe(marker))
                cursor.fetchone()
        except Exception as exc:
            client_error = str(exc)
            client_timed_out = not deadline.overran
        finally:
            self._close(connection)

        server, observation = self._observe_server_stop(config, marker)
        status, reason = judge_query_timeout(client_timed_out=client_timed_out, server=server)
        evidence = {
            "probe_level": TIMEOUT_PROBE_LEVEL,
            "client_elapsed_s": round(deadline.elapsed_s, 3),
            "client_allowed_s": deadline.allowed_s,
            "client_error": client_error,
            SERVER_STOP_EVIDENCE_KEY: server.value,
            "observation": observation,
        }
        return CheckOutcome(status=status, summary=reason, evidence=evidence)

    def _observe_server_stop(self, config: ConnectionConfig, marker: str) -> tuple[ServerStopEvidence, dict[str, Any]]:
        """Ask a second connection whether this run's probe is still running.

        Args:
            config: The mode configuration, carrying the optional observer account.
            marker: The token embedded in the timeout probe.

        Returns:
            The observation, and the evidence behind it. Without an observer account
            the answer is ``UNDETERMINED`` rather than a guess -- a confirmed
            server-side stop is a gate for the runtime selection, so an unobservable
            outcome must not be reported as one.
        """
        if not (config.observer_user and config.observer_password):
            return ServerStopEvidence.UNDETERMINED, {"reason": "未配置观测账号（OB_ORACLE_OBSERVER_USER/PASSWORD）"}

        budget_s = max(5.0, min(15.0, config.query_timeout_s / 2))
        deadline = Deadline.start(budget_s)
        observations: list[dict[str, Any]] = []
        try:
            observer = self._module.connect(
                user=config.observer_user,
                password=config.observer_password,
                dsn=build_dsn(config),
                tcp_connect_timeout=config.connect_timeout_s,
            )
            observer.call_timeout = int(budget_s * MILLISECONDS_PER_SECOND)
        except Exception as exc:
            return ServerStopEvidence.UNDETERMINED, {"reason": f"观测连接失败：{exc}"}

        try:
            while True:
                with observer.cursor() as cursor:
                    cursor.execute(SERVER_QUERY_PROBE_SQL, {"pattern": f"%{marker}%"})
                    row = cursor.fetchone()
                still_running = bool(row and tuple(row)[0])
                observations.append({"t_s": round(deadline.elapsed_s, 3), "still_running": still_running})
                if not still_running:
                    return ServerStopEvidence.STOPPED, {"polls": observations}
                if deadline.elapsed_s >= budget_s:
                    return ServerStopEvidence.STILL_RUNNING, {"polls": observations}
                time.sleep(1.0)
        except Exception as exc:
            return ServerStopEvidence.UNDETERMINED, {"reason": f"观测查询失败：{exc}", "polls": observations}
        finally:
            self._close(observer)

    def _check_metadata(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """O7: read schema, table and column metadata through two independent paths."""
        owner = (config.schema or config.user.split("@", 1)[0]).upper()

        connection = self._connect(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute(TABLES_SQL, {"owner": owner})
                tables = [str(tuple(row)[0]) for row in cursor.fetchall()]
                if not tables:
                    return CheckOutcome(status=CheckStatus.FAIL, summary=f"schema {owner} 下未读到任何表", evidence={"owner": owner})

                target = (config.metadata_table or tables[0]).upper()
                if target not in tables:
                    return CheckOutcome(
                        status=CheckStatus.FAIL,
                        summary=f"指定的表 {target} 不在该 schema 的已授权表清单内",
                        evidence={"owner": owner, "requested": target},
                    )

                cursor.execute(COLUMNS_SQL, {"owner": owner, "table": target})
                catalog_columns = [tuple(row) for row in cursor.fetchall()]

                cursor.execute(TABLE_COMMENT_SQL, {"owner": owner, "table": target})
                comment_row = cursor.fetchone()
                table_comment = tuple(comment_row)[0] if comment_row else None

                # Second path: the server's own description of a real statement. The
                # identifier is interpolated, which is safe only because it was just
                # enumerated from the catalog and is checked against that list.
                quoted = resolve_identifier(target, allowed=tables, quote='"')
                cursor.execute(DESCRIBE_SQL_TEMPLATE.format(table=quoted))
                described = [str(description[0]) for description in (cursor.description or [])]
        finally:
            self._close(connection)

        catalog_names = [str(row[0]) for row in catalog_columns]
        evidence = {
            "owner": owner,
            "table": target,
            "table_count": len(tables),
            "column_count": len(catalog_columns),
            "catalog_columns": catalog_names,
            "described_columns": described,
            "table_comment": table_comment,
            # The case-semantics observation the downstream design needs: an unquoted
            # Oracle identifier is folded to upper case, so a POC that reports the
            # folded name must say so rather than implying the catalog stored it.
            "names_are_uppercase": all(name == name.upper() for name in catalog_names),
        }
        if not catalog_names:
            return CheckOutcome(status=CheckStatus.FAIL, summary=f"表 {target} 未读到任何列", evidence=evidence)
        if catalog_names != described:
            return CheckOutcome(
                status=CheckStatus.FAIL,
                summary=f"两条元数据路径不一致：字典视图 {len(catalog_names)} 列，结果集描述 {len(described)} 列",
                evidence=evidence,
            )
        return CheckOutcome(
            status=CheckStatus.PASS,
            summary=f"{len(tables)} 张表；{target} 的 {len(catalog_columns)} 列在字典视图与结果集描述下一致",
            evidence=evidence,
        )

    def _check_close_reconnect(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """O8: a closed connection must be unusable, and reconnecting must work."""
        connection = self._connect(config)
        self._close(connection)

        reuse_error: str | None = None
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM DUAL")
                cursor.fetchone()
        except Exception as exc:
            reuse_error = str(exc)
        if reuse_error is None:
            return CheckOutcome(status=CheckStatus.FAIL, summary="已关闭的连接仍可执行查询")

        reconnected = self._connect(config)
        try:
            with reconnected.cursor() as cursor:
                cursor.execute("SELECT 1 FROM DUAL")
                cursor.fetchone()
        finally:
            self._close(reconnected)

        return CheckOutcome(
            status=CheckStatus.PASS,
            summary="关闭后复用报错可辨识，重连成功",
            evidence={"reuse_error": reuse_error},
        )

    def _check_error_handling(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """O9: each failure class must map to its stable shared error code."""
        results: list[dict[str, Any]] = []
        mismatches: list[str] = []

        for probe in ERROR_PROBES:
            try:
                if probe.statement is not None:
                    connection = self._connect(config)
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute(probe.statement)
                            cursor.fetchall()
                    finally:
                        self._close(connection)
                else:
                    connection = self._module.connect(**build_probe_connect_kwargs(config, probe))
                    self._close(connection)
            except Exception as exc:
                code, native = classify_oracle_error(str(exc))
                matched = code in probe.expect
                results.append({"key": probe.key, "expected": [c.value for c in probe.expect], "got": code.value, "native": native, "matched": matched})
                if not matched:
                    mismatches.append(f"{probe.key}: 期望 {[c.value for c in probe.expect]}，得到 {code.value}")
            else:
                results.append({"key": probe.key, "expected": [c.value for c in probe.expect], "got": None, "matched": False})
                mismatches.append(f"{probe.key}: 期望失败却成功")

        if mismatches:
            return CheckOutcome(
                status=CheckStatus.FAIL,
                summary=f"{len(mismatches)}/{len(ERROR_PROBES)} 个错误分类不符",
                evidence={"probes": results, "mismatches": mismatches},
            )
        return CheckOutcome(
            status=CheckStatus.PASS,
            summary=f"{len(ERROR_PROBES)} 类失败均映射到稳定错误码",
            evidence={"probes": results},
        )


def driver(redactor: Redactor) -> OracleDBDriver:
    """Build the Oracle-mode driver adapter.

    Args:
        redactor: The redactor carrying this run's secret values.

    Returns:
        The adapter, wrapping the installed python-oracledb module when present.
    """
    module, version = load()
    return OracleDBDriver(module, version, redactor)
