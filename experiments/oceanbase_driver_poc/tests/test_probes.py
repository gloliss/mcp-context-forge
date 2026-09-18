"""L1: the mode-agnostic primitives the L2 checks are built on.

The pool is here rather than behind a database because it is the piece most likely
to be subtly wrong and the least likely to be caught by a single happy-path run: an
acquisition path that waits when it should create, or reuses a connection it should
have discarded, still works fine until the pool is actually contended.
"""

from __future__ import annotations

import threading
import time

import pytest
from common.probes import (
    BIND_CASES,
    BoundedPool,
    Deadline,
    IdentifierNotAuthorized,
    PoolExhausted,
    resolve_identifier,
)


class TestDeadline:
    """Deadline arithmetic backs the claim that a timeout was honoured."""

    def test_elapsed_grows_and_slack_is_applied(self) -> None:
        """The allowance is the configured budget plus slack, not the budget."""
        deadline = Deadline.start(0.05)
        assert deadline.allowed_s == pytest.approx(0.05 * 2.5)
        assert deadline.overran is False
        time.sleep(0.2)
        assert deadline.overran is True

    def test_describe_reports_both_measurements(self) -> None:
        """A failure summary has to show the configured value and the observed one."""
        described = Deadline.start(2.0).describe()
        assert "2s" in described
        assert "上限" in described

    def test_a_prompt_failure_has_not_exercised_the_deadline(self) -> None:
        """Returning immediately means the deadline was never tested.

        A closed port refuses at once; treating that as evidence the connect timeout
        works would let the check pass without measuring anything.
        """
        assert Deadline.start(5.0).exercised() is False

    def test_waiting_out_most_of_the_budget_counts_as_exercised(self) -> None:
        """A target that stalled did test the deadline."""
        deadline = Deadline.start(0.05)
        time.sleep(0.1)
        assert deadline.exercised() is True


class TestBoundPool:
    """A bounded pool over a trivial factory: no database, just the semantics."""

    @staticmethod
    def _pool(*, max_size: int = 2, timeout: float = 0.2, healthy=None) -> tuple[BoundedPool, list]:
        made: list[dict] = []

        def factory():
            conn = {"id": len(made), "open": True}
            made.append(conn)
            return conn

        pool = BoundedPool(factory, max_size=max_size, acquire_timeout_s=timeout, is_healthy=healthy, closer=lambda c: c.update(open=False))
        return pool, made

    def test_rejects_a_non_positive_ceiling(self) -> None:
        """A pool that can hold nothing would deadlock every caller."""
        with pytest.raises(ValueError):
            BoundedPool(object, max_size=0, acquire_timeout_s=1.0)

    def test_exhaustion_is_bounded_and_identifiable(self) -> None:
        """The ceiling is enforced, and hitting it fails rather than hanging."""
        pool, made = self._pool(max_size=2, timeout=0.1)
        first = pool.acquire()
        second = pool.acquire()
        start = time.perf_counter()
        with pytest.raises(PoolExhausted):
            pool.acquire()
        assert time.perf_counter() - start < 1.0
        assert len(made) == 2
        pool.release(first)
        pool.release(second)

    def test_a_released_connection_is_reused(self) -> None:
        """Returning a connection must make it available again, not replace it."""
        pool, made = self._pool(max_size=1)
        first = pool.acquire()
        pool.release(first)
        assert pool.acquire() is first
        assert len(made) == 1

    def test_an_unhealthy_connection_is_replaced_not_handed_out(self) -> None:
        """A dead connection must never reach the next caller."""
        alive = {"ok": True}
        pool, made = self._pool(max_size=1, healthy=lambda conn: alive["ok"])
        first = pool.acquire()
        pool.release(first)
        assert first["open"] is True

        alive["ok"] = False  # the connection died while it was idle
        second = pool.acquire()
        assert second is not first
        assert len(made) == 2

    def test_discarding_stale_connections_does_not_end_the_attempt(self) -> None:
        """Two unusable idle connections must still yield a fresh one.

        This is the case a naive implementation gets wrong: after discarding a stale
        connection it waits for a release instead of trying the next one, and reports
        exhaustion while the pool is in fact empty and able to build a connection.
        """
        alive = {"ok": True}
        pool, made = self._pool(max_size=2, timeout=0.5, healthy=lambda conn: alive["ok"])
        borrowed = [pool.acquire(), pool.acquire()]
        for conn in borrowed:
            pool.release(conn)
        assert pool.idle == 2

        alive["ok"] = False  # both pooled connections are now stale
        conn = pool.acquire()
        assert conn["open"] is True
        assert conn not in borrowed
        assert len(made) == 3

    def test_exhaustion_does_not_leak_the_live_count(self) -> None:
        """A failed acquisition must not permanently consume a slot."""
        pool, _ = self._pool(max_size=1, timeout=0.05)
        pool.acquire()
        with pytest.raises(PoolExhausted):
            pool.acquire()
        pool.close_all()
        assert pool.live == 0

    def test_close_all_drops_everything(self) -> None:
        """Closing the pool must leave no idle connections behind."""
        pool, _ = self._pool(max_size=3)
        conns = [pool.acquire() for _ in range(3)]
        for conn in conns:
            pool.release(conn)
        assert pool.idle == 3
        pool.close_all()
        assert pool.idle == 0
        assert pool.live == 0

    def test_a_concurrent_caller_gets_a_slot_when_one_is_released(self) -> None:
        """Release must wake a waiter rather than making it time out."""
        pool, _ = self._pool(max_size=1, timeout=2.0)
        held = pool.acquire()
        result: list[object] = []

        def waiter() -> None:
            result.append(pool.acquire())

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        pool.release(held)
        thread.join(timeout=3.0)
        assert result and result[0] is held


