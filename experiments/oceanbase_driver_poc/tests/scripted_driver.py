"""Scripted driver fakes for driving the real check methods without a server.

These do not pretend to be a database. They fake the driver module at the Python API
level -- ``connect``, ``cursor``, ``execute``, ``fetchone`` -- which is enough to run
the real ``_check_*`` methods and catch the failure mode that matters most here: a
check that reports PASS while having established less than it claims.

That failure mode is not hypothetical. Three separate instances have been found this
way, all invisible without executing the code:

* the MySQL bind check read a missing row as "the driver returned NULL", so the NULL
  case passed on a query that returned nothing;
* the Oracle bind check zipped expected and observed values without a length guard, so
  a short projection stopped the comparison early;
* the query-timeout check treated *any* fast client failure as "the client timed out",
  and an absent probe marker then read as "the server stopped" -- a pass for a probe
  that never ran.

None of these is reachable through the L1 tests that only exercise pure functions.
"""

from __future__ import annotations


class ScriptedCursor:
    """A cursor that answers from a callable instead of a server."""

    def __init__(self, resolver) -> None:
        """Record the resolver that decides each statement's result."""
        self._resolver = resolver
        self._row = None
        self.executed: list[tuple[str, object]] = []

    def __enter__(self) -> ScriptedCursor:
        """Enter the context manager."""
        return self

    def __exit__(self, *exc: object) -> bool:
        """Leave the context manager without suppressing anything."""
        return False

    def execute(self, sql: str, params: object = None) -> None:
        """Record the statement and take its scripted result."""
        self.executed.append((sql, params))
        self._row = self._resolver(sql, params)

    def fetchone(self):
        """Return the scripted row."""
        return self._row

    def fetchall(self):
        """Return the scripted row as a one-row result set."""
        return [] if self._row is None else [self._row]

    def close(self) -> None:
        """Close the cursor."""


class ScriptedConnection:
    """A connection handing out scripted cursors."""

    def __init__(self, resolver) -> None:
        """Record the resolver passed to every cursor."""
        self._resolver = resolver
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        """Return a new scripted cursor."""
        return ScriptedCursor(self._resolver)

    def close(self) -> None:
        """Mark the connection closed."""
        self.closed = True

    def ping(self, reconnect: bool = False) -> None:
        """Report the connection as alive."""


class ScriptedModule:
    """A stand-in for a database driver module."""

    def __init__(self, resolver) -> None:
        """Record the resolver used for every connection."""
        self._resolver = resolver
        self.opened = 0

    def connect(self, **kwargs: object) -> ScriptedConnection:
        """Return a scripted connection."""
        self.opened += 1
        return ScriptedConnection(self._resolver)


class RaisingResolver:
    """A resolver that raises for matching statements and defers otherwise.

    Used to make one statement fail the way a server would, without needing a server.
    """

    def __init__(self, *, match: str, error: BaseException, otherwise=None) -> None:
        """Record what to raise and when.

        Args:
            match: A substring identifying the statement that should fail.
            error: The exception to raise for it.
            otherwise: Resolver used for every other statement; ``None`` means no row.
        """
        self._match = match
        self._error = error
        self._otherwise = otherwise

    def __call__(self, sql: str, params: object = None):
        """Raise for the matching statement, defer otherwise."""
        if self._match in sql:
            raise self._error
        return self._otherwise(sql, params) if self._otherwise else None
