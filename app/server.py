"""FastAPI backend for the desktop app (see app/SPEC-APP.md).

create_app(project_dir="") -> FastAPI     # app/run.py imports this

Static UI lives in app/static (index.html + app.js + ...); all REST routes are
under /api.  Errors are always JSON {"detail": "<Chinese sentence>"}.

CLI:  python -m app.server --selftest          # fast smoke test (no TIF)
      python -m app.selftest_api --tif ...     # full end-to-end with a real page
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config as config_mod
from . import jobs as jobs_mod
from . import paths
from . import pipeline
from . import buildinfo
from . import store as store_mod
from .store import ProjectStore, StoreError

log = logging.getLogger("app.server")

VERSION = buildinfo.VERSION
APP_TITLE = "彝文切字工具"
DEFAULT_PREVIEW_W = pipeline.PREVIEW_W

# A preview can be missing or half-written (cancelled job, killed process).
# Both the detail endpoint and the image endpoint repair it on demand.
_PREVIEW_LOCKS: dict = {}
_PREVIEW_LOCKS_GUARD = threading.Lock()


def _preview_lock(pid: str):
    with _PREVIEW_LOCKS_GUARD:
        lk = _PREVIEW_LOCKS.get(pid)
        if lk is None:
            lk = threading.Lock()
            _PREVIEW_LOCKS[pid] = lk
        return lk


def _preview_ok(path) -> bool:
    """True when preview.png exists and looks like a complete PNG."""
    try:
        if not path.is_file() or path.stat().st_size < 1024:
            return False
        with open(path, "rb") as fh:
            head = fh.read(8)
            fh.seek(-12, os.SEEK_END)
            tail = fh.read()
        return head == b"\x89PNG\r\n\x1a\n" and b"IEND" in tail
    except Exception:
        return False


def _ensure_preview(store: ProjectStore, pid: str, width: int = DEFAULT_PREVIEW_W,
                    rebuild: bool = False) -> None:
    path = store.preview_path(pid)
    if not rebuild and _preview_ok(path):
        return
    with _preview_lock(pid):
        if rebuild or not _preview_ok(path):
            pipeline.build_preview(store, pid, width=width)

PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>彝文切字工具</title>
<style>body{font-family:"Microsoft YaHei",sans-serif;margin:80px;color:#333}
code{background:#f2f0ec;padding:2px 6px;border-radius:3px}</style></head>
<body><h2>彝文切字工具 后端已启动</h2>
<p>前端静态文件还没生成：<code>app/static/index.html</code>。</p>
<p>API 可用：<a href="/api/health">/api/health</a> · <a href="/api/state">/api/state</a> ·
<a href="/api/docs">/api/docs</a></p></body></html>"""


# --------------------------------------------------------------------- helpers
def _glyphbank_available() -> bool:
    try:
        from . import yi_ref
        return bool(yi_ref.available())
    except Exception:
        return False


def _resolve_project_dir(cfg, project_dir) -> Path:
    raw = str(project_dir or "").strip()
    if raw:
        p = Path(os.path.abspath(os.path.expanduser(raw)))
    elif cfg.last_project and Path(cfg.last_project).is_dir():
        p = Path(cfg.last_project)
    else:
        p = Path(paths.default_project_dir()) / "默认项目"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _state(app: FastAPI) -> dict:
    st: ProjectStore = app.state.store
    cfg = app.state.config
    pub = cfg.public()
    return {
        "version": VERSION,
        "project": {"path": str(st.root), "name": st.name,
                    "version": store_mod.PROJECT_VERSION},
        "project_name": st.name,
        "project_path": str(st.root),
        "pages": st.state_pages(),
        "settings": {"defaults": pub["export"]},
           }


def _resolve_ids(st: ProjectStore, spec, *, only_with_boxes: bool = False) -> list:
    if spec is None or (isinstance(spec, str) and spec.strip().lower() == "all"):
        ids = st.page_ids()
    elif isinstance(spec, str):
        ids = [spec] if st.has_page(spec) else []
    elif isinstance(spec, (list, tuple)):
        ids = []
        for raw in spec:
            pid = str(raw)
            if st.has_page(pid) and pid not in ids:
                ids.append(pid)
    else:
        ids = []
    if only_with_boxes:
        ids = [pid for pid in ids if st.boxes(pid)]
    return ids


def _png_response(data: bytes) -> Response:
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


def _dialog_disabled() -> bool:
    v = os.environ.get("YI_SEGMENT_NO_DIALOG", "").strip().lower()
    return v not in ("", "0", "false", "no")


def _tk_root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    return root


def _ask_files() -> list:
    if _dialog_disabled():
        return []
    root = None
    try:
        root = _tk_root()
        from tkinter import filedialog
        picked = filedialog.askopenfilenames(
            parent=root, title="选择扫描页（可多选）",
            filetypes=[("扫描页 TIF", "*.tif *.tiff"),
                       ("图片文件", "*.tif *.tiff *.png *.jpg *.jpeg *.bmp *.webp"),
                       ("所有文件", "*.*")])
        if isinstance(picked, str):
            picked = [picked]
        return [str(p) for p in (picked or [])]
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def _ask_dir(title: str, initial: str = "") -> str:
    if _dialog_disabled():
        return ""
    root = None
    try:
        root = _tk_root()
        from tkinter import filedialog
        picked = filedialog.askdirectory(parent=root, title=title,
                                         initialdir=initial or os.path.expanduser("~"))
        return str(picked or "")
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


