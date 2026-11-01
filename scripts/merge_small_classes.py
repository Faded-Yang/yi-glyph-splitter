"""Merge classes with <3 members into the most similar larger class (zero API cost)."""
import json, os, sys
import numpy as np
import cv2
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from glyphmgr.core import Library, load_gray, norm_ink, unit

def mean_ink(lib, c, k=3):
    inks = []
    for m in c["members"][:k]:
        page = lib.pages.get(m["page"])
        if page is None:
            continue
        fp = page.tile_path(m["file"])
        if not os.path.isfile(fp):
            continue
        try:
            inks.append(norm_ink(load_gray(fp)))
        except Exception:
            continue
    if not inks:
        return None
    return np.mean(np.stack(inks).astype(np.float32), axis=0).astype(np.uint8)

def main():
    thr = float(sys.argv[1]) if len(sys.argv) > 1 else 0.35
    min_n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    lib = Library(os.path.join(ROOT, "out"))
    classes = lib.classes or []
    print("before:", len(classes), "classes")
    small = [c for c in classes if c["n"] < min_n]
    big = [c for c in classes if c["n"] >= min_n]
    print("small:", len(small), "| big:", len(big))
    if not small or not big:
        print("nothing to do")
        return
    def feat(c):
        ink = mean_ink(lib, c)
        if ink is None:
            return None
        return unit(cv2.resize(ink, (48, 48), interpolation=cv2.INTER_AREA).reshape(1, -1))[0]
    big_feats = [(c, feat(c)) for c in big]
    big_feats = [(c, f) for c, f in big_feats if f is not None]
    F = np.stack([f for _, f in big_feats])
    merged = 0
    skipped = 0
    for c in small:
        f = feat(c)
        if f is None:
            skipped += 1
            continue
        sims = F @ f
        j = int(np.argmax(sims))
        if float(sims[j]) < thr:
            skipped += 1
            continue
        target = big_feats[j][0]
        r = lib.merge_classes(c.get("key"), target.get("key"))
        if r.get("ok"):
            merged += 1
        else:
            skipped += 1
    lib.refresh_classes()
    print("merged small classes:", merged, "| skipped:", skipped)
    print("after:", len(lib.classes or []), "classes")

if __name__ == "__main__":
    main()