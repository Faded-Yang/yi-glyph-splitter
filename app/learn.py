"""In-app one-click learning: tune segmentation params from the boxes you edited.

This is the same idea as scripts/learn_from_correction.py but takes the
reference boxes straight from the page's boxes.json (what you fixed in the
app), so no export round-trip is needed and no rebuild: the profile is written
to %LOCALAPPDATA%\\yi-segment\\calibration\\segment_params.json, which
pipeline.calibration_params() prefers over the bundled default.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time

import cv2
import numpy as np

from . import paths, store as store_mod
from yi_core import common as C
from yi_core import segment_v1 as S1

IOU_THR = 0.5
SEARCH_SCALE = 0.25

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


def learn_from_page(store, pid, progress_cb=None, cancel_cb=None) -> dict:
    """Search params that best reproduce the boxes.json of this page."""
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
    cb(0.04, "读取原始扫描")
    gray = C.load_gray(src)
    h, w = gray.shape[:2]
    small = cv2.resize(gray, (max(50, int(w * SEARCH_SCALE)), max(50, int(h * SEARCH_SCALE))),
                       interpolation=cv2.INTER_AREA)
    if cancelled():
        raise store_mod.StoreError("已取消")

    def evaluate(overrides, scale, frac_lo, msg):
        params = dataclasses.replace(S1.Params(), **overrides) if overrides else S1.Params()
        img = small if scale != 1.0 else gray
        page = S1.segment_page(img, base_scale=scale, params=params)
        st = score(_boxes_of(page, scale), manual)
        if progress_cb:
            progress_cb(frac_lo, msg)
        return st

    cb(0.08, "基线：跑一遍当前分割")
    params_default = S1.Params()
    base_page = S1.segment_page(small, base_scale=SEARCH_SCALE, params=params_default)
    before = score(_boxes_of(base_page, SEARCH_SCALE), manual)
    if cancelled():
        raise store_mod.StoreError("已取消")

    # ---- stage 1: coarse search at 1/4 scale
    overrides = {}
    cur = evaluate(overrides, SEARCH_SCALE, 0.1, "粗搜")
    total_trials = sum(len(v) for _, v in SEARCH)
    done = 0
    for name, values in SEARCH:
        for v in values:
            if cancelled():
                raise store_mod.StoreError("已取消")
            trial = dict(overrides)
            trial[name] = v
            st = evaluate(trial, SEARCH_SCALE, 0.1, "%s=%s" % (name, v))
            done += 1
            cb(0.1 + 0.45 * done / max(1, total_trials), "粗搜 %s=%s（F1 %.3f）" % (name, v, st["f1"]))
            if (st["f1"], st["iou_median"]) > (cur["f1"], cur["iou_median"]):
                overrides, cur = trial, st

    # ---- stage 2: tall-split trials on the full image (most common error)
    cur_full = None
    trials = [
        {"tall_split": True, "tall_split_ratio": 1.30},
        {"tall_split": True, "tall_split_ratio": 1.45},
    ]
    for k, t in enumerate(trials):
        if cancelled():
            raise store_mod.StoreError("已取消")
        trial = dict(overrides)
        trial.update(t)
        params = S1.Params(**trial)
        page = S1.segment_page(gray, params=params)
        st = score(_boxes_of(page, 1.0), manual)
        cb(0.55 + 0.35 * (k + 1) / len(trials), "整页试切 %s（F1 %.3f）" % (t["tall_split_ratio"], st["f1"]))
        if cur_full is None or (st["f1"], st["iou_median"]) > (cur_full["f1"], cur_full["iou_median"]):
            cur_full = st
            best_overrides = trial
        if cancelled():
            raise store_mod.StoreError("已取消")
    if cur_full is None:
        best_overrides = dict(overrides)
        params = S1.Params(**best_overrides) if best_overrides else S1.Params()
        cur_full = score(_boxes_of(S1.segment_page(gray, params=params), 1.0), manual)

    after = cur_full
    profile = {
        "source_page": os.path.basename(entry.get("name") or pid),
        "learned_at": time.strftime("%Y-%m-%d %H:%M"),
        "page_box_count": len(manual),
        "params": best_overrides,
        "before": before,
        "after": after,
    }
    out_dir = paths.data_dir() / "calibration"
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / "segment_params.json"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=1)
    cb(0.99, "保存参数")
    profile["file"] = str(fp)
    profile["seconds"] = round(time.time() - t_start, 1)
    return profile
