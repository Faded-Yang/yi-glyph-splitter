"""Build the manuscript glyph model from the hand-cut pages (user-local).

Reads out/<page>/index.csv + scans/<page>.tif (READ ONLY) and stores every
hand-cut glyph as a normalised 48x48 ink map in
    %LOCALAPPDATA%/yi-segment/glyphbank/refs_manual.npz
That bank is what makes box quality measurable: a real glyph of THIS manuscript
has a close neighbour in the bank; a merged pair / fragment / stain does not.

Usage: python scripts/build_manual_refs.py [--project <dir>]
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from yi_core import common as C

SIDE = 48
COARSE = 16


def norm_ink(gray, side=SIDE):
    import cv2
    from PIL import Image
    g = np.asarray(gray)
    if g.ndim == 3:
        g = cv2.cvtColor(g, cv2.COLOR_RGB2GRAY)
    g = g.astype(np.uint8)
    if g.size == 0:
        return np.zeros((side, side), np.uint8)
    thr, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink = (g < max(60, int(thr))).astype(np.uint8) * 255
    ys, xs = np.nonzero(ink)
    if ys.size < 10:
        return np.zeros((side, side), np.uint8)
    crop = ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = crop.shape
    s = max(h, w)
    canvas = np.zeros((s, s), np.uint8)
    canvas[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = crop
    return np.asarray(Image.fromarray(canvas).resize((side, side), Image.LANCZOS), np.uint8)


def refs_from_out():
    out_root = os.path.join(ROOT, "out")
    scans = os.path.join(ROOT, "scans")
    for d in sorted(os.listdir(out_root)):
        csvf = os.path.join(out_root, d, "index.csv")
        tif = os.path.join(scans, d + ".tif")
        if not (os.path.isfile(csvf) and os.path.isfile(tif)):
            continue
        rows = list(csv.DictReader(open(csvf, encoding="utf-8-sig")))
        boxes = [(int(float(r["gx0"])), int(float(r["gy0"])),
                  int(float(r["gx1"])), int(float(r["gy1"]))) for r in rows]
        yield d, tif, boxes


def refs_from_project(project):
    pages = os.path.join(project, "pages")
    if not os.path.isdir(pages):
        return
    for pid in sorted(os.listdir(pages)):
        pf = os.path.join(pages, pid, "page.json")
        bf = os.path.join(pages, pid, "boxes.json")
        if not (os.path.isfile(pf) and os.path.isfile(bf)):
            continue
        page = json.load(open(pf, encoding="utf-8"))
        src = page.get("source")
        if not src or not os.path.isfile(src):
            continue
        boxes = [(int(b["x0"]), int(b["y0"]), int(b["x1"]), int(b["y1"]))
                 for b in (json.load(open(bf, encoding="utf-8")).get("boxes") or [])]
        if boxes:
            yield os.path.splitext(os.path.basename(src))[0], src, boxes


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                                      "yi-segment", "projects", "默认项目"))
    ap.add_argument("--out", default=os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                                  "yi-segment", "glyphbank", "refs_manual.npz"))
    args = ap.parse_args()

    sources = list(refs_from_out())
    if not sources:
        sources = list(refs_from_project(args.project))
    if not sources:
        print("no hand-cut pages found (out/ or project)")
        return 1

    inks48, inks16, pages_used, boxes_used = [], [], [], []
    for name, tif, boxes in sources:
        gray = C.load_gray(tif)
        t0 = time.time()
        n = 0
        for (x0, y0, x1, y1) in boxes:
            tile = gray[y0:y1, x0:x1]
            if tile.size == 0:
                continue
            ink = norm_ink(tile)
            if int((ink > 0).sum()) < 20:
                continue
            inks48.append(ink)
            import cv2
            inks16.append(cv2.resize(ink, (COARSE, COARSE), interpolation=cv2.INTER_AREA))
            boxes_used.append((x0, y0, x1, y1))
            n += 1
        pages_used.append({"name": name, "tif": tif, "n": n})
        print("%-28s %4d glyphs  %.1fs" % (name, n, time.time() - t0), flush=True)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    np.savez_compressed(args.out, refs48=np.stack(inks48), refs16=np.stack(inks16))
    meta = {"pages": pages_used, "n": len(inks48), "built": time.strftime("%Y-%m-%d %H:%M"),
            "file": args.out}
    with open(os.path.splitext(args.out)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print("saved %d glyphs -> %s" % (len(inks48), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
