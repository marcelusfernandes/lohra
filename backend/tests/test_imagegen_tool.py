"""Tests for the image_gen tool — intercepted, runner-bound (mirror of vision)."""

import json


from lohra.imagegen.tool import ImageGenTool


def _tool(runner):
    return ImageGenTool(runner)


def _call(tool, **args):
    return json.loads(tool.handle(args))


def test_generate_returns_saved_image_paths():
    seen = {}

    def runner(prompt, size, n):
        seen.update(prompt=prompt, size=size, n=n)
        return ["/tmp/a.png", "/tmp/b.png"]

    out = _call(_tool(runner), prompt="a red bicycle", size="1024x1024", n=2)
    assert out["ok"] is True
    assert out["images"] == ["/tmp/a.png", "/tmp/b.png"]
    assert seen == {"prompt": "a red bicycle", "size": "1024x1024", "n": 2}


def test_defaults_n_to_one_and_size_to_none():
    seen = {}

    def runner(prompt, size, n):
        seen.update(size=size, n=n)
        return ["/tmp/a.png"]

    _call(_tool(runner), prompt="a cat")
    assert seen == {"size": None, "n": 1}


def test_missing_prompt_errors():
    out = _call(_tool(lambda *a: []), prompt="   ")
    assert "error" in out
    out2 = _call(_tool(lambda *a: []))
    assert "error" in out2


def test_non_integer_n_falls_back_to_one():
    seen = {}

    def runner(prompt, size, n):
        seen["n"] = n
        return ["/tmp/a.png"]

    _call(_tool(runner), prompt="x", n="lots")
    assert seen["n"] == 1


def test_n_is_capped_at_the_provider_max():
    seen = {}

    def runner(prompt, size, n):
        seen["n"] = n
        return ["/tmp/a.png"]

    _call(_tool(runner), prompt="x", n=99)
    assert seen["n"] == 10  # MAX_IMAGES


def test_make_runner_generates_and_saves(tmp_path):
    import base64

    from lohra.imagegen.tool import make_image_gen_runner

    pixel = base64.b64encode(b"\x89PNG\r\n").decode("ascii")
    captured = {}

    class FakeClient:
        def generate_image(self, *, prompt, model, size=None, n=1):
            captured.update(prompt=prompt, model=model, size=size, n=n)
            return [pixel]

    runner = make_image_gen_runner(FakeClient(), str(tmp_path), model="gpt-image-1")
    paths = runner("a dog", "1024x1024", 1)
    assert len(paths) == 1
    assert paths[0].endswith(".png")
    assert captured == {"prompt": "a dog", "model": "gpt-image-1", "size": "1024x1024", "n": 1}


def test_register_schema_and_intercepted_fallback():
    from lohra.imagegen.tool import register_image_gen_tool_schema
    from lohra.tools import registry

    register_image_gen_tool_schema()
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert "image_gen" in names
    out = json.loads(registry.dispatch("image_gen", {"prompt": "x"}))
    assert "error" in out  # must be intercepted with a runner
