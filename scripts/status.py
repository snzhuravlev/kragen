#!/usr/bin/env python3
"""Show whether Kragen HTTP API is running and probe /health."""

from __future__ import annotations

import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
_src = _repo / "src"
if _src.is_dir() and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from kragen.cli.web_server_ctl import cmd_status  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(cmd_status())
