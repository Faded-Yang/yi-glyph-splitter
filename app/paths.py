"""Filesystem layout for the app (frozen-exe aware)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "yi-segment"


def app_root() -> Path:
    """Directory that contains the app/ package (works inside a PyInstaller exe)."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return Path(base)
    return Path(__file__).resolve().parent.parent


def resource_path(*parts) -> Path:
    return app_root().joinpath(*parts)


def static_dir() -> Path:
    return resource_path("app", "static")


def data_dir() -> Path:
    """Per-user writable directory (config, logs, default projects).

    YI_SEGMENT_HOME overrides it, so tests never touch the real user config.
    """
    override = os.environ.get("YI_SEGMENT_HOME")
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return data_dir() / "config.json"


def log_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_project_dir() -> Path:
    d = data_dir() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def temp_dir() -> Path:
    d = Path(tempfile.gettempdir()) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_tools_dir() -> Path:
    """tools/ must be importable (yi_core.*)."""
    for cand in (app_root() / "tools", app_root().parent / "tools"):
        if (cand / "yi_core").is_dir():
            return cand
    raise RuntimeError("找不到 tools/yi_core：exe 或源码目录缺少该依赖")
