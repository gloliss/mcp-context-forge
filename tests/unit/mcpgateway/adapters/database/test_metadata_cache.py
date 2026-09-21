# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/adapters/database/test_metadata_cache.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for the database metadata cache (OB-07).

Covers the default TTL, put/get round-trip, TTL expiry, config-fingerprint key
changes (password/host/mode), the credential-free fingerprint guarantee, and
invalidate/clear lifecycle.
"""

# First-Party
from mcpgateway.adapters.database import DEFAULT_TTL_SECONDS, DatabaseMetadataCache
from mcpgateway.db import DatabaseSource


def _source(**overrides) -> DatabaseSource:
    """Build an unpersisted source with stable defaults for fingerprinting."""
    data = {
        "id": "src-1",
        "name": "src",
        "slug": "src",
        "engine": "oceanbase",
        "compatibility_mode": "mysql",
        "host": "127.0.0.1",
        "port": 2881,
        "database_name": "app",
        "username": "root",
        "password": "secret",
    }
    data.update(overrides)
    return DatabaseSource(**data)


class _FakeClock:
    """A controllable monotonic clock for TTL-expiry tests."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Advance the clock by ``seconds``."""
        self.now += seconds


def test_default_ttl_is_300_seconds():
    """The OB-07 default TTL is 300 seconds."""
    assert DEFAULT_TTL_SECONDS == 300.0
    assert DatabaseMetadataCache().ttl_seconds == 300.0


def test_put_get_roundtrip():
    """A stored value is returned until it expires."""
    cache = DatabaseMetadataCache()
    source = _source()
    key = cache.key_for(source, kind="table", name="users", limit=100)

    assert cache.get(key) is None
    cache.put(key, {"rows": [["APP", "USERS", "table"]]})
    assert cache.get(key) == {"rows": [["APP", "USERS", "table"]]}
    assert len(cache) == 1


def test_ttl_expiry():
    """An entry is evicted once its TTL elapses."""
    clock = _FakeClock()
    cache = DatabaseMetadataCache(ttl_seconds=5.0, clock=clock)
    key = cache.key_for(_source(), kind="table")
    cache.put(key, "value")

    clock.advance(4.9)
    assert cache.get(key) == "value"

    clock.advance(0.2)  # total 5.1s > 5.0s TTL
    assert cache.get(key) is None
    assert len(cache) == 0


def test_key_changes_on_password_change():
    """A credential change produces a different key (digest, never plaintext)."""
    cache = DatabaseMetadataCache()
    assert cache.key_for(_source(password="first"), kind="table") != cache.key_for(_source(password="second"), kind="table")


def test_key_changes_on_host_change():
    """A host change invalidates the cache key."""
    cache = DatabaseMetadataCache()
    assert cache.key_for(_source(host="10.0.0.1"), kind="table") != cache.key_for(_source(host="10.0.0.2"), kind="table")


def test_key_changes_on_mode_change():
    """A compatibility-mode change invalidates the cache key."""
    cache = DatabaseMetadataCache()
    assert cache.key_for(_source(compatibility_mode="mysql"), kind="table") != cache.key_for(_source(compatibility_mode="oracle"), kind="table")


def test_fingerprint_never_contains_plaintext_password():
    """The fingerprint contains only the digest, never the plaintext credential."""
    fingerprint = DatabaseMetadataCache().fingerprint_of(_source(password="sup3rSecret!"))
    assert "sup3rSecret!" not in fingerprint


def test_invalidate_drops_only_that_source():
    """Invalidation removes one source's entries and leaves others intact."""
    cache = DatabaseMetadataCache()
    a = _source(id="src-a")
    b = _source(id="src-b")

    cache.put(cache.key_for(a, kind="table"), 1)
    cache.put(cache.key_for(a, kind="view"), 2)
    cache.put(cache.key_for(b, kind="table"), 3)

    assert cache.invalidate("src-a") == 2
    assert len(cache) == 1
    assert cache.get(cache.key_for(b, kind="table")) == 3


def test_clear_drops_everything():
    """Clear removes every cached entry."""
    cache = DatabaseMetadataCache()
    cache.put("k1", 1)
    cache.put("k2", 2)

    assert cache.clear() == 2
    assert len(cache) == 0
