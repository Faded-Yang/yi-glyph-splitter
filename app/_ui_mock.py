"""_ui_mock.py - 前端开发用最小假后端（app-ui 私有，不属于交付后端）。

不依赖真实项目 / 分割算法：内存里合成几页纸面图和框，实现 SPEC-APP.md 的 REST 契约子集，
供 app/static 前端联调、交互测试与截图核对：

    python -m app._ui_mock --serve --port 8781     # 起假服务（自动挂 app/static）
    python -m app._ui_mock --selftest              # TestClient 接口自测
"""
from __future__ import annotations

import argparse
import io
import json
import math
import random
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw

from . import paths

PAGE_W, PAGE_H = 1500, 2100
COL_PITCH, ROW_PITCH = 215, 119
N_COL, N_ROW = 6, 16
MOCK_SCAN = "C:/mock/scan/彝族扫描3-0002_right.tif"


# --------------------------------------------------------------- 合成页面图像
def _draw_glyph(d: ImageDraw.ImageDraw, rnd: random.Random, cx: int, cy: int, w: int, h: int) -> None:
    x0, y0 = cx - w // 2, cy - h // 2
    for _ in range(rnd.randrange(2, 6)):
        shade = rnd.randrange(18, 72)
        c = (shade, shade, shade)
        k = rnd.randrange(3)
        if k == 0:
            x1 = x0 + rnd.randrange(8, w - 24)
            y1 = y0 + rnd.randrange(8, h - 18)
            d.line([x1, y1, x1 + rnd.randrange(18, w // 2), y1 + rnd.randrange(-10, 10)],
                   fill=c, width=rnd.randrange(3, 7))
        elif k == 1:
            x1 = x0 + rnd.randrange(10, max(12, w - 44))
            y1 = y0 + rnd.randrange(10, max(12, h - 28))
            d.ellipse([x1, y1, x1 + rnd.randrange(14, 42), y1 + rnd.randrange(14, 42)],
                      outline=c, width=3)
        else:
            x1 = x0 + rnd.randrange(10, max(12, w - 34))
            y1 = y0 + rnd.randrange(10, max(12, h - 26))
            d.rectangle([x1, y1, x1 + rnd.randrange(12, 38), y1 + rnd.randrange(10, 32)],
                        outline=c, width=2)


def make_page_image(seed: int) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", (PAGE_W, PAGE_H), (206, 198, 180))
    d = ImageDraw.Draw(img)
    for _ in range(700):
        x, y = rnd.randrange(PAGE_W), rnd.randrange(PAGE_H)
        r = rnd.randrange(20, 140)
        d.ellipse([x - r, y - r, x + r, y + r],
                  fill=rnd.choice([(199, 191, 173), (212, 205, 188), (191, 183, 166), (216, 210, 196)]))
    for i in range(N_COL + 1):
        x = 60 + i * COL_PITCH
        d.line([x, 50, x, PAGE_H - 50], fill=(184, 176, 160), width=1)
    for ci in range(N_COL):
        for ri in range(N_ROW):
            cx = 60 + ci * COL_PITCH + COL_PITCH // 2
            cy = 90 + ri * ROW_PITCH + ROW_PITCH // 2
            _draw_glyph(d, rnd, cx, cy, 150, 88)
    return img


def make_boxes(seed: int, n_col: int = N_COL, n_row: int = N_ROW) -> list:
    rnd = random.Random(seed * 7 + 1)
    out, gid = [], 0
    for ci in range(n_col):           # 列自左向右（与 out/page1 的 id 顺序一致）
        for ri in range(n_row):
            if rnd.random() < 0.06:
                continue
            gid += 1
            cx = 60 + ci * COL_PITCH + COL_PITCH // 2 + rnd.randrange(-6, 7)
            cy = 90 + ri * ROW_PITCH + ROW_PITCH // 2 + rnd.randrange(-6, 7)
            w = rnd.randrange(104, 150)
            h = rnd.randrange(62, 92)
            out.append({"id": gid,
                        "x0": cx - w // 2, "y0": cy - h // 2,
                        "x1": cx + w // 2, "y1": cy + h // 2,
                        "flags": [], "review": 0, "note": ""})
    return out


def _columns() -> list:
    return [{"idx": i, "x0": 60 + i * COL_PITCH, "x1": 60 + (i + 1) * COL_PITCH,
             "w": COL_PITCH, "ink": 1000 + i} for i in range(N_COL)]


# ------------------------------------------------------------------- 假项目状态
class MockState:
    def __init__(self) -> None:
        self.images: dict = {}
        self.pages: dict = {}
        self.order: list = []
        self.jobs: dict = {}
        self.lock = threading.Lock()
        self.proposals: dict = {}
        self.settings = {
            "ai": {"api_key": "", "base_url": "https://api.deepseek.com",
                   "model": "deepseek-flash", "max_workers": 4},
            "defaults": {"pad_ratio": 0.10, "min_pad": 6, "order": "visual", "fmt": "png"},
        }
        self._add_page("p0001", "彝族扫描3-0001_right.tif", 1, "processed", 1.0)
        self._add_page("p0002", "彝族扫描3-0002_right.tif", 2, "reviewing", 0.45)
        self._add_page("p0003", "彝族扫描3-0003_right.tif", 3, "pending", 0.0)
        self._add_page("p0004", "彝族扫描3-0004_right.tif", 4, "done", 1.0)

    def _add_page(self, pid: str, name: str, seed: int, status: str, reviewed_frac: float) -> None:
        img = make_page_image(seed)
        self.images[pid] = img
        boxes = make_boxes(seed) if status != "pending" else []
        rnd = random.Random(seed + 99)
        for b in boxes:
            if rnd.random() < reviewed_frac:
                b["review"] = 1
            elif rnd.random() < 0.08:
                b["review"] = 2
        self.pages[pid] = {
            "id": pid, "name": name, "src": MOCK_SCAN, "status": status, "error": "",
            "width": PAGE_W, "height": PAGE_H, "n_boxes": len(boxes),
            "reviewed": sum(1 for b in boxes if b["review"]),
            "ai_refined": False, "boxes": boxes,
        }
        self.order.append(pid)

    # ---------------------------------------------------------------- 汇总/查找
    def summary(self, pid: str) -> dict:
        p = self.pages[pid]
        d = {k: p[k] for k in ("id", "name", "src", "status", "error", "width", "height",
                              "n_boxes", "reviewed", "ai_refined")}
        d["status_label"] = {"pending": "未处理", "processed": "已处理",
                             "reviewing": "复核中", "done": "已完成", "error": "出错"}.get(p["status"], p["status"])
        return d

    def state(self) -> dict:
        ai = dict(self.settings["ai"])
        key = ai.get("api_key") or ""
        ai["configured"] = bool(key)
        ai["api_key"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else "")
        return {"project": {"name": "mock-project", "path": "C:/mock/project"},
                "pages": [self.summary(pid) for pid in self.order],
                "settings": {"ai": ai, "defaults": dict(self.settings["defaults"])}}

    def page_or_404(self, pid: str) -> dict:
        if pid not in self.pages:
            raise HTTPException(404, "页面不存在：" + pid)
        return self.pages[pid]

    # --------------------------------------------------------------- 后台任务
    def start_job(self, kind: str, total: int, step) -> str:
        with self.lock:
            jid = "j%04d" % (len(self.jobs) + 1)
            job = {"id": jid, "kind": kind, "state": "running", "done": 0, "total": max(total, 1),
                   "message": "开始", "errors": [], "results": [], "cancel": False, "current": None}
            self.jobs[jid] = job

        def worker() -> None:
            try:
                for i in range(job["total"]):
                    if job["cancel"]:
                        job["state"] = "canceled"
                        job["message"] = "已取消"
                        return
                    time.sleep(0.45)
                    msg = step(job, i)
                    job["done"] = i + 1
                    if msg:
                        job["message"] = msg
                job["state"] = "done"
                job["message"] = "完成"
            except Exception as exc:      # noqa: BLE001
                job["state"] = "error"
                job["message"] = "失败：" + str(exc)
                job["errors"].append(str(exc))

        threading.Thread(target=worker, daemon=True).start()
        return jid

    def preview_png(self, pid: str, w: int, with_boxes: bool) -> bytes:
        p = self.pages[pid]
        img = self.images[pid]
        if w and w < PAGE_W:
            h = int(round(w * PAGE_H / PAGE_W))
            img = img.resize((w, h), Image.LANCZOS)
        else:
            img = img.copy()
        if with_boxes:
            d = ImageDraw.Draw(img)
            k = img.width / PAGE_W
            for b in p["boxes"]:
                d.rectangle([b["x0"] * k, b["y0"] * k, b["x1"] * k, b["y1"] * k],
                            outline=(0, 200, 255), width=1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


STATE = MockState()
APP = FastAPI(title="yi-segment UI mock", docs_url="/api/docs")


# --------------------------------------------------------------------- 状态接口
@APP.get("/api/state")
def api_state() -> dict:
    return STATE.state()


@APP.get("/api/dialog/files")
def api_dialog_files() -> dict:
    return {"paths": ["C:/mock/scan/彝族扫描3-0005_right.tif",
                      "C:/mock/scan/彝族扫描3-0006_right.tif"]}


@APP.get("/api/dialog/folder")
def api_dialog_folder() -> dict:
    return {"path": "C:/mock/scan"}


@APP.get("/api/dialog/export_dir")
def api_dialog_export_dir() -> dict:
    return {"path": "C:/mock/out"}


@APP.post("/api/pages/add")
def api_pages_add(payload: dict = Body(...)) -> dict:
    paths_in = payload.get("paths") or []
    folder = payload.get("folder") or ""
    pattern = payload.get("pattern") or "*.tif"
    names = []
    if paths_in:
        names = [Path(p).name for p in paths_in]
    elif folder:
        names = ["彝族扫描3-000%d_right.tif" % i for i in (7, 8)]
    added = []
    for i, name in enumerate(names):
        pid = "p%04d" % (len(STATE.order) + 1)
        if any(p["name"] == name for p in STATE.pages.values()):
            continue
        STATE._add_page(pid, name, 11 + i, "new", 0.0)
        added.append(pid)
    return {"added": added, "pages": [STATE.summary(p) for p in STATE.order]}


@APP.delete("/api/pages/{pid}")
def api_page_delete(pid: str) -> dict:
    STATE.page_or_404(pid)
    STATE.order.remove(pid)
    STATE.pages.pop(pid, None)
    return {"ok": True}


@APP.post("/api/pages/process")
def api_pages_process(payload: dict = Body(...)) -> dict:
    sel = payload.get("ids", "all")
    ids = list(STATE.order) if sel in ("all", None, "") else [str(x) for x in sel]
    for pid in ids:
        STATE.page_or_404(pid)

    def step(job: dict, i: int) -> str:
        pid = ids[i]
        p = STATE.pages[pid]
        job["current"] = {"pid": pid, "frac": 1.0, "stage": "分割"}
        p["status"] = "processed"
        if not p["boxes"]:
            p["boxes"] = make_boxes(5 + i)
            p["n_boxes"] = len(p["boxes"])
        return "已处理 " + p["name"]

    return {"job_id": STATE.start_job("process", len(ids), step)}


@APP.get("/api/pages/{pid}")
def api_page(pid: str) -> dict:
    p = STATE.page_or_404(pid)
    props = STATE.proposals.get(pid) or []
    return {"page": {"w": p["width"], "h": p["height"], "columns": _columns(), "source": p["src"]},
            "boxes": [dict(b) for b in p["boxes"]],
            "preview_url": "/api/pages/" + pid + "/preview.png?w=1600",
            "status": p["status"], "name": p["name"],
            "n_proposals": len([x for x in props if x["status"] == "pending"]),
            "ai_available": True, "configured": bool(STATE.settings["ai"].get("api_key"))}


@APP.get("/api/pages/{pid}/preview.png")
def api_page_preview(pid: str, w: int = 1600, boxes: int = 1) -> Response:
    p = STATE.page_or_404(pid)
    if not p["boxes"]:
        raise HTTPException(404, "该页尚未处理，没有预览图")
    png = STATE.preview_png(pid, w, boxes != 0)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@APP.get("/api/pages/{pid}/crop")
def api_page_crop(pid: str, x0: int = 0, y0: int = 0, x1: int = 200, y1: int = 200,
                  scale: float = 2.0) -> Response:
    p = STATE.page_or_404(pid)
    x0 = max(0, min(x0, PAGE_W - 2))
    y0 = max(0, min(y0, PAGE_H - 2))
    x1 = max(x0 + 2, min(x1, PAGE_W))
    y1 = max(y0 + 2, min(y1, PAGE_H))
    scale = max(0.2, min(scale, 8.0))
    img = STATE.images[pid].crop((x0, y0, x1, y1))
    nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
    nw, nh = min(nw, 4000), min(nh, 4000)
    img = img.resize((nw, nh), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@APP.put("/api/pages/{pid}/boxes")
def api_page_put_boxes(pid: str, payload: dict = Body(...)) -> dict:
    p = STATE.page_or_404(pid)
    raw = payload.get("boxes") or []
    warnings = []
    clean = []
    for i, b in enumerate(raw, 1):
        try:
            x0 = int(round(float(b.get("x0", 0))))
            y0 = int(round(float(b.get("y0", 0))))
            x1 = int(round(float(b.get("x1", 0))))
            y1 = int(round(float(b.get("y1", 0))))
        except (TypeError, ValueError):
            warnings.append("第 " + str(i) + " 个框坐标无法解析，已跳过")
            continue
        x0, y0 = max(0, min(x0, PAGE_W - 2)), max(0, min(y0, PAGE_H - 2))
        x1, y1 = max(x0 + 2, min(x1, PAGE_W)), max(y0 + 2, min(y1, PAGE_H))
        if x1 - x0 < 4 or y1 - y0 < 4:
            warnings.append("第 " + str(i) + " 个框过小已跳过")
            continue
        clean.append({"id": len(clean) + 1, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                      "flags": [str(f) for f in (b.get("flags") or [])],
                      "review": int(b.get("review") or 0),
                      "note": str(b.get("note") or "")})
    p["boxes"] = clean
    p["n_boxes"] = len(clean)
    p["reviewed"] = sum(1 for b in clean if b["review"])
    if p["status"] in ("new", ""):
        p["status"] = "reviewing"
    elif p["status"] == "processed" and p["reviewed"]:
        p["status"] = "reviewing"
    return {"boxes": [dict(b) for b in clean], "warnings": warnings,
            "status": p["status"], "reviewed": p["reviewed"], "n_boxes": p["n_boxes"]}


def _gen_proposals(pid: str) -> list:
    p = STATE.pages[pid]
    out = []
    picks = [b for b in p["boxes"] if b["review"] == 0][:4] or p["boxes"][:4]
    acts = ["tighten", "split", "merge", "keep"]
    for i, b in enumerate(picks):
        act = acts[i % len(acts)]
        w, h = b["x1"] - b["x0"], b["y1"] - b["y0"]
        before = [b["x0"], b["y0"], b["x1"], b["y1"]]
        def rect(r):
            return {"x0": int(r[0]), "y0": int(r[1]), "x1": int(r[2]), "y1": int(r[3])}
        if act == "tighten":
            after = [rect([b["x0"] + int(w * 0.05), b["y0"] + int(h * 0.04),
                           b["x1"] - int(w * 0.05), b["y1"] - int(h * 0.04)])]
        elif act == "split":
            ym = b["y0"] + h // 2
            after = [rect([b["x0"], b["y0"], b["x1"], ym]), rect([b["x0"], ym, b["x1"], b["y1"]])]
        elif act == "merge":
            after = [rect([b["x0"], b["y0"], b["x1"], b["y1"] + int(h * 0.9)])]
        else:
            after = [rect(before)]
        out.append({"index": i, "box_id": b["id"], "action": act, "before": rect(before), "after": after,
                    "confidence": round(0.62 + 0.09 * i, 2),
                    "reason": ["框略松，收紧一点", "疑似两字粘连", "与下字相连", "无需修改"][i % 4],
                    "status": "pending"})
    return out


@APP.get("/api/pages/{pid}/proposals")
def api_page_proposals(pid: str) -> dict:
    STATE.page_or_404(pid)
    if pid not in STATE.proposals:
        STATE.proposals[pid] = _gen_proposals(pid)
    return {"proposals": [dict(x) for x in STATE.proposals[pid]]}


@APP.post("/api/pages/{pid}/proposals")
def api_page_proposals_apply(pid: str, payload: dict = Body(...)) -> dict:
    p = STATE.page_or_404(pid)
    props = STATE.proposals.setdefault(pid, [])
    apply_idx = list(payload.get("apply") or [])
    reject_idx = list(payload.get("reject") or [])
    if payload.get("apply_all"):
        apply_idx = [i for i, x in enumerate(props) if x["status"] == "pending"]
    apply_idx = [i for i in apply_idx if 0 <= i < len(props) and props[i]["status"] == "pending"]
    reject_idx = [i for i in reject_idx if 0 <= i < len(props)]
    boxes = p["boxes"]
    for i in sorted(apply_idx, reverse=True):            # 从后往前改，避免 id 位移
        prop = props[i]
        target = next((b for b in boxes if b["id"] == prop["box_id"]), None)
        if target is None:
            prop["status"] = "stale"
            continue
        j = boxes.index(target)
        rects = prop["after"] or [prop["before"]]
        newb = []
        for r in rects:
            newb.append({"id": 0, "x0": int(r["x0"]), "y0": int(r["y1"]) if False else int(r["y0"]), "x1": int(r["x1"]), "y1": int(r["y1"]),
                         "flags": ["ai"], "review": 1,
                         "note": "AI:" + prop["action"] + " (conf " + str(prop["confidence"]) + ")"})
        boxes[j:j + 1] = newb
        prop["status"] = "accepted"
    for i in reject_idx:
        props[i]["status"] = "rejected"
    for k, b in enumerate(boxes, 1):
        b["id"] = k
    p["boxes"] = boxes
    p["n_boxes"] = len(boxes)
    p["reviewed"] = sum(1 for b in boxes if b["review"])
    p["ai_refined"] = True
    return {"boxes": [dict(b) for b in boxes],
            "proposals": [dict(x) for x in props], "warnings": []}


@APP.post("/api/pages/{pid}/ai-refine")
def api_page_ai_refine(pid: str, payload: dict = Body(default={})) -> dict:
    p = STATE.page_or_404(pid)
    scope = payload.get("ids", "suspect")
    if not STATE.settings["ai"].get("api_key") and not STATE.settings["ai"].get("enabled", True):
        raise HTTPException(400, "未配置 API Key，无法调用 AI 复核")

    def step(job: dict, i: int) -> str:
        if i == job["total"] - 1:
            STATE.proposals[pid] = _gen_proposals(pid)
            p["ai_refined"] = True
        return "AI 复核 " + str(i + 1) + "/" + str(job["total"])

    total = 4 if scope == "suspect" else 6
    return {"job_id": STATE.start_job("ai-refine", total, step)}


@APP.post("/api/pages/{pid}/export")
def api_page_export(pid: str, payload: dict = Body(default={})) -> dict:
    out = payload.get("out") or "C:/mock/out"
    if pid == "all":
        ids = [str(x) for x in (payload.get("ids") or STATE.order)]
    else:
        ids = [pid]
    for x in ids:
        STATE.page_or_404(x)

    def step(job: dict, i: int) -> str:
        p = STATE.pages[ids[i]]
        job["current"] = {"pid": ids[i], "frac": 1.0, "stage": "导出"}
        return "导出 " + p["name"] + "  " + str(len(p["boxes"])) + " tiles -> " + out

    return {"job_id": STATE.start_job("export", len(ids), step)}


@APP.get("/api/jobs/{jid}")
def api_job(jid: str) -> dict:
    job = STATE.jobs.get(jid)
    if not job:
        raise HTTPException(404, "任务不存在：" + jid)
    return {k: job[k] for k in ("id", "kind", "state", "done", "total", "message", "errors", "results")}


@APP.post("/api/jobs/{jid}/cancel")
def api_job_cancel(jid: str) -> dict:
    job = STATE.jobs.get(jid)
    if not job:
        raise HTTPException(404, "任务不存在：" + jid)
    job["cancel"] = True
    return {"ok": True}


@APP.post("/api/settings")
def api_settings(payload: dict = Body(...)) -> dict:
    ai = payload.get("ai")
    if isinstance(ai, dict):
        for k in ("api_key", "base_url", "model", "max_workers"):
            if k in ai and ai[k] not in ("", None):
                STATE.settings["ai"][k] = ai[k]
    defaults = payload.get("defaults")
    if isinstance(defaults, dict):
        for k, v in defaults.items():
            if k in STATE.settings["defaults"] and v is not None:
                STATE.settings["defaults"][k] = v
    return {"ok": True}


@APP.post("/api/ai/test")
def api_ai_test(payload: dict = Body(default={})) -> dict:
    ai = dict(STATE.settings["ai"])
    ai.update({k: v for k, v in (payload.get("ai") or {}).items() if v})
    if not ai.get("api_key"):
        return {"ok": False, "message": "未填写 API Key"}
    return {"ok": True, "message": "连接成功（mock）：" + str(ai.get("base_url")) + " / " + str(ai.get("model"))}


@APP.post("/api/project/open")
def api_project_open(payload: dict = Body(...)) -> dict:
    return {"ok": True, "state": STATE.state()}


@APP.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


def _mount_static() -> None:
    sd = paths.static_dir()
    if sd.is_dir():
        APP.mount("/", StaticFiles(directory=str(sd), html=True), name="static")


_mount_static()


# ---------------------------------------------------------------------- 自测/启动
def selftest() -> int:
    from fastapi.testclient import TestClient

    c = TestClient(APP)
    fails = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print(("PASS  " if cond else "FAIL  ") + name + (("  " + extra) if extra else ""))
        if not cond:
            fails.append(name)

    st = c.get("/api/state").json()
    check("state.pages", len(st["pages"]) == 4, str(len(st["pages"])))
    check("state.settings.defaults", "pad_ratio" in st["settings"]["defaults"])
    pid = st["pages"][0]["id"]
    pg = c.get("/api/pages/" + pid).json()
    check("page.boxes", len(pg["boxes"]) > 50, str(len(pg["boxes"])))
    r = c.get("/api/pages/" + pid + "/preview.png?w=800&boxes=0")
    check("preview.png", r.status_code == 200 and r.headers["content-type"] == "image/png")
    r = c.get("/api/pages/" + pid + "/crop?x0=100&y0=100&x1=400&y1=400&scale=3")
    check("crop", r.status_code == 200 and len(r.content) > 500)
    boxes = [dict(b) for b in pg["boxes"][:3]]
    boxes.append({"x0": 10, "y0": 10, "x1": 120, "y1": 90, "flags": ["manual"], "review": 1, "note": "t"})
    r = c.put("/api/pages/" + pid + "/boxes", json={"boxes": boxes})
    check("PUT boxes", r.status_code == 200 and len(r.json()["boxes"]) == 4, str(r.status_code))
    r = c.post("/api/pages/process", json={"ids": "all"})
    jid = r.json()["job_id"]
    check("process job", jid.startswith("j"))
    for _ in range(40):
        j = c.get("/api/jobs/" + jid).json()
        if j["state"] != "running":
            break
        time.sleep(0.2)
    check("job done", j["state"] == "done", j["state"] + " " + j["message"])
    props = c.get("/api/pages/" + pid + "/proposals").json()["proposals"]
    check("proposals", len(props) >= 3, str(len(props)))
    r = c.post("/api/pages/" + pid + "/proposals", json={"apply_all": True})
    check("proposals apply", r.status_code == 200 and "boxes" in r.json())
    r = c.post("/api/pages/" + pid + "/export", json={"out": "C:/mock/out", "order": "visual"})
    check("export job", r.status_code == 200 and r.json()["job_id"].startswith("j"))
    r = c.post("/api/settings", json={"ai": {"api_key": "sk-test-123456789012"}, "defaults": {"pad_ratio": 0.12}})
    check("settings", r.json().get("ok") is True)
    r = c.post("/api/ai/test", json={"ai": {"api_key": "sk-test-123456789012"}})
    check("ai test", r.json().get("ok") is True, r.json().get("message", ""))
    r = c.get("/api/dialog/files")
    check("dialog files", len(r.json()["paths"]) == 2)
    r = c.post("/api/pages/add", json={"paths": ["C:/mock/scan/新页9.tif"]})
    check("pages add", len(r.json()["added"]) == 1)
    r = c.get("/")
    check("index.html", r.status_code == 200 and "彝文切字" in r.text, str(r.status_code))
    r = c.get("/app.js")
    check("app.js", r.status_code == 200 and len(r.content) > 1000, str(r.status_code))
    print("")
    print("FAILED " + str(len(fails)) if fails else "ALL PASS")
    return 1 if fails else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app._ui_mock")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--port", type=int, default=8781)
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    import uvicorn
    uvicorn.run(APP, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