# --------------------------------------------------------------- job builders
def _make_progress(job, pid: str):
    """Adapter: pipeline progress_cb(frac, message) -> job fields."""
    def cb(frac, message=None):
        cur = {"pid": pid, "frac": frac}
        if message:
            cur["stage"] = str(message)
        job.update(current=cur, message=str(message) if message else job.message)
    return cb


def _process_job(st: ProjectStore, ids: list, force: bool):
    total = max(1, len(ids))

    def fn(job):
        ok = 0
        job.update(total=total, done=0, message="准备处理 %d 页" % total)
        for i, pid in enumerate(ids, 1):
            job.check()
            entry = st.get_page(pid)
            if not entry:
                job.add_error("页面已不存在：%s" % pid)
                continue
            name = entry.get("name") or pid
            prog = _make_progress(job, pid)
            try:
                res = pipeline.process_page(st, pid, progress_cb=prog,
                                            cancel_cb=job.is_canceled, force=force)
                ok += 1
                job.add_result({"pid": pid, "ok": True, "name": res["name"],
                                "n_boxes": res["n_boxes"],
                                "seconds": res["seconds"],
                                "timings": res["timings"],
                                "reused": res["reused_page_json"],
                                "warning": res["warning"]})
            except pipeline.Canceled:
                raise jobs_mod.JobCanceled()
            except Exception as exc:  # noqa: BLE001
                msg = str(exc) or exc.__class__.__name__
                try:
                    st.set_error(pid, msg)
                except Exception:
                    pass
                job.add_error("%s：%s" % (name, msg))
                job.add_result({"pid": pid, "ok": False, "name": name, "error": msg})
            job.update(done=i, total=total, current={"pid": pid, "frac": 1.0},
                       message="已处理 %d/%d" % (i, total))
        if ids and ok == 0 and job.errors:
            raise RuntimeError(job.errors[0])
        return []

    return fn


def _export_job(st: ProjectStore, ids: list, out: str, cfg: "config_mod.AppConfig",
                pad_ratio: float, min_pad: int, order: str, fmt: str):
    total = max(1, len(ids))

    def fn(job):
        ok = 0
        job.update(total=total, done=0, message="准备导出 %d 页" % total)
        for i, pid in enumerate(ids, 1):
            job.check()
            entry = st.get_page(pid)
            if not entry:
                job.add_error("页面已不存在：%s" % pid)
                continue
            name = entry.get("name") or pid
            prog = _make_progress(job, pid)
            try:
                res = pipeline.export_page(
                    st, pid, out, pad_ratio=pad_ratio, min_pad=min_pad,
                    order=order, fmt=fmt, progress_cb=prog, cancel_cb=job.is_canceled)
                ok += 1
                job.add_result({"pid": pid, "ok": True, "name": res["name"],
                                "out": res["out"], "n_tiles": res["n_tiles"],
                                "seconds": res["seconds"],
                                "index_csv": res["index_csv"],
                                "sheet_png": res["sheet_png"]})
            except pipeline.Canceled:
                raise jobs_mod.JobCanceled()
            except Exception as exc:  # noqa: BLE001
                msg = str(exc) or exc.__class__.__name__
                job.add_error("%s：%s" % (name, msg))
                job.add_result({"pid": pid, "ok": False, "name": name, "error": msg})
            job.update(done=i, total=total, current={"pid": pid, "frac": 1.0},
                       message="已导出 %d/%d" % (i, total))
        if ids and ok == 0 and job.errors:
            raise RuntimeError(job.errors[0])
        return []

    return fn


def _clean_proposal(raw, boxes) -> Optional[dict]:
    valid = {int(b.get("id") or 0) for b in boxes}
    if not isinstance(raw, dict):
        return None

    def bbox(v):
        if isinstance(v, dict):
            try:
                return {"x0": int(round(float(v.get("x0")))), "y0": int(round(float(v.get("y0")))),
                        "x1": int(round(float(v.get("x1")))), "y1": int(round(float(v.get("y1"))))}
            except (TypeError, ValueError):
                return None
        if isinstance(v, (list, tuple)) and len(v) >= 4:
            try:
                return {"x0": int(round(float(v[0]))), "y0": int(round(float(v[1]))),
                        "x1": int(round(float(v[2]))), "y1": int(round(float(v[3])))}
            except (TypeError, ValueError):
                return None
        return None

    try:
        box_id = int(raw.get("box_id"))
    except (TypeError, ValueError):
        return None
    if box_id not in valid:
        return None
    src = next((b for b in boxes if int(b.get("id") or 0) == box_id), None)
    before = bbox(raw.get("before"))
    if before is None and src is not None:
        before = {"x0": src["x0"], "y0": src["y0"], "x1": src["x1"], "y1": src["y1"]}
    action = str(raw.get("action") or "keep").strip().lower()
    if action not in ("tighten", "split", "merge", "keep"):
        action = "keep"
    raw_after = raw.get("after")
    after = []
    if isinstance(raw_after, (list, tuple)):
        for item in raw_after:
            b = bbox(item)
            if b:
                after.append(b)
    else:
        b = bbox(raw_after)
        if b:
            after.append(b)
    if action == "split" and len(after) < 2:
        action = "keep"
    if not after:
        after = [before] if before else []
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    return {"box_id": box_id, "action": action, "before": before, "after": after,
            "confidence": round(conf, 3),
            "reason": str(raw.get("reason") or "")[:60],
            "status": "pending"}


