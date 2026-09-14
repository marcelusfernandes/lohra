"""Run with the fresh venv's python -I; imports must come from its site-packages.

This file deliberately has no repository-package imports at module scope.
The --cli mode runs the installed console script with network access disabled.
"""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import runpy
import sys
import sysconfig


def verify_origin(path: Path, purelib: Path) -> None:
    if not Path(path).resolve().is_relative_to(purelib.resolve()):
        raise ValueError(f"import/distribution outside venv site-packages: {path}")


def verify_installed_files(purelib: Path, expected: dict[str, str]) -> None:
    if not expected:
        raise ValueError("empty installed package expectation")
    for name, wanted in expected.items():
        path = purelib / name
        verify_origin(path, purelib)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != wanted:
            raise ValueError(f"installed package missing or changed: {name}")


def check_no_provider(code: int, output: str) -> dict:
    try:
        data = json.loads(output)
        valid = (code == 2 and data["completed"] is False and data["api_calls"] == 0
                 and data["output"] is None and data["tool_calls"] == []
                 and data["error"].startswith("no provider configured"))
    except (ValueError, KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("unexpected unconfigured CLI exit/JSON envelope")
    return data


def inspect_install(manifest_path: Path, home: Path) -> dict:
    import lohra
    import lohra.cli
    from lohra.skills.store import SkillStore, builtin_root

    manifest = json.loads(manifest_path.read_text())
    purelib = Path(sysconfig.get_path("purelib")).resolve()
    for module in (lohra, lohra.cli):
        verify_origin(Path(module.__file__), purelib)
    dist = importlib.metadata.distribution("lohra")
    if Path(dist.locate_file("")).resolve() != purelib:
        raise ValueError("distribution outside venv site-packages")
    verify_installed_files(purelib, manifest["files"])
    if dist.version != manifest["version"] or lohra.__version__ != dist.version:
        raise ValueError("installed module/distribution version differs from source")
    entry_points = {entry.name: entry.value for entry in dist.entry_points
                    if entry.group == "console_scripts"}
    if entry_points != manifest["scripts"]:
        raise ValueError("installed entry points differ from source")
    store = SkillStore(home, builtin_roots=(builtin_root(),))
    skills = store.scan()
    actual = {skill.path.resolve().relative_to(purelib).as_posix() for skill in skills}
    expected = {name for name in manifest["files"]
                if name.startswith("lohra/skills/builtin/") and name.endswith("/SKILL.md")}
    if not expected or actual != expected or not all(skill.body for skill in skills):
        raise ValueError("installed builtin skills did not load from package data")
    return {"python": sys.version, "prefix": sys.prefix, "purelib": str(purelib),
            "package_import": lohra.__file__, "cli_import": lohra.cli.__file__,
            "version": dist.version, "entry_points": entry_points,
            "verified_files": len(manifest["files"]), "builtins": sorted(actual),
            "requires_dist": dist.requires}


def guarded_cli(entrypoint: str, args: list[str]) -> None:
    def deny_network(event, _args):
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
            print(f"wheel smoke blocked network: {event}", file=sys.stderr)
            # Liveness probes treat this as an unavailable daemon. No production
            # provider function is mocked and no inference can leave the process.
            raise OSError("network disabled for wheel smoke")

    sys.addaudithook(deny_network)
    sys.argv = [entrypoint, *args]
    runpy.run_path(entrypoint, run_name="__main__")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--cli", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.cli:
        guarded_cli(args.cli[0], args.cli[1:])
    elif args.manifest and args.home:
        print(json.dumps(inspect_install(args.manifest, args.home)))
    else:
        parser.error("use --manifest/--home or --cli ENTRYPOINT [ARGS...]")


if __name__ == "__main__":
    main()
