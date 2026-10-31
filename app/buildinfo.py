"""Build identity: version + build time.

Shown in 设置 → 关于 and /api/health so it is obvious whether the running
copy is the freshly built one or a stale exe/portable folder.
"""
from __future__ import annotations

import os
import sys
import time

VERSION = "1.0.1"


def build_stamp() -> str:
    """mtime of the frozen exe (packaged build) or of run.py (source run)."""
    try:
        if getattr(sys, "frozen", False):
            target = sys.executable
        else:
            target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(target)))
    except Exception:  # noqa: BLE001
        return "?"