def _refcheck_job(app: FastAPI, pid: str):
    """Background job: score all boxes against the standard glyph bank."""
    st: ProjectStore = app.state.store

    def fn(job):
        def progress(frac, message):
            try:
                job.progress(int(round(float(frac) * 100)), 100, message)
            except Exception:
                pass
        try:
            res = pipeline.run_refcheck(st, pid, progress_cb=progress, cancel_cb=job.is_canceled)
        except store_mod.StoreError as exc:
            raise RuntimeError(str(exc))
        job.add_result({"pid": pid, "stats": res.get("stats") or {}})
        return {"pid": pid, "stats": res.get("stats") or {},
                "proposals": len(res.get("proposals") or [])}

    return fn
def _userbank_stats() -> dict:
    try:
        from . import userbank
        st = userbank.stats()
        return {"n": st.get("n", 0), "labels": st.get("labels") or {}, "updated": st.get("updated") or ""}
    except Exception:  # noqa: BLE001
        return {"n": 0, "labels": {}}


def _page_columns(store, pid) -> list:
    """Columns for the UI: auto-split merged bands once, unless the user edited them."""
    try:
        page = store.load_page_json(pid)
    except Exception:  # noqa: BLE001
        return []
    try:
        cols = pipeline.ensure_columns(store, pid, page)
    except Exception:  # noqa: BLE001
        cols = page.get("columns") or []
    out = []
    for i, c in enumerate(cols or []):
        try:
            out.append({"x0": int(c["x0"]), "x1": int(c["x1"])})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda c: c["x0"])
    for i, c in enumerate(out):
        c["idx"] = i
    return out


