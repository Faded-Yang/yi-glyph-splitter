"""In-app one-click learning WITH ACCUMULATION.

Every time you learn from a page, that page's boxes are kept as a reference
under %LOCALAPPDATA%/yi-segment/calibration/references/, and the parameters are
re-derived from ALL stored references (mean F1 over pages).  So learning a new
page does not throw away earlier evidence: it adds to it.  The current
parameters are used as the starting point of the search.

Per-user files (never committed):
    calibration/segment_params.json        current parameters + stats
    calibration/references/<page>.json     {tif, boxes[[x0,y0,x1,y1],...]}
"""
from __future__ import annotations

import dataclasses
import glob
import json
import os
import re
import time

import cv2
import numpy as np

from . import paths, store as store_mod
from yi_core import common as C
from yi_core import segment_v1 as S1

IOU_THR = 0.5
SEARCH_SCALE = 0.25
MAX_REFS = 8          # keep the most recent N pages of evidence
MAX_FULL_PAGES = 3    # full-resolution stage looks at most at N pages

SEARCH = [
    ("cut_base", [0.6, 0.9, 1.2]),
    ("ps_w_ratio", [1.45, 1.55, 1.70]),
    ("frag_merge_frac", [0.35, 0.45, 0.60]),
    ("ink_frac_max", [0.50, 0.55, 0.62]),
    ("core_frac_min", [0.10, 0.15, 0.22]),
    ("edge_band_ink", [0.30, 0.35, 0.45]),
]


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def score(auto, manual):
    pairs = []
    for i, a in enumerate(auto):
        for j, m in enumerate(manual):
            v = _iou(a, m)
            if v >= IOU_THR:
                pairs.append((v, i, j))
    pairs.sort(reverse=True)
    ua, um, ious = set(), set(), []
    for v, i, j in pairs:
        if i in ua or j in um:
            continue
        ua.add(i)
        um.add(j)
        ious.append(v)
    tp = len(ua)
    prec = tp / max(1, len(auto))
    rec = tp / max(1, len(manual))
    f1 = 2 * prec * rec / max(1e-9, prec + rec)
    return {
        "auto": len(auto), "manual": len(manual), "matched": tp,
        "missed": len(manual) - tp, "extra": len(auto) - tp,
        "precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4),
        "iou_median": round(float(np.median(ious)), 4) if ious else 0.0,
    }


def _boxes_of(page, scale):
    if scale == 1.0:
        return [(g["x0"], g["y0"], g["x1"], g["y1"]) for g in page["glyphs"]]
    return [(int(round(g["x0"] / scale)), int(round(g["y0"] / scale)),
             int(round(g["x1"] / scale)), int(round(g["y1"] / scale))) for g in page["glyphs"]]


def _safe_name(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff.-]+", "_", text)[:80] or "page"


def _ref_dir():
    d = paths.data_dir() / "calibration" / "references"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_references() -> list:
    refs = []
    for fp in sorted(glob.glob(str(_ref_dir() / "*.json"))):
        try:
            data = json.loads(open(fp, encoding="utf-8").read())
            tif = data.get("tif") or ""
            boxes = [(int(b[0]), int(b[1]), int(b[2]), int(b[3])) for b in data.get("boxes") or []]
            if tif and os.path.isfile(tif) and boxes:
                refs.append({"file": fp, "tif": tif, "name": data.get("name") or os.path.basename(tif),
                             "boxes": boxes, "saved_at": data.get("saved_at") or ""})
        except Exception:  # noqa: BLE001
            continue
    return refs


def _save_reference(tif: str, name: str, boxes) -> str:
    fp = os.path.join(str(_ref_dir()), _safe_name(os.path.splitext(name)[0]) + ".json")
    payload = {"tif": os.path.abspath(tif), "name": name,
               "saved_at": time.strftime("%Y-%m-%d %H:%M"),
               "boxes": [[int(b[0]), int(b[1]), int(b[2]), int(b[3])] for b in boxes]}
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return fp


def _trim_references(refs):
    refs = list(refs)
    while len(refs) > MAX_REFS:
        refs.sort(key=lambda r: r.get("saved_at") or "")
        try:
            os.remove(refs[0]["file"])
        except OSError:
            pass
        refs.pop(0)
    return refs


