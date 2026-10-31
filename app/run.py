"""Entry point: start the local API + UI server and open the browser.

    python -m app.run [--project PATH] [--port 8765] [--no-browser] [--debug]

Works both as a source checkout and as a PyInstaller one-file exe (where this
file runs as __main__, so only absolute imports are safe).
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import webbrowser

# --- make "app" importable both from source and from the frozen exe ----------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import paths  # noqa: E402


def _ensure_text_streams() -> None:
    """A windowed (--noconsole) exe has sys.stdout / sys.stderr == None.

    uvicorn's default logging config writes to sys.stderr and crashes with
    "NoneType has no attribute isatty" in that situation, so make sure the
    streams exist before anything logs.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8",
                                        buffering=1, errors="replace"))
            except Exception:
                pass


def _fatal(message: str) -> None:
    """Show a readable dialog instead of PyInstaller's raw traceback window."""
    try:
        (paths.log_dir() / "crash.log").write_text(message, encoding="utf-8")
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, message[-1500:], "彝文切字工具 · 启动失败", 0x10)
    except Exception:
        pass


_ensure_text_streams()


def _free_port(start: int) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到空闲端口")


def _setup_logging(debug: bool) -> None:
    log_file = paths.log_dir() / "app.log"
    handlers = [logging.FileHandler(log_file, encoding="utf-8")]
    if sys.stderr is not None:          # a windowed exe has no console
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def main(argv=None) -> int:
    try:
        return _main(argv)
    except SystemExit:
        raise
    except BaseException:
        import traceback
        _fatal("启动失败：" + chr(10) + chr(10) + traceback.format_exc())
        return 1


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="yi-segment", description="彝文古籍切字工具（本地桌面程序）")
    ap.add_argument("--project", default="", help="启动时打开/创建的项目目录")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    _setup_logging(args.debug)
    log = logging.getLogger("app.run")

    tools = paths.find_tools_dir()
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))

    from app.server import create_app

    port = _free_port(args.port)
    url = "http://127.0.0.1:%d/" % port
    app = create_app(project_dir=args.project)
    try:
        app.state.url = url
    except Exception:
        pass

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    log.info("serving %s (project=%r, frozen=%s)", url, args.project, getattr(sys, "frozen", False))
    try:
        import uvicorn
        # log_config=None: we configured logging ourselves; uvicorn's default
        # dictConfig writes to sys.stderr, which does not exist in a windowed exe.
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_config=None,
                                log_level="debug" if args.debug else "warning",
                                access_log=False)
        uvicorn.Server(config).run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
