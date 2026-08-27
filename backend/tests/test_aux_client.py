"""Tests for the auxiliary client (cheap-model side tasks: summarize, title)."""

from lohra.agent.aux import AuxClient
from lohra.agent.client import ModelClient
from lohra.providers.transports import get_transport


class _RecordingClient(ModelClient):
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": [{"type": "text", "text": self.reply}], "stop_reason": "end_turn", "usage": None}


def _aux(reply):
    client = _RecordingClient(reply)
    return AuxClient(client=client, transport=get_transport("anthropic_messages"), model="claude-haiku-4-5"), client


def test_summarize_returns_model_text():
    aux, _ = _aux("Active Task: x\nGoal: y")
    out = aux.summarize("a long transcript")
    assert "Active Task" in out


def test_summarize_uses_aux_model_and_passes_transcript():
    aux, client = _aux("summary")
    aux.summarize("THE TRANSCRIPT")
    sent = client.calls[0]
    assert sent["model"] == "claude-haiku-4-5"
    assert "THE TRANSCRIPT" in sent["messages"][0]["content"]


def test_title_returns_terse_text():
    aux, client = _aux("Auth setup")
    title = aux.title("a conversation about configuring OAuth")
    assert title == "Auth setup"
    assert client.calls[0]["max_tokens"] <= 64  # titles are short


def test_summarizer_callable_binds_to_summarize():
    aux, _ = _aux("S")
    fn = aux.summarizer()
    assert fn("text") == "S"