def _current_params() -> dict:
    fp = paths.data_dir() / "calibration" / "segment_params.json"
    try:
        data = json.loads(open(str(fp), encoding="utf-8").read())
        params = data.get("params") or {}
        return params if isinstance(params, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _load_pages(refs, progress_cb, cancel_cb) -> list:
    pages = []
    for r in refs:
        if cancel_cb and cancel_cb():
            raise store_mod.StoreError("已取消")
        if progress_cb:
            progress_cb(0.05, "载入样本 %s" % r["name"])
        try:
            gray = C.load_gray(r["tif"])
        except Exception:  # noqa: BLE001
            continue
        h, w = gray.shape[:2]
        small = cv2.resize(gray, (max(50, int(w * SEARCH_SCALE)), max(50, int(h * SEARCH_SCALE))),
                           interpolation=cv2.INTER_AREA)
        pages.append({"name": r["name"], "gray": gray, "small": small, "manual": r["boxes"]})
    return pages


def learn_from_page(store, pid, progress_cb=None, cancel_cb=None) -> dict:
    def cb(frac, msg):
        if progress_cb:
            progress_cb(frac, msg)

    def cancelled():
        return bool(cancel_cb and cancel_cb())

    entry = store.get_page(pid)
    src = entry.get("src") or store.src(pid)
    if not src or not os.path.isfile(src):
        raise store_mod.StoreError("源文件不存在：%s" % src)
    manual = [(int(b["x0"]), int(b["y0"]), int(b["x1"]), int(b["y1"])) for b in store.boxes(pid)]
    if not manual:
        raise store_mod.StoreError("这一页还没有框，无法学习")

    t_start = time.time()
    cb(0.03, "登记这一页的标准答案")
    _save_reference(src, entry.get("name") or pid, manual)
    refs = _trim_references(load_references())
    if not refs:
        raise store_mod.StoreError("没有可用的学习样本")

    pages = _load_pages(refs, cb, cancelled)
    if not pages:
        raise store_mod.StoreError("学习样本读不到（源文件被移动了？）")

    def evaluate(overrides, scale):
        params = dataclasses.replace(S1.Params(), **overrides) if overrides else S1.Params()
        per = {}
        for pg in pages:
            img = pg["small"] if scale != 1.0 else pg["gray"]
            page = S1.segment_page(img, base_scale=scale, params=params)
            per[pg["name"]] = score(_boxes_of(page, scale), pg["manual"])
        mean_f1 = float(np.mean([v["f1"] for v in per.values()]))
        return round(mean_f1, 4), per

    seed = {k: v for k, v in _current_params().items()
            if isinstance(v, (int, float, bool))}
    cb(0.1, "基线（%d 页样本）" % len(pages))
    base_f1, _ = evaluate(seed, SEARCH_SCALE)
    if cancelled():
        raise store_mod.StoreError("已取消")

    overrides = dict(seed)
    cur = base_f1
    total = sum(len(v) for _, v in SEARCH)
    done = 0
    for name, values in SEARCH:
        for v in values:
            if cancelled():
                raise store_mod.StoreError("已取消")
            if overrides.get(name) == v:
                done += 1
                continue
            trial = dict(overrides)
            trial[name] = v
            f1, _ = evaluate(trial, SEARCH_SCALE)
            done += 1
            cb(0.1 + 0.45 * done / max(1, total), "%s=%s（平均 F1 %.3f）" % (name, v, f1))
            if f1 > cur:
                overrides, cur = trial, f1

    full_pages = pages[:MAX_FULL_PAGES]

    def evaluate_full(ov):
        params = dataclasses.replace(S1.Params(), **ov) if ov else S1.Params()
        per = {}
        for pg in full_pages:
            page = S1.segment_page(pg["gray"], base_scale=1.0, params=params)
            per[pg["name"]] = score(_boxes_of(page, 1.0), pg["manual"])
        return round(float(np.mean([v["f1"] for v in per.values()])), 4), per

    cb(0.6, "整页校验（%d 页）" % len(full_pages))
    best_full_f1, best_full_per = evaluate_full(overrides)
    best_overrides = dict(overrides)
    for k, t in enumerate(({"tall_split": True, "tall_split_ratio": 1.30},
                           {"tall_split": True, "tall_split_ratio": 1.45})):
        if cancelled():
            raise store_mod.StoreError("已取消")
        trial = dict(overrides)
        trial.update(t)
        f1, per = evaluate_full(trial)
        cb(0.6 + 0.3 * (k + 1) / 2.0, "整页试切 %s（平均 F1 %.3f）" % (t["tall_split_ratio"], f1))
        if f1 > best_full_f1:
            best_full_f1, best_full_per, best_overrides = f1, per, trial

    profile = {
        "learned_at": time.strftime("%Y-%m-%d %H:%M"),
        "params": best_overrides,
        "pages_used": [{"name": p["name"], "boxes": len(p["manual"])} for p in pages],
        "pages_count": len(pages),
        "full_check_pages": [p["name"] for p in full_pages],
        "mean_f1": best_full_f1,
        "per_page": best_full_per,
        "this_page": {"name": entry.get("name") or pid, "boxes": len(manual)},
    }
    out_dir = paths.data_dir() / "calibration"
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / "segment_params.json"
    with open(str(fp), "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=1)
    cb(0.99, "保存参数")
    profile["file"] = str(fp)
    profile["seconds"] = round(time.time() - t_start, 1)
    return profile
