"""Tests for the vision_analyze tool — intercepted, runner-bound (spec §9)."""

import base64
import json


from lohra.vision.tool import VisionTool

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _tool(runner):
    return VisionTool(runner)


def _call(tool, **args):
    return json.loads(tool.handle(args))


def test_analyze_url_builds_image_message_and_returns_text():
    seen = {}

    def runner(messages):
        seen["messages"] = messages
        return "a cat sitting on a mat"

    out = _call(_tool(runner), url="https://x.test/cat.jpg", prompt="what is in this image?")
    assert out["ok"] is True
    assert out["analysis"] == "a cat sitting on a mat"
    parts = seen["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "what is in this image?"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == "https://x.test/cat.jpg"


def test_analyze_file_embeds_data_uri(tmp_path):
    f = tmp_path / "pixel.png"
    f.write_bytes(_PNG)
    seen = {}

    def runner(messages):
        seen["messages"] = messages
        return "ok"

    _call(_tool(runner), path=str(f))
    url = seen["messages"][-1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_default_prompt_when_omitted():
    seen = {}

    def runner(messages):
        seen["messages"] = messages
        return "ok"

    _call(_tool(runner), url="https://x.test/cat.jpg")
    assert seen["messages"][-1]["content"][0]["text"]  # a non-empty default


def test_missing_image_errors():
    out = _call(_tool(lambda m: "x"))
    assert "error" in out


def test_bad_file_errors(tmp_path):
    out = _call(_tool(lambda m: "x"), path=str(tmp_path / "nope.png"))
    assert "error" in out


def test_make_vision_runner_calls_the_model():
    from lohra.agent.types import NormalizedResponse
    from lohra.vision.tool import make_vision_runner

    captured = {}

    class FakeTransport:
        def build_kwargs(self, **kwargs):
            captured["kwargs"] = kwargs
            return {"model": kwargs["model"], "messages": kwargs["messages"]}

        def normalize_response(self, raw):
            return NormalizedResponse(content="it's a diagram", finish_reason="stop")

    class FakeClient:
        def create(self, **kwargs):
            captured["create"] = kwargs
            return {"raw": True}

    runner = make_vision_runner(FakeClient(), FakeTransport(), "vision-model")
    messages = [{"role": "user", "content": [{"type": "text", "text": "?"}]}]
    assert runner(messages) == "it's a diagram"
    assert captured["kwargs"]["model"] == "vision-model"
    assert captured["kwargs"]["messages"] == messages


def test_register_schema_and_intercepted_fallback():
    from lohra.tools import registry
    from lohra.vision.tool import register_vision_tool_schema

    register_vision_tool_schema()
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert "vision_analyze" in names
    out = json.loads(registry.dispatch("vision_analyze", {"url": "x"}))
    assert "error" in out  # must be intercepted with a runner
