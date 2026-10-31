"""Learn segmentation parameters from human-corrected exports (multi-page).

Usage:
    python scripts/learn_from_correction.py \\
        --pair "out/彝族扫描3-0002_right:scans/彝族扫描3-0002_right.tif" \\
        --pair "out/彝族扫描3-0003_left:scans/彝族扫描3-0003_left.tif"

Each --pair is <manual-export-dir>:<original-tif>.  The export dir is what the
app/CLI wrote after you fixed the boxes (index.csv, gx0..gy1 = tight boxes).

Stage 1 hill-climbs 8 parameters on 1/4-scale images (mean F1 over all pages),
stage 2 tunes the tall-box split on full resolution, then the profile is saved
to data/calibration/segment_params.json and to the per-user data dir, which
pipeline.process_page() and the CLI load automatically.
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
sys.path.insert(0, ROOT)

from yi_core import common as C  # noqa: E402
from yi_core import segment_v1 as S1  # noqa: E402

IOU_THR = 0.5


def load_manual(csv_path):
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    out = []
    for r in rows:
        try:
            out.append((int(float(r["gx0"])), int(float(r["gy0"])),
                        int(float(r["gx1"])), int(float(r["gy1"]))))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def iou(a, b):
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
            v = iou(a, m)
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
        "auto": len(auto),
        "manual": len(manual),
        "matched": tp,
        "missed": len(manual) - tp,
        "extra": len(auto) - tp,
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(f1, 4),
        "iou_median": round(float(np.median(ious)), 4) if ious else 0.0,
    }


class Page:
    def __init__(self, export, tif, scale):
        self.name = os.path.basename(export.rstrip("/\\"))
        self.manual = load_manual(os.path.join(export, "index.csv"))
        self.full = C.load_gray(tif)
        h, w = self.full.shape
        self.scale = scale
        if scale != 1.0:
            self.small = cv2.resize(self.full, (max(50, int(w * scale)), max(50, int(h * scale))),
                                    interpolation=cv2.INTER_AREA)
        else:
            self.small = self.full

    def boxes(self, params, scale):
        img = self.small if scale != 1.0 else self.full
        page = S1.segment_page(img, base_scale=scale, params=params)
        if scale == 1.0:
            return [(g["x0"], g["y0"], g["x1"], g["y1"]) for g in page["glyphs"]]
        return [(int(round(g["x0"] / scale)), int(round(g["y0"] / scale)),
                 int(round(g["x1"] / scale)), int(round(g["y1"] / scale))) for g in page["glyphs"]]


SEARCH = [
    ("cut_base", [0.6, 0.9, 1.2]),
    ("shape_w", [6.0, 8.0, 10.0]),
    ("ps_h_ratio", [1.30, 1.45, 1.60]),
    ("ps_w_ratio", [1.45, 1.55, 1.70]),
    ("frag_merge_frac", [0.35, 0.45, 0.60]),
    ("ink_frac_max", [0.50, 0.55, 0.62]),
    ("core_frac_min", [0.10, 0.15, 0.22]),
    ("edge_band_ink", [0.30, 0.35, 0.45]),
]


def evaluate(pages, overrides, scale):
    params = dataclasses.replace(S1.Params(), **overrides) if overrides else S1.Params()
    per = {}
    for pg in pages:
        per[pg.name] = score(pg.boxes(params, scale), pg.manual)
    f1 = float(np.mean([v["f1"] for v in per.values()]))
    iou = float(np.mean([v["iou_median"] for v in per.values()]))
    return {"f1": round(f1, 4), "iou_median": round(iou, 4), "per_page": per}


def hill_climb(pages, overrides, scale, passes=2, label=""):
    cur = evaluate(pages, overrides, scale)
    print("%s start: f1=%.4f iou=%.4f" % (label, cur["f1"], cur["iou_median"]))
    for _ in range(passes):
        improved = False
        for name, values in SEARCH:
            for v in values:
                if overrides.get(name) == v:
                    continue
                trial = dict(overrides)
                trial[name] = v
                st = evaluate(pages, trial, scale)
                if (st["f1"], st["iou_median"]) > (cur["f1"], cur["iou_median"]):
                    overrides, cur = trial, st
                    improved = True
                    print("  + %s=%s -> f1=%.4f" % (name, v, st["f1"]))
        if not improved:
            break
    return overrides, cur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", action="append", required=True,
                    help="<manual export dir>:<original tif>")
    ap.add_argument("--search-scale", type=float, default=0.25)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "calibration"))
    args = ap.parse_args()

    pages = []
    for spec in args.pair:
        export, _, tif = spec.rpartition(":")
        if not export or not tif:
            raise SystemExit("bad --pair (need EXPORT:TIF): %s" % spec)
        export = export.strip(chr(34))
        tif = tif.strip(chr(34))
        pg = Page(export, tif, args.search_scale)
        pages.append(pg)
        print("page %-28s manual=%d" % (pg.name, len(pg.manual)))

    before = evaluate(pages, {}, 1.0)
    print("baseline full-res:", json.dumps(before["per_page"], ensure_ascii=False))

    overrides, small = hill_climb(pages, {}, args.search_scale, label="small")
    print("stage1 overrides:", overrides)

    cur = evaluate(pages, overrides, 1.0)
    print("stage2 start: f1=%.4f" % cur["f1"])
    trials = []
    for ratio in (1.15, 1.30, 1.45):
        trials.append({"tall_split": True, "tall_split_ratio": ratio})
    for depth in (0.50, 0.80):
        trials.append({"tall_split": True, "tall_split_ratio": cur.get("tall_split_ratio", 1.30),
                       "tall_split_depth": depth})
    for t in trials:
        trial = dict(overrides)
        trial.update(t)
        st = evaluate(pages, trial, 1.0)
        print("  trial %s -> f1=%.4f" % (t, st["f1"]))
        if (st["f1"], st["iou_median"]) > (cur["f1"], cur["iou_median"]):
            overrides, cur = trial, st
            print("    + kept")

    after = evaluate(pages, overrides, 1.0)
    print("learned:", json.dumps(after["per_page"], ensure_ascii=False))
    print("mean F1: %.4f -> %.4f" % (before["f1"], after["f1"]))

    prof = {
        "pages": [{"export": p.name, "manual": len(p.manual)} for p in pages],
        "learned_at": time.strftime("%Y-%m-%d %H:%M"),
        "params": overrides,
        "before": before,
        "after": after,
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "segment_params.json"), "w", encoding="utf-8") as f:
        json.dump(prof, f, ensure_ascii=False, indent=1)
    lines = ["# 从人工修正学习到的分割参数", "",
             "- 学习页：%d 页" % len(pages),
             "- 平均 F1：%.4f -> %.4f" % (before["f1"], after["f1"]),
             "- 参数：`%s`" % json.dumps(overrides, ensure_ascii=False), "",
             "| 页面 | 人工框 | 学习前 F1 | 学习后 F1 |", "|---|---|---|---|"]
    for p in pages:
        b = before["per_page"][p.name]
        a = after["per_page"][p.name]
        lines.append("| %s | %d | %.4f | %.4f |" % (p.name, len(p.manual), b["f1"], a["f1"]))
    open(os.path.join(args.out, "REPORT.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("saved ->", os.path.join(args.out, "segment_params.json"))
    try:
        from app import paths as _paths
        user_dir = _paths.data_dir() / "calibration"
        user_dir.mkdir(parents=True, exist_ok=True)
        with open(user_dir / "segment_params.json", "w", encoding="utf-8") as f2:
            json.dump(prof, f2, ensure_ascii=False, indent=1)
        print("also copied ->", user_dir / "segment_params.json")
    except Exception as exc:  # noqa: BLE001
        print("skip user copy:", exc)


if __name__ == "__main__":
    main()
