"""Approval ownership crosses workers, but never another dispatch or handler args."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Event, get_ident

import pytest

from lohra.agent.equip import build_session_dispatch, build_session_stores
from lohra.agent.loop import _execute_tool_calls
from lohra.agent.types import ToolCall
from lohra.tools import ApprovalManager, approval, bind_approval_dispatch, require_approval
from lohra.tools.registry import ToolRegistry, registry
from tests.approval_lab import lab as lab
from tests.approval_lab import COMMAND, OTHER


def manager(*, yolo=False, callback=None):
    result = ApprovalManager()
    result.set_yolo(yolo)
    result.set_callback(callback)
    return result


def invoke(dispatch, command=COMMAND, **extra):
    return "error" not in json.loads(dispatch("terminal", {"command": command, **extra}))


def test_two_real_batches_keep_worker_authority_and_emitted_result_order(lab):
    barrier = Barrier(4)

    def base(name, args):
        barrier.wait(5)
        return registry.dispatch(name, args)

    allowed = bind_approval_dispatch(base, manager=manager(yolo=True))
    denied = bind_approval_dispatch(base)
    calls = tuple(ToolCall(str(i), "terminal", json.dumps({"command": COMMAND})) for i in range(2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_execute_tool_calls, calls, allowed)
        second = pool.submit(_execute_tool_calls, calls, denied)
        first_result, second_result = first.result(5), second.result(5)
    assert ["error" not in json.loads(r["content"]) for r in first_result] == [True, True]
    assert ["error" in json.loads(r["content"]) for r in second_result] == [True, True]
    assert [r["tool_call_id"] for r in first_result] == ["0", "1"]
    assert lab.commands == [COMMAND, COMMAND]
    assert not require_approval(COMMAND)


def test_default_builder_never_inherits_outer_binding_or_legacy_singleton(lab):
    approval.set_yolo(True)

    def outer(name, args):
        stores = build_session_stores(lab.root)
        inner = build_session_dispatch(*stores, session_id="same-id")
        assert not invoke(inner)
        return registry.dispatch(name, args)

    assert invoke(bind_approval_dispatch(outer, manager=manager(yolo=True)))
    assert not invoke(registry.dispatch)
    assert not invoke(bind_approval_dispatch(registry.dispatch))
    assert invoke(
        bind_approval_dispatch(registry.dispatch, manager=approval)
    )  # explicit legacy opt-in
    assert lab.commands == [COMMAND, COMMAND]


def test_session_cache_and_reset_belong_to_explicit_manager(lab):
    prompts = []

    def callback(command, *_args, **_kwargs):
        prompts.append(command)
        return "session" if command == COMMAND else "deny"

    first, second = manager(callback=callback), manager()
    stores = build_session_stores(lab.root)
    granted = build_session_dispatch(*stores, approval_manager=first)
    denied = build_session_dispatch(*stores, approval_manager=second)
    assert invoke(granted) and invoke(granted)
    assert not invoke(denied)
    second.reset()
    assert invoke(granted) and not invoke(granted, OTHER)
    first.reset()
    assert invoke(granted)
    assert prompts == [COMMAND, OTHER, COMMAND]


@pytest.mark.parametrize("choice", ["once", "session", "always", "deny", None, True, "SESSION"])
def test_callback_choices_keep_existing_contract_and_exact_command(lab, choice):
    prompts = []

    def callback(command, *_args, **_kwargs):
        prompts.append(command)
        return choice

    dispatch = bind_approval_dispatch(registry.dispatch, manager=manager(callback=callback))
    expected = choice in ("once", "session", "always")
    assert invoke(dispatch) == expected
    assert invoke(dispatch) == expected
    assert prompts == [COMMAND] * (1 if choice in ("session", "always") else 2)


def test_callback_can_fail_without_authorizing_or_poisoning_worker(lab):
    def broken(*_args, **_kwargs):
        raise ValueError("synthetic callback failure")

    dispatch = bind_approval_dispatch(registry.dispatch, manager=manager(callback=broken))
    assert not invoke(dispatch) and lab.commands == []
    assert not require_approval(COMMAND)


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_nested_dispatch_restores_outer_authority_after_baseexception(lab, exception):
    def fail(*_):
        raise exception("synthetic child interruption")

    child = bind_approval_dispatch(fail)
    denied = bind_approval_dispatch(registry.dispatch)

    def parent(name, args):
        assert not invoke(denied)
        with pytest.raises(exception):
            child(name, args)
        return registry.dispatch(name, args)

    assert invoke(bind_approval_dispatch(parent, manager=manager(yolo=True)))
    assert lab.commands == [COMMAND] and not require_approval(COMMAND)


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_reused_worker_has_no_authority_after_dispatch_failure(lab, exception):
    def fail(*_):
        raise exception("synthetic failure")

    def run(dispatch):
        tid = get_ident()
        try:
            granted = invoke(dispatch)
        except BaseException as exc:
            granted = type(exc).__name__
        return tid, granted, require_approval(COMMAND)

    with ThreadPoolExecutor(max_workers=1) as pool:
        rows = [
            pool.submit(run, bind_approval_dispatch(base, manager=manager(yolo=True))).result(5)
            for base in (registry.dispatch, fail)
        ]
        rows.append(pool.submit(run, bind_approval_dispatch(registry.dispatch)).result(5))
    assert len({row[0] for row in rows}) == 1
    assert [row[1:] for row in rows] == [(True, False), (exception.__name__, False), (False, False)]


def test_queued_cancellation_never_installs_authority(lab):
    entered, release = Event(), Event()

    def blocker():
        entered.set()
        assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(blocker)
        try:
            assert entered.wait(5)
            queued = pool.submit(
                invoke, bind_approval_dispatch(registry.dispatch, manager=manager(yolo=True))
            )
            assert queued.cancel()
        finally:
            release.set()
        blocked.result(5)
        assert pool.submit(require_approval, COMMAND).result(5) is False
    assert lab.commands == []


def test_callback_runs_outside_manager_coordination_lock(lab):
    entered, release = Event(), Event()

    def callback(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return "once"

    owned = manager(callback=callback)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(invoke, bind_approval_dispatch(registry.dispatch, manager=owned))
        try:
            assert entered.wait(5)
            pool.submit(owned.reset).result(2)  # blocks if require holds the manager's lock
        finally:
            release.set()
        assert running.result(5)


def test_binding_never_changes_args_or_handler_signature(lab):
    local = ToolRegistry()
    args = {"command": COMMAND, "approval": True, "approval_manager": "forged", "manager": "yolo"}
    before = dict(args)

    def handler(received):  # valid third-party handler: no kwargs parameter
        assert received is args and received == before
        return json.dumps({"approved": require_approval(received["command"])})

    local.register("terminal", "synthetic", {}, handler)
    denied = bind_approval_dispatch(local.dispatch)
    granted = bind_approval_dispatch(local.dispatch, manager=manager(yolo=True))
    assert json.loads(denied("terminal", args)) == {"approved": False}
    assert json.loads(granted("terminal", args)) == {"approved": True}
    assert "error" in json.loads(
        registry.dispatch("terminal", args, approval_manager=manager(yolo=True))
    )
    assert args == before and lab.commands == []
