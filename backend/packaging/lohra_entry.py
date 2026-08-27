"""Frozen-binary entry point for the lohra backend sidecar.

PyInstaller freezes this into a standalone executable so the desktop app can
ship and spawn the backend without a system Python. It just delegates to the
normal CLI ``main`` — every subcommand (dashboard, serve, chat, …) works.
"""

import sys

from lohra.cli import main

if __name__ == "__main__":
    sys.exit(main())
