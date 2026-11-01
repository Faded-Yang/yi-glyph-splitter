"""Offline label propagation: give unlabeled classes the label of the nearest labeled class.

Zero API cost: uses normalised mean ink maps and cosine similarity.
Usage: python scripts/propagate_labels.py [--sim 0.62]
"""
import json, os, sys
import numpy as np
import cv2
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from glyphmgr.core import Library, load_gray, norm_ink, unit

def main():
    sim_thr = float(sys.argv[1]) if len(sys.argv) > 1 else 0.62
    lib = Library(os.path.join(ROOT, "out"))
    classes = lib.classes or []
    feats, labels = [], []
    for c in classes:
        inks = []
        for m in c["members"][:3]:
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
            feats.append(None); labels.append(c.get("label") or ""); continue
        feats.append(np.mean(np.stack(inks).astype(np.float32), axis=0).astype(np.uint8))
        labels.append(c.get("label") or "")
    idx_ok = [i for i, f in enumerate(feats) if f is not None]
    V = unit(np.stack([cv2.resize(feats[i], (48, 48), interpolation=cv2.INTER_AREA).reshape(-1)
                       for i in idx_ok]))
    pos = {i: k for k, i in enumerate(idx_ok)}
    labeled = [k for k, i in enumerate(idx_ok) if labels[i]]
    if not labeled:
        print("no labeled classes"); return
    L = V[labeled]
    n = 0
    for k, i in enumerate(idx_ok):
        if labels[i]:
            continue
        sims = L @ V[k]
        j = int(np.argmax(sims))
        if float(sims[j]) >= sim_thr:
            lib.set_label(classes[i].get("key"), "≈" + labels[idx_ok[labeled[j]]])
            n += 1
    print("propagated labels:", n, "| remaining unlabeled:",
          sum(1 for c in (lib.classes or []) if not c.get("label")))

if __name__ == "__main__":
    main()