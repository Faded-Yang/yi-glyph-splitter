"""Learn segmentation parameters from a human-corrected export.

Usage:
    python scripts/learn_from_correction.py \
        --export out/彝族扫描3-0002_right \
        --tif scans/彝族扫描3-0002_right.tif

The manual folder is what the app/CLI wrote after you edited the boxes
(index.csv has the tight glyph boxes gx0..gy1).  We run the segmenter,
match its boxes against yours, then hill-climb a few parameters on a
downscaled copy and write the winner to data/calibration/segment_params.json.
pipeline.process_page() loads that file automatically, so the next
"一键处理" uses the learned values.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import sys
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from yi_core import common as C  # noqa: E402
from yi_core import segment_v1 as S1  # noqa: E402

IOU_THR = 0.5


def load_manual(csv_path: str):
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    out = []
    for r in rows:
        try:
            x0, y0, x1, y1 = (int(float(r[k])) for k in ("gx0", "gy0", "gx1", "gy1"))
        except (KeyError, TypeError, ValueError):
            continue
        out.append((x0, y0, x1, y1))
    return out


def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def match(auto, manual):
    """Greedy one-to-one matching by IoU desc."""
    pairs = []
    for i, a in enumerate(auto):
        for j, m in enumerate(manual):
            v = iou(a, m)
            if v >= IOU_THR:
                pairs.append((v, i, j))
    pairs.sort(reverse=True)
    used_a, used_m, matched = set(), set(), []
    for v, i, j in pairs:
        if i in used_a or j in used_m:
            continue
        used_a.add(i)
        used_m.add(j)
        matched.append((v, i, j))
    miss = [manual[j] for j in range(len(manual)) if j not in used_m]
    extra = [auto[i] for i in range(len(auto)) if i not in used_a]
    return matched, miss, extra


def score(auto, manual):
    matched, miss, extra = match(auto, manual)
    tp = len(matched)
    prec = tp / max(1, len(auto))
    rec = tp / max(1, len(manual))
    f1 = 2 * prec * rec / max(1e-9, prec + rec)
    ious = [v for v, _, _ in matched]
    return {
        "auto": len(auto),
        "manual": len(manual),
        "matched": tp,
        "missed": len(miss),
        "extra": len(extra),
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(f1, 4),
        "iou_median": round(float(np.median(ious)), 4) if ious else 0.0,
        "iou_p10": round(float(np.percentile(ious, 10)), 4) if ious else 0.0,
    }, matched, miss, extra


def run_segment(gray, params=None, scale=1.0):
    if scale == 1.0:
        page = S1.segment_page(gray, base_scale=1.0, params=params)
        return [(g["x0"], g["y0"], g["x1"], g["y1"]) for g in page["glyphs"]]
    s = float(scale)
    small = cv2.resize(gray, (max(50, int(gray.shape[1] * s)), max(50, int(gray.shape[0] * s))),
                       interpolation=cv2.INTER_AREA)
    page = S1.segment_page(small, base_scale=s, params=params)
    return [(int(round(g["x0"] / s)), int(round(g["y0"] / s)),
             int(round(g["x1"] / s)), int(round(g["y1"] / s))) for g in page["glyphs"]]


SEARCH = [
    ("cut_base", [0.6, 0.9, 1.2, 1.5]),
    ("shape_w", [6.0, 8.0, 10.0]),
    ("ps_h_ratio", [1.30, 1.45, 1.60]),
    ("ps_w_ratio", [1.45, 1.55, 1.70]),
    ("frag_merge_frac", [0.35, 0.45, 0.60]),
    ("ink_frac_max", [0.50, 0.55, 0.62]),
    ("core_frac_min", [0.10, 0.15, 0.22]),
]


def search(gray, manual, scale, passes=2):
    overrides = {}
    base = S1.Params()
    def evaluate(ov, s=scale):
        p = dataclasses.replace(base, **ov) if ov else base
        t0 = time.time()
        boxes = run_segment(gray_small_source[0], params=p, scale=s)
        st = score(boxes, manual)[0]
        st["seconds"] = round(time.time() - t0, 1)
        return st
    gray_small_source = [gray]
    cur = evaluate(overrides)
    print("start:", cur)
    for p in range(passes):
        improved = False
        for name, values in SEARCH:
            for v in values:
                if overrides.get(name) == v:
                    continue
                trial = dict(overrides)
                trial[name] = v
                st = evaluate(trial)
                if (st["f1"], st["iou_median"]) > (cur["f1"], cur["iou_median"]):
                    overrides = trial
                    cur = st
                    improved = True
                    print("  + %s=%s -> f1=%.4f iou=%.4f" % (name, v, st["f1"], st["iou_median"]))
        if not improved:
            break
    return overrides, cur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True, help="人工修改后的导出目录（含 index.csv）")
    ap.add_argument("--tif", required=True)
    ap.add_argument("--search-scale", type=float, default=0.25)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "calibration"))
    args = ap.parse_args()

    manual = load_manual(os.path.join(args.export, "index.csv"))
    print("manual boxes:", len(manual))
    gray = C.load_gray(args.tif)
    print("page:", gray.shape, "| search scale:", args.search_scale)

    t0 = time.time()
    auto_full = run_segment(gray, None, 1.0)
    before, matched, miss, extra = score(auto_full, manual)
    before["seconds"] = round(time.time() - t0, 1)
    print("baseline:", before)

    overrides, small_best = search(gray, manual, args.search_scale)

    # Stage 2: the dominant error is "two glyphs merged into one tall box",
    # so tune the tall-split post-pass on the full-resolution image.
    base = S1.Params()
    def full_eval(ov):
        p = S1.Params(**ov) if ov else base
        t0 = time.time()
        boxes = run_segment(gray, params=p, scale=1.0)
        st = score(boxes, manual)[0]
        st["seconds"] = round(time.time() - t0, 1)
        return st

    cur = full_eval(overrides)
    print("full baseline:", cur)
    tall_trials = []
    for ratio in (1.15, 1.30, 1.45):
        tall_trials.append({"tall_split": True, "tall_split_ratio": ratio})
    for depth in (0.50, 0.80):
        tall_trials.append({"tall_split": True, "tall_split_ratio": cur.get("tall_split_ratio", 1.30),
                            "tall_split_depth": depth})
    for t in tall_trials:
        trial = dict(overrides)
        trial.update(t)
        st = full_eval(trial)
        print("  trial %s -> f1=%.4f iou=%.4f (%ss)" % (t, st["f1"], st["iou_median"], st["seconds"]))
        if (st["f1"], st["iou_median"]) > (cur["f1"], cur["iou_median"]):
            overrides = trial
            cur = st
            print("    + kept")
    print("after tall search:", cur, "| overrides:", overrides)
    print("best overrides:", overrides, "| small-scale score:", small_best)

    best_params = S1.Params(**overrides) if overrides else S1.Params()
    t0 = time.time()
    auto_best = run_segment(gray, best_params, 1.0)
    after, matched2, miss2, extra2 = score(auto_best, manual)
    after["seconds"] = round(time.time() - t0, 1)
    print("learned:", after)

    # geometric bias of the matched pairs (auto vs manual)
    dx, dy, ww, hh = [], [], [], []
    for v, i, j in matched2:
        a, m = auto_best[i], manual[j]
        dx.append(m[0] - a[0])
        dy.append(m[1] - a[1])
        ww.append((m[2] - m[0]) / max(1, a[2] - a[0]))
        hh.append((m[3] - m[1]) / max(1, a[3] - a[1]))
    bias = {
        "dx_median": round(float(np.median(dx)), 1) if dx else 0.0,
        "dy_median": round(float(np.median(dy)), 1) if dy else 0.0,
        "w_ratio_median": round(float(np.median(ww)), 3) if ww else 1.0,
        "h_ratio_median": round(float(np.median(hh)), 3) if hh else 1.0,
    }

    os.makedirs(args.out, exist_ok=True)
    prof = {
        "source_tif": os.path.basename(args.tif),
        "manual_export": os.path.basename(args.export.rstrip(os.sep)),
        "learned_at": time.strftime("%Y-%m-%d %H:%M"),
        "params": overrides,
        "before": before,
        "after": after,
        "bias": bias,
    }
    with open(os.path.join(args.out, "segment_params.json"), "w", encoding="utf-8") as f:
        json.dump(prof, f, ensure_ascii=False, indent=1)
    md = [
        "# 从人工修正学习到的分割参数",
        "",
        "- 学习页：%s" % prof["source_tif"],
        "- 人工框数：%d" % before["manual"],
        "- 学习前：F1 %.4f（precision %.4f / recall %.4f，IoU 中位 %.3f）"
        % (before["f1"], before["precision"], before["recall"], before["iou_median"]),
        "- 学习后：F1 %.4f（precision %.4f / recall %.4f，IoU 中位 %.3f）"
        % (after["f1"], after["precision"], after["recall"], after["iou_median"]),
        "- 参数：`%s`" % json.dumps(overrides, ensure_ascii=False),
        "- 几何偏差（人工相对自动）：dx %.1f px, dy %.1f px, 宽比 %.3f, 高比 %.3f"
        % (bias["dx_median"], bias["dy_median"], bias["w_ratio_median"], bias["h_ratio_median"]),
        "",
        "`pipeline.process_page()` 会自动读取 `data/calibration/segment_params.json`。",
    ]
    open(os.path.join(args.out, "REPORT.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    print("saved ->", os.path.join(args.out, "segment_params.json"))
    # copy into the per-user data dir so the installed app uses it right away
    try:
        sys.path.insert(0, ROOT)
        from app import paths as _paths
        user_dir = _paths.data_dir() / "calibration"
        user_dir.mkdir(parents=True, exist_ok=True)
        with open(user_dir / "segment_params.json", "w", encoding="utf-8") as f2:
            json.dump(prof, f2, ensure_ascii=False, indent=1)
        print("also copied ->", user_dir / "segment_params.json")
    except Exception as exc:  # noqa: BLE001
        print("skip user copy:", exc)
    print("\n".join(md))


if __name__ == "__main__":
    main()
