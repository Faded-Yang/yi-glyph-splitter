"""User glyph bank: single glyphs cut and confirmed by the user.

The standard bank comes from the printed manual, but handwriting is different;
the glyphs the user confirms (review=1) are better templates for this manuscript.
They live in %LOCALAPPDATA%/yi-segment/glyphbank/user.npz (+ user_meta.json) and
yi_ref.match() considers them alongside the standard bank (they win ties).
"""
from __future__ import annotations

import json
import os
import threading
import time

import numpy as np

from . import paths

SIDE = 48
COARSE = 16

_CACHE = None
_LOCK = threading.Lock()


def bank_dir():
    d = paths.data_dir() / "glyphbank"
    d.mkdir(parents=True, exist_ok=True)
    return d


def npz_path():
    return bank_dir() / "user.npz"


def meta_path():
    return bank_dir() / "user_meta.json"


CLUSTER_SIM = 0.72     # two glyphs are "the same form" above this cosine similarity (48x48 ink)
CLUSTER_VERSION = 3


def _unit_rows(feats: np.ndarray) -> np.ndarray:
    v = feats.reshape(len(feats), -1).astype(np.float32)
    v = v - v.mean(1, keepdims=True)
    v /= (np.linalg.norm(v, axis=1, keepdims=True) + 1e-6)
    return v


def cluster_centroids(feats: np.ndarray, entries: list):
    """(centroid matrix, cluster ids, counts) from already-clustered entries."""
    n = len(feats)
    ids = []
    for e in entries[:n]:
        cid = e.get("cluster") if isinstance(e, dict) else None
        ids.append(int(cid) if cid is not None else -1)
    good = [i for i in range(n) if ids[i] >= 0]
    if not good:
        return None, None, None
    v = _unit_rows(feats)
    uniq = sorted({ids[i] for i in good})
    idx = {c: k for k, c in enumerate(uniq)}
    C = np.zeros((len(uniq), v.shape[1]), np.float32)
    counts = np.zeros(len(uniq), np.float32)
    for i in good:
        C[idx[ids[i]]] += v[i]
        counts[idx[ids[i]]] += 1.0
    C /= np.maximum(counts[:, None], 1.0)
    C /= (np.linalg.norm(C, axis=1, keepdims=True) + 1e-6)
    return C, uniq, counts


def assign_clusters(feats: np.ndarray, entries: list) -> list:
    """Incremental greedy clustering: each glyph joins the most similar cluster.

    feats must be the 48x48 ink maps (flattened) -- 16x16 is too coarse to tell
    handwriting forms apart, 48x48 groups repeated characters reliably.
    """
    n = len(feats)
    if not n:
        return []
    v = _unit_rows(feats)
    clusters = [-1] * n
    members = {}
    next_id = 0
    for i in range(n):
        best, best_sim = -1, CLUSTER_SIM
        for cid, idxs in members.items():
            sim = float(np.max(v[idxs] @ v[i]))
            if sim > best_sim:
                best, best_sim = cid, sim
        if best < 0:
            best = next_id
            next_id += 1
            members[best] = []
        members[best].append(i)
        clusters[i] = best
    for i, e in enumerate(entries[:n]):
        if isinstance(e, dict):
            e["cluster"] = int(clusters[i])
    return clusters


def _read_meta():
    try:
        data = json.loads(open(str(meta_path()), encoding="utf-8").read())
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"entries": [], "updated": ""}


def available() -> bool:
    return os.path.isfile(str(npz_path()))


def load() -> dict:
    """Normalised user glyphs (empty dict when the user has none yet)."""
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        fp = str(npz_path())
        if not os.path.isfile(fp):
            _CACHE = {"n": 0, "f16": np.zeros((0, COARSE * COARSE), np.float32),
                      "f48": np.zeros((0, SIDE * SIDE), np.float32),
                      "labels": np.array([], dtype=object), "refs48": np.zeros((0, SIDE, SIDE), np.uint8),
                      "entries": []}
            return _CACHE
        z = np.load(fp, allow_pickle=False)
        refs48 = z["refs48"]
        refs16 = z["refs16"]
        labels = z["labels"]
        n = len(refs48)
        f16 = refs16.reshape(n, -1).astype(np.float32)
        f16 -= f16.mean(1, keepdims=True)
        f16 /= (np.linalg.norm(f16, axis=1, keepdims=True) + 1e-6)
        f48 = refs48.reshape(n, -1).astype(np.float32)
        f48 -= f48.mean(1, keepdims=True)
        f48 /= (np.linalg.norm(f48, axis=1, keepdims=True) + 1e-6)
        entries = _read_meta().get("entries") or []
        if len(entries) != n:
            entries = entries[:n] + [{}] * max(0, n - len(entries))
        meta_now = _read_meta()
        if meta_now.get("cluster_version") != CLUSTER_VERSION:
            # 只补缺失的 cluster，绝不重算整个库（重算是 O(n^2)，5000+ 字形会把程序卡死）
            feats48 = refs48.reshape(n, -1)
            C, uniq, _ = cluster_centroids(feats48, entries)
            v = _unit_rows(feats48)
            next_id = (max(uniq) + 1) if uniq else 0
            for i, e in enumerate(entries[:n]):
                if isinstance(e, dict) and e.get("cluster") is None:
                    if C is not None:
                        j = int(np.argmax(C @ v[i]))
                        e["cluster"] = int(uniq[j])
                    else:
                        e["cluster"] = next_id
                        next_id += 1
            try:
                with open(str(meta_path()), "w", encoding="utf-8") as f:
                    json.dump({"entries": entries, "cluster_version": CLUSTER_VERSION,
                               "updated": time.strftime("%Y-%m-%d %H:%M")},
                              f, ensure_ascii=False, indent=1)
            except OSError:
                pass
        _CACHE = {"refs48": refs48, "refs16": refs16, "labels": labels,
                  "f16": f16, "f48": f48, "n": n, "entries": entries}
        return _CACHE


