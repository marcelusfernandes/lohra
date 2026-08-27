"""Self-update — pull the latest backend code from its git checkout.

``lohra update`` finds the repo from the *installed package location* (not the
CWD), runs pre-flight state checks (dirty tree, upstream, divergence), then a
fast-forward-only pull. An editable install picks up pure-Python changes for
free; only a changed ``pyproject.toml`` needs a reinstall. The running process
keeps its loaded modules, so a successful update reports "restart to apply".
"""
