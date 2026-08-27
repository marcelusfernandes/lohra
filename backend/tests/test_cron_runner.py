"""Tests for the cron job runner (fresh agent per run, optional persistence)."""


from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.cron.runner import make_cron_runner
from lohra.providers import get_provider_profile
from lohra.state import SessionDB


class FakeClient(ModelClient):
    def __init__(self, text="done"):
        self._text = text

    def create(self, **kwargs):
        return {"content": [{"type": "text", "text": self._text}], "stop_reason": "end_turn", "usage": None}


def _factory(text="done"):
    return lambda: Agent(
        model="m", provider=get_provider_profile("anthropic"), client=FakeClient(text)
    )


def test_runner_runs_the_prompt():
    seen = {}

    class Recording(ModelClient):
        def create(self, **kwargs):
            seen["messages"] = kwargs.get("messages")
            return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": None}

    factory = lambda: Agent(  # noqa: E731
        model="m", provider=get_provider_profile("anthropic"), client=Recording()
    )
    run_job = make_cron_runner(factory)
    result = run_job({"id": "j1", "name": "x", "prompt": "do the thing"})
    assert result["final_response"] == "ok"
    assert seen["messages"][-1]["content"] == "do the thing"


def test_runner_persists_a_cron_session(tmp_path):
    db = SessionDB(":memory:")
    try:
        run_job = make_cron_runner(_factory("hello"), db=db)
        run_job({"id": "j1", "name": "daily", "prompt": "summarize"})
        sessions = db.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["title"] == "cron: daily"
        sid = sessions[0]["id"]
        msgs = db.load_messages(sid)
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[1]["content"] == "hello"
    finally:
        db.close()


def test_runner_logs_and_skips_persist_on_api_error(caplog):
    import logging

    db = SessionDB(":memory:")

    class Boom(ModelClient):
        def create(self, **kwargs):
            raise RuntimeError("api down")

    factory = lambda: Agent(  # noqa: E731
        model="m", provider=get_provider_profile("anthropic"), client=Boom()
    )
    try:
        with caplog.at_level(logging.WARNING):
            run_job = make_cron_runner(factory, db=db)
            result = run_job({"id": "j1", "name": "x", "prompt": "p"})
        assert result["error"]  # in-band error
        assert "run failed" in caplog.text  # surfaced, not silently dropped
        assert db.list_sessions() == []  # not persisted
    finally:
        db.close()


def test_runner_persists_the_model(tmp_path):
    db = SessionDB(":memory:")
    try:
        make_cron_runner(_factory(), db=db)({"id": "j1", "name": "x", "prompt": "p"})
        assert db.list_sessions()[0]["model"] == "m"
    finally:
        db.close()


def test_runner_without_db_does_not_persist():
    run_job = make_cron_runner(_factory())  # no db
    result = run_job({"id": "j1", "name": "x", "prompt": "p"})
    assert result["final_response"] == "done"  # runs fine, nothing persisted
