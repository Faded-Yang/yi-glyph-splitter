"""Entry point: native desktop window + local API server.

    python -m app.run [--project PATH] [--port 8765] [--browser] [--debug]

Window mode (default)
    Runs the FastAPI/uvicorn server in a background thread and shows the UI in a
    real desktop window (pywebview + WebView2).  No browser tabs, no address bar.
    Fallbacks if the embedded browser is unavailable:
        1. Microsoft Edge in --app mode (chromeless window)
        2. the user's default browser

    python -m app.run --browser     always use the default browser instead
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

# --- make "app" importable both from source and from the frozen exe ----------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import buildinfo  # noqa: E402
from app import paths  # noqa: E402

APP_TITLE = "彝文切字工具"


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
        ctypes.windll.user32.MessageBoxW(None, message[-1500:],
                                         APP_TITLE + " · 启动失败", 0x10)
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


# --------------------------------------------------------------- window frontends
def _edge_exe() -> str:
    for p in (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"):
        cand = os.path.expandvars(p)
        if os.path.isfile(cand):
            return cand
    return ""


def _show_native_window(url: str, log: logging.Logger) -> bool:
    """Real desktop window (pywebview / WebView2).  Blocks until closed."""
    try:
        import webview  # noqa: F401
    except Exception as exc:
        log.warning("pywebview 不可用（%s），尝试 Edge 应用窗口", exc)
        return False
    try:
        webview.create_window(APP_TITLE + " v" + buildinfo.VERSION, url, width=1520, height=960,
                              min_size=(1024, 680), text_select=True)
        log.info("native window created")
        webview.start()
        return True
    except Exception:
        log.exception("原生窗口启动失败，降级到浏览器")
        return False


def _show_edge_app(url: str, log: logging.Logger) -> bool:
    edge = _edge_exe()
    if not edge:
        return False
    try:
        subprocess.Popen([edge, "--app=" + url, "--window-size=1520,960"],
                         close_fds=True)
        log.info("opened edge --app window")
        return True
    except Exception:
        log.exception("Edge 应用窗口启动失败")
        return False


def _server_thread(app, port: int, debug: bool):
    import uvicorn
    # log_config=None: we configured logging ourselves; uvicorn's default
    # dictConfig writes to sys.stderr, which does not exist in a windowed exe.
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None,
                            log_level="debug" if debug else "warning", access_log=False)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    t.start()
    deadline = time.time() + 30
    while time.time() < deadline and not getattr(server, "started", False) and t.is_alive():
        time.sleep(0.05)
    return server, t


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
    ap.add_argument("--browser", action="store_true", help="用系统浏览器打开（不使用内嵌窗口）")
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

    server, thread = _server_thread(app, port, args.debug)
    log.info("serving %s (project=%r, frozen=%s)", url, args.project,
             getattr(sys, "frozen", False))

    try:
        if args.browser:
            webbrowser.open(url)
            while thread.is_alive():
                time.sleep(0.5)
        elif _show_native_window(url, log):
            pass                                  # window closed -> shut down
        elif _show_edge_app(url, log):
            while thread.is_alive():
                time.sleep(0.5)
        else:
            webbrowser.open(url)
            while thread.is_alive():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.should_exit = True
            thread.join(timeout=5)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
