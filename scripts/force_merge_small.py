"""Force-merge every class with <3 members into the most similar bigger class."""
import os, sys, time
import numpy as np, cv2
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from glyphmgr.core import Library, load_gray, norm_ink, unit

def log(*a): print("[%s]" % time.strftime("%H:%M:%S"), *a, flush=True)

def feat(lib, c):
    inks = []
    for m in c["members"][:3]:
        page = lib.pages.get(m["page"])
        if page is None: continue
        fp = page.tile_path(m["file"])
        if not os.path.isfile(fp): continue
        try: inks.append(norm_ink(load_gray(fp)))
        except Exception: continue
    if not inks: return None
    ink = np.mean(np.stack(inks).astype(np.float32), axis=0).astype(np.uint8)
    return unit(cv2.resize(ink, (48, 48), interpolation=cv2.INTER_AREA).reshape(1, -1))[0]

def main():
    lib = Library(os.path.join(ROOT, "out"))
    classes = lib.classes or []
    small = [c for c in classes if c["n"] < 3]
    big = [c for c in classes if c["n"] >= 3]
    log("classes:", len(classes), "small:", len(small), "big:", len(big))
    if not small:
        print("nothing to do"); return
    big_nodes = [(c, feat(lib, c)) for c in big]
    big_nodes = [(c, f) for c, f in big_nodes if f is not None]
    FB = np.stack([f for _, f in big_nodes])
    merged = 0
    for c in small:
        f = feat(lib, c)
        if f is None:
            continue
        sims = FB @ f
        j = int(np.argmax(sims))
        if lib.merge_classes(c.get("key"), big_nodes[j][0].get("key")).get("ok"):
            merged += 1
    lib.refresh_classes()
    cs = lib.classes or []
    log("merged:", merged, "| classes:", len(cs), "| still n<3:", sum(1 for c in cs if c["n"] < 3))

if __name__ == "__main__":
    main()