"""Durable RUN-WIDE external steering budget (cross-resume, TDD).

The in-process ``SteeringLimits`` resets when the process dies, so a resumed
run comes back with its run-wide steering budget refilled — a ceiling the
operator set per run would silently become a ceiling per STRETCH. These tests
pin the durable half: three methods on ``SessionDB`` backed by a dedicated
table, transactional so two connections never both win the last slot.
"""

import threading

import pytest

from lohra.state import SessionDB


@pytest.fixture()
def db(tmp_path):
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


class TestReserveReleaseUsedContract:
    def test_three_accept_then_the_fourth_is_refused(self, db):
        for used in (1, 2, 3):
            accepted, used_now = db.steering_reserve("r1", limit=3)
            assert accepted is True
            assert used_now == used
        accepted, used_now = db.steering_reserve("r1", limit=3)
        assert accepted is False
        # A refusal must not consume anything.
        assert used_now == 3
        assert db.steering_used("r1") == 3

    def test_release_restores_and_used_reads_before_first_reserve(self, db):
        assert db.steering_used("r1") == 0  # unknown run, not an error
        assert db.steering_reserve("r1", limit=1)[0] is True
        assert db.steering_reserve("r1", limit=1)[0] is False
        assert db.steering_release("r1") is True
        assert db.steering_used("r1") == 0
        # The freed slot is reservable again.
        assert db.steering_reserve("r1", limit=1)[0] is True

    def test_release_without_open_slot_returns_false(self, db):
        assert db.steering_release("never-seen") is False
        assert db.steering_reserve("r2", limit=5)[0] is True
        assert db.steering_release("r2") is True
        assert db.steering_release("r2") is False  # already at zero

    def test_independent_runs_have_independent_budgets(self, db):
        assert db.steering_reserve("a", limit=1)[0] is True
        assert db.steering_reserve("b", limit=1)[0] is True
        assert db.steering_used("a") == 1
        assert db.steering_used("b") == 1

    def test_limit_zero_refuses_everything(self, db):
        accepted, used = db.steering_reserve("r1", limit=0)
        assert accepted is False
        assert used == 0


class TestDurabilityAcrossReopen:
    def test_counters_survive_a_reopen(self, tmp_path):
        path = str(tmp_path / "state.db")
        first = SessionDB(path)
        try:
            for _ in range(3):
                assert first.steering_reserve("r1", limit=3)[0] is True
        finally:
            first.close()

        second = SessionDB(path)
        try:
            assert second.steering_used("r1") == 3
            assert second.steering_reserve("r1", limit=3)[0] is False
            assert second.steering_release("r1") is True
            assert second.steering_reserve("r1", limit=3)[0] is True
        finally:
            second.close()


class TestConcurrentContention:
    def test_threads_disputing_limit_one_have_exactly_one_winner(self, db):
        barrier = threading.Barrier(8, timeout=10)
        winners = []
        lock = threading.Lock()

        def contend():
            barrier.wait()
            accepted, _used = db.steering_reserve("r1", limit=1)
            with lock:
                winners.append(accepted)

        threads = [threading.Thread(target=contend) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        assert winners.count(True) == 1
        assert winners.count(False) == 7
        assert db.steering_used("r1") == 1

    def test_two_instances_over_one_file_have_one_winner(self, tmp_path):
        path = str(tmp_path / "state.db")
        first = SessionDB(path)
        second = SessionDB(path)
        try:
            a = first.steering_reserve("r1", limit=1)
            b = second.steering_reserve("r1", limit=1)
            assert sorted([a[0], b[0]], reverse=True) == [True, False]
            assert first.steering_used("r1") == second.steering_used("r1")
            # Whichever instance holds the slot can release it and the other
            # instance observes the restored budget immediately.
            holder, observer = (first, second) if a[0] else (second, first)
            assert holder.steering_release("r1") is True
            assert observer.steering_used("r1") == 0
            assert observer.steering_reserve("r1", limit=1)[0] is True
        finally:
            first.close()
            second.close()
