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
from . import store as store_mod
from .store import ProjectStore, StoreError

log = logging.getLogger("app.server")

VERSION = "1.0"
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
def _ai_module():
    """Import app/ai_refine.py lazily (it is being written in parallel)."""
    try:
        from . import ai_refine
        return ai_refine
    except Exception:
        return None


def _ai_available() -> bool:
    return _ai_module() is not None


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
        "settings": {"ai": pub["ai"], "defaults": pub["export"]},
        "ai_available": _ai_available(),
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


def _ai_refine_job(app: FastAPI, pid: str, ids_spec, mode: str):
    st: ProjectStore = app.state.store

    def fn(job):
        ai = _ai_module()
        if ai is None:
            raise RuntimeError("AI 复核模块未就绪（app/ai_refine.py 缺失或导入失败）")
        entry = st.get_page(pid)
        if not entry:
            raise RuntimeError("页面不存在：%s" % pid)
        boxes = st.boxes(pid)
        if not boxes:
            raise RuntimeError("这一页还没有框，无法复核（请先处理这一页）")
        if isinstance(ids_spec, str) and ids_spec.strip().lower() == "suspect":
            target = pipeline.suspect_ids(boxes)
        elif ids_spec is None or (isinstance(ids_spec, str) and ids_spec.strip().lower() == "all"):
            target = [int(b["id"]) for b in boxes]
        elif isinstance(ids_spec, (list, tuple)):
            valid = {int(b["id"]) for b in boxes}
            target = []
            for raw in ids_spec:
                try:
                    n = int(raw)
                except (TypeError, ValueError):
                    continue
                if n in valid and n not in target:
                    target.append(n)
        else:
            target = [int(b["id"]) for b in boxes]
        if not target:
            raise RuntimeError("没有需要复核的框（可疑框为空，可以先在界面上手动标记存疑）")
        src = entry.get("src") or ""
        if not os.path.isfile(src):
            raise RuntimeError("源文件不存在：%s" % src)
        cfg = app.state.config.ai_config()
        job.update(total=len(target), done=0,
                   message="准备复核 %d 个框" % len(target),
                   current={"pid": pid, "frac": 0.0, "stage": "读取原图"})
        rgb = pipeline.C.load_rgb(src)
        job.check()
        job.update(current={"pid": pid, "frac": 0.03, "stage": "请求模型"})

        def prog(*args, **kwargs):
            nums = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
            strings = [a for a in args if isinstance(a, str)]
            if len(nums) >= 2 and nums[1]:
                frac = float(nums[0]) / float(nums[1])
                job.update(done=int(nums[0]), total=int(nums[1]),
                           current={"pid": pid, "frac": max(0.03, min(0.98, frac)),
                                    "stage": strings[0] if strings else "复核中"})
            elif len(nums) == 1:
                job.update(current={"pid": pid, "frac": max(0.03, min(0.98, float(nums[0]))),
                                    "stage": strings[0] if strings else "复核中"})

        try:
            result = ai.refine_boxes(rgb, boxes, ids=target, mode=mode or "all", cfg=cfg,
                                     progress_cb=prog, cancel_cb=job.is_canceled)
        except TypeError:
            result = ai.refine_boxes(rgb, boxes, ids=target, cfg=cfg,
                                     progress_cb=prog, cancel_cb=job.is_canceled)
        job.check()
        clean = []
        for raw in result or []:
            item = _clean_proposal(raw, boxes)
            if item:
                clean.append(item)
        meta = {"mode": mode or "all", "ids": target, "model": getattr(cfg, "model", ""),
                "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        st.save_proposals(pid, clean, meta=meta)
        st.update_page(pid, ai_refined=True)
        job.update(done=len(target), current={"pid": pid, "frac": 1.0},
                   message="复核完成：%d 条建议" % len(clean))
        job.add_result({"pid": pid, "ok": True, "name": entry.get("name") or "",
                        "n_proposals": len(clean), "n_targets": len(target), "mode": meta["mode"]})
        return []

    return fn


def _apply_proposals(boxes: list, proposals: list, apply_idx, reject_idx, apply_all: bool):
    """Apply AI proposal indexes to boxes; returns (new boxes, proposals, warnings)."""
    warnings = []
    props = [dict(p) for p in (proposals or [])]
    accepted = set()
    rejected = set()
    if apply_all:
        for i, p in enumerate(props):
            if p.get("status", "pending") == "pending" and p.get("action") != "keep":
                accepted.add(i)
            elif p.get("status", "pending") == "pending":
                accepted.add(i)
    for raw in apply_idx or []:
        try:
            accepted.add(int(raw))
        except (TypeError, ValueError):
            warnings.append("忽略一个非数字的接受索引")
    for raw in reject_idx or []:
        try:
            rejected.add(int(raw))
        except (TypeError, ValueError):
            warnings.append("忽略一个非数字的拒绝索引")

    by_id = {}
    order = []
    for b in boxes or []:
        nb = dict(b)
        nb["id"] = int(nb.get("id") or (len(order) + 1))
        by_id[nb["id"]] = nb
        order.append(nb["id"])
    next_id = (max(order) if order else 0) + 1

    def insert_after(ref_id, newbox):
        nonlocal next_id
        nb = dict(newbox)
        nb["id"] = next_id
        next_id += 1
        by_id[nb["id"]] = nb
        if ref_id in order:
            order.insert(order.index(ref_id) + 1, nb["id"])
        else:
            order.append(nb["id"])
        return nb

    def remove(box_id):
        by_id.pop(box_id, None)
        if box_id in order:
            order.remove(box_id)

    for i, prop in enumerate(props):
        if i not in accepted:
            if i in rejected:
                prop["status"] = "rejected"
            continue
        action = prop.get("action") or "keep"
        try:
            box_id = int(prop.get("box_id"))
        except (TypeError, ValueError):
            warnings.append("建议缺少 box_id，已跳过")
            prop["status"] = "rejected"
            continue
        if box_id not in by_id:
            warnings.append("建议引用的框 %s 已不存在，已跳过" % box_id)
            prop["status"] = "rejected"
            continue
        after = [a for a in (prop.get("after") or []) if isinstance(a, dict)]
        target = by_id[box_id]
        if action == "keep" or not after:
            pass
        elif action == "tighten":
            box = after[0]
            target.update({"x0": box["x0"], "y0": box["y0"], "x1": box["x1"], "y1": box["y1"]})
        elif action == "split" and len(after) >= 2:
            first = after[0]
            target.update({"x0": first["x0"], "y0": first["y0"],
                           "x1": first["x1"], "y1": first["y1"],
                           "flags": list(dict.fromkeys((target.get("flags") or []) + ["split"]))})
            for extra in after[1:]:
                insert_after(box_id, {"x0": extra["x0"], "y0": extra["y0"],
                                      "x1": extra["x1"], "y1": extra["y1"],
                                      "flags": ["split"], "review": 0, "note": ""})
        elif action == "merge":
            merged = after[0]
            cx = (merged["x0"] + merged["x1"]) / 2.0
            cy = (merged["y0"] + merged["y1"]) / 2.0
            best = None
            bestd = None
            for oid in list(order):
                if oid == box_id:
                    continue
                ob = by_id.get(oid)
                if not ob:
                    continue
                ocx = (ob["x0"] + ob["x1"]) / 2.0
                ocy = (ob["y0"] + ob["y1"]) / 2.0
                inside = (merged["x0"] - 4 <= ocx <= merged["x1"] + 4 and
                          merged["y0"] - 4 <= ocy <= merged["y1"] + 4)
                if not inside:
                    continue
                d = (ocx - cx) ** 2 + (ocy - cy) ** 2
                if bestd is None or d < bestd:
                    best, bestd = oid, d
            if best is None:
                warnings.append("合并建议找不到相邻框，按收紧处理（框 %s）" % box_id)
            else:
                remove(best)
            target.update({"x0": merged["x0"], "y0": merged["y0"],
                           "x1": merged["x1"], "y1": merged["y1"],
                           "flags": list(dict.fromkeys((target.get("flags") or []) + ["merged"]))})
        prop["status"] = "accepted"
    new_boxes = [by_id[i] for i in order if i in by_id]
    return new_boxes, props, warnings


# ---------------------------------------------------------------------- app
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
                "project": str(store.root), "tools": pipeline.ensure_tools(),
                "ai_available": _ai_available(),
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
        props = store.proposals(pid)
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
            "ai_refined": bool(entry.get("ai_refined")),
            "n_boxes": len(boxes),
            "page": {"width": w, "height": h,
                     "columns": page.get("columns") or [],
                     "source": page.get("source") or entry.get("src") or "",
                     "w": w, "h": h},
            "boxes": boxes,
            "preview_url": "/api/pages/%s/preview.png?w=%d" % (pid, DEFAULT_PREVIEW_W),
            "has_preview": _preview_ok(store.preview_path(pid)),
            "n_proposals": len(props.get("proposals") or []),
            "ai_available": _ai_available(),
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

    # --------------------------------------------------------------- proposals
    @app.get("/api/pages/{pid}/proposals")
    def api_proposals_get(pid: str, request: Request):
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        data = store.proposals(pid)
        cfg = request.app.state.config
        return {"proposals": data.get("proposals") or [],
                "meta": data.get("meta") or {},
                "ai_available": _ai_available(),
                "configured": bool(cfg.ai.get("api_key"))}

    @app.post("/api/pages/{pid}/proposals")
    def api_proposals_apply(pid: str, request: Request, body: Any = Body(default_factory=dict)):
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        data = body if isinstance(body, dict) else {}
        boxes = store.boxes(pid)
        data_props = store.proposals(pid)
        props = data_props.get("proposals") or []
        if not props:
            raise HTTPException(status_code=400, detail="没有 AI 建议可处理")
        new_boxes, props2, warns = _apply_proposals(
            boxes, props, data.get("apply") or [], data.get("reject") or [],
            bool(data.get("apply_all")))
        saved, norm_warns = store.save_boxes(pid, new_boxes)
        avoids = [w for w in warns if w]
        payload = store.save_proposals(pid, props2, meta=data_props.get("meta") or {})
        entry = store.get_page(pid)
        return {"boxes": saved, "proposals": payload.get("proposals") or [],
                "warnings": avoids + list(norm_warns),
                "status": entry.get("status"), "n_boxes": len(saved)}

    @app.post("/api/pages/{pid}/ai-refine")
    def api_ai_refine(pid: str, request: Request, body: Any = Body(default_factory=dict)):
        store: ProjectStore = request.app.state.store
        if not store.has_page(pid):
            raise HTTPException(status_code=404, detail="页面不存在：%s" % pid)
        data = body if isinstance(body, dict) else {}
        ids_spec = data.get("ids", "all")
        mode = str(data.get("mode") or "all")
        job_id = request.app.state.jobs.submit(
            "ai-refine", _ai_refine_job(request.app, pid, ids_spec, mode),
            total=0, message="排队中：AI 复核",
            meta={"pid": pid, "ids": ids_spec, "mode": mode})
        return {"job_id": job_id, "pid": pid, "mode": mode}

    # ---------------------------------------------------------------- settings
    @app.post("/api/settings")
    def api_settings(request: Request, body: Any = Body(default_factory=dict)):
        cfg = request.app.state.config
        data = body if isinstance(body, dict) else {}
        ai = data.get("ai")
        defaults = data.get("defaults")
        if defaults is None:
            defaults = data.get("export")
        if isinstance(ai, dict):
            for key in ("api_key", "base_url", "model", "max_workers", "timeout"):
                if key not in ai:
                    continue
                val = ai.get(key)
                if key == "api_key":
                    if val is None:
                        continue
                    text = str(val)
                    if text == "" or "…" in text:
                        continue
                    cfg.ai["api_key"] = text.strip()
                elif key == "max_workers":
                    try:
                        cfg.ai["max_workers"] = max(1, min(16, int(val)))
                    except (TypeError, ValueError):
                        pass
                elif key == "timeout":
                    try:
                        cfg.ai["timeout"] = max(5.0, min(300.0, float(val)))
                    except (TypeError, ValueError):
                        pass
                elif key == "base_url":
                    text = str(val).strip()
                    if text:
                        cfg.ai["base_url"] = text
                elif key == "model":
                    text = str(val).strip()
                    if text:
                        cfg.ai["model"] = text
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
        return {"ok": True, "settings": {"ai": pub["ai"], "defaults": pub["export"]}}

    @app.post("/api/ai/test")
    def api_ai_test(request: Request, body: Any = Body(default_factory=dict)):
        ai_mod = _ai_module()
        if ai_mod is None:
            return {"ok": False, "message": "AI 复核模块未就绪（app/ai_refine.py 缺失或导入失败）"}
        cfg = request.app.state.config
        data = body if isinstance(body, dict) else {}
        override = data.get("ai") if isinstance(data.get("ai"), dict) else {}
        values = dict(cfg.ai)
        for key in ("api_key", "base_url", "model", "max_workers", "timeout"):
            if key in override:
                val = override[key]
                if key == "api_key" and val is not None and "…" in str(val):
                    continue
                if val not in (None, ""):
                    values[key] = val
        if not values.get("api_key"):
            return {"ok": False,
                    "message": "还没有配置 API Key：请先在设置里填写 DeepSeek API Key"}
        try:
            kwargs = {k: values[k] for k in ("api_key", "base_url", "model",
                                             "max_workers", "timeout") if k in values}
            try:
                ai_cfg = ai_mod.AIConfig(**kwargs)
            except TypeError:
                ai_cfg = ai_mod.AIConfig(api_key=values.get("api_key", ""))
            result = ai_mod.test_connection(ai_cfg)
            if isinstance(result, dict):
                return {"ok": bool(result.get("ok")), "message": str(result.get("message") or "")}
            return {"ok": bool(result), "message": "连接成功" if result else "连接失败"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "测试连接失败：%s" % exc}

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
    @app.get("/api/settings")
    def api_settings_get(request: Request):
        pub = request.app.state.config.public()
        return {"settings": {"ai": pub["ai"], "defaults": pub["export"]},
                "ai_available": _ai_available()}

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
        check("defaults" in body["settings"] and "ai" in body["settings"], "settings in state")

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

        r = client.post("/api/pages/nope/proposals", json={"apply_all": True})
        check(r.status_code == 404, "proposals apply unknown page 404")
        r = client.post("/api/pages/nope/ai-refine", json={"ids": "all"})
        check(r.status_code == 404, "ai-refine unknown page 404")

        r = client.post("/api/ai/test", json={})
        check(r.status_code == 200 and r.json()["ok"] is False, "ai/test without key -> ok false")

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
