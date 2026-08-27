# PyInstaller spec for the lohra backend sidecar (onedir).
#
# This codebase imports its providers and subsystems LAZILY (e.g. `import openai`
# inside build_client, the MCP SDK inside connectors, per-feature tool modules).
# PyInstaller's static analysis misses those, so we explicitly collect the whole
# `lohra` package plus the heavy third-party trees (uvicorn's dynamic loaders,
# the provider SDKs). "It built" is NOT the test — the frozen binary must run a
# real dashboard/chat to prove the lazy imports were collected.

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# The backend source root (parent of this packaging/ dir). Put it on pathex so
# PyInstaller bundles the real `lohra` source instead of failing to resolve the
# editable (-e) install's import hook.
_BACKEND_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

_BUNDLE_PACKAGES = (
    "uvicorn",
    # uvicorn[standard] loads these by string at runtime (event loop + HTTP/WS
    # protocols); static analysis misses them, so collect them explicitly or the
    # server starts, fails its loop/protocol setup, and never binds — silently.
    "uvloop",
    "httptools",
    "websockets",
    "watchfiles",
    "h11",
    "fastapi",
    "starlette",
    "httpx",
    "anthropic",
    "openai",
    "mcp",
    "yaml",
    # jsonschema validates workflow leaf output; it loads its vendored meta-schemas
    # via importlib.metadata, so collect it whole or the frozen binary 404s them.
    "jsonschema",
    "jsonschema_specifications",
)

datas, binaries, hiddenimports = [], [], []
for package in _BUNDLE_PACKAGES:
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:  # an optional package that isn't installed — skip it
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Grab every lohra submodule, since many are imported lazily inside functions.
hiddenimports += collect_submodules("lohra")
# ...and its DATA files (the builtin SKILL.md library): collect_submodules only
# finds modules, so without this the frozen app ships with no builtin skills.
datas += collect_data_files("lohra")

a = Analysis(
    ["lohra_entry.py"],
    pathex=[_BACKEND_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
# onedir (not onefile): no bootloader re-exec / temp-unpack on every launch —
# faster start, and it avoids the onefile-specific server hangs.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lohra",
    console=True,
    strip=False,
    upx=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="lohra",
)
