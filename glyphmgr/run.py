"""Start the glyph-library manager:  python -m glyphmgr.run [--out DIR] [--port 9400]"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "out"))
    ap.add_argument("--port", type=int, default=9400)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--build", action="store_true", help="rebuild glyph classes on start")
    args = ap.parse_args()

    import uvicorn
    from glyphmgr.server import create_app

    app = create_app(args.out, build=args.build)
    print("字库管理： http://%s:%d/" % (args.host, args.port), flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
