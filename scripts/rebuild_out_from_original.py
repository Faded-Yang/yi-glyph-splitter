"""Rebuild every out/<page> tile from the ORIGINAL scan at native resolution.

For each page directory:
  * find the source TIF (marker name; fall back to stripping a "_xxxx" suffix)
  * re-crop every tile from that TIF using the glyph box + the page's own pad policy
  * rewrite index.csv and sheet.png, verify a random sample pixel-for-pixel
Existing tiles are backed up under out/.glyphmgr/rebuild/<page>/<time>/ first.
"""
import csv, json, os, random, shutil, sys, time
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from glyphmgr.core import Page, Library

def log(*a): print("[%s]" % time.strftime("%H:%M:%S"), *a, flush=True)

def find_tif(page_dir, scans):
    marker = os.path.join(page_dir, ".yisegment_page.json")
    names = []
    if os.path.isfile(marker):
        try:
            names.append(json.load(open(marker, encoding="utf-8")).get("name") or "")
        except Exception:
            pass
    names.append(os.path.basename(page_dir) + ".tif")
    # drop an export-collision suffix such as "_46a5"
    base = os.path.basename(page_dir)
    if "_" in base:
        names.append(base.rsplit("_", 1)[0] + ".tif")
    for n in names:
        if not n:
            continue
        fp = os.path.join(scans, os.path.basename(n))
        if os.path.isfile(fp):
            return fp
    return ""

def main():
    out_root = os.path.join(ROOT, "out")
    scans = os.path.join(ROOT, "scans")
    lib = Library(out_root)
    total_pages = 0
    total_tiles = 0
    for name, page in sorted(lib.pages.items()):
        tif = find_tif(os.path.join(out_root, name), scans)
        if not tif:
            log("SKIP %s (no source TIF)" % name)
            continue
        if not page.rows:
            continue
        bak = os.path.join(out_root, ".glyphmgr", "rebuild", name, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(bak, exist_ok=True)
        for fn in os.listdir(os.path.join(out_root, name)):
            if fn.lower().endswith(".png") or fn == "index.csv":
                shutil.copy2(os.path.join(out_root, name, fn), os.path.join(bak, fn))
        page.root = out_root
        page.marker = {"name": os.path.basename(tif)}
        boxes = page.glyph_boxes()
        n = page.reexport(boxes)
        total_pages += 1
        total_tiles += n
        log("%-26s rebuilt %4d tiles from %s" % (name, n, os.path.basename(tif)))

    log("done: %d pages, %d tiles" % (total_pages, total_tiles))

    # verification pass: sample tiles must equal native crops
    lib2 = Library(out_root)
    for name, page in sorted(lib2.pages.items()):
        tif = find_tif(os.path.join(out_root, name), scans)
        if not tif or not page.rows:
            continue
        rgb = np.asarray(Image.open(tif).convert("RGB"))
        bad = 0
        sample = random.sample(page.rows, min(5, len(page.rows)))
        for r in sample:
            x0, y0, x1, y1 = (int(float(r["x0"])), int(float(r["y0"])), int(float(r["x1"])), int(float(r["y1"])))
            fp = os.path.join(out_root, name, r["filename"])
            tile = np.asarray(Image.open(fp).convert("RGB"), dtype=np.int16)
            crop = rgb[y0:y1, x0:x1].astype(np.int16)
            if tile.shape != crop.shape or int(np.abs(tile - crop).max()) > 2:
                bad += 1
        log("%-26s verify: %d/5 mismatches" % (name, bad))

if __name__ == "__main__":
    main()