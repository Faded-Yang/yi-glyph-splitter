"""Build glyph templates from the hand-cut export (out/<page>/index.csv)."""
from __future__ import annotations

import csv, json, os, sys
import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from yi_core import templatedp
from yi_core import common as C


def load_manual_inks():
    out_root = os.path.join(ROOT, "out")
    scans = os.path.join(ROOT, "scans")
    inks = []
    pages = []
    for d in sorted(os.listdir(out_root)):
        csvf = os.path.join(out_root, d, "index.csv")
        tif = os.path.join(scans, d + ".tif")
        if not (os.path.isfile(csvf) and os.path.isfile(tif)):
            continue
        gray = C.load_gray(tif)
        if gray is None:
            continue
        n = 0
        for r in csv.DictReader(open(csvf, encoding="utf-8-sig")):
            x0, y0, x1, y1 = (int(float(r["gx0"])), int(float(r["gy0"])),
                              int(float(r["gx1"])), int(float(r["gy1"])))
            tile = gray[y0:y1, x0:x1]
            if tile.size == 0:
                continue
            ink = _norm(tile)
            if int((ink > 0).sum()) < 20:
                continue
            inks.append(ink)
            n += 1
        pages.append({"name": d, "n": n})
        print("%-24s %4d" % (d, n), flush=True)
    return inks, pages


def _norm(gray):
    g = np.asarray(gray, np.uint8)
    thr, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    m = (g < max(60, int(thr))).astype(np.uint8)
    ys, xs = np.nonzero(m)
    if ys.size < 10:
        return np.zeros((48, 48), np.uint8)
    crop = (m[ys.min():ys.max() + 1, xs.min():xs.max() + 1] * 255).astype(np.uint8)
    h, w = crop.shape
    s = max(h, w)
    canvas = np.zeros((s, s), np.uint8)
    canvas[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = crop
    return cv2.resize(canvas, (48, 48), interpolation=cv2.INTER_AREA)


def main():
    inks, pages = load_manual_inks()
    print("total glyphs:", len(inks), flush=True)
    if not inks:
        print("no references found")
        return 1
    tpl = templatedp.templates_from_refs(inks)
    dst = os.path.join(os.environ.get("LOCALAPPDATA", ""), "yi-segment", "glyphbank", "templates.npz")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    np.savez_compressed(dst, templates=tpl)
    meta = {"templates": int(len(tpl)), "glyphs": len(inks), "pages": pages,
            "built": __import__("time").strftime("%Y-%m-%d %H:%M"), "file": dst}
    json.dump(meta, open(os.path.splitext(dst)[0] + ".json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("saved %d templates -> %s" % (len(tpl), dst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
