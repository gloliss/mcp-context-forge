"""Environment-backed connection configuration for one compatibility mode.

Connection details come from environment variables only. Nothing is hard-coded and
nothing is persisted: the requirement forbids credentials in source, in the
repository, and in logs.

A missing or blank required variable is not an error condition here. It is the
normal state of a machine that has no OceanBase instance attached, so
:func:`load_config` returns ``None`` and the harness turns that into
``SKIP_NO_ENV`` for every check. Raising instead would make "no environment" look
like "broken code", which is the confusion the result vocabulary exists to prevent.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from common.redaction import Redactor

ENV_PREFIX: dict[str, str] = {"mysql": "OB_MYSQL", "oracle": "OB_ORACLE"}

# Port is required rather than defaulted. OceanBase is reached through OBProxy on
# one port and directly on another, so a hard-coded default would silently point the
# POC at the wrong endpoint and produce a confident, wrong verdict.
REQUIRED_FIELDS: tuple[str, ...] = ("HOST", "PORT", "USER", "PASSWORD")


@dataclass(frozen=True)
class ConnectionConfig:
    """Connection settings for one OceanBase compatibility mode."""

    mode: str
    host: str
    port: int
    user: str
    password: str
    database: str | None = None
    service_name: str | None = None
    schema: str | None = None
    connect_timeout_s: float = 5.0
    query_timeout_s: float = 30.0
    pool_min: int = 0
    pool_max: int = 5

    def redacted(self, redactor: Redactor) -> dict[str, Any]:
        """Render the target for a result file with credentials removed.

        Args:
            redactor: The redactor carrying this run's secret values.

        Returns:
            A JSON-safe mapping. The password appears masked, and the username keeps
            only its account part so tenant and cluster names do not travel.
        """
        return redactor.redact_deep(
            {
                "host": self.host,
                "port": self.port,
                "user": self.user,
                "password": self.password,
                "database": self.database,
                "service_name": self.service_name,
                "schema": self.schema,
                "connect_timeout_s": self.connect_timeout_s,
                "query_timeout_s": self.query_timeout_s,
                "pool_min": self.pool_min,
                "pool_max": self.pool_max,
            }
        )


def env_var_names(mode: str) -> dict[str, str]:
    """Return the environment variable name for each configurable field.

    Args:
        mode: ``"mysql"`` or ``"oracle"``.

    Returns:
        A mapping from field name to environment variable name.
    """
    prefix = ENV_PREFIX[mode]
    return {
        "host": f"{prefix}_HOST",
        "port": f"{prefix}_PORT",
        "user": f"{prefix}_USER",
        "password": f"{prefix}_PASSWORD",
        "database": f"{prefix}_DATABASE",
        "service_name": f"{prefix}_SERVICE_NAME",
        "schema": f"{prefix}_SCHEMA",
        "connect_timeout_s": f"{prefix}_CONNECT_TIMEOUT_S",
        "query_timeout_s": f"{prefix}_QUERY_TIMEOUT_S",
        "pool_min": f"{prefix}_POOL_MIN",
        "pool_max": f"{prefix}_POOL_MAX",
    }


def missing_required(mode: str, env: Mapping[str, str] | None = None) -> list[str]:
    """List the required environment variables that are absent or blank.

    Args:
        mode: ``"mysql"`` or ``"oracle"``.
        env: Environment mapping to read; defaults to :data:`os.environ`.

    Returns:
        The missing variable names, in declaration order. Empty when the mode is
        fully configured.
    """
    source = os.environ if env is None else env
    names = env_var_names(mode)
    return [names[field.lower()] for field in REQUIRED_FIELDS if not _clean(source.get(names[field.lower()]))]


def load_config(mode: str, env: Mapping[str, str] | None = None) -> ConnectionConfig | None:
    """Load one mode's configuration from the environment.

    Args:
        mode: ``"mysql"`` or ``"oracle"``.
        env: Environment mapping to read; defaults to :data:`os.environ`.

    Returns:
        The configuration, or ``None`` when any required variable is missing or
        blank. ``None`` means "this machine has no target for this mode", which the
        harness reports as ``SKIP_NO_ENV``.

    Raises:
        KeyError: If ``mode`` is not a known compatibility mode.
    """
    if mode not in ENV_PREFIX:
        raise KeyError(f"unknown compatibility mode: {mode!r}")

    source = os.environ if env is None else env
    names = env_var_names(mode)

    if missing_required(mode, source):
        return None

    return ConnectionConfig(
        mode=mode,
        host=str(_clean(source.get(names["host"]))),
        port=int(str(_clean(source.get(names["port"])))),
        user=str(_clean(source.get(names["user"]))),
        password=str(_clean(source.get(names["password"]))),
        database=_clean(source.get(names["database"])),
        service_name=_clean(source.get(names["service_name"])),
        schema=_clean(source.get(names["schema"])),
        connect_timeout_s=_as_float(source.get(names["connect_timeout_s"]), 5.0),
        query_timeout_s=_as_float(source.get(names["query_timeout_s"]), 30.0),
        pool_min=_as_int(source.get(names["pool_min"]), 0),
        pool_max=_as_int(source.get(names["pool_max"]), 5),
    )


def _clean(value: str | None) -> str | None:
    """Normalise an environment value, treating blank as absent."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _as_float(value: str | None, default: float) -> float:
    """Parse a float, falling back to ``default`` on missing or malformed input."""
    cleaned = _clean(value)
    if cleaned is None:
        return default
    try:
        return float(cleaned)
    except ValueError:
        return default


def _as_int(value: str | None, default: int) -> int:
    """Parse an int, falling back to ``default`` on missing or malformed input."""
    cleaned = _clean(value)
    if cleaned is None:
        return default
    try:
        return int(cleaned)
    except ValueError:
        return default
