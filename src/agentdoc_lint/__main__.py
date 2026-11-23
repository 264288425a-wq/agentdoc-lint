"""Module entry point for `python -m agentdoc_lint`."""

from __future__ import annotations

import sys

from agentdoc_lint.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
