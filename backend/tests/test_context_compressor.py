"""Tests for ContextCompressor — prune/protect/summarize long histories (§5)."""

from lohra.agent.context import COMPACTION_PREFIX, ContextCompressor


def _user(text):
    return {"role": "user", "content": text}


def _assistant(text):
    return {"role": "assistant", "content": text, "finish_reason": "stop"}


def _fake_summarize(text):
    return f"SUMMARY({len(text)} chars)"


def test_should_compress_threshold():
    c = ContextCompressor(threshold_percent=0.5)
    assert c.should_compress(prompt_tokens=600, context_window=1000) is True
    assert c.should_compress(prompt_tokens=400, context_window=1000) is False


def test_short_history_is_unchanged():
    c = ContextCompressor(protect_first_n=2, protect_last_n=2)
    messages = [_user("a"), _assistant("b"), _user("c")]
    assert c.compress(messages, summarize=_fake_summarize) == messages


def test_compress_replaces_middle_with_summary():
    c = ContextCompressor(protect_first_n=2, protect_last_n=2)
    messages = [_user(f"m{i}") for i in range(10)]
    out = c.compress(messages, summarize=_fake_summarize)
    # head (2) + 1 summary + tail (2)
    assert len(out) == 5
    assert out[0] == messages[0] and out[1] == messages[1]
    assert out[-1] == messages[-1] and out[-2] == messages[-2]
    summary = out[2]
    assert summary["role"] == "user"
    assert summary["content"].startswith(COMPACTION_PREFIX)
    assert "SUMMARY(" in summary["content"]


def test_summarize_receives_middle_content():
    seen = {}

    def capture(text):
        seen["text"] = text
        return "s"

    c = ContextCompressor(protect_first_n=1, protect_last_n=1)
    messages = [_user("HEAD"), _user("MIDDLE-ONE"), _user("MIDDLE-TWO"), _user("TAIL")]
    c.compress(messages, summarize=capture)
    assert "MIDDLE-ONE" in seen["text"]
    assert "MIDDLE-TWO" in seen["text"]
    assert "HEAD" not in seen["text"]
    assert "TAIL" not in seen["text"]


def test_orphan_tool_result_in_tail_is_dropped():
    # The assistant tool_call lives in the (summarized) middle; its result lands
    # at the start of the tail with no matching tool_use -> must be dropped.
    c = ContextCompressor(protect_first_n=1, protect_last_n=2)
    messages = [
        _user("start"),
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "name": "f", "tool_call_id": "tc1", "content": "result"},
        _user("end"),
    ]
    out = c.compress(messages, summarize=_fake_summarize)
    # tail was [tool(tc1), user(end)]; the orphan tool is dropped
    assert not any(m.get("role") == "tool" for m in out[2:])
    assert out[-1] == _user("end")


def test_orphan_tool_call_in_head_is_stripped():
    # The assistant tool_call is in the head but its result is in the middle.
    c = ContextCompressor(protect_first_n=2, protect_last_n=1)
    messages = [
        _user("start"),
        {"role": "assistant", "content": "calling", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "name": "f", "tool_call_id": "tc1", "content": "result"},
        _user("more"),
        _user("end"),
    ]
    out = c.compress(messages, summarize=_fake_summarize)
    head_assistant = out[1]
    assert "tool_calls" not in head_assistant  # stripped (orphan tool_use)
    assert head_assistant["content"] == "calling"  # text content kept
