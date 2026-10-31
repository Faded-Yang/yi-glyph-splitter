"""Rebuild the project pages: restore the 6 hand-cut pages, segment the other 4.

The 6 hand-cut pages take their boxes from out/<page>/index.csv (READ ONLY).
The remaining 4 pages are segmented with the current pipeline and saved as boxes.
"""
import csv, json, os, sys, time

ROOT = r"C:/Users/95959/OneDrive/Desktop/Work/yi-segment"
os.environ.setdefault("YI_SEGMENT_HOME", os.path.join(os.environ["LOCALAPPDATA"], "yi-segment"))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient
from app.server import create_app

PROJ = os.path.join(os.environ["LOCALAPPDATA"], "yi-segment", "projects", "默认项目")
SCANS = os.path.join(ROOT, "scans")
MANUAL = ["彝族扫描3-0002_right", "彝族扫描3-0003_left", "彝族扫描3-0003_right",
          "彝族扫描3-0004_left", "彝族扫描3-0004_right", "彝族扫描3-0005_left"]
OTHER = ["彝族扫描3-0005_right", "彝族扫描3-0006_left", "彝族扫描3-0006_right", "彝族扫描3-0007_left"]

c = TestClient(create_app(project_dir=PROJ))


def wait(job_id):
    for _ in range(3000):
        j = c.get("/api/jobs/%s" % job_id).json()
        if j["state"] not in ("running", "queued"):
            return j
        time.sleep(0.5)
    raise SystemExit("job timeout")


def pages():
    st = c.get("/api/state").json()
    return {p["name"]: p["id"] for p in st.get("pages") or []}


have = pages()
need = ["%s.tif" % n for n in MANUAL + OTHER]
missing = [os.path.join(SCANS, n) for n in need if n not in have]
if missing:
    print("adding %d pages" % len(missing), flush=True)
    c.post("/api/pages/add", json={"paths": missing})
have = pages()

for name in MANUAL:
    tif = name + ".tif"
    pid = have.get(tif)
    if not pid:
        print("MISSING page", tif, flush=True)
        continue
    rows = list(csv.DictReader(open(os.path.join(ROOT, "out", name, "index.csv"), encoding="utf-8-sig")))
    boxes = [{"x0": int(float(r["gx0"])), "y0": int(float(r["gy0"])),
              "x1": int(float(r["gx1"])), "y1": int(float(r["gy1"]))} for r in rows]
    res = c.put("/api/pages/%s/boxes" % pid, json={"boxes": boxes})
    print("restored %-28s %4d boxes (%s)" % (tif, len(boxes), res.status_code), flush=True)

for name in OTHER:
    tif = name + ".tif"
    pid = have.get(tif)
    if not pid:
        print("MISSING page", tif, flush=True)
        continue
    t0 = time.time()
    job = c.post("/api/pages/process", json={"ids": [pid]}).json()["job_id"]
    j = wait(job)
    d = c.get("/api/pages/%s" % pid).json()
    print("segmented %-28s %4d boxes  %.1fs  (%s)" % (tif, len(d.get("boxes") or []), time.time() - t0, j["state"]), flush=True)

print("done")