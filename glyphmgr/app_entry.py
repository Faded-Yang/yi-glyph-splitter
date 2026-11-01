"""Entry point for the packaged glyph-library manager (YiGlyphMgr.exe)."""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser


def log(msg):
    try:
        base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "yi-segment", "logs")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "glyphmgr.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + chr(10))
    except Exception:
        pass


def _free_port(start: int) -> int:
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def _open_when_ready(port: int):
    for _ in range(120):
        time.sleep(0.5)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                webbrowser.open("http://127.0.0.1:%d/" % port)
                return
        except OSError:
            continue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="out 文件夹（默认：exe 同级的 out/，或当前工作目录的 out/）")
    ap.add_argument("--port", type=int, default=9400)
    args = ap.parse_args()

    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    out_dir = args.out
    if not out_dir:
        for cand in (os.path.join(base, "out"), os.path.join(os.getcwd(), "out"),
                     os.path.join(os.path.dirname(base), "out")):
            if os.path.isdir(cand):
                out_dir = cand
                break
    if not out_dir or not os.path.isdir(out_dir):
        out_dir = os.path.join(base, "out")

    import uvicorn
    from glyphmgr.server import create_app

    # windowed exe: stdout/stderr are None -> uvicorn's colour formatter crashes
    if sys.stdout is None or sys.stderr is None:
        devnull = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = devnull
        if sys.stderr is None:
            sys.stderr = devnull

    port = _free_port(args.port)
    log("start out=%s port=%d base=%s" % (out_dir, port, base))
    app = create_app(out_dir)
    threading.Thread(target=_open_when_ready, args=(port,), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", log_config=None)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL " + traceback.format_exc())
        raise
