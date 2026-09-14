"""Build and smoke the checked-out commit's wheel in a fresh, isolated venv.

Run from backend: python -m ci.wheel_gate --output /tmp/lohra-wheel-unique
Only build/install use the network. The unconfigured CLI control blocks it.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib

from .installed_smoke import check_no_provider
from .wheel_contents import inventory_from_paths, sha256, verify_wheel


def clean_environment(environ: dict[str, str]) -> dict[str, str]:
    kept = {"PATH", "HOME", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
    result = {key: value for key, value in environ.items() if key in kept}
    # Pip config isolation does not stop requests from reading HOME/.netrc.
    # Preserve the caller's homes while disabling implicit installer auth,
    # including keyring/interactive recovery after a 401. Build children inherit
    # the same restrictions as the fresh venv's normal dependency installer.
    result.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
                  PIP_CONFIG_FILE=os.devnull, NETRC=os.devnull,
                  PIP_KEYRING_PROVIDER="disabled", PIP_NO_INPUT="1")
    return result


def provenance(source_sha: str, source_tree: str, event: dict) -> dict:
    pr = event.get("pull_request") or {}
    return {"source_sha": source_sha, "source_tree": source_tree,
            "pr_head_sha": pr.get("head", {}).get("sha"),
            "pr_base_sha": pr.get("base", {}).get("sha")}


def expect(condition, message):
    if not condition:
        raise ValueError(message)


class Gate:
    def __init__(self, output: Path):
        self.output = output
        self.started = time.monotonic()
        self.data = {"python": sys.version, "platform": platform.platform(), "phases": []}

    def save(self):
        self.data["total_seconds"] = round(time.monotonic() - self.started, 3)
        (self.output / "result.json").write_text(json.dumps(self.data, indent=2))

    def stage(self, name, action):
        phase = {"name": name, "status": "running"}
        self.data["phases"].append(phase)
        started = time.monotonic()
        print(f"wheel gate: {name}", flush=True)
        try:
            result = action()
            phase["status"] = "passed"
            return result
        except Exception as exc:
            phase.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            phase["seconds"] = round(time.monotonic() - started, 3)
            self.save()

    def command(self, name, args, cwd, env, *, expected=0, timeout=300):
        def invoke():
            phase = self.data["phases"][-1]
            phase["command"] = [str(arg) for arg in args]
            log = self.output / f"{name}.log"
            phase["log"] = str(log)
            try:
                result = subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                                        text=True, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                log.write_text(str(exc.stdout or "") + str(exc.stderr or ""))
                print(log.read_text(), file=sys.stderr)
                raise RuntimeError(f"command timed out after {timeout}s; see {log}") from exc
            phase["exit_code"] = result.returncode
            log.write_text(result.stdout + result.stderr)
            if result.returncode != expected:
                print(log.read_text(), file=sys.stderr)
                raise RuntimeError(f"command exited {result.returncode}, expected {expected}; see {log}")
            return result

        return self.stage(name, invoke)


def prepare_source(gate: Gate, repo: Path, env: dict) -> tuple[Path, dict]:
    paths = ["backend/lohra", "backend/ci", "backend/pyproject.toml",
             "backend/README.md", ".github/workflows/ci.yml"]
    status = gate.command("source-clean", ["git", "status", "--porcelain", "--untracked-files=all",
                                           "--", *paths], repo, env)
    if status.stdout:
        raise ValueError("commit package/build/helper/CI edits before running the wheel gate")
    sha = gate.command("source-sha", ["git", "rev-parse", "HEAD"], repo, env).stdout.strip()
    tree = gate.command("source-tree", ["git", "rev-parse", "HEAD^{tree}"], repo, env).stdout.strip()
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text()) if event_path else {}
    gate.data.update(provenance(sha, tree, event))
    archive = gate.output / "source.tar"
    gate.command("source-archive", ["git", "archive", "--format=tar", "--output", str(archive),
                                     sha, "backend"], repo, env)
    source = gate.output / "source"
    source.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(source, filter="data")
    tracked = gate.command("source-inventory", ["git", "ls-tree", "-r", "--name-only", "-z",
                                                 sha, "--", "backend/lohra"], repo, env)
    expected = inventory_from_paths(source, [name for name in tracked.stdout.split("\0") if name])
    project = tomllib.loads((source / "backend/pyproject.toml").read_text())["project"]
    manifest = {"files": expected, "version": project["version"], "scripts": project["scripts"]}
    (gate.output / "expected-package.json").write_text(json.dumps(manifest, indent=2))
    gate.data["tracked_package_files"] = len(expected)
    return source, manifest


def run_gate(gate: Gate, repo: Path) -> None:
    env = clean_environment(dict(os.environ))
    source, manifest = gate.stage("prepare-source", lambda: prepare_source(gate, repo, env))
    work = gate.output / "work"
    work.mkdir()
    wheel_dir = gate.output / "wheel"
    wheel_dir.mkdir()
    gate.command("build", [sys.executable, "-m", "pip", "wheel", "--disable-pip-version-check",
                            "--no-deps", "--wheel-dir", str(wheel_dir), str(source / "backend")],
                 work, env)
    wheels = list(wheel_dir.glob("*.whl"))
    gate.stage("wheel-selection", lambda: expect(
        len(wheels) == 1, f"build must produce exactly one wheel, found {len(wheels)}"))
    wheel = wheels[0]
    gate.data["wheel"] = str(wheel)
    gate.data["artifact"] = gate.stage("wheel-content", lambda: verify_wheel(
        wheel, manifest["files"], manifest["version"]))
    venv = gate.output / "venv"
    gate.command("venv", [sys.executable, "-m", "venv", str(venv)], work, env)
    gate.stage("venv-isolation", lambda: expect(
        "include-system-site-packages = false" in (venv / "pyvenv.cfg").read_text(),
        "wheel venv must not inherit system-site-packages"))
    python, cli = venv / "bin/python", venv / "bin/lohra"
    gate.command("install", [str(python), "-m", "pip", "install", "--disable-pip-version-check",
                              str(wheel)], work, env)
    gate.command("dependencies", [str(python), "-m", "pip", "check"], work, env)
    home = gate.output / "home"
    smoke_env = {**env, "LOHRA_HOME": str(home)}
    checker = source / "backend/ci/installed_smoke.py"
    imported = gate.command("installed-content", [str(python), "-I", str(checker), "--manifest",
                            str(gate.output / "expected-package.json"), "--home", str(home)],
                            work, smoke_env, timeout=30)
    gate.data["installed"] = json.loads(imported.stdout)

    version = gate.command("cli-version", [str(cli), "--version"], work, smoke_env, timeout=30)
    gate.stage("cli-version-contract", lambda: expect(
        version.stdout.strip() == "lohra " + manifest["version"],
        "CLI version differs from source/distribution"))
    help_result = gate.command("cli-help", [str(cli), "--help"], work, smoke_env, timeout=30)
    gate.stage("cli-help-contract", lambda: expect(
        "usage:" in help_result.stdout.lower() and "chat" in help_result.stdout,
        "CLI help did not describe the command surface"))
    export = gate.output / "export"
    gate.command("cli-export", [str(cli), "skill", "export", "use-lohra", "--to", str(export)],
                 work, smoke_env, timeout=30)
    exported = export / "use-lohra/SKILL.md"
    gate.stage("export-content", lambda: expect(
        sha256(exported.read_bytes()) == manifest["files"]["lohra/skills/export/use-lohra/SKILL.md"],
        "exported skill differs from source"))
    workflows = gate.command("cli-workflows", [str(cli), "workflow", "list"],
                             work, smoke_env, timeout=30)
    gate.stage("cli-workflows-contract", lambda: expect(
        workflows.stdout.strip() == "no workflow runs", "workflow list did not use an empty home"))
    negative = gate.command("cli-unconfigured", [str(python), "-I", str(checker), "--cli", str(cli),
                           "chat", "--json", "--no-input", "--no-tools", "wheel smoke"],
                           work, smoke_env, expected=2, timeout=30)
    gate.data["unconfigured"] = gate.stage("unconfigured-contract", lambda: check_no_provider(
        negative.returncode, negative.stdout))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    if Path(__file__).resolve().parent != repo / "backend/ci":
        parser.error("run the helper belonging to the selected repository")
    output = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix="lohra-wheel-"))
    if output.is_relative_to(repo):
        parser.error("wheel gate output must be outside the checkout")
    if args.output:
        output.mkdir(parents=True, exist_ok=False)
    gate = Gate(output)
    try:
        run_gate(gate, repo)
        gate.data["status"] = "passed"
        return 0
    except Exception as exc:
        gate.data.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        print(gate.data["error"], file=sys.stderr)
        return 1
    finally:
        gate.save()
        print(json.dumps(gate.data, indent=2), flush=True)
        print(f"wheel gate {gate.data['status']}: {output / 'result.json'}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
