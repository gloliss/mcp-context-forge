# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/types.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Shared contracts for the database runtime (OB-02).

Everything the adapter registry, connection pool, and runtime client agree on
lives here: the normalized adapter key, the connection-pool identity, the pool
configuration, and the unified result contract.
"""

# Standard
from dataclasses import dataclass, field
import hashlib
from typing import Any, Optional

# Engine identity.  OceanBase is a single ``oceanbase`` engine whose tenant
# wire mode is captured in ``compatibility_mode`` (``mysql`` or ``oracle``).
ENGINE_OCEANBASE = "oceanbase"
ENGINE_ORACLE = "oracle"
ENGINE_MYSQL = "mysql"
ENGINE_POSTGRESQL = "postgresql"

COMPAT_MODE_MYSQL = "mysql"
COMPAT_MODE_ORACLE = "oracle"

# The registry must grow to new engines without touching upper-layer tool
# contracts; this tuple only documents the built-in keys.
SUPPORTED_ADAPTER_KEYS = (
    (ENGINE_OCEANBASE, COMPAT_MODE_MYSQL),
    (ENGINE_OCEANBASE, COMPAT_MODE_ORACLE),
    (ENGINE_ORACLE, None),
    (ENGINE_MYSQL, None),
    (ENGINE_POSTGRESQL, None),
)


@dataclass(frozen=True)
class AdapterKey:
    """Normalized registry key: ``engine`` plus optional ``compatibility_mode``."""

    engine: str
    compatibility_mode: Optional[str] = None

    def __str__(self) -> str:
        """Render the key as ``engine/mode`` for diagnostics."""
        mode = self.compatibility_mode or "native"
        return f"{self.engine}/{mode}"


def _digest_secret(secret: Optional[str]) -> Optional[str]:
    """Return a non-reversible digest of a credential, or ``None`` when absent.

    The digest participates in pool identity so a password change invalidates
    the pool, without ever retaining the plaintext credential in memory.
    """
    if not secret:
        return None
    return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PoolIdentity:
    """Invariant connection identity for one database source.

    Any change to one of these fields must invalidate the source's pool: host,
    port, tenant, database, schema, username, password, compatibility_mode.
    ``engine`` is folded in as well because a source that switches engine must
    never reuse a pool built by the previous engine's driver.
    """

    engine: str
    host: str
    port: int
    tenant: Optional[str]
    database: Optional[str]
    schema: Optional[str]
    username: Optional[str]
    password_digest: Optional[str]
    compatibility_mode: Optional[str]

    @classmethod
    def from_source(cls, source: Any) -> "PoolIdentity":
        """Build an identity from a ``DatabaseSource`` (or compatible object)."""
        return cls(
            engine=str(getattr(source, "engine", "") or ""),
            host=str(getattr(source, "host", "") or ""),
            port=int(getattr(source, "port", 0) or 0),
            tenant=_opt(source, "tenant_name"),
            database=_opt(source, "database_name"),
            schema=_opt(source, "schema_name"),
            username=_opt(source, "username"),
            password_digest=_digest_secret(getattr(source, "password", None)),
            compatibility_mode=_opt(source, "compatibility_mode"),
        )

    def __repr__(self) -> str:
        """Render a redacted identity that never exposes the credential."""
        return (
            f"PoolIdentity(engine={self.engine!r}, host={self.host!r}, port={self.port}, "
            f"tenant={self.tenant!r}, database={self.database!r}, schema={self.schema!r}, "
            f"username={self.username!r}, compatibility_mode={self.compatibility_mode!r}, "
            "password=***)"
        )


def _opt(source: Any, name: str) -> Optional[str]:
    """Return a string attribute or ``None``, tolerating absent attributes."""
    value = getattr(source, name, None)
    return value if value is None or value == "" else str(value)


#: Default pool tuning, as specified by OB-02.
POOL_CONFIG_FIELDS = ("pool_size", "max_overflow", "acquire_timeout_seconds", "recycle_seconds", "pre_ping")


@dataclass
class PoolConfig:
    """Per-source connection-pool tuning with OB-02 defaults."""

    pool_size: int = 5
    max_overflow: int = 5
    acquire_timeout_seconds: float = 5.0
    recycle_seconds: int = 300
    pre_ping: bool = True

    @classmethod
    def from_source(cls, source: Any) -> "PoolConfig":
        """Merge the source's ``pool_config`` JSON over the defaults."""
        raw = getattr(source, "pool_config", None) or {}
        overrides = {key: raw[key] for key in POOL_CONFIG_FIELDS if key in raw and raw[key] is not None}
        return cls(**overrides)


@dataclass
class QueryResult:
    """Unified result contract returned by every adapter operation.

    The serialized shape is stable and is the boundary the tool layer relies
    on: ``{"columns": [], "rows": [], "row_count": 0, "truncated": false,
    "elapsed_ms": 0, "warnings": []}``.
    """

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the stable result-contract mapping."""
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
            "warnings": self.warnings,
        }


#: Metadata object kinds returned by the adapter metadata API.
METADATA_TYPE_SCHEMA = "schema"
METADATA_TYPE_TABLE = "table"
METADATA_TYPE_VIEW = "view"
METADATA_TYPE_COLUMN = "column"
METADATA_TYPE_INDEX = "index"
METADATA_TYPE_PROCEDURE = "procedure"
METADATA_TYPE_FUNCTION = "function"


@dataclass
class MetadataObject:
    """A normalized metadata node shared across every engine mode.

    The serialized shape is the boundary the upper tool layer relies on so it
    never branches on ``INFORMATION_SCHEMA`` vs ``ALL_TABLES``:
    ``{"schema": ..., "name": ..., "type": ..., "description": null,
    "columns": []}``.
    """

    name: str
    type: str
    schema: Optional[str] = None
    description: Optional[str] = None
    columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the stable metadata-contract mapping."""
        return {
            "schema": self.schema,
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "columns": self.columns,
        }
