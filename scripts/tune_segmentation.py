"""Coordinate-descent tuning of the segmenter against the hand-cut pages.

Reads the 6 hand-cut references from out/<page>/index.csv (read-only) and
maximises mean F1 at a small scale, then prints the best parameter set.
Nothing is applied to the user profile unless --apply is given.

Usage:
    python scripts/tune_segmentation.py --scale 0.25 [--passes 2] [--apply]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from yi_core import common as C
from yi_core import segment_v1 as S1

CACHE = {}


def load_refs(scale):
    out_root = os.path.join(ROOT, "out")
    scans = os.path.join(ROOT, "scans")
    refs = []
    import csv
    for d in sorted(os.listdir(out_root)):
        csvf = os.path.join(out_root, d, "index.csv")
        tif = os.path.join(scans, d + ".tif")
        if not (os.path.isfile(csvf) and os.path.isfile(tif)):
            continue
        manual = []
        for r in csv.DictReader(open(csvf, encoding="utf-8-sig")):
            manual.append((int(float(r["gx0"])), int(float(r["gy0"])),
                           int(float(r["gx1"])), int(float(r["gy1"]))))
        gray = C.load_gray(tif)
        h, w = gray.shape[:2]
        small = cv2.resize(gray, (max(50, int(w * scale)), max(50, int(h * scale))),
                           interpolation=cv2.INTER_AREA) if scale != 1.0 else gray
        refs.append({"name": d, "gray": small, "manual": manual})
    return refs


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def f1_of(auto, manual, thr=0.5):
    pairs = []
    for i, a in enumerate(auto):
        for j, m in enumerate(manual):
            v = iou(a, m)
            if v >= thr:
                pairs.append((v, i, j))
    pairs.sort(reverse=True)
    ua, um = set(), set()
    for v, i, j in pairs:
        if i in ua or j in um:
            continue
        ua.add(i)
        um.add(j)
    tp = len(ua)
    p = tp / max(1, len(auto))
    r = tp / max(1, len(manual))
    return 2 * p * r / max(1e-9, p + r)


def boxes_of(page, scale):
    if scale == 1.0:
        return [(g["x0"], g["y0"], g["x1"], g["y1"]) for g in page["glyphs"]]
    return [(int(round(g["x0"] / scale)), int(round(g["y0"] / scale)),
             int(round(g["x1"] / scale)), int(round(g["y1"] / scale))) for g in page["glyphs"]]


def evaluate(refs, overrides, scale):
    params = dataclasses.replace(S1.Params(), **overrides)
    scores = []
    for ref in refs:
        page = S1.segment_page(ref["gray"], base_scale=scale, params=params)
        auto = boxes_of(page, scale)
        scores.append(f1_of(auto, ref["manual"]))
    return float(np.mean(scores))


SEARCH = {
    "cut_base": [0.6, 0.9, 1.2],
    "cut_w": [1.4, 2.0, 2.6],
    "shape_w": [6.0, 8.0, 10.0],
    "min_h_frac": [0.38, 0.45, 0.52],
    "tall_split_ratio": [1.25, 1.35, 1.45],
    "tall_split_depth": [0.55, 0.65, 0.80],
    "wide_split_ratio": [1.35, 1.50, 1.70],
    "frag_merge_frac": [0.35, 0.45, 0.55],
    "core_frac_min": [0.10, 0.15, 0.22],
}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=0.25)
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "calibration", "tuned_segment_params.json"))
    args = ap.parse_args()

    refs = load_refs(args.scale)
    print("references: %d pages" % len(refs), flush=True)

    user = os.path.join(os.environ.get("LOCALAPPDATA", ""), "yi-segment", "calibration", "segment_params.json")
    start = {}
    if os.path.isfile(user):
        try:
            start = json.load(open(user, encoding="utf-8")).get("params") or {}
        except Exception:  # noqa: BLE001
            start = {}
    valid = {k: v for k, v in start.items() if hasattr(S1.Params, k)}
    best = dict(valid)
    best_f1 = evaluate(refs, best, args.scale)
    print("start F1: %.4f  params: %s" % (best_f1, json.dumps(best, ensure_ascii=False)), flush=True)

    t0 = time.time()
    for p in range(args.passes):
        for name, values in SEARCH.items():
            cur = best.get(name, getattr(S1.Params(), name))
            local_best, local_f1 = cur, best_f1
            for v in values:
                if v == cur:
                    continue
                trial = dict(best)
                trial[name] = v
                try:
                    f1 = evaluate(refs, trial, args.scale)
                except Exception as exc:  # noqa: BLE001
                    print("  %s=%s failed: %s" % (name, v, exc), flush=True)
                    continue
                print("  pass%d %-18s = %-6s F1 %.4f%s" % (p + 1, name, v, f1, "  *" if f1 > local_f1 else ""), flush=True)
                if f1 > local_f1:
                    local_best, local_f1 = v, f1
            if local_best != cur:
                best[name] = local_best
                best_f1 = local_f1
        print("pass %d done: F1 %.4f (%.0fs)" % (p + 1, best_f1, time.time() - t0), flush=True)

    print("BEST F1: %.4f" % best_f1)
    print("params:", json.dumps(best, ensure_ascii=False))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"params": best, "mean_f1": round(best_f1, 4), "scale": args.scale,
                   "pages": [r["name"] for r in refs],
                   "tuned_at": time.strftime("%Y-%m-%d %H:%M")}, f, ensure_ascii=False, indent=1)
    print("written:", args.out)
    if args.apply:
        for dst in (user, os.path.join(ROOT, "data", "calibration", "segment_params.json")):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as f:
                json.dump({"learned_at": time.strftime("%Y-%m-%d %H:%M"), "params": best,
                           "mean_f1": round(best_f1, 4), "source": "tune_segmentation"}, f,
                          ensure_ascii=False, indent=1)
        print("applied to:", user)
    return 0


if __name__ == "__main__":
    sys.exit(main())
