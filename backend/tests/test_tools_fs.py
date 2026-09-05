"""Tests for the filesystem tools (read_file, write_file)."""

import json
import threading

from lohra.tools.fs import read_file, write_file
from lohra.tools.registry import registry


def test_read_file_returns_content(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("olá mundo", encoding="utf-8")
    out = json.loads(read_file({"path": str(f)}))
    assert out["ok"] is True
    assert out["data"] == "olá mundo"
    assert out["truncated"] is False


def test_read_file_missing_path():
    out = json.loads(read_file({}))
    assert "error" in out and "path" in out["error"]


def test_read_file_not_found(tmp_path):
    out = json.loads(read_file({"path": str(tmp_path / "nope.txt")}))
    assert "not found" in out["error"]


def test_read_file_directory(tmp_path):
    out = json.loads(read_file({"path": str(tmp_path)}))
    assert "directory" in out["error"]


def test_read_file_truncates_large_content(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x" * 200_000, encoding="utf-8")
    out = json.loads(read_file({"path": str(f)}))
    assert out["truncated"] is True
    assert len(out["data"]) == 100_000


def test_write_file_creates_and_writes(tmp_path):
    f = tmp_path / "out.txt"
    out = json.loads(write_file({"path": str(f), "content": "written"}))
    assert out["ok"] is True
    assert f.read_text(encoding="utf-8") == "written"
    assert out["bytes_written"] == len("written".encode("utf-8"))


def test_write_file_creates_parent_dirs(tmp_path):
    f = tmp_path / "sub" / "dir" / "out.txt"
    json.loads(write_file({"path": str(f), "content": "x"}))
    assert f.exists()


def test_write_file_missing_content(tmp_path):
    out = json.loads(write_file({"path": str(tmp_path / "x.txt")}))
    assert "content" in out["error"]


def test_write_file_mode_append_appends_without_overwriting(tmp_path):
    # H (issue #67): write_file has no append mode today. `mode` is an extra
    # key the handler never reads (dispatch does no schema validation of
    # args), so passing mode="append" is silently ignored and the second
    # write overwrites the first — exactly the read-modify-write hazard #62
    # measured. This is the desired post-fix behaviour: it must FAIL before
    # the fix and PASS after.
    f = tmp_path / "shared.txt"
    write_file({"path": str(f), "content": "first\n"})
    out = json.loads(write_file({"path": str(f), "content": "second\n", "mode": "append"}))
    assert out["ok"] is True
    assert f.read_text(encoding="utf-8") == "first\nsecond\n"


def test_write_file_mode_append_survives_concurrent_writers(tmp_path):
    # Two threads append to the SAME path with no coordination — reproduces
    # exp #62 config A/B1 (parallel branches sharing a path). A single-call
    # append (O_APPEND, one write()) must lose nothing, unlike the
    # read-modify-write that overwrite-mode forces on callers.
    f = tmp_path / "shared.txt"
    f.write_text("", encoding="utf-8")
    barrier = threading.Barrier(2)

    def worker(line: str) -> None:
        barrier.wait()
        write_file({"path": str(f), "content": line, "mode": "append"})

    threads = [
        threading.Thread(target=worker, args=(f"line-{i}\n",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    content = f.read_text(encoding="utf-8")
    assert "line-0\n" in content
    assert "line-1\n" in content
    assert content.count("\n") == 2  # both lines present, nothing lost or torn


def test_fs_tools_registered():
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert {"read_file", "write_file"} <= names
