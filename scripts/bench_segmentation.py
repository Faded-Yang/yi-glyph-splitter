"""Benchmark the segmenter against the hand-cut pages (read-only).

For every page in the project that has hand-edited boxes, run the CURRENT
segmentation (with the calibration in use) at full resolution and compare the
boxes with the manual ones (IoU >= 0.5 matching).  Prints per-page P/R/F1 plus
a size/geometry diagnosis to steer parameter tuning.

Usage:
    python scripts/bench_segmentation.py [--scale 0.25] [--pages ...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from yi_core import common as C
from yi_core import segment_v1 as S1


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def match(auto, manual, thr=0.5):
    pairs = []
    for i, a in enumerate(auto):
        for j, m in enumerate(manual):
            v = iou(a, m)
            if v >= thr:
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
    p = tp / max(1, len(auto))
    r = tp / max(1, len(manual))
    f1 = 2 * p * r / max(1e-9, p + r)
    missed = [j for j in range(len(manual)) if j not in um]
    extra = [i for i in range(len(auto)) if i not in ua]
    # diagnose error types: an extra box covering 2+ manual boxes = merged pair;
    # an auto box covering nothing = spurious; a missed manual box covered by 2+ = over-split
    def cover_frac(a, b):
        ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        if inter <= 0:
            return 0.0
        area_b = max(1, (b[2] - b[0]) * (b[3] - b[1]))
        return inter / area_b

    diag = {"merged": 0, "spurious": 0, "offset": 0, "oversplit": 0, "dropped": 0}
    for i in extra:
        a = auto[i]
        hits = sum(1 for m in manual if cover_frac(a, m) >= 0.55)
        if hits >= 2:
            diag["merged"] += 1
        elif hits == 0:
            diag["spurious"] += 1
        else:
            diag["offset"] += 1
    for j in missed:
        m = manual[j]
        hits = sum(1 for a in auto if cover_frac(a, m) >= 0.55)
        if hits >= 2:
            diag["oversplit"] += 1
        elif hits == 0:
            diag["dropped"] += 1
        else:
            diag["offset"] += 1

    return {"tp": tp, "p": p, "r": r, "f1": f1, "iou_med": float(np.median(ious)) if ious else 0.0,
            "missed": missed, "extra": extra, "n_auto": len(auto), "n_manual": len(manual),
            "diag": diag}


def to_boxes(page, scale=1.0):
    out = []
    for g in page["glyphs"]:
        if scale == 1.0:
            out.append((g["x0"], g["y0"], g["x1"], g["y1"]))
        else:
            out.append((int(round(g["x0"] / scale)), int(round(g["y0"] / scale)),
                        int(round(g["x1"] / scale)), int(round(g["y1"] / scale))))
    return out


def load_params():
    """Params in effect for the app (user calibration first, then repo defaults)."""
    cands = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "yi-segment", "calibration", "segment_params.json"),
             os.path.join(ROOT, "data", "calibration", "segment_params.json")]
    for fp in cands:
        if fp and os.path.isfile(fp):
            try:
                data = json.load(open(fp, encoding="utf-8"))
                params = data.get("params") or data
                return dict(params), fp
            except Exception:  # noqa: BLE001
                continue
    return {}, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--pages", default="")
    ap.add_argument("--project", default=os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                                      "yi-segment", "projects", "默认项目"))
    ap.add_argument("--json", default="")
    ap.add_argument("--model", action="store_true",
                    help="refine boxes with the hand-cut glyph model (refs_manual.npz)")
    ap.add_argument("--from-out", action="store_true",
                    help="read hand-cut boxes from out/<page>/index.csv instead of the project")
    args = ap.parse_args()

    overrides, used = load_params()
    print("params file:", used or "(built-in defaults)")
    print("overrides:", json.dumps(overrides, ensure_ascii=False))
    import dataclasses
    valid = {k: v for k, v in overrides.items() if hasattr(S1.Params, k)}
    params = dataclasses.replace(S1.Params(), **valid) if valid else S1.Params()

    if args.from_out:
        return bench_from_out(args, params, overrides)

    pages_dir = os.path.join(args.project, "pages")
    names = set(p for p in args.pages.split(",") if p)
    rows = []
    for pid in sorted(os.listdir(pages_dir)):
        pf = os.path.join(pages_dir, pid, "page.json")
        bf = os.path.join(pages_dir, pid, "boxes.json")
        if not (os.path.isfile(pf) and os.path.isfile(bf)):
            continue
        page = json.load(open(pf, encoding="utf-8"))
        name = os.path.basename(page.get("source") or pid)
        if names and name not in names:
            continue
        src = page.get("source")
        if not src or not os.path.isfile(src):
            print("skip (no source):", name)
            continue
        manual = [(int(b["x0"]), int(b["y0"]), int(b["x1"]), int(b["y1"]))
                  for b in (json.load(open(bf, encoding="utf-8")).get("boxes") or [])]
        auto_n = len(page.get("glyphs") or [])
        if not manual or len(manual) == auto_n:
            print("skip (not hand-edited):", name)
            continue
        t = time.time()
        gray = C.load_gray(src)
        if args.scale != 1.0:
            import cv2
            h, w = gray.shape[:2]
            gray = cv2.resize(gray, (int(w * args.scale), int(h * args.scale)), interpolation=cv2.INTER_AREA)
        page_out = S1.segment_page(gray, base_scale=args.scale, params=params)
        auto = to_boxes(page_out, args.scale)
        if args.model:
            auto = refine_with_model(gray, auto, d, args.scale)
        m = match(auto, manual)
        m["name"] = name
        m["sec"] = round(time.time() - t, 1)
        rows.append(m)
        print("%-30s auto=%4d manual=%4d tp=%4d P=%.3f R=%.3f F1=%.4f iou_med=%.3f (%.1fs)"
              % (name, m["n_auto"], m["n_manual"], m["tp"], m["p"], m["r"], m["f1"], m["iou_med"], m["sec"]))
    if not rows:
        print("no hand-edited pages found")
        return 1
    mean = float(np.mean([r["f1"] for r in rows]))
    print("MEAN F1: %.4f over %d pages" % (mean, len(rows)))
    print("total extra=%d missed=%d" % (sum(len(r["extra"]) for r in rows), sum(len(r["missed"]) for r in rows)))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"mean_f1": mean, "pages": [{k: v for k, v in r.items() if k not in ("extra", "missed")} for r in rows]},
                      f, ensure_ascii=False, indent=1)
    return 0


def refine_with_model(gray, boxes, page_name, scale=1.0):
    """Apply the manuscript glyph model to the boxes (read-only refinement)."""
    try:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from yi_core import glyphmodel
        from yi_core import model_refine
        model = glyphmodel.load_model()
        if model is None:
            print("  (no refs_manual.npz, --model ignored)")
            return boxes
        out, _ = model_refine.refine_boxes(gray, boxes, model, page_name)
        return out
    except Exception as exc:  # noqa: BLE001
        print("  (model refine failed: %s)" % exc)
        return boxes


def bench_from_out(args, params, overrides):
    """Compare against the exporter output (index.csv) of the 6 hand-cut pages."""
    import csv
    out_root = os.path.join(ROOT, "out")
    scans = os.path.join(ROOT, "scans")
    rows = []
    for d in sorted(os.listdir(out_root)):
        csvf = os.path.join(out_root, d, "index.csv")
        tif = os.path.join(scans, d + ".tif")
        if not (os.path.isfile(csvf) and os.path.isfile(tif)):
            continue
        if args.pages and d not in args.pages.split(","):
            continue
        manual = []
        with open(csvf, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                manual.append((int(float(r["gx0"])), int(float(r["gy0"])),
                               int(float(r["gx1"])), int(float(r["gy1"]))))
        t = time.time()
        gray = C.load_gray(tif)
        if args.scale != 1.0:
            import cv2
            h, w = gray.shape[:2]
            gray = cv2.resize(gray, (int(w * args.scale), int(h * args.scale)), interpolation=cv2.INTER_AREA)
        page_out = S1.segment_page(gray, base_scale=args.scale, params=params)
        auto = to_boxes(page_out, args.scale)
        m = match(auto, manual)
        m["name"] = d
        m["sec"] = round(time.time() - t, 1)
        rows.append(m)
        dg = m["diag"]
        print("%-22s auto=%4d manual=%4d tp=%4d P=%.3f R=%.3f F1=%.4f extra=%3d(mrg%2d spur%2d off%2d) missed=%3d(split%2d drop%2d off%2d)"
              % (d, m["n_auto"], m["n_manual"], m["tp"], m["p"], m["r"], m["f1"],
                 len(m["extra"]), dg["merged"], dg["spurious"], dg["offset"],
                 len(m["missed"]), dg["oversplit"], dg["dropped"], dg["offset"]))
    if not rows:
        print("no exports found under out/")
        return 1
    mean = float(np.mean([r["f1"] for r in rows]))
    tot = {}
    for r in rows:
        for k, v in r["diag"].items():
            tot[k] = tot.get(k, 0) + v
    print("MEAN F1: %.4f over %d pages | extra=%d missed=%d"
          % (mean, len(rows), sum(len(r["extra"]) for r in rows), sum(len(r["missed"]) for r in rows)))
    print("error types: merged=%d spurious=%d oversplit=%d dropped=%d offset=%d"
          % (tot.get("merged", 0), tot.get("spurious", 0), tot.get("oversplit", 0),
             tot.get("dropped", 0), tot.get("offset", 0)))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"mean_f1": mean,
                       "pages": [{k: v for k, v in r.items() if k not in ("extra", "missed")} for r in rows]},
                      f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
