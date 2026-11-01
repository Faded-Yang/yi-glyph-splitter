"""Clean big classes: move members that do not match their class mean into a noise bucket."""
import os, sys, time
import numpy as np, cv2
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from glyphmgr.core import Library, load_gray, norm_ink, unit

THR = float(sys.argv[1]) if len(sys.argv) > 1 else 0.45
def log(*a): print("[%s]" % time.strftime("%H:%M:%S"), *a, flush=True)

def ink_of(lib, m):
    page = lib.pages.get(m["page"])
    if page is None: return None
    fp = page.tile_path(m["file"])
    if not os.path.isfile(fp): return None
    try: return norm_ink(load_gray(fp))
    except Exception: return None

lib = Library(os.path.join(ROOT, "out"))
cs = lib.classes or []
log("classes:", len(cs), "threshold:", THR)
outliers = []
for c in cs:
    members = c["members"]
    if len(members) < 4:
        continue
    inks = [ink_of(lib, m) for m in members[:5]]
    inks = [i for i in inks if i is not None]
    if not inks:
        continue
    mean = np.mean(np.stack(inks).astype(np.float32), axis=0).astype(np.uint8)
    mv = unit(cv2.resize(mean, (48, 48), interpolation=cv2.INTER_AREA).reshape(1, -1))[0]
    for m in members:
        ink = ink_of(lib, m)
        if ink is None:
            continue
        v = unit(cv2.resize(ink, (48, 48), interpolation=cv2.INTER_AREA).reshape(1, -1))[0]
        if float(mv @ v) < THR:
            outliers.append(m)
log("outliers found:", len(outliers))
if outliers:
    items = [{"page": m["page"], "file": m["file"]} for m in outliers]
    r = lib.assign(items, target="new", label="杂类·疑似错分")
    log("moved:", r.get("moved"), "-> classes:", len(lib.classes or []))