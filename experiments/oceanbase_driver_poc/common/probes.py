"""Mode-agnostic building blocks for the L2 checks.

The L2 checks open sockets, which means they cannot run in CI. Everything that can
be decided without a database is kept here instead, so the L1 layer can pin it down:

* the parameter values a bind check must survive, and the fact that binding must
  return them unchanged rather than merely not erroring;
* a bounded pool, because PyMySQL ships no pool of its own and "can this driver be
  pooled" is a question the downstream design actually asks;
* deadline arithmetic, because "the timeout worked" is a claim about elapsed time,
  not about an exception being raised.

Nothing here imports a driver. That is what keeps it testable on a machine with no
OceanBase and no client libraries.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Values a bound parameter must come back as, unchanged. The point is not that the
# query succeeds -- it is that the value is carried by the protocol rather than by
# string interpolation. Anything that has meaning to a SQL parser belongs here.
#
# Loaded from a JSON file rather than declared inline because the TypeScript
# evaluation binds the same set: two hand-maintained lists in two languages drift,
# and a case that exists on only one side would make the two runtimes' results
# silently incomparable.
BIND_CASES: tuple[tuple[str, Any], ...] = tuple(
    (label, value) for label, value in json.loads(Path(__file__).with_name("bind_cases.json").read_text(encoding="utf-8"))
)

# Multiplied into the configured connect timeout to decide how late is too late.
# Generous on purpose: the check is "the deadline was honoured", not "the clock is
# exact", and a tight bound would turn scheduler noise into a failed capability.
DEADLINE_SLACK = 2.5

# A reserved, non-routable address (RFC 5737 TEST-NET-1) used as the default
# unreachable target. Pointing a connect-timeout check at a real host with a closed
# port would measure connection *refusal*, which returns immediately and proves
# nothing about the timeout setting.
DEFAULT_UNREACHABLE_HOST = "192.0.2.1"


class PoolExhausted(RuntimeError):
    """Raised when a bounded pool could not hand out a connection in time.

    Deliberately not a driver error: exhaustion is a property of the pool the
    caller configured, and the check asserts that it surfaces as a bounded,
    identifiable failure rather than as an unbounded wait.
    """


# Object names a driver accepts without quoting. Deliberately conservative: the
# point is to reject anything that could terminate the identifier and start new SQL.
_PLAIN_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


class IdentifierNotAuthorized(ValueError):
    """Raised when an object name is not one the caller is allowed to reference."""


def resolve_identifier(name: str, *, allowed: Collection[str], quote: str = "`") -> str:
    """Quote an object name for interpolation, but only if it is authorised.

    A table name cannot be a bound parameter -- the protocol binds values, not
    identifiers -- so the metadata check that runs ``SHOW COLUMNS`` has to
    interpolate one. Interpolating a name that came from the server is only safe
    when it is first checked against the set of names that server just listed, which
    is what this does: membership in ``allowed`` proves provenance, and the
    character check rejects anything that could carry SQL syntax.

    Args:
        name: The object name to quote.
        allowed: Names already enumerated from an authoritative source.
        quote: The identifier quote character for the dialect.

    Returns:
        The name wrapped in the dialect's identifier quotes.

    Raises:
        IdentifierNotAuthorized: If the name is not in ``allowed``, or contains
            characters that are not valid in an unquoted identifier.
    """
    if name not in allowed:
        raise IdentifierNotAuthorized(f"对象名不在已授权集合内：{name!r}")
    if not _PLAIN_IDENTIFIER_RE.match(name):
        raise IdentifierNotAuthorized(f"对象名含不安全字符：{name!r}")
    return f"{quote}{name}{quote}"


@dataclass
class Deadline:
    """Elapsed-time arithmetic for a check that asserts a deadline was honoured."""

    budget_s: float
    started: float

    @classmethod
    def start(cls, budget_s: float) -> Deadline:
        """Begin measuring against ``budget_s``.

        Args:
            budget_s: The configured deadline, in seconds.

        Returns:
            A running deadline.
        """
        return cls(budget_s=budget_s, started=time.perf_counter())

    @property
    def elapsed_s(self) -> float:
        """Seconds elapsed since :meth:`start`."""
        return time.perf_counter() - self.started

    @property
    def allowed_s(self) -> float:
        """The deadline plus slack, i.e. the latest acceptable finish."""
        return self.budget_s * DEADLINE_SLACK

    @property
    def overran(self) -> bool:
        """Whether the elapsed time exceeded the deadline plus slack."""
        return self.elapsed_s > self.allowed_s

    def exercised(self, fraction: float = 0.5) -> bool:
        """Whether enough of the budget elapsed for the deadline to have been tested.

        A failure that returns promptly did not touch the configured deadline, so it
        cannot be evidence that the deadline is honoured -- a closed port refuses
        immediately and proves nothing about a connect timeout. Callers use this to
        report "undetermined" rather than a pass in that case.

        Args:
            fraction: The share of the budget that must have elapsed.

        Returns:
            ``True`` when the elapsed time reached ``fraction`` of the budget.
        """
        return self.elapsed_s >= self.budget_s * fraction

    def describe(self) -> str:
        """Render the measurement for a check summary."""
        return f"配置 {self.budget_s:g}s，实测 {self.elapsed_s:.2f}s（上限 {self.allowed_s:g}s）"


class BoundedPool:
    """A minimal, thread-safe bounded connection pool.

    This exists because PyMySQL ships no pool: pooling in the MySQL mode is
    something the caller builds, so the POC has to build one to have anything to
    verify. The behaviours the check cares about are the ones a production pool must
    get right -- acquisition is bounded rather than unbounded, a returned connection
    is reused rather than replaced, and a connection that died while idle is
    discarded instead of handed out.
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        max_size: int,
        acquire_timeout_s: float,
        is_healthy: Callable[[Any], bool] | None = None,
        closer: Callable[[Any], None] | None = None,
    ) -> None:
        """Build an empty pool that creates connections on demand.

        Args:
            factory: Opens a new connection.
            max_size: The hard ceiling on live connections.
            acquire_timeout_s: How long an acquisition may wait for a free slot.
            is_healthy: Probes a pooled connection; unhealthy ones are replaced.
            closer: Closes a connection the pool discards.

        Raises:
            ValueError: If ``max_size`` is not positive.
        """
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        self._factory = factory
        self._max_size = max_size
        self._acquire_timeout_s = acquire_timeout_s
        self._is_healthy = is_healthy or (lambda _conn: True)
        self._closer = closer
        self._idle: list[Any] = []
        self._live = 0
        self._lock = threading.Lock()
        self._available = threading.Condition(self._lock)

    @property
    def live(self) -> int:
        """How many connections the pool has handed out or is holding."""
        with self._lock:
            return self._live

    @property
    def idle(self) -> int:
        """How many connections are currently free."""
        with self._lock:
            return len(self._idle)

    def acquire(self) -> Any:
        """Take a connection, waiting at most the configured acquisition timeout.

        Returns:
            A healthy connection, reused when one is idle.

        Raises:
            PoolExhausted: When no connection became available in time.
        """
        deadline = time.perf_counter() + self._acquire_timeout_s
        with self._available:
            while True:
                # Drain idle connections until one is healthy. Discarding an
                # unhealthy one must not end the attempt: the loop continues so the
                # caller either reuses the next idle connection or gets a fresh one.
                while self._idle:
                    conn = self._idle.pop()
                    if self._is_healthy(conn):
                        return conn
                    self._live -= 1
                    self._discard(conn)

                if self._live < self._max_size:
                    self._live += 1
                    break

                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise PoolExhausted(f"池已满（上限 {self._max_size}），等待 {self._acquire_timeout_s:g}s 后放弃")
                self._available.wait(timeout=remaining)

        # Created outside the lock so a slow connect does not block other callers.
        try:
            return self._factory()
        except Exception:
            with self._available:
                self._live -= 1
                self._available.notify()
            raise

    def release(self, conn: Any, *, healthy: bool = True) -> None:
        """Return a connection to the pool, or discard it.

        Args:
            conn: The connection being returned.
            healthy: Whether it is still usable. An unhealthy connection is closed
                rather than pooled, so a dead connection is never handed to the next
                caller.
        """
        with self._available:
            if healthy and self._is_healthy(conn):
                self._idle.append(conn)
            else:
                self._live -= 1
                self._discard(conn)
            self._available.notify()

    def close_all(self) -> None:
        """Discard every idle connection and drop the live count to zero."""
        with self._available:
            while self._idle:
                self._discard(self._idle.pop())
            self._live = 0
            self._available.notify_all()

    def _discard(self, conn: Any) -> None:
        """Close a connection, swallowing a close failure on an already-dead one."""
        if self._closer is None:
            return
        try:
            self._closer(conn)
        except Exception:  # a connection that cannot be closed is already gone
            pass
