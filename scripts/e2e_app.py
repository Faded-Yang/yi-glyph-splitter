"""End-to-end acceptance test for the desktop app backend (uses the real TIF).

    python scripts/e2e_app.py [--fast]

--fast uses a small crop of the page (a 1200x2400 window saved as a scratch TIF) so the
segmentation step takes ~1s instead of ~13s.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TIF = r"A:\Downloads\xwechat_files\wxid_0vb7dipk5onr22_9677\msg\file\2026-10\彝族扫描3-0002_right.tif"

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    if not cond:
        failures.append(name)


def wait_job(c, jid, timeout=300, label=""):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        r = c.get("/api/jobs/%s" % jid)
        if r.status_code != 200:
            return None
        j = r.json()
        if j.get("state") != last:
            last = j.get("state")
            print("    [%s] %s %s/%s %s" % (label, last, j.get("done"), j.get("total"), j.get("message") or ""))
        if j.get("state") in ("done", "error", "canceled"):
            return j
        time.sleep(0.4)
    return {"state": "timeout"}


def small_page(tmp):
    """The whole page at 1/3 resolution (same layout, ~2 s instead of ~13 s)."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(TIF)
    w, h = im.size
    small = im.resize((w // 3, h // 3), Image.LANCZOS)
    p = os.path.join(tmp, "page_third.tif")
    small.save(p)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the temp project for inspection")
    args = ap.parse_args()

    from app.server import create_app
    from fastapi.testclient import TestClient

    tmp = tempfile.mkdtemp(prefix="yiseg_e2e_")
    proj = os.path.join(tmp, "proj")
    outdir = os.path.join(tmp, "tiles_out")
    os.makedirs(proj, exist_ok=True)
    os.makedirs(outdir, exist_ok=True)

    page_file = small_page(tmp) if args.fast else TIF
    print("page:", page_file)

    app = create_app(project_dir=proj)
    c = TestClient(app)

    print("\n[1] state")
    r = c.get("/api/state")
    check("GET /api/state 200", r.status_code == 200, r.status_code)
    st = r.json()
    check("state has pages list", isinstance(st.get("pages"), list))
    check("state has settings", isinstance(st.get("settings"), dict))

    print("\n[2] add page")
    r = c.post("/api/pages/add", json={"paths": [page_file]})
    check("POST /api/pages/add 200", r.status_code == 200, r.text[:200])
    added = r.json()
    pages = added.get("pages") or added.get("added")
    check("one page registered", bool(pages) and len(pages) == 1, pages)
    pid = (st.get("pages") or [{}])[0].get("id") if False else pages[0]["id"]
    print("    pid =", pid)

    print("\n[3] process (segment)")
    t0 = time.time()
    r = c.post("/api/pages/process", json={"ids": [pid]})
    check("POST /api/pages/process 200", r.status_code == 200, r.text[:200])
    jid = r.json()["job_id"]
    j = wait_job(c, jid, label="process")
    check("process finished", j and j.get("state") == "done", j and j.get("state"))
    print("    segmentation took %.1fs" % (time.time() - t0))

    print("\n[4] page payload")
    r = c.get("/api/pages/%s" % pid)
    check("GET /api/pages/{pid} 200", r.status_code == 200, r.status_code)
    page = r.json()
    boxes = page["boxes"]
    check("got boxes", len(boxes) > 50, len(boxes))
    check("boxes have coords", all(all(k in b for k in ("x0", "y0", "x1", "y1")) for b in boxes[:5]))
    pv = c.get("/api/pages/%s/preview.png" % pid)
    check("GET preview.png 200/png", pv.status_code == 200 and pv.content[:8] == b"\x89PNG\r\n\x1a\n", pv.status_code)

    print("\n[5] crop endpoint")
    b0 = boxes[0]
    r = c.get("/api/pages/%s/crop" % pid, params={"x0": b0["x0"], "y0": b0["y0"], "x1": b0["x1"], "y1": b0["y1"], "scale": 2})
    check("GET crop 200/png", r.status_code == 200 and r.content[:4] == b"\x89PNG", r.status_code)
    r = c.get("/api/pages/%s/crop" % pid, params={"x0": -999, "y0": -999, "x1": 50, "y1": 50})
    check("crop clamps out-of-range", r.status_code in (200, 400, 404), r.status_code)

    print("\n[6] edit boxes (PUT)")
    edited = [dict(b) for b in boxes]
    edited[0] = dict(edited[0])
    edited[0]["x0"] += 5
    edited[0]["review"] = 1
    r = c.put("/api/pages/%s/boxes" % pid, json={"boxes": edited})
    check("PUT boxes 200", r.status_code == 200, r.text[:300])
    after = r.json()["boxes"]
    check("box count preserved", len(after) == len(edited), (len(after), len(edited)))
    check("edit persisted", abs(after[0]["x0"] - edited[0]["x0"]) <= 2, (after[0]["x0"], edited[0]["x0"]))
    r2 = c.get("/api/pages/%s" % pid).json()
    check("GET reflects edit", abs(r2["boxes"][0]["x0"] - edited[0]["x0"]) <= 2)
    # delete one box
    fewer = [dict(b) for b in after]
    del fewer[3]
    r = c.put("/api/pages/%s/boxes" % pid, json={"boxes": fewer})
    check("PUT delete box", r.status_code == 200 and len(r.json()["boxes"]) == len(fewer), r.status_code)
    # restore
    c.put("/api/pages/%s/boxes" % pid, json={"boxes": after})

    print("\n[7] 规范字校验（离线字库）")
    r = c.post("/api/pages/%s/refcheck" % pid)
    check("POST refcheck 200", r.status_code == 200, r.text[:200])
    if r.status_code == 200:
        jid = r.json()["job_id"]
        j = wait_job(c, jid, label="refcheck")
        check("refcheck job finished", j and j.get("state") == "done", j and j.get("state"))
        rc = c.get("/api/pages/%s/refcheck" % pid)
        check("GET refcheck 200", rc.status_code == 200, rc.status_code)
        body = rc.json() or {}
        stats = body.get("stats") or {}
        check("refcheck scored every box", stats.get("n") == len(boxes), (stats.get("n"), len(boxes)))
        check("refcheck median in (0,1)", 0.0 < float(stats.get("median") or 0) < 1.0, stats.get("median"))
        first = (body.get("result") or {}).get("boxes") or []
        if first:
            rr = c.get("/api/glyphref/%d.png" % first[0]["top"][0]["idx"])
            check("GET glyphref png", rr.status_code == 200 and rr.content[:4] == b"\x89PNG", rr.status_code)

    print("\n[8] export tiles")
    r = c.post("/api/pages/%s/export" % pid,
               json={"out": outdir, "pad_ratio": 0.10, "min_pad": 6, "order": "visual", "fmt": "png"})
    check("POST export 200", r.status_code == 200, r.text[:300])
    jid = r.json()["job_id"]
    j = wait_job(c, jid, label="export")
    check("export finished", j and j.get("state") == "done", j and j.get("state"))
    subdirs = [d for d in os.listdir(outdir) if os.path.isdir(os.path.join(outdir, d))]
    check("export created a page dir", len(subdirs) == 1, subdirs)
    exp = os.path.join(outdir, subdirs[0]) if subdirs else outdir
    tiles = [f for f in os.listdir(exp) if f.endswith(".png") and f[0].isdigit()]
    csv_path = os.path.join(exp, "index.csv")
    has_csv = os.path.exists(csv_path)
    n_csv = sum(1 for _ in open(csv_path, encoding="utf-8-sig")) - 1 if has_csv else -1
    check("tiles written", len(tiles) > 50, len(tiles))
    check("index.csv rows == tiles", n_csv == len(tiles), (n_csv, len(tiles)))
    check("sheet.png exists", os.path.exists(os.path.join(exp, "sheet.png")))

    print("\n[9] error handling")
    r = c.get("/api/pages/nonexistent")
    check("unknown page -> 404", r.status_code == 404, r.status_code)
    r = c.post("/api/pages/add", json={"paths": [r"C:\no\such\file.tif"]})
    check("bad path -> 4xx", 400 <= r.status_code < 500, r.status_code)
    r = c.put("/api/pages/%s/boxes" % pid, json={"boxes": [{"x0": 10, "y0": 10, "x1": 5, "y1": 5}]})
    check("degenerate box rejected or fixed", r.status_code in (200, 400), r.status_code)

    print("\n==== %d checks, %d failures ====" % (0, len(failures)))
    if failures:
        print("FAILED:", failures)
    if args.keep:
        print("temp project kept at", tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
