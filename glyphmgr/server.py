"""Glyph-library manager: FastAPI backend (port 9400 by default)."""
from __future__ import annotations

import os
import sys

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .core import Library

HERE = os.path.dirname(os.path.abspath(__file__))


def create_app(root: str, build: bool = False) -> FastAPI:
    app = FastAPI(title="彝文字库管理")
    lib = Library(root)
    app.state.lib = lib

    @app.get("/api/summary")
    def summary():
        if lib.classes is None:
            lib.build_classes()
        return lib.summary()

    @app.post("/api/build")
    def build():
        lib.build_classes(force=True)
        return lib.summary()

    @app.get("/api/classes")
    def classes(min_n: int = 1):
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
        p = lib.pages.get(page)
        if p is None:
            raise HTTPException(404, "page not found")
        fp = p.tile_path(file)
        if not os.path.isfile(fp):
            raise HTTPException(404, "tile not found")
        return FileResponse(fp, media_type="image/png")

    @app.post("/api/split")
    async def split(request: Request):
        data = await request.json()
        return lib.split_glyph(data.get("page"), int(data.get("idx") or 0),
                               rel=float(data.get("rel") or 0.5),
                               axis=str(data.get("axis") or "h"),
                               apply_class=data.get("apply_class"))

    @app.post("/api/merge")
    async def merge(request: Request):
        data = await request.json()
        return lib.merge_glyph(data.get("page"), int(data.get("idx") or 0),
                               with_next=bool(data.get("with_next", True)))

    @app.post("/api/undo")
    def undo():
        return lib.undo()

    @app.get("/api/pages")
    def pages():
        return [{"name": n, "n": len(p.rows)} for n, p in lib.pages.items()]

    app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
    return app
