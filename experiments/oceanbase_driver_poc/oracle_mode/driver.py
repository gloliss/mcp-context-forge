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

As in the MySQL suite, configuration mapping is pure and tested now, while anything
that opens a socket is an L2 seam.
"""

from __future__ import annotations

from typing import Any

from common.checks import CheckSpec
from common.config import ConnectionConfig
from common.harness import CheckOutcome
from common.redaction import Redactor
from common.results import CheckStatus
from common.versions import resolve_version

DRIVER_PACKAGE = "oracledb"
INSTALL_HINT = "安装 POC 依赖：pip install -r requirements-poc.txt"

# OceanBase's own statement-level timeout, in microseconds.
OB_QUERY_TIMEOUT_UNIT = 1_000_000

MILLISECONDS_PER_SECOND = 1000

L2_PENDING = "需要真实 OceanBase 实例，属 L2 工作项"


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
        }

    @property
    def installed(self) -> bool:
        """Whether the driver package is available for use."""
        return self._module is not None

    def run_check(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """Execute one verification item.

        Args:
            check: The verification item to execute.
            config: The Oracle-mode connection configuration.

        Returns:
            The outcome. Items not yet wired up report ``UNSUPPORTED`` with
            ``unimplemented`` set.
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
        """Open a connection and apply the per-call and session query timeouts."""
        connection = self._module.connect(**build_connect_kwargs(config))
        connection.call_timeout = call_timeout_ms(config)
        with connection.cursor() as cursor:
            for statement in build_session_timeout_statements(config):
                cursor.execute(statement)
        return connection

    def _check_connect(self, check: CheckSpec, config: ConnectionConfig) -> CheckOutcome:
        """O1: establish a connection and read back the instance version."""
        connection = self._connect(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM DUAL")
                cursor.fetchone()
                cursor.execute("SELECT banner FROM v$version WHERE ROWNUM = 1")
                row = cursor.fetchone()
        finally:
            connection.close()

        version = row[0] if row else "unknown"
        return CheckOutcome(
            status=CheckStatus.PASS,
            summary=f"连接成功（{describe_client_mode(self._module)} 模式），实例版本 {version}",
            evidence={
                "version": version,
                "client_mode": describe_client_mode(self._module),
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
            connection.close()

        return CheckOutcome(
            status=CheckStatus.PASS,
            summary="简单 SELECT 返回预期结果",
            evidence={"select_1": list(scalar) if scalar else None, "row": list(row) if row else None},
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