def reset_cache():
    global _CACHE
    with _LOCK:
        _CACHE = None


def stats() -> dict:
    meta = _read_meta()
    entries = meta.get("entries") or []
    labels = {}
    for e in entries:
        lab = e.get("label") or ""
        if lab:
            labels[lab] = labels.get(lab, 0) + 1
    clusters = {}
    for e in entries:
        cid = e.get("cluster")
        if cid is None:
            continue
        clusters[cid] = clusters.get(cid, 0) + 1
    by_level = {}
    for e in entries:
        lv = e.get("level") or "unknown"
        by_level[lv] = by_level.get(lv, 0) + 1
    pages = {}
    for e in entries:
        nm = e.get("page") or "?"
        pages[nm] = pages.get(nm, 0) + 1
    page_list = [{"name": k, "n": v} for k, v in sorted(pages.items())]
    return {"n": len(entries), "labels": labels, "by_level": by_level,
            "clusters": len(clusters), "cluster_sizes": clusters,
            "pages": page_list, "pages_count": len(page_list),
            "updated": meta.get("updated") or "", "file": str(npz_path())}


def label_counts() -> dict:
    return stats().get("labels") or {}


def add_glyphs(glyphs, *, page: str = "", label_of=None) -> dict:
    """Append normalised glyphs (48x48 uint8) to the user bank.

    glyphs: iterable of dicts {ink48, x0,y0,x1,y1, label?}; duplicates are
    detected by (page, box) so re-harvesting the same page is a no-op.
    """
    items = list(glyphs or [])
    if not items:
        out = stats()
        out["added"] = 0
        return out
    meta = _read_meta()
    entries = meta.get("entries") or []
    seen = {(e.get("page") or "", e.get("x0"), e.get("y0"), e.get("x1"), e.get("y1")) for e in entries}
    new_48, new_16, new_labels, new_entries = [], [], [], []
    for g in items:
        key = (page, int(g["x0"]), int(g["y0"]), int(g["x1"]), int(g["y1"]))
        if key in seen:
            continue
        ink = np.asarray(g["ink48"], np.uint8)
        if ink.shape != (SIDE, SIDE) or int((ink > 0).sum()) < 20:
            continue
        lab = str(g.get("label") or "")
        import cv2
        small = cv2.resize(ink, (COARSE, COARSE), interpolation=cv2.INTER_AREA)
        new_48.append(ink)
        new_16.append(small)
        new_labels.append(lab)
        new_entries.append({"page": page, "x0": key[1], "y0": key[2], "x1": key[3], "y1": key[4],
                            "label": lab,
                            "review": int(g.get("review") or 0),
                            "level": str(g.get("level") or ""),
                            "saved_at": time.strftime("%Y-%m-%d %H:%M")})
        seen.add(key)
    if not new_48:
        out = stats()
        out["added"] = 0
        return out
    if entries:
        z = np.load(str(npz_path()))
        old48, old16, old_labels = z["refs48"], z["refs16"], list(z["labels"])
    else:
        old48 = np.zeros((0, SIDE, SIDE), np.uint8)
        old16 = np.zeros((0, COARSE, COARSE), np.uint8)
        old_labels = []
    refs48 = np.concatenate([old48, np.stack(new_48)], axis=0)
    refs16 = np.concatenate([old16, np.stack(new_16)], axis=0)
    labels = np.array(old_labels + new_labels, dtype=object).astype(str)
    all_entries = entries + new_entries
    feats = refs48.reshape(len(refs48), -1)
    old_n = len(entries)
    if old_n:
        C, uniq, _ = cluster_centroids(feats[:old_n], entries)
    else:
        C, uniq = None, None
    v_new = _unit_rows(feats[old_n:])
    if C is not None and len(v_new):
        sims = v_new @ C.T
        best = np.argmax(sims, axis=1)
        next_id = int(max(uniq) + 1) if uniq else 0
        for k, row in enumerate(new_entries):
            if float(sims[k, best[k]]) >= CLUSTER_SIM:
                row["cluster"] = int(uniq[int(best[k])])
            else:
                row["cluster"] = next_id
                next_id += 1
    else:
        for k, row in enumerate(new_entries):
            row["cluster"] = k
    for i, e in enumerate(entries[:old_n]):
        if isinstance(e, dict) and e.get("cluster") is None:
            e["cluster"] = (C is not None and int(uniq[0])) or 0
    np.savez_compressed(str(npz_path()), refs48=refs48, refs16=refs16, labels=labels)
    entries = all_entries
    meta = {"entries": entries, "cluster_version": CLUSTER_VERSION,
            "updated": time.strftime("%Y-%m-%d %H:%M")}
    with open(str(meta_path()), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    reset_cache()
    out = stats()
    out["added"] = len(new_48)
    return out
