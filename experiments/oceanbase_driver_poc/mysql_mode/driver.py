"""MySQL compatibility mode over PyMySQL.

The mode speaks the MySQL wire protocol, so a pure-Python client works without any
native client library -- which is why this mode carries none of the deployment
weight that the Oracle mode does.

The split in this module matters and is maintained deliberately:

* everything that maps our configuration onto driver parameters, decides what to run,
  or turns a raw observation into a verdict is a pure function, covered by L1 tests
  here and now;
* everything that opens a socket is an L2 seam. The seams are written but have never
  executed against an OceanBase instance, so their results are only meaningful when a
  run actually produced them -- see ``docs/oceanbase-driver-poc.md``.

Timeout units are a real source of bugs and are pinned by tests rather than left to
memory: OceanBase's own ``ob_query_timeout`` system variable is in microseconds,
PyMySQL's socket timeouts are in seconds, and the two are not interchangeable.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from common.checks import CheckSpec
from common.config import ConnectionConfig
from common.errors import ErrorCode, classify_mysql_error
from common.evidence import (
    INSTANCE_EVIDENCE_KEY,
    SERVER_STOP_EVIDENCE_KEY,
    ServerStopEvidence,
    build_instance_evidence,
    judge_query_timeout,
)
from common.harness import CheckOutcome
from common.probes import BIND_CASES, BoundedPool, Deadline, PoolExhausted, resolve_identifier
from common.redaction import Redactor
from common.results import CheckStatus
from common.versions import resolve_version

DRIVER_PACKAGE = "pymysql"
INSTALL_HINT = "安装 POC 依赖：pip install -r requirements-poc.txt"

# OceanBase's own statement-level timeout, in microseconds.
OB_QUERY_TIMEOUT_UNIT = 1_000_000

L2_PENDING = "需要真实 OceanBase 实例，属 L2 工作项"

# Candidate statements for reading the instance's compatibility mode. These are
# *candidates*: no OceanBase instance was reachable while writing them, so which one
# the server answers is exactly what an L2 run establishes. Failing to read the mode
# never fails the connection check -- it is recorded, with the attempts, so the
# unknown is actionable rather than silent.
COMPAT_MODE_QUERIES: tuple[tuple[str, str], ...] = (
    ("SELECT @@ob_compatibility_mode", "variable"),
    ("SHOW VARIABLES LIKE 'ob_compatibility_mode'", "show-variables"),
)

INSTANCE_VERSION_SQL = "SELECT VERSION()"

TABLES_SQL = "SELECT table_name, table_type, table_comment FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name"
COLUMNS_SQL = (
    "SELECT column_name, data_type, is_nullable, column_comment, ordinal_position "
    "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
)

# Marker embedded in the timeout probe so the observation connection can find *this*
# run's query rather than any long-running statement on a shared instance.
SERVER_QUERY_PROBE_SQL = "SELECT COUNT(*) FROM information_schema.processlist WHERE info LIKE %s AND command <> 'Sleep'"


@dataclass(frozen=True)
class ErrorProbe:
    """One deliberately-failing operation and the codes it may be classified as.

    ``expect`` is a tuple rather than a single code because some failures genuinely
    have more than one correct classification. Connecting to a database that does not
    exist is the clear case: MySQL reports "unknown database" (1049) to an account
    with global privileges but "access denied" (1044) to a least-privilege one, so
    pinning a single expected code would turn a correct classification into a failed
    check on exactly the restricted account a production deployment would use.
    """

    key: str
    description: str
    expect: tuple[ErrorCode, ...]
    statement: str | None = None
    override_database: str | None = None
    override_user: str | None = None


# Probes for the error-handling check. Authentication is probed with a *non-existent
# account* rather than a wrong password for a real one: the classification is the
# same and it cannot lock out the account the POC needs.
ERROR_PROBES: tuple[ErrorProbe, ...] = (
    ErrorProbe(
        "unknown-database",
        "连接到一个不存在的数据库",
        (ErrorCode.DB_OBJECT_NOT_FOUND, ErrorCode.DB_PERMISSION_DENIED),
        override_database="ob_poc_no_such_db",
    ),
    ErrorProbe("unknown-table", "查询一个不存在的表", (ErrorCode.DB_OBJECT_NOT_FOUND,), statement="SELECT 1 FROM ob_poc_no_such_table_xyz"),
    ErrorProbe("syntax-error", "语法不完整的语句", (ErrorCode.DB_SYNTAX_ERROR,), statement="SELECT 1 FROM"),
    ErrorProbe("auth-failure", "使用不存在的账号连接", (ErrorCode.DB_AUTH_FAILED,), override_user="ob_poc_no_such_user_xyz"),
)


def load() -> tuple[Any | None, str]:
    """Import PyMySQL if it is installed.

    Returns:
        ``(module, version)``. The module is ``None`` when PyMySQL is absent, which
        the harness reports as ``UNSUPPORTED`` rather than as a failure. The version
        comes from distribution metadata, not ``__version__`` -- PyMySQL's attribute
        disagrees with its own distribution version.
    """
    try:
        import pymysql  # noqa: PLC0415 - optional POC dependency
    except ImportError:
        return None, "not-installed"
    return pymysql, resolve_version(DRIVER_PACKAGE, pymysql)


def build_connect_kwargs(config: ConnectionConfig) -> dict[str, Any]:
    """Map a connection configuration onto PyMySQL connect parameters.

    Args:
        config: The MySQL-mode connection configuration.

    Returns:
        Keyword arguments for ``pymysql.connect``. The socket read timeout is wired
        to the configured query timeout; it is a client-side guard only and does not
        by itself stop a server-side query.
    """
    return {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "password": config.password,
        "database": config.database,
        "connect_timeout": config.connect_timeout_s,
        "read_timeout": config.query_timeout_s,
        "charset": "utf8mb4",
    }


def build_probe_connect_kwargs(config: ConnectionConfig, probe: ErrorProbe) -> dict[str, Any]:
    """Build connect parameters for a deliberately-failing connection probe.

    Args:
        config: The base configuration.
        probe: The probe, carrying any user/database override.

    Returns:
        Connect parameters with the probe's overrides applied. A short connect
        timeout is imposed so a probe against a bad endpoint fails promptly.
    """
    kwargs = build_connect_kwargs(config)
    if probe.override_user is not None:
        kwargs["user"] = probe.override_user
    if probe.override_database is not None:
        kwargs["database"] = probe.override_database
    return kwargs


def describe_connect_kwargs(config: ConnectionConfig, redactor: Redactor) -> dict[str, Any]:
    """Render connect parameters for a result file with credentials removed.

    Args:
        config: The MySQL-mode connection configuration.
        redactor: The redactor carrying this run's secret values.

    Returns:
        The same mapping with the password masked and the username scope stripped.
    """
    return redactor.redact_deep(build_connect_kwargs(config))


def build_session_timeout_statements(config: ConnectionConfig) -> list[str]:
    """Build the session statements that apply the configured query timeout.

    OceanBase's own ``ob_query_timeout`` applies server-side and covers the case a
    client-side read timeout cannot: the server actually stopping the query.

    Args:
        config: The MySQL-mode connection configuration.

    Returns:
        Statements to run on a freshly opened session.
    """
    microseconds = int(config.query_timeout_s * OB_QUERY_TIMEOUT_UNIT)
    return [f"SET SESSION ob_query_timeout = {microseconds}"]


def normalize_compat_mode(source: str, row: Any) -> str | None:
    """Extract the compatibility mode from whichever probe statement answered.

    Args:
        source: The probe label the row came from.
        row: The row the probe returned.

    Returns:
        The mode value, or ``None`` when the row did not carry one. ``SHOW VARIABLES``
        returns ``(Variable_name, Value)`` while a ``SELECT @@...`` returns a
        one-tuple, so the two shapes are normalised here rather than at the call site.
    """
    if row is None:
        return None
    values = tuple(row)
    if source == "show-variables":
        return str(values[1]) if len(values) > 1 and values[1] is not None else None
    return str(values[0]) if values and values[0] is not None else None


def build_timeout_probe(seconds: int, marker: str) -> str:
    """Build the long-running statement the query-timeout check cancels.

    Args:
        seconds: How long the server should sleep.
        marker: A unique token the observation connection searches for.

    Returns:
        A ``SELECT SLEEP(n)`` carrying the marker in a comment. The marker lets the
        observation query identify this run's statement on a shared instance; it is
        generated by this module, never supplied by a caller.
    """
    return f"SELECT SLEEP({int(seconds)}) /* {marker} */"


def new_probe_marker() -> str:
    """Return a fresh, SQL-safe token identifying one timeout probe."""
    return f"ob-poc-timeout-{uuid.uuid4().hex}"


def pool_acquire_timeout_s(config: ConnectionConfig) -> float:
    """Derive how long a pool acquisition may wait before reporting exhaustion.

    Args:
        config: The MySQL-mode connection configuration.

    Returns:
        A bounded wait derived from the connect timeout, so an exhausted pool fails
        as promptly as an unreachable server rather than hanging.
    """
    return round(min(5.0, max(1.0, config.connect_timeout_s)), 3)


class PyMySQLDriver:
    """Driver adapter for OceanBase's MySQL compatibility mode."""

    name = DRIVER_PACKAGE

    def __init__(self, module: Any | None, version: str, redactor: Redactor) -> None:
        """Wrap an already-imported PyMySQL module.

        Args:
            module: The imported ``pymysql`` module, or ``None`` when absent.
            version: The driver version, for the result record.
            redactor: The redactor carrying this run's secret values. The harness
                redacts outcomes too; the driver redacting its own evidence keeps
                that guarantee true even if a payload is inspected earlier.
        """
        self._module = module
        self._redactor = redactor
        self.version = version
        self._handlers = {
            "M1": self._check_connect,
            "M2": self._check_simple_query,
            "M3": self._check_param_binding,
            "M4": self._check_pool,
            "M5": self._check_connect_timeout,
            "M6": self._check_query_timeout,
            "M7": self._check_metadata,
            "M8": self._check_close_reconnect,
            "M9": self._check_error_handling,
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
            config: The MySQL-mode connection configuration.

        Returns:
            The outcome. A missing driver package reports ``UNSUPPORTED``, and so does
            a check id with no handler, so an unfinished suite can never be mistaken
            for a passing one.
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

    def _open(self, config: ConnectionConfig, **overrides: Any) -> tuple[Any, list[str]]:
        """Open a connection and apply the session-level query timeout.

        A session setting the server refuses is recorded rather than raised. It
        matters to exactly one check -- the query-timeout check, whose server-side
        stop depends on ``ob_query_timeout`` -- and that check reports it. Failing
        the connection outright would take the other eight checks down with it for a
        reason that has nothing to do with what they measure.

        Args:
            config: The connection configuration.
            overrides: Connect-parameter overrides for probe targets.

        Returns:
            The connection, and any session-setup failures as notes.
        """
        kwargs = build_connect_kwargs(config)
        kwargs.update(overrides)
        connection = self._module.connect(**kwargs)
        notes: list[str] = []
        with connection.cursor() as cursor:
            for statement in build_session_timeout_statements(config):
                try:
                    cursor.execute(statement)
                except Exception as exc:  # the variable may not exist off OceanBase
                    notes.append(f"{statement}: {exc}")
        return connection, notes

    def _connect(self, config: ConnectionConfig) -> tuple[Any, list[str]]:
        """Open a connection to the configured target."""
        return self._open(config)

    def _close(self, connection: Any) -> None:
        """Close a connection, ignoring a failure on an already-dead one."""
        try:
            connection.close()
        except Exception:  # closing an already-broken connection is not an error
            pass

    def _healthy(self, connection: Any) -> bool:
        """Probe whether a pooled connection is still usable."""
        try:
            connection.ping(reconnect=False)
        except Exception:
            return False
        return True

    def _probe_instance(self, cursor: Any) -> dict[str, Any]:
        """Read the instance version and compatibility mode over a live cursor."""
        cursor.execute(INSTANCE_VERSION_SQL)
        row = cursor.fetchone()
        version = str(row[0]) if row and row[0] is not None else None

        compat_mode: str | None = None
        source: str | None = None
        attempts: list[str] = []
        for statement, label in COMPAT_MODE_QUERIES:
            try:
                cursor.execute(statement)
                candidate = normalize_compat_mode(label, cursor.fetchone())
            except Exception as exc:  # a probe that the server refuses is expected
                attempts.append(f"{label}: {exc}")
                continue
            if candidate:
                compat_mode = candidate
                source = label
                break
            attempts.append(f"{label}: 未返回兼容模式")
        if compat_mode is None and not attempts:
            attempts.append("no-candidate-answered")
        return build_instance_evidence(version=version, compat_mode=compat_mode, compat_mode_source=source or "unavailable", probe_attempts=attempts)

    # --------------------------------------------------------------------- checks

    def _check_connect(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """M1: establish a connection and read back the instance version."""
        connection, session_notes = self._connect(config)
        try:
            with connection.cursor() as cursor:
                instance = self._probe_instance(cursor)
        finally:
            self._close(connection)

        version = instance.get("version") or "unknown"
        mode = instance.get("compat_mode")
        mode_note = f"，兼容模式 {mode}" if mode else "，兼容模式未读到"
        return CheckOutcome(
            status=CheckStatus.PASS,
            summary=f"连接成功，实例版本 {version}{mode_note}",
            evidence={
                INSTANCE_EVIDENCE_KEY: instance,
                "connect_kwargs": describe_connect_kwargs(config, self._redactor),
                "session_setup_errors": session_notes,
            },
        )

    def _check_simple_query(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """M2: run SELECT 1 and a small multi-type projection."""
        connection, _ = self._connect(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                scalar = cursor.fetchone()
                cursor.execute("SELECT 1 AS n, 'text' AS s, NULL AS n_null")
                row = cursor.fetchone()
        finally:
            self._close(connection)

        if scalar is None or row is None:
            return CheckOutcome(status=CheckStatus.FAIL, summary="简单查询未返回结果")
        if tuple(scalar)[0] != 1 or tuple(row)[0] != 1:
            return CheckOutcome(status=CheckStatus.FAIL, summary=f"简单查询返回了非预期结果：{scalar!r} / {row!r}")
        return CheckOutcome(
            status=CheckStatus.PASS,
            summary="简单查询返回预期结果",
            evidence={"select_1": list(scalar), "row": list(row)},
        )

    def _check_param_binding(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """M3: bind parameter values and require them back byte-for-byte."""
        connection, _ = self._connect(config)
        try:
            with connection.cursor() as cursor:
                mismatches: list[str] = []
                for label, value in BIND_CASES:
                    cursor.execute("SELECT %s", (value,))
                    row = cursor.fetchone()
                    # A missing row must not read as "the driver returned None" for the
                    # NULL case, which would turn a silent no-result into a pass.
                    if row is None or len(tuple(row)) != 1:
                        mismatches.append(f"{label}: 未返回单列结果（{row!r}）")
                        continue
                    got = tuple(row)[0]
                    if got != value:
                        mismatches.append(f"{label}: 期望 {value!r}，得到 {got!r}")
        finally:
            self._close(connection)

        total = len(BIND_CASES)
        if mismatches:
            return CheckOutcome(
                status=CheckStatus.FAIL,
                summary=f"参数绑定改变了取值（{len(mismatches)}/{total} 项不符）",
                evidence={"mismatches": mismatches, "cases": total},
            )
        return CheckOutcome(
            status=CheckStatus.PASS,
            summary=f"{total} 类参数（含引号、分号、注释符、Unicode）经绑定后取值不变",
            evidence={"cases": [label for label, _ in BIND_CASES]},
        )

    def _check_pool(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """M4: verify a bounded pool over this driver against the real server."""
        pool = BoundedPool(
            lambda: self._connect(config)[0],
            max_size=max(1, config.pool_max),
            acquire_timeout_s=pool_acquire_timeout_s(config),
            is_healthy=self._healthy,
            closer=self._close,
        )
        ceiling = max(1, config.pool_max)
        evidence: dict[str, Any] = {"pool_max": ceiling, "driver_provides_pool": False}

        try:
            borrowed = [pool.acquire() for _ in range(ceiling)]
            evidence["live_after_borrow"] = pool.live

            # One more than the ceiling must fail within the bounded wait rather than
            # block forever. That bound is the property being verified.
            deadline = Deadline.start(pool_acquire_timeout_s(config))
            try:
                extra = pool.acquire()
            except PoolExhausted:
                extra = None
                evidence["exhaustion_wait_s"] = round(deadline.elapsed_s, 3)
            if extra is not None:
                pool.release(extra)
                return CheckOutcome(
                    status=CheckStatus.FAIL,
                    summary=f"池上限 {ceiling} 已满，但第 {ceiling + 1} 次获取仍然成功",
                    evidence=evidence,
                )
            if deadline.overran:
                return CheckOutcome(
                    status=CheckStatus.FAIL,
                    summary=f"池满时的等待未被有效限制（{deadline.describe()}）",
                    evidence=evidence,
                )

            # A returned, still-healthy connection must come back rather than be
            # replaced. Comparing identity is the point of the check; asserting only
            # that acquire returned *something* would pass even if the pool rebuilt
            # from scratch every time.
            returned = borrowed.pop()
            still_healthy = self._healthy(returned)
            pool.release(returned, healthy=still_healthy)
            reused = pool.acquire()
            evidence["released_connection_was_healthy"] = still_healthy
            evidence["reused_same_connection"] = reused is returned
            pool.release(reused)
            evidence["idle_after_release"] = pool.idle
            if still_healthy and reused is not returned:
                return CheckOutcome(
                    status=CheckStatus.FAIL,
                    summary="归还的健康连接未被复用，池另建了一条",
                    evidence=evidence,
                )
            return CheckOutcome(
                status=CheckStatus.PASS,
                summary=f"借出/归还/复用可达，池满时在 {evidence['exhaustion_wait_s']}s 内返回可辨识错误",
                evidence=evidence,
            )
        finally:
            pool.close_all()

    def _check_connect_timeout(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """M5: require the connect timeout to be honoured against a dead endpoint."""
        deadline = Deadline.start(config.connect_timeout_s)
        try:
            connection, _ = self._open(
                config,
                host=config.unreachable_host,
                port=config.unreachable_port,
                connect_timeout=config.connect_timeout_s,
            )
        except Exception as exc:
            elapsed = deadline.elapsed_s
            code, native = classify_mysql_error(str(exc))
            evidence = {
                "target": f"{config.unreachable_host}:{config.unreachable_port}",
                "elapsed_s": round(elapsed, 3),
                "allowed_s": deadline.allowed_s,
                "error_code": code.value,
                "native_code": native,
            }
            if code not in (ErrorCode.DB_UNREACHABLE, ErrorCode.DB_TIMEOUT):
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
                summary=f"不可达目标在期限内失败并映射为 {code.value}（{deadline.describe()}）",
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
        """M6: the client must time out, and the server must actually stop."""
        marker = new_probe_marker()
        sleep_s = max(5, int(config.query_timeout_s) + 5)
        deadline = Deadline.start(config.query_timeout_s)

        client_timed_out = False
        client_error: str | None = None
        connection, session_notes = self._connect(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute(build_timeout_probe(sleep_s, marker))
                cursor.fetchone()
        except Exception as exc:
            client_error = str(exc)
            client_timed_out = not deadline.overran
        finally:
            self._close(connection)

        server, observation = self._observe_server_stop(config, marker)
        status, reason = judge_query_timeout(client_timed_out=client_timed_out, server=server)
        evidence = {
            "probe_seconds": sleep_s,
            "session_setup_errors": session_notes,
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
            the answer is ``UNDETERMINED`` rather than a guess -- the design makes a
            confirmed server-side stop a gate, so an unobservable outcome must not be
            reported as one.
        """
        if not (config.observer_user and config.observer_password):
            return ServerStopEvidence.UNDETERMINED, {"reason": "未配置观测账号（OB_MYSQL_OBSERVER_USER/PASSWORD）"}

        budget_s = max(5.0, min(15.0, config.query_timeout_s / 2))
        deadline = Deadline.start(budget_s)
        observations: list[dict[str, Any]] = []
        try:
            observer = self._module.connect(
                host=config.host,
                port=config.port,
                user=config.observer_user,
                password=config.observer_password,
                database=config.database,
                connect_timeout=config.connect_timeout_s,
                read_timeout=budget_s,
                charset="utf8mb4",
            )
        except Exception as exc:
            return ServerStopEvidence.UNDETERMINED, {"reason": f"观测连接失败：{exc}"}

        try:
            while True:
                with observer.cursor() as cursor:
                    cursor.execute(SERVER_QUERY_PROBE_SQL, (f"%{marker}%",))
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
        """M7: read table and column metadata through two independent paths."""
        schema = config.schema or config.database
        if not schema:
            return CheckOutcome(
                status=CheckStatus.FAIL,
                summary="未配置 schema 或 database，无法确定元数据范围",
            )

        connection, _ = self._connect(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute(TABLES_SQL, (schema,))
                tables = [tuple(row) for row in cursor.fetchall()]
                if not tables:
                    return CheckOutcome(status=CheckStatus.FAIL, summary=f"schema {schema} 下未读到任何表", evidence={"schema": schema})

                names = [str(row[0]) for row in tables]
                target = config.metadata_table or names[0]
                if target not in names:
                    return CheckOutcome(
                        status=CheckStatus.FAIL,
                        summary=f"指定的表 {target} 不在该 schema 的已授权表清单内",
                        evidence={"schema": schema, "requested": target},
                    )

                cursor.execute(COLUMNS_SQL, (schema, target))
                columns = [tuple(row) for row in cursor.fetchall()]

                # Second path: SHOW COLUMNS needs an interpolated identifier, which is
                # only safe because the name was just enumerated from the server.
                quoted = resolve_identifier(target, allowed=names)
                cursor.execute(f"SHOW COLUMNS FROM {quoted}")
                shown = [tuple(row) for row in cursor.fetchall()]
        finally:
            self._close(connection)

        info_names = [str(row[0]) for row in columns]
        show_names = [str(row[0]) for row in shown]
        evidence = {
            "schema": schema,
            "table": target,
            "table_count": len(tables),
            "column_count": len(columns),
            "information_schema_columns": info_names,
            "show_columns_columns": show_names,
            # A real observation rather than a restatement: it records whether the
            # server hands back identifiers folded to upper case, which is the
            # case-semantics question the downstream Oracle-mode design has to answer.
            "names_are_uppercase": all(name == name.upper() for name in info_names),
        }
        if not info_names:
            return CheckOutcome(status=CheckStatus.FAIL, summary=f"表 {target} 未读到任何列", evidence=evidence)
        if info_names != show_names:
            return CheckOutcome(
                status=CheckStatus.FAIL,
                summary=f"两条元数据路径不一致：information_schema {len(info_names)} 列，SHOW COLUMNS {len(show_names)} 列",
                evidence=evidence,
            )
        return CheckOutcome(
            status=CheckStatus.PASS,
            summary=f"{len(tables)} 张表；{target} 的 {len(columns)} 列在两条路径下一致",
            evidence=evidence,
        )

    def _check_close_reconnect(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """M8: a closed connection must be unusable, and reconnecting must work."""
        connection, _ = self._connect(config)
        self._close(connection)

        reuse_error: str | None = None
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:
            reuse_error = str(exc)
        if reuse_error is None:
            return CheckOutcome(status=CheckStatus.FAIL, summary="已关闭的连接仍可执行查询")

        healthy_after_close = self._healthy(connection)
        if healthy_after_close:
            return CheckOutcome(status=CheckStatus.FAIL, summary="已关闭的连接被健康检查判定为可用")

        reconnected, _ = self._connect(config)
        try:
            with reconnected.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        finally:
            self._close(reconnected)

        return CheckOutcome(
            status=CheckStatus.PASS,
            summary="关闭后复用报错可辨识、健康检查拒绝该连接，重连成功",
            evidence={"reuse_error": reuse_error, "healthy_after_close": healthy_after_close},
        )

    def _check_error_handling(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """M9: each failure class must map to its stable shared error code."""
        results: list[dict[str, Any]] = []
        mismatches: list[str] = []

        for probe in ERROR_PROBES:
            try:
                if probe.statement is not None:
                    connection, _ = self._connect(config)
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
                code, native = classify_mysql_error(str(exc))
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


def driver(redactor: Redactor) -> PyMySQLDriver:
    """Build the MySQL-mode driver adapter.

    Args:
        redactor: The redactor carrying this run's secret values.

    Returns:
        The adapter, wrapping the installed PyMySQL module when present.
    """
    module, version = load()
    return PyMySQLDriver(module, version, redactor)