class TestResolveIdentifier:
    """Interpolating an object name is only safe against an authorised list."""

    def test_quotes_a_name_that_was_enumerated(self) -> None:
        """A name the server just listed is usable."""
        assert resolve_identifier("sample_table", allowed={"sample_table"}) == "`sample_table`"

    def test_honours_the_dialect_quote(self) -> None:
        """Oracle's identifier quote is a double quote, not a backtick."""
        assert resolve_identifier("T1", allowed={"T1"}, quote='"') == '"T1"'

    def test_rejects_a_name_outside_the_authorised_set(self) -> None:
        """Provenance is the authorisation: a name from nowhere is not usable."""
        with pytest.raises(IdentifierNotAuthorized):
            resolve_identifier("other_table", allowed={"sample_table"})

    def test_rejects_characters_that_could_carry_sql(self) -> None:
        """Even an allowed-looking name must not contain syntax."""
        with pytest.raises(IdentifierNotAuthorized):
            resolve_identifier("t`; DROP TABLE x; --", allowed={"t`; DROP TABLE x; --"})

    def test_rejects_an_embedded_quote(self) -> None:
        """A quote inside the name would terminate the quoting."""
        with pytest.raises(IdentifierNotAuthorized):
            resolve_identifier('a"b', allowed={'a"b'}, quote='"')


class TestBindCases:
    """The bind cases are the specification for what binding must survive."""

    def test_covers_the_metacharacters_that_matter(self) -> None:
        """Each value here exists because string interpolation would break on it."""
        labels = {label for label, _ in BIND_CASES}
        assert {"single-quote", "semicolon", "comment-marker", "unicode"} <= labels

    def test_labels_are_unique(self) -> None:
        """Duplicated labels would hide a case in the failure report."""
        labels = [label for label, _ in BIND_CASES]
        assert len(labels) == len(set(labels))

    def test_includes_null_and_a_numeric(self) -> None:
        """Both are common sources of "the driver coerced my value" surprises."""
        values = [value for _, value in BIND_CASES]
        assert None in values
        assert any(isinstance(value, int) for value in values)