def create_app(project_dir: str = "") -> FastAPI:
    cfg = config_mod.AppConfig.load()
    root = _resolve_project_dir(cfg, project_dir)
    try:
        st = ProjectStore(root)
    except Exception as exc:  # pragma: no cover - unreadable project dir
        raise RuntimeError("无法打开项目目录 %s：%s" % (root, exc))
    if not cfg.last_project or cfg.last_project != str(root):
        cfg.last_project = str(root)
        try:
            cfg.save()
        except Exception:
            log.warning("config save failed", exc_info=True)

    app = FastAPI(title=APP_TITLE, version=VERSION, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)
    app.state.store = st
    app.state.jobs = jobs_mod.JobManager()
    app.state.config = cfg


    # ------------------------------------------------------------- health/state
    @app.get("/api/health")
    def api_health(request: Request):
        store = request.app.state.store
        return {"ok": True, "app": APP_TITLE, "version": VERSION,
                "build": buildinfo.build_stamp(),
                "project": str(store.root), "tools": pipeline.ensure_tools(),
                "glyphbank": _glyphbank_available(),
                "time": time.strftime("%Y-%m-%d %H:%M:%S")}

    @app.get("/api/state")
    def api_state(request: Request):
        try:
            return _state(request.app)
        except Exception as exc:
            raise HTTPException(status_code=500, detail="读取项目状态失败：%s" % exc)

    # ----------------------------------------------------------------- dialogs
    @app.get("/api/dialog/files")
    def api_dialog_files():
        try:
            paths_found = _ask_files()
        except Exception as exc:  # noqa: BLE001 - tkinter missing / no desktop
            raise HTTPException(status_code=500,
                                detail="无法打开系统文件对话框（%s）；可以手动输入路径或改用导入文件夹" % exc)
        return {"paths": paths_found, "canceled": not paths_found}

    @app.get("/api/dialog/folder")
    def api_dialog_folder():
        try:
            folder = _ask_dir("选择扫描页所在文件夹")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500,
                                detail="无法打开系统文件夹对话框（%s）；可以手动输入路径" % exc)
        return {"path": folder, "canceled": not folder}

    @app.get("/api/dialog/export_dir")
    def api_dialog_export_dir():
        try:
            folder = _ask_dir("选择导出目录")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500,
                                detail="无法打开系统文件夹对话框（%s）；可以手动输入路径" % exc)
        return {"path": folder, "canceled": not folder}

    # ------------------------------------------------------------------- pages
    @app.post("/api/pages/add")
    def api_pages_add(request: Request, body: Any = Body(default_factory=dict)):
        store: ProjectStore = request.app.state.store
        data = body if isinstance(body, dict) else {}
        try:
            if data.get("paths"):
                raw_paths = data.get("paths")
                if isinstance(raw_paths, str):
                    raw_paths = [raw_paths]
                added, skipped = store.add_paths(raw_paths)
            elif data.get("folder"):
                added, skipped = store.add_folder(
                    data.get("folder"), data.get("pattern") or "",
                    recursive=bool(data.get("recursive")))
            else:
                raise HTTPException(status_code=400,
                                    detail="请提供 paths（文件列表）或 folder（文件夹路径）")
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not added and skipped:
            reasons = [str(s.get("reason") or "") for s in skipped]
            invalid = all(("文件不存在" in r) or ("路径不合法" in r) or
                          ("不是支持的图片格式" in r) for r in reasons)
            if invalid:
                raise HTTPException(
                    status_code=400,
                    detail="没有可以导入的图片：%s（共 %d 个路径无效，已忽略）"
                           % (reasons[0], len(reasons)))
        return {"added": added, "skipped": skipped, "pages": store.state_pages()}

    @app.delete("/api/pages/{pid}")
    def api_pages_delete(pid: str, request: Request):
        store: ProjectStore = request.app.state.store
        try:
            store.remove_page(pid)
        except StoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True, "pages": store.state_pages()}

    @app.get("/api/pages/{pid}")
    def api_page_detail(pid: str, request: Request):
        store: ProjectStore = request.app.state.store
        entry = store.get_page(pid)
        if not entry:
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        page = store.load_page_json(pid)
        boxes = store.boxes(pid)
        w, h = store.dims(pid)
        if w <= 0 or h <= 0:                       # stale/partial project data
            try:
                w, h = pipeline._image_size(entry.get("src") or "")
                store.update_page(pid, width=w, height=h)
            except Exception:
                w, h = w or 0, h or 0
        return {
            "pid": pid,
            "name": entry.get("name") or "",
            "src": entry.get("src") or "",
            "status": entry.get("status"),
            "status_label": store_mod.STATUS_LABELS.get(entry.get("status"), entry.get("status")),
            "error": entry.get("error") or "",
            "width": w,
            "height": h,
            "reviewed": bool(entry.get("reviewed")),
            "n_boxes": len(boxes),
            "page": {"width": w, "height": h,
                     "columns": _page_columns(store, pid),
                     "source": page.get("source") or entry.get("src") or "",
                     "w": w, "h": h},
            "boxes": boxes,
            "preview_url": "/api/pages/%s/preview.png?w=%d" % (pid, DEFAULT_PREVIEW_W),
            "has_preview": _preview_ok(store.preview_path(pid)),
            "refcheck": (store.refcheck(pid).get("stats") or {}),
            "userbank": _userbank_stats(),
            "glyphbank": _glyphbank_available(),
        }

    @app.get("/api/pages/{pid}/preview.png")
    def api_preview(pid: str, request: Request,
                    w: int = Query(DEFAULT_PREVIEW_W), boxes: str = "",
                    rebuild: int = 0):
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        w = max(200, min(4000, int(w or DEFAULT_PREVIEW_W)))
        try:
            _ensure_preview(store, pid, width=max(DEFAULT_PREVIEW_W, w),
                            rebuild=bool(rebuild))
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="生成预览图失败：%s" % exc)
        p = store.preview_path(pid)
        try:
            with open(p, "rb") as f:
                data = f.read()
            if abs(w - DEFAULT_PREVIEW_W) <= 1:
                return _png_response(data)
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                im = im.convert("RGB")
                ow, oh = im.size
                nh = max(1, int(round(oh * (w / float(ow)))))
                small = im.resize((w, nh), Image.LANCZOS)
                buf = io.BytesIO()
                small.save(buf, format="PNG", compress_level=3)
            return _png_response(buf.getvalue())
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="读取预览图失败：%s" % exc)

    @app.get("/api/pages/{pid}/crop")
    def api_crop(pid: str, request: Request,
                 x0: Optional[int] = Query(None), y0: Optional[int] = Query(None),
                 x1: Optional[int] = Query(None), y1: Optional[int] = Query(None),
                 scale: float = Query(2.0)):
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        if None in (x0, y0, x1, y1):
            raise HTTPException(status_code=400,
                                detail="缺少裁剪范围参数：需要 x0、y0、x1、y1（原图像素）")
        try:
            png = pipeline.render_crop(store, pid, x0, y0, x1, y1, scale)
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="裁图失败：%s" % exc)
        return _png_response(png)

    @app.put("/api/pages/{pid}/boxes")
    def api_boxes_put(pid: str, request: Request, body: Any = Body(None)):
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        if body is None:
            raise HTTPException(status_code=400, detail="缺少框数据（body 应为数组或 {boxes: [...]}）")
        if isinstance(body, dict):
            raw_boxes = body.get("boxes")
        elif isinstance(body, (list, tuple)):
            raw_boxes = body
        else:
            raise HTTPException(status_code=400, detail="框数据格式不对：应当是数组")
        if raw_boxes is None:
            raise HTTPException(status_code=400, detail="缺少 boxes 数组")
        try:
            boxes, warns = store.save_boxes(pid, raw_boxes)
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        entry = store.get_page(pid)
        return {"boxes": boxes, "warnings": warns, "n_boxes": len(boxes),
                "status": entry.get("status"),
                "status_label": store_mod.STATUS_LABELS.get(entry.get("status"), ""),
                "reviewed": bool(entry.get("reviewed"))}

    # -------------------------------------------------------------------- jobs
    @app.post("/api/pages/process")
    def api_pages_process(request: Request, body: Any = Body(default_factory=dict)):
        store: ProjectStore = request.app.state.store
        data = body if isinstance(body, dict) else {}
        ids = _resolve_ids(store, data.get("ids", "all"))
        if not ids:
            raise HTTPException(status_code=400, detail="没有可处理的页面：请先导入 TIF 扫描页")
        force = bool(data.get("force"))
        job_id = request.app.state.jobs.submit(
            "process", _process_job(store, ids, force), total=len(ids),
            message="排队中：%d 页" % len(ids), meta={"ids": ids, "force": force})
        return {"job_id": job_id, "ids": ids, "total": len(ids)}

    @app.get("/api/jobs/{jid}")
    def api_jobs_get(jid: str, request: Request):
        job = request.app.state.jobs.get(jid)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在或已被清理：%s" % jid)
        return job

    @app.get("/api/jobs")
    def api_jobs_list(request: Request, limit: int = Query(20)):
        return {"jobs": request.app.state.jobs.list(limit)}

    @app.post("/api/jobs/{jid}/cancel")
    def api_jobs_cancel(jid: str, request: Request):
        job = request.app.state.jobs.get(jid)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在或已被清理：%s" % jid)
        ok = request.app.state.jobs.cancel(jid)
        return {"ok": ok, "state": request.app.state.jobs.get(jid).get("state")}

    # ------------------------------------------------------------------ export
    def _start_export(request: Request, pid: str, body: Any):
        store: ProjectStore = request.app.state.store
        cfg = request.app.state.config
        data = body if isinstance(body, dict) else {}
        if pid and pid != "all":
            if not store.has_page(pid):
                raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
            ids = [pid]
        else:
            ids = _resolve_ids(store, data.get("ids", "all"), only_with_boxes=True)
            if not ids:
                raise HTTPException(status_code=400,
                                    detail="没有可导出的页面：请先处理页面（有框的页才能导出）")
        out = str(data.get("out") or "").strip() or str(pipeline.default_export_dir(store))
        # Server-side fallbacks are always the documented DEFAULT_EXPORT (app/config.py),
        # never whatever a stale config.json happens to contain; the UI sends explicit
        # values from settings.defaults when the user changed them.
        defaults = dict(config_mod.DEFAULT_EXPORT)
        try:
            pad_ratio = max(0.0, min(0.5, float(data.get("pad_ratio", defaults.get("pad_ratio", 0.10)))))
        except (TypeError, ValueError):
            pad_ratio = float(defaults.get("pad_ratio", 0.10))
        try:
            min_pad = max(0, min(200, int(data.get("min_pad", defaults.get("min_pad", 6)))))
        except (TypeError, ValueError):
            min_pad = int(defaults.get("min_pad", 6))
        order = str(data.get("order") or defaults.get("order") or "visual").lower()
        if order not in pipeline.ORDER_VALUES:
            order = "visual"
        fmt = str(data.get("fmt") or defaults.get("fmt") or "png").lower().lstrip(".")
        if fmt not in pipeline.FMT_VALUES:
            fmt = "png"
        job_id = request.app.state.jobs.submit(
            "export", _export_job(store, ids, out, cfg, pad_ratio, min_pad, order, fmt),
            total=len(ids), message="排队中：导出 %d 页" % len(ids),
            meta={"ids": ids, "out": out, "order": order, "fmt": fmt,
                  "pad_ratio": pad_ratio, "min_pad": min_pad})
        return {"job_id": job_id, "ids": ids, "total": len(ids), "out": out,
                "order": order, "fmt": fmt, "pad_ratio": pad_ratio, "min_pad": min_pad}

    @app.post("/api/pages/{pid}/export")
    def api_export_page(pid: str, request: Request, body: Any = Body(default_factory=dict)):
        return _start_export(request, pid, body)

    @app.post("/api/export")
    def api_export_all(request: Request, body: Any = Body(default_factory=dict)):
        return _start_export(request, "all", body)

    # ------------------------------------------------- standard glyph reference
    @app.get("/api/pages/{pid}/refcheck")
    def api_refcheck_get(pid: str, request: Request):
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        data = store.refcheck(pid)
        return {"available": bool(data), "result": data.get("boxes") and data or {},
                "stats": (data.get("stats") or {}),
                "proposals": data.get("proposals") or [],
                "glyphbank": _glyphbank_available()}

    @app.post("/api/pages/{pid}/refcheck")
    def api_refcheck_run(pid: str, request: Request):
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        if not _glyphbank_available():
            raise HTTPException(status_code=400, detail="规范字库未安装（data/glyphbank/bank.npz）")
        job_id = request.app.state.jobs.submit(
            "refcheck", _refcheck_job(request.app, pid), total=1,
            message="排队中：规范字校验", meta={"pid": pid})
        return {"job_id": job_id, "pid": pid}

    @app.put("/api/pages/{pid}/columns")
    def api_put_columns(pid: str, request: Request, body: Any = Body(default_factory=dict)):
        """User-adjusted column boundaries (drag the guide lines on the canvas)."""
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        data = body if isinstance(body, dict) else {}
        page = store.load_page_json(pid)
        if data.get("reset"):
            page = dict(page)
            page.pop("columns_refined", None)
            page["columns_manual"] = False
            store.save_page_json(pid, page)
            cols = pipeline.ensure_columns(store, pid, page)
            return {"columns": [{"idx": int(c.get("idx", i)), "x0": int(c["x0"]), "x1": int(c["x1"])}
                                   for i, c in enumerate(cols or [])]}
        raw = data.get("columns")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(status_code=400, detail="缺少 columns")
        W, H = store.dims(pid)
        clean = []
        for c in raw[:64]:
            try:
                x0, x1 = int(round(float(c["x0"]))), int(round(float(c["x1"])))
            except (KeyError, TypeError, ValueError):
                continue
            x0, x1 = max(0, min(W, x0)), max(0, min(W, x1))
            if x1 - x0 < 60:
                raise HTTPException(status_code=400, detail="列宽太窄（<60px）")
            clean.append((x0, x1))
        clean.sort(key=lambda t: t[0])
        for a, b in zip(clean, clean[1:]):
            if b[0] < a[1] - 1:
                raise HTTPException(status_code=400, detail="列区间重叠")
        cols = [{"idx": i, "x0": a, "x1": b, "w": b - a} for i, (a, b) in enumerate(clean)]
        page = dict(page)
        page["columns"] = cols
        page["columns_manual"] = True
        store.save_page_json(pid, page)
        return {"columns": [{"idx": c["idx"], "x0": c["x0"], "x1": c["x1"]} for c in cols]}

    @app.post("/api/pages/{pid}/score-box")
    def api_score_box(pid: str, request: Request, body: Any = Body(default_factory=dict)):
        """Re-score ONE box (called automatically after the user edits that box)."""
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        if not _glyphbank_available():
            raise HTTPException(status_code=400, detail="规范字库未安装")
        data = body if isinstance(body, dict) else {}
        boxes = data.get("boxes")
        if not isinstance(boxes, list) or not boxes:
            raise HTTPException(status_code=400, detail="缺少 boxes")
        try:
            scored = pipeline.score_boxes(store, pid, boxes[:64])
        except store_mod.StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="规范字校验失败：%s" % exc)
        return {"boxes": scored}

    @app.get("/api/glyphref/{idx}.png")
    def api_glyphref(idx: int):
        try:
            from . import yi_ref
            data = yi_ref.ref_png(idx, scale=4)
        except IndexError:
            raise HTTPException(status_code=404, detail="字形索引越界：%s" % idx)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="读取参考字形失败：%s" % exc)
        return Response(content=data, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.post("/api/pages/{pid}/harvest")
    def api_harvest(pid: str, request: Request):
        """把这一页「已确认」的框收进自建字库（切好的单字字形）。"""
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        try:
            added = pipeline.harvest_confirmed(store, pid)
        except store_mod.StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="收录失败：%s" % exc)
        from . import userbank
        return {"added": int(added), "userbank": userbank.stats()}

    @app.post("/api/harvest_all")
    def api_harvest_all(request: Request):
        """把项目里所有「手工改过」的页一次收录进自建字库。"""
        store: ProjectStore = request.app.state.store
        from . import userbank
        done, added_total, skipped = [], 0, []
        for entry in store.pages():
            pid = entry.get("id")
            name = entry.get("name") or pid
            if not pid:
                continue
            try:
                edited = pipeline.page_is_edited(store, pid)
            except Exception:
                edited = False
            if not edited:
                skipped.append(name)
                continue
            try:
                n = pipeline.harvest_confirmed(store, pid)
                done.append({"name": name, "added": int(n)})
                added_total += int(n)
            except Exception as exc:  # noqa: BLE001
                skipped.append("%s（%s）" % (name, exc))
        return {"added": added_total, "pages": done, "skipped": skipped,
                "userbank": userbank.stats()}

    @app.get("/api/userbank")
    def api_userbank():
        from . import userbank
        return userbank.stats()

    @app.post("/api/pages/{pid}/import_out")
    def api_import_out(pid: str, request: Request, body: Any = Body(default_factory=dict)):
        """用 out/<页>/index.csv 里的框坐标覆盖这一页（以手工导出为准）。"""
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        entry = store.get_page(pid)
        src = entry.get("src") or ""
        data = body if isinstance(body, dict) else {}
        want = (data.get("dir") or "").strip()
        name = os.path.splitext(os.path.basename(src))[0]

        def find_page_dir(base):
            if not base or not os.path.isdir(base):
                return ""
            if os.path.isfile(os.path.join(base, "index.csv")):
                return base
            sub = os.path.join(base, name)
            if os.path.isfile(os.path.join(sub, "index.csv")):
                return sub
            for cand in os.listdir(base):
                if cand == name and os.path.isfile(os.path.join(base, cand, "index.csv")):
                    return os.path.join(base, cand)
            return ""

        candidates = []
        if want:
            candidates.append(want)
            candidates.append(os.path.dirname(want))
        here = os.path.dirname(src)
        for _ in range(4):
            if not here or here == os.path.dirname(here):
                break
            candidates.append(os.path.join(here, "out"))
            candidates.append(here)
            here = os.path.dirname(here)
        page_dir = ""
        for cand in candidates:
            page_dir = find_page_dir(cand)
            if page_dir:
                break
        if not page_dir:
            raise HTTPException(status_code=400,
                                detail="没找到 %s 的 out 导出目录（可把 out 文件夹路径填进来）" % name)

        csv_path = os.path.join(page_dir, "index.csv")
        boxes = []
        try:
            import csv as _csv
            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                for r in _csv.DictReader(f):
                    if "gx0" in r and r.get("gx0"):
                        boxes.append({"x0": int(float(r["gx0"])), "y0": int(float(r["gy0"])),
                                      "x1": int(float(r["gx1"])), "y1": int(float(r["gy1"]))})
                    else:
                        boxes.append({"x0": int(float(r["x0"])), "y0": int(float(r["y0"])),
                                      "x1": int(float(r["x1"])), "y1": int(float(r["y1"]))})
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="读不了 index.csv：%s" % exc)
        if not boxes:
            raise HTTPException(status_code=400, detail="index.csv 里没有框")
        store.save_boxes(pid, boxes, width=entry.get("w"), height=entry.get("h"))
        return {"ok": True, "count": len(boxes), "dir": page_dir,
                "boxes": [{"x0": b["x0"], "y0": b["y0"], "x1": b["x1"], "y1": b["y1"],
                           "id": i + 1} for i, b in enumerate(boxes)]}

    @app.post("/api/project/open")
    def api_project_open(request: Request, body: Any = Body(default_factory=dict)):
        data = body if isinstance(body, dict) else {}
        raw = str(data.get("path") or "").strip()
        if not raw:
            raise HTTPException(status_code=400, detail="请提供项目目录路径")
        try:
            new_root = Path(os.path.abspath(os.path.expanduser(raw)))
            new_root.mkdir(parents=True, exist_ok=True)
            new_store = ProjectStore(new_root)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="打开项目失败：%s" % exc)
        request.app.state.store = new_store
        cfg = request.app.state.config
        cfg.last_project = str(new_store.root)
        try:
            cfg.save()
        except Exception:
            log.warning("config save failed", exc_info=True)
        return {"ok": True, "state": _state(request.app)}

    # ------------------------------------------------------------------- misc
    # (job factories below)
    @app.post("/api/settings")
    def api_settings(request: Request, body: Any = Body(default_factory=dict)):
        cfg = request.app.state.config
        data = body if isinstance(body, dict) else {}
        defaults = data.get("defaults")
        if defaults is None:
            defaults = data.get("export")
        if isinstance(defaults, dict):
            if "pad_ratio" in defaults:
                try:
                    cfg.export["pad_ratio"] = max(0.0, min(0.5, float(defaults["pad_ratio"])))
                except (TypeError, ValueError):
                    pass
            if "min_pad" in defaults:
                try:
                    cfg.export["min_pad"] = max(0, min(200, int(defaults["min_pad"])))
                except (TypeError, ValueError):
                    pass
            if "order" in defaults and str(defaults["order"]) in pipeline.ORDER_VALUES:
                cfg.export["order"] = str(defaults["order"])
            if "fmt" in defaults and str(defaults["fmt"]).lower().lstrip(".") in pipeline.FMT_VALUES:
                cfg.export["fmt"] = str(defaults["fmt"]).lower().lstrip(".")
        try:
            cfg.save()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="保存设置失败：%s" % exc)
        pub = cfg.public()
        return {"ok": True, "settings": {"defaults": pub["export"]}}

    @app.get("/api/settings")
    def api_settings_get(request: Request):
        pub = request.app.state.config.public()
        return {"settings": {"defaults": pub["export"]}}

    # unknown /api/* -> Chinese JSON 404 (must be registered before the static mount)
    @app.api_route("/api/{rest:path}",
                   methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    def api_missing(rest: str):
        raise HTTPException(status_code=404, detail="接口不存在：/api/%s" % rest)

    # ------------------------------------------------------------------ static
    static = Path(paths.static_dir())
    try:
        static.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    @app.get("/", include_in_schema=False)
    def root_index():
        idx = static / "index.html"
        if idx.is_file():
            return FileResponse(str(idx), media_type="text/html; charset=utf-8",
                                headers={"Cache-Control": "no-store"})
        return HTMLResponse(PLACEHOLDER_HTML)

    app.mount("/", StaticFiles(directory=str(static), html=True, check_dir=False), name="static")

    # -------------------------------------------------------------- exceptions
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        parts = []
        for err in (exc.errors() or [])[:3]:
            loc = [str(x) for x in (err.get("loc") or []) if x not in ("body", "query", "path")]
            parts.append("%s: %s" % (".".join(loc) or "参数", err.get("msg") or "不合法"))
        return JSONResponse(status_code=422,
                            content={"detail": "请求参数不合法：" + "；".join(parts)})

    @app.exception_handler(StoreError)
    async def _store_error(request: Request, exc: StoreError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500,
                            content={"detail": "服务器内部错误：%s" % (str(exc) or exc.__class__.__name__)})

    return app


# ---------------------------------------------------------------- self test
def _selftest() -> int:
    import tempfile
    import shutil
    from fastapi.testclient import TestClient

    ok = fail = 0
    def check(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("PASS", label, flush=True)
        else:
            fail += 1
            print("FAIL", label, flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="yi_server_"))
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    # never touch the real %LOCALAPPDATA%/yi-segment/config.json
    os.environ["YI_SEGMENT_HOME"] = str(home)
    os.environ["YI_SEGMENT_NO_DIALOG"] = "1"
    try:
        app = create_app(str(tmp / "proj"))
        client = TestClient(app)

        r = client.get("/api/health")
        check(r.status_code == 200 and r.json().get("ok"), "health ok")
        st = client.get("/api/state")
        body = st.json()
        check(st.status_code == 200 and body["pages"] == [], "empty state")
        check(body["project"]["path"].endswith("proj"), "project path")
        check("defaults" in body["settings"], "settings in state")

        r = client.post("/api/settings", json={"defaults": {"pad_ratio": 0.2, "order": "reading"}})
        check(r.status_code == 200 and r.json()["settings"]["defaults"]["pad_ratio"] == 0.2,
              "settings saved")
        r = client.get("/api/state")
        check(r.json()["settings"]["defaults"]["order"] == "reading", "settings persisted")

        r = client.post("/api/settings", json={"defaults": {"pad_ratio": 99, "order": "bogus"}})
        check(r.status_code == 200, "bad defaults tolerated")
        r = client.post("/api/settings", json={"ai": {"api_key": ""}})
        check(r.status_code == 200, "empty api_key ignored")

        r = client.post("/api/pages/add", json={"paths": []})
        check(r.status_code == 400 and "paths" in r.json()["detail"], "add without paths -> 400")
        r = client.post("/api/pages/add", json={"paths": [str(tmp / "nope.tif")]})
        check(r.status_code == 400 and "没有可以导入" in r.json()["detail"],
              "add all-invalid paths -> 400 chinese")
        r = client.post("/api/pages/process", json={"ids": "all"})
        check(r.status_code == 400 and "页面" in r.json()["detail"], "process no pages -> 400")

        from PIL import Image as _Image
        good = tmp / "good.png"
        _Image.new("RGB", (20, 20), (255, 255, 255)).save(good)
        r = client.post("/api/pages/add", json={"paths": [str(good), str(tmp / "nope.tif")]})
        check(r.status_code == 200 and len(r.json()["added"]) == 1,
              "partially invalid paths -> 200 + skipped")
        good_pid = (r.json().get("added") or [""])[0]
        r = client.delete("/api/pages/%s" % good_pid)
        check(r.status_code == 200, "delete unprocessed page")

        r = client.get("/api/pages/nope")
        check(r.status_code == 404 and "不存在" in r.json()["detail"], "unknown page 404 chinese")
        r = client.get("/api/pages/nope/preview.png")
        check(r.status_code == 404, "unknown page preview 404")
        r = client.get("/api/jobs/nope")
        check(r.status_code == 404, "unknown job 404")
        r = client.get("/api/pages/nope/crop?x0=0&y0=0&x1=10&y1=10")
        check(r.status_code == 404, "unknown page crop 404")
        r = client.get("/api/nope")
        check(r.status_code == 404 and r.json()["detail"].startswith("接口不存在"),
              "unknown api 404 json")
        r = client.get("/api/pages/nope/crop?x0=a")
        check(r.status_code in (404, 422), "bad crop params tolerated")
        r = client.post("/api/export", json={})
        check(r.status_code == 400 and "导出" in r.json()["detail"], "export nothing -> 400")

        r = client.get("/api/dialog/files")
        check(r.status_code == 200 and r.json()["paths"] == [], "dialog disabled -> empty")
        r = client.get("/api/dialog/folder")
        check(r.status_code == 200 and r.json()["path"] == "", "folder dialog disabled")

        r = client.post("/api/project/open", json={"path": str(tmp / "proj2")})
        check(r.status_code == 200 and r.json()["ok"], "project open creates dir")
        check((tmp / "proj2" / "project.json").is_file(), "project.json created")
        r = client.post("/api/project/open", json={"path": ""})
        check(r.status_code == 400, "project open without path -> 400")

        r = client.get("/")
        check(r.status_code == 200 and "彝文切字工具" in r.text, "root serves placeholder")
        r = client.get("/api/docs")
        check(r.status_code == 200, "docs available")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("YI_SEGMENT_NO_DIALOG", None)
        os.environ.pop("YI_SEGMENT_HOME", None)

    print("server selftest: %d passed, %d failed" % (ok, fail), flush=True)
    return 0 if fail == 0 else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="app.server", description="FastAPI 后端服务")
    ap.add_argument("--selftest", action="store_true", help="离线冒烟自测（TestClient）")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
