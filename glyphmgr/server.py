"""Glyph-library manager: FastAPI backend (port 9400 by default)."""
from __future__ import annotations

import json
import os
import sys

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .core import Library

HERE = os.path.dirname(os.path.abspath(__file__))


def _cfg_path():
    base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "yi-segment")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "glyphmgr.json")


def _remember_root(path: str):
    try:
        with open(_cfg_path(), "w", encoding="utf-8") as f:
            json.dump({"root": path}, f, ensure_ascii=False)
    except OSError:
        pass


def remember_root(path: str):
    _remember_root(path)


def create_app(root: str, build: bool = False) -> FastAPI:
    app = FastAPI(title="彝文字库管理")
    lib = Library(root)
    app.state.lib = lib

    def current() -> Library:
        return app.state.lib

    @app.get("/api/summary")
    def summary():
        lib = current()
        if lib.classes is None:
            lib.build_classes()
        data = lib.summary()
        data["root"] = lib.root
        return data

    @app.post("/api/build")
    def build():
        lib = current()
        lib.build_classes(force=True)
        return lib.summary()

    @app.get("/api/root")
    def get_root():
        return {"root": current().root}

    @app.post("/api/root")
    async def set_root(request: Request):
        data = await request.json()
        path = os.path.abspath(str(data.get("path") or ""))
        if not os.path.isdir(path):
            raise HTTPException(400, "目录不存在：%s" % path)
        app.state.lib = Library(path)
        _remember_root(path)
        return {"ok": True, "root": path, "pages": len(app.state.lib.pages)}

    @app.get("/api/classes")
    def classes(min_n: int = 1):
        lib = current()
        if lib.classes is None:
            lib.build_classes()
        out = []
        for c in lib.classes:
            if c["n"] < max(1, min_n):
                continue
            m = c["members"][0]
            out.append({"id": c["id"], "n": c["n"],
                        "sample": "/api/tile/%s/%s" % (m["page"], m["file"]),
                        "pages": sorted({x["page"] for x in c["members"]})})
        return out

    @app.get("/api/class/{cid}")
    def class_detail(cid: int):
        lib = current()
        members = lib.class_members(cid)
        if not members:
            raise HTTPException(404, "class not found")
        out = []
        for m in members:
            g = lib.glyph(m["page"], m["file"])
            if g is None:
                continue
            out.append({"page": m["page"], "file": m["file"], "idx": g["idx"],
                        "tile": "/api/tile/%s/%s" % (m["page"], m["file"]),
                        "box": g["box"]})
        return {"id": cid, "n": len(out), "members": out}

    @app.get("/api/tile/{page}/{file}")
    def tile(page: str, file: str):
        lib = current()
        p = lib.pages.get(page)
        if p is None:
            raise HTTPException(404, "page not found")
        fp = p.tile_path(file)
        if not os.path.isfile(fp):
            raise HTTPException(404, "tile not found")
        return FileResponse(fp, media_type="image/png")

    @app.post("/api/split")
    async def split(request: Request):
        lib = current()
        data = await request.json()
        return lib.split_glyph(data.get("page"), int(data.get("idx") or 0),
                               rel=float(data.get("rel") or 0.5),
                               axis=str(data.get("axis") or "h"),
                               apply_class=data.get("apply_class"))

    @app.post("/api/merge")
    async def merge(request: Request):
        lib = current()
        data = await request.json()
        return lib.merge_glyph(data.get("page"), int(data.get("idx") or 0),
                               with_next=bool(data.get("with_next", True)))

    @app.post("/api/undo")
    def undo():
        return current().undo()

    @app.get("/api/pages")
    def pages():
        lib = current()
        return [{"name": n, "n": len(p.rows)} for n, p in lib.pages.items()]

    app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
    return app
