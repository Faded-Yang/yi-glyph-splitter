"""One-shot local refinement: merge tiny classes, then agglomerate near-duplicates.

Zero API cost. Uses mean-ink cosine similarity of classes.
Usage: python scripts/refine_all.py [--small 3] [--thr 0.70]
"""
import os, sys, time
import numpy as np
import cv2
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from glyphmgr.core import Library, load_gray, norm_ink, unit

def log(*a):
    print("[%s]" % time.strftime("%H:%M:%S"), *a, flush=True)

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

def feats(lib, classes):
    F = np.zeros((len(classes), 48 * 48), np.float32)
    ok = []
    for i, c in enumerate(classes):
        ink = mean_ink(lib, c)
        if ink is None:
            continue
        v = unit(cv2.resize(ink, (48, 48), interpolation=cv2.INTER_AREA).reshape(1, -1))[0]
        F[i] = v
        ok.append(i)
    return F, ok

def main():
    small_n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    thr = float(sys.argv[2]) if len(sys.argv) > 2 else 0.70
    lib = Library(os.path.join(ROOT, "out"))
    log("start:", len(lib.classes or []), "classes,", len(lib.manual.get("moves") or {}), "manual moves")

    # ---- pass 1: tiny classes -> nearest bigger class
    classes = lib.classes or []
    small = [c for c in classes if c["n"] < small_n]
    big = [c for c in classes if c["n"] >= small_n]
    log("small:", len(small), "big:", len(big))
    if small and big:
        Fb, okb = feats(lib, big)
        big_ok = [big[i] for i in okb]
        FB = Fb[okb]
        Fs, oks = feats(lib, small)
        merged = 0
        for k, i in enumerate(oks):
            sims = FB @ Fs[i]
            j = int(np.argmax(sims))
            if float(sims[j]) < 0.35:
                continue
            if lib.merge_classes(small[i].get("key"), big_ok[j].get("key")).get("ok"):
                merged += 1
        log("small merged:", merged, "->", len(lib.classes or []), "classes")

    # ---- pass 2: agglomerative merge of near-duplicate classes
    classes = lib.classes or []
    F, ok = feats(lib, classes)
    cs = [classes[i] for i in ok]
    F = F[ok]
    n = len(cs)
    log("pass2 over", n, "classes")
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    sims = F @ F.T
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sims[i, j])
            if s >= thr:
                pairs.append((s, i, j))
    pairs.sort(reverse=True)
    log("candidate pairs >= %.2f:" % thr, len(pairs))
    group_size = {i: cs[i]["n"] for i in range(n)}
    merged = 0
    for s, i, j in pairs:
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        if group_size[ri] + group_size[rj] > 260:
            continue
        if lib.merge_classes(cs[rj].get("key"), cs[ri].get("key")).get("ok"):
            parent[rj] = ri
            group_size[ri] += group_size[rj]
            merged += 1
    lib.refresh_classes()
    log("pass2 merged:", merged, "| final classes:", len(lib.classes or []))

if __name__ == "__main__":
    main()