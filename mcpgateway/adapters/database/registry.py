# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/registry.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Engine -> adapter registry (OB-02).

Adapters are resolved by ``(engine, compatibility_mode)`` so business code
never branches on ``engine == ...``.  New engines register here; upper-layer
tool contracts stay untouched.
"""

# Standard
import threading
from typing import Any, Callable, Optional, Type

# First-Party
from mcpgateway.adapters.database.base import DatabaseAdapter
from mcpgateway.adapters.database.exceptions import UnknownCompatibilityModeError, UnknownEngineError
from mcpgateway.adapters.database.types import AdapterKey


class AdapterRegistry:
    """Resolve adapter classes by normalized ``(engine, compatibility_mode)``."""

    def __init__(self):
        """Initialize an empty registry."""
        self._adapters: dict[AdapterKey, Type[DatabaseAdapter]] = {}
        self._lock = threading.Lock()

    def register(self, engine: str, compatibility_mode: Optional[str], adapter_cls: Type[DatabaseAdapter]) -> None:
        """Register an adapter class under ``(engine, compatibility_mode)``.

        Args:
            engine: Engine name (e.g. ``oceanbase``, ``mysql``).
            compatibility_mode: Optional mode for multi-mode engines.
            adapter_cls: A :class:`DatabaseAdapter` subclass.
        """
        key = AdapterKey(engine, compatibility_mode)
        with self._lock:
            self._adapters[key] = adapter_cls

    def lookup(self, engine: Optional[str], compatibility_mode: Optional[str] = None) -> Type[DatabaseAdapter]:
        """Return the adapter class for a key, distinguishing error causes.

        Raises:
            UnknownEngineError: If no adapter exists for the engine at all.
            UnknownCompatibilityModeError: If the engine exists but the mode
                is not registered.
        """
        key = AdapterKey(engine or "", compatibility_mode)
        with self._lock:
            adapter_cls = self._adapters.get(key)
            engine_known = adapter_cls is not None or any(k.engine == key.engine for k in self._adapters)
        if adapter_cls is not None:
            return adapter_cls
        if not engine_known:
            raise UnknownEngineError(f"Unknown database engine: {engine!r}")
        raise UnknownCompatibilityModeError(f"Unknown compatibility mode {compatibility_mode!r} for engine {engine!r}")

    def create_adapter(self, source: Any, pool: Any = None) -> DatabaseAdapter:
        """Build an adapter for ``source``, optionally bound to an existing pool.

        Args:
            source: A ``DatabaseSource`` (or compatible object).
            pool: Optional pre-built pool; when omitted the adapter builds its
                own engine (and thus needs its driver installed).

        Returns:
            DatabaseAdapter: A fresh adapter instance for the source.
        """
        adapter_cls = self.lookup(getattr(source, "engine", None), getattr(source, "compatibility_mode", None))
        return adapter_cls(source, pool=pool)

    def registered_keys(self) -> list[AdapterKey]:
        """Return a snapshot of registered keys (for introspection/tests)."""
        with self._lock:
            return list(self._adapters.keys())


def register_adapter(engine: str, compatibility_mode: Optional[str] = None) -> Callable[[Type[DatabaseAdapter]], Type[DatabaseAdapter]]:
    """Class decorator registering an adapter under the default registry.

    Built-in adapters register explicitly via :func:`_register_builtins`;
    third-party or future adapters may use this decorator instead.
    """

    def decorate(adapter_cls: Type[DatabaseAdapter]) -> Type[DatabaseAdapter]:
        default_registry.register(engine, compatibility_mode, adapter_cls)
        return adapter_cls

    return decorate


def _register_builtins(registry: AdapterRegistry) -> None:
    """Register the built-in adapter implementations."""
    # First-Party (imported lazily to avoid a module cycle with registry)
    from mcpgateway.adapters.database.adapters import (  # pylint: disable=import-outside-toplevel
        MySQLAdapter,
        OracleAdapter,
        PostgreSQLAdapter,
    )
    from mcpgateway.adapters.database.oceanbase import (  # pylint: disable=import-outside-toplevel
        OceanBaseAdapter,
    )

    # OceanBase serves both wire modes through the single unified adapter; the
    # mode is selected at instantiation time from the source's compatibility
    # mode rather than by duplicating the adapter class.
    registry.register("oceanbase", "mysql", OceanBaseAdapter)
    registry.register("oceanbase", "oracle", OceanBaseAdapter)
    registry.register("oracle", None, OracleAdapter)
    registry.register("mysql", None, MySQLAdapter)
    registry.register("postgresql", None, PostgreSQLAdapter)


#: Process-local default registry, populated once at import time.
default_registry = AdapterRegistry()


def get_default_registry() -> AdapterRegistry:
    """Return the process-local default registry (already populated)."""
    return default_registry


_register_builtins(default_registry)
