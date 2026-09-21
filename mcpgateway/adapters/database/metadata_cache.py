# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/metadata_cache.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Database metadata cache (OB-07).

A TTL-bounded, thread-safe cache for database metadata (schema, table, view,
column, index).  Each entry is keyed by the source id plus a connection
fingerprint, so a source configuration change produces a different key and
automatically misses; :meth:`invalidate` additionally drops a source's entries
eagerly on update/delete.
"""

# Standard
import threading
import time
from typing import Any, Optional

#: Default cache time-to-live, in seconds (OB-07).
DEFAULT_TTL_SECONDS = 300.0


class DatabaseMetadataCache:
    """TTL cache for per-source metadata keyed by (identity, kind, name, limit)."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS, *, clock: Any = None):
        """Initialize the cache.

        Args:
            ttl_seconds: Entry lifetime in seconds.
            clock: Optional monotonic clock (test injection); defaults to
                :func:`time.monotonic`.
        """
        self._ttl = float(ttl_seconds)
        self._clock = clock if clock is not None else time.monotonic
        self._entries: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> float:
        """Return the configured TTL."""
        return self._ttl

    @staticmethod
    def source_id_of(source: Any) -> str:
        """Return a stable string id for a persisted or unpersisted source."""
        source_id = getattr(source, "id", None)
        return str(source_id) if source_id is not None else f"<unpersisted:{id(source)}>"

    @staticmethod
    def fingerprint_of(source: Any) -> str:
        """Return a credential-free config fingerprint for ``source``.

        The fingerprint captures every field that changes the metadata a
        source exposes (engine, addressing, tenant/database/schema, username,
        password digest, compatibility mode).  A password change alters the
        digest; the plaintext credential never leaves ``PoolIdentity``.
        """
        # First-Party (lazy to avoid an import cycle at module load).
        from mcpgateway.adapters.database.types import PoolIdentity  # pylint: disable=import-outside-toplevel

        identity = PoolIdentity.from_source(source)
        return (
            f"{identity.engine}|{identity.host}|{identity.port}|{identity.tenant}|"
            f"{identity.database}|{identity.schema}|{identity.username}|"
            f"{identity.password_digest}|{identity.compatibility_mode}"
        )

    def key_for(self, source: Any, kind: Optional[str] = None, name: Optional[str] = None, limit: Optional[int] = None) -> str:
        """Build a cache key for a metadata lookup against ``source``.

        Args:
            source: The database source.
            kind: Metadata kind (schema/table/view/column/index).
            name: Optional object-name filter.
            limit: Optional result limit.

        Returns:
            str: A stable key that changes when the source config changes.
        """
        return f"{self.source_id_of(source)}|{self.fingerprint_of(source)}|{kind}|{name}|{limit}"

    def get(self, key: str) -> Optional[Any]:
        """Return a live cached value for ``key``, or ``None`` on miss/expiry."""
        with self._lock:
            item = self._entries.get(key)
            if item is None:
                return None
            expires_at, value = item
            if self._clock() >= expires_at:
                self._entries.pop(key, None)
                return None
            return value

    def put(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key`` with the configured TTL."""
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl, value)

    def invalidate(self, source_id: str) -> int:
        """Drop every entry for ``source_id`` and return the number removed."""
        prefix = f"{str(source_id)}|"
        with self._lock:
            stale = [key for key in self._entries if key.startswith(prefix)]
            for key in stale:
                self._entries.pop(key, None)
        return len(stale)

    def clear(self) -> int:
        """Drop every entry and return the number removed."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
        return count

    def __len__(self) -> int:
        """Return the number of entries currently held."""
        with self._lock:
            return len(self._entries)
