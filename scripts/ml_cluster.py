"""Offline ML clustering of the glyph library (hierarchical, scipy).

Replaces the automatic base classes with a proper agglomerative clustering on
PCA features of the 48x48 ink maps, then keeps the manual (v2 anchor) overlay.
Long-running: prints progress; safe to interrupt (only writes at the end).

Usage: python scripts/ml_cluster.py [--target 600]
"""
import json, os, sys, time
import numpy as np
import cv2
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from glyphmgr.core import Library, load_gray, norm_ink, unit

def log(*a): print("[%s]" % time.strftime("%H:%M:%S"), *a, flush=True)

def pca_random(X, k=48, seed=0):
    rng = np.random.default_rng(seed)
    R = rng.standard_normal((X.shape[1], k + 10)).astype(np.float32)
    Y = X @ R
    Q, _ = np.linalg.qr(Y)
    B = Q.T @ X
    _, _, Vt = np.linalg.svd(B, full_matrices=False)
    P = Vt[:k].T
    Z = X @ P
    return Z

def main():
    target = 600
    for i, a in enumerate(sys.argv[1:]):
        if a == "--target" and i + 2 <= len(sys.argv) - 1:
            target = int(sys.argv[i + 2])
    lib = Library(os.path.join(ROOT, "out"))
    items = []
    for name, page in lib.pages.items():
        for i, row in enumerate(page.rows, start=1):
            fp = page.tile_path(row["filename"])
            if not os.path.isfile(fp):
                continue
            try:
                img = load_gray(fp)
            except Exception:
                continue
            ink = norm_ink(img)
            if int((ink > 0).sum()) < 20:
                continue
            items.append({"page": name, "file": row["filename"], "idx": i, "ink": ink})
    log("glyphs:", len(items))
    X = np.stack([it["ink"].reshape(-1) for it in items]).astype(np.float32)
    X -= X.mean(1, keepdims=True)
    X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-6)
    log("PCA …")
    Z = pca_random(X, 48)
    Z /= (np.linalg.norm(Z, axis=1, keepdims=True) + 1e-6)
    log("pairwise distances …")
    D = 1.0 - (Z @ Z.T)
    np.fill_diagonal(D, 0.0)
    from scipy.spatial.distance import squareform
    cond = squareform(D, checks=False)
    from scipy.cluster.hierarchy import linkage, fcluster
    log("hierarchical linkage …")
    Zl = linkage(cond, method="average")
    best = None
    for thr in np.linspace(0.35, 0.95, 25):
        lab = fcluster(Zl, t=float(thr), criterion="distance")
        k = len(set(lab))
        if best is None or abs(k - target) < abs(best[0] - target):
            best = (k, float(thr), lab)
    k, thr, lab = best
    log("chosen threshold %.2f -> %d classes (target %d)" % (thr, k, target))
    groups = {}
    for i, c in enumerate(lab):
        groups.setdefault(int(c), []).append(i)
    classes = []
    for cid, idxs in groups.items():
        members = [{"page": items[i]["page"], "file": items[i]["file"], "idx": items[i]["idx"]} for i in idxs]
        classes.append({"id": 0, "n": len(members), "members": members})
    classes.sort(key=lambda c: -c["n"])
    for i, c in enumerate(classes):
        c["id"] = i
    st = os.path.join(ROOT, "out", ".glyphmgr")
    json.dump({"built": time.strftime("%Y-%m-%d %H:%M"), "sim": round(thr, 3),
               "pages": list(lib.pages), "classes": classes, "method": "hierarchical-pca48"},
              open(os.path.join(st, "classes.json"), "w", encoding="utf-8"), ensure_ascii=False)
    log("wrote classes.json:", len(classes), "classes")
    lib2 = Library(os.path.join(ROOT, "out"))
    log("with manual overlay:", len(lib2.classes or []), "classes |",
        sum(1 for c in lib2.classes or [] if c.get("label")), "labeled")
    sizes = [c["n"] for c in (lib2.classes or [])]
    if sizes:
        log("size stats: max %d, median %d, min %d" % (max(sizes), int(np.median(sizes)), min(sizes)))

if __name__ == "__main__":
    main()