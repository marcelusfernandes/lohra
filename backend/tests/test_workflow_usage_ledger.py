"""Per-execution financial union and debit/marker failure boundaries (#112)."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from lohra.agent.types import Usage
from lohra.workflow.budget import Budget


U, V = Usage(11, 13, 17, 19, 7), Usage(3, 5, 7, 11, 2)
UV = Usage(14, 18, 24, 30, 9)


def ledger(budget):
    from lohra.workflow.usage_ledger import AcquisitionUsageLedger
    return AcquisitionUsageLedger(budget)


@pytest.mark.parametrize("applied_first", [False, True])
def test_received_applied_union_keeps_five_axes_and_one_measurement(applied_first):
    budget = Budget(token_budget=30, tokens_in=2, tokens_out=3, charges=1)
    book = ledger(budget)
    if applied_first:
        assert not book.apply("same", U)
    book.receive("same", U)
    book.receive("same", UV)
    book.receive("same", U)
    if not applied_first:
        assert not book.apply("same", U)
    # This distinct UUID has disjoint contributions despite identical node use.
    book.receive("sibling", V)
    result = book.finalize()
    assert result.usage == Usage(17, 23, 31, 41, 11)
    assert (result.tokens_in, result.tokens_out, result.charges) == (19, 26, 3)
    assert result.overrun == 15
    assert budget.est_leaf_cost == 15
    assert book.finalize() is result
    assert not book.apply("same", U)
    assert budget.tokens_spent == 45


def test_keyed_charge_preserves_unkeyed_consumers_and_does_not_measure_report_axes():
    budget = Budget(token_budget=8)
    book = ledger(budget)
    book.receive("report", Usage(cache_read_tokens=7, reasoning_tokens=23))
    assert not book.apply("report", Usage(cache_read_tokens=7, reasoning_tokens=23))
    assert not budget.has_measurement
    assert not budget.charge_tokens(2, 3)
    assert book.apply("leaf", U)
    book.receive("leaf", U)
    final = book.finalize()
    assert (final.tokens_in, final.tokens_out, final.charges) == (13, 16, 2)
    assert final.usage == Usage(11, 13, 24, 19, 30)


@pytest.mark.parametrize("after_commit", [False, True])
def test_retry_cannot_double_debit_if_keyed_application_raises(monkeypatch, after_commit):
    budget = Budget()
    book = ledger(budget)
    book.receive("leaf", U)
    original = budget.apply_execution_usage
    fired = False

    def fail_once(sub_id, usage):
        nonlocal fired
        if not fired:
            fired = True
            if after_commit:
                original(sub_id, usage)
            raise RuntimeError("application boundary")
        return original(sub_id, usage)

    monkeypatch.setattr(budget, "apply_execution_usage", fail_once)
    with pytest.raises(RuntimeError, match="application boundary"):
        book.apply("leaf", U)
    assert budget.tokens_spent == (24 if after_commit else 0)
    final = book.finalize()
    assert (final.tokens_in, final.tokens_out, final.charges) == (11, 13, 1)
    assert final.usage == U


@pytest.mark.parametrize("at_freeze", [False, True])
def test_partial_finalization_is_retryable_and_concurrent_calls_share_snapshot(monkeypatch, at_freeze):
    budget = Budget()
    book = ledger(budget)
    book.receive("one", U)
    book.receive("two", V)
    target, name = (book, "_freeze") if at_freeze else (budget, "apply_execution_usage")
    original = getattr(target, name)
    failed = False

    def fail_once(*args):
        nonlocal failed
        if not failed and (at_freeze or args[0] == "two"):
            failed = True
            raise RuntimeError("finalization boundary")
        return original(*args)

    monkeypatch.setattr(target, name, fail_once)
    with pytest.raises(RuntimeError, match="finalization boundary"):
        book.finalize()
    with ThreadPoolExecutor(max_workers=4) as pool:
        snapshots = list(pool.map(lambda _: book.finalize(), range(8)))
    assert all(s is snapshots[0] for s in snapshots)
    assert snapshots[0].usage == UV
    assert (snapshots[0].tokens_in, snapshots[0].tokens_out, snapshots[0].charges) == (14, 18, 2)


def test_freeze_accepts_late_prefix_but_reports_new_usage_without_reopening(caplog):
    budget = Budget()
    book = ledger(budget)
    book.receive("leaf", U)
    final = book.finalize()
    book.observe("leaf", U)
    book.observe("never-ran", Usage())
    assert not book.errors
    book.observe("leaf", UV)
    assert book.errors and "after financial freeze" in caplog.text
    assert book.finalize() is final
    assert budget.tokens_spent == 24


def test_failed_capture_is_visible_but_successful_apply_remains_authoritative(monkeypatch, caplog):
    budget = Budget()
    book = ledger(budget)
    book.apply("leaf", U)

    def fail(*args):
        raise ValueError("receipt failure")

    monkeypatch.setattr(book, "receive", fail)
    book.observe("leaf", U)
    final = book.finalize()
    assert final.usage == U and final.errors
    assert "receipt failure" in caplog.text


def test_preparation_failure_leaves_both_debit_and_marker_unchanged(monkeypatch):
    from lohra.workflow.usage_book import TokenState
    budget = Budget()
    initial = budget.token_state()
    prepare = TokenState.apply

    def fail_after_preparation(state, sub_id, usage):
        prepared = prepare(state, sub_id, usage)
        assert prepared.applied[sub_id] == U and prepared.tokens_in == 11
        raise MemoryError("prepared but not committed")

    with monkeypatch.context() as patch:
        patch.setattr(TokenState, "apply", fail_after_preparation)
        with pytest.raises(MemoryError, match="prepared but not committed"):
            budget.apply_execution_usage("leaf", U)
    assert budget.token_state() is initial and budget.tokens_spent == 0
    budget.apply_execution_usage("leaf", U)
    assert initial.applied == {}
    with pytest.raises(TypeError):
        budget.token_state().applied["leaf"] = V
    assert budget.tokens_spent == 24 and budget.token_state().charges == 1


def test_receipt_retention_is_not_capped_by_the_core_registry():
    budget = Budget(lifetime=1000)
    book = ledger(budget)
    for index in range(1000):
        book.receive(str(index), U)
    final = book.finalize()
    assert final.usage == Usage(11000, 13000, 17000, 19000, 7000)
    assert final.charges == 1000 and budget.lifetime_remaining == 1000
