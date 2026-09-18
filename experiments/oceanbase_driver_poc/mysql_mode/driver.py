"""MySQL compatibility mode over PyMySQL.

The mode speaks the MySQL wire protocol, so a pure-Python client works without any
native client library -- which is why this mode carries none of the deployment
weight that the Oracle mode does.

The split in this module matters:

* everything that maps our configuration onto driver parameters is a pure function
  and is covered by L1 tests here and now;
* everything that opens a socket is an L2 seam that reports ``UNSUPPORTED`` until a
  reachable OceanBase instance exists.

Timeout units are a real source of bugs and are pinned by tests rather than left to
memory: OceanBase's own ``ob_query_timeout`` system variable is in microseconds,
PyMySQL's socket timeouts are in seconds, and the two are not interchangeable.
"""

from __future__ import annotations

from typing import Any

from common.checks import CheckSpec
from common.config import ConnectionConfig
from common.harness import CheckOutcome
from common.redaction import Redactor
from common.results import CheckStatus
from common.versions import resolve_version

DRIVER_PACKAGE = "pymysql"
INSTALL_HINT = "安装 POC 依赖：pip install -r requirements-poc.txt"

# OceanBase's own statement-level timeout, in microseconds.
OB_QUERY_TIMEOUT_UNIT = 1_000_000

L2_PENDING = "需要真实 OceanBase 实例，属 L2 工作项"


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
        }

    @property
    def installed(self) -> bool:
        """Whether the driver package is available for use."""
        return self._module is not None

    def run_check(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """Execute one verification item.

        Args:
            check: The verification item to execute.
            config: The MySQL-mode connection configuration.

        Returns:
            The outcome. Items not yet wired up report ``UNSUPPORTED`` with
            ``unimplemented`` set, so an unfinished skeleton can never be mistaken
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

    def _connect(self, config: ConnectionConfig) -> Any:
        """Open a connection and apply the session-level query timeout."""
        connection = self._module.connect(**build_connect_kwargs(config))
        with connection.cursor() as cursor:
            for statement in build_session_timeout_statements(config):
                cursor.execute(statement)
        return connection

    def _check_connect(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """M1: establish a connection and read back the instance version."""
        connection = self._connect(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.execute("SELECT VERSION()")
                row = cursor.fetchone()
        finally:
            connection.close()

        version = row[0] if row else "unknown"
        return CheckOutcome(
            status=CheckStatus.PASS,
            summary=f"连接成功，实例版本 {version}",
            evidence={"version": version, "connect_kwargs": describe_connect_kwargs(config, self._redactor)},
        )

    def _check_simple_query(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """M2: run SELECT 1 and a small multi-type projection."""
        connection = self._connect(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                scalar = cursor.fetchone()
                cursor.execute("SELECT 1 AS n, 'text' AS s, NULL AS n_null")
                row = cursor.fetchone()
        finally:
            connection.close()

        return CheckOutcome(
            status=CheckStatus.PASS,
            summary="简单查询返回预期结果",
            evidence={"select_1": list(scalar) if scalar else None, "row": list(row) if row else None},
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
