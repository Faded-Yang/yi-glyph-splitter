"""Template-likelihood DP for glyph segmentation.

The classic projection/ink DP only knows "is there ink at the cut".  This module
adds the missing question: "does the region between two cuts look like a glyph of
THIS manuscript?"  It is answered with templates distilled from the hand-cut
pages (script build_templates.py), so nothing here is hand-tuned to the Latin
alphabet or to printed script.

Per column band:
  1. resize the binary band to a fixed width (64 px) -> strip
  2. score every window of height H' with normalised cross-correlation against
     a bank of glyph templates (3 heights)  -> score[y]
  3. DP over cut positions: maximise sum(score) with a glyph-height prior and a
     mild penalty for cutting through ink.

Result: cut positions (page coordinates) that prefer "glyph-like" windows.
"""
from __future__ import annotations

import cv2
import numpy as np

STRIP_W = 64
H_FACTORS = (0.85, 1.0, 1.15)

_TEMPLATES = None


def template_path():
    import os
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "yi-segment", "glyphbank", "templates.npz")


def set_templates(tpl):
    global _TEMPLATES
    _TEMPLATES = tpl


def load_default(force=False):
    """Load %LOCALAPPDATA%/yi-segment/glyphbank/templates.npz (None when absent)."""
    global _TEMPLATES
    if _TEMPLATES is not None and not force:
        return _TEMPLATES
    import os
    fp = template_path()
    if not os.path.isfile(fp):
        return None
    try:
        z = np.load(fp)
        tpl = z["templates"]
        _TEMPLATES = tpl
        return tpl
    except Exception:
        return None


def available():
    return load_default() is not None


def templates_from_refs(inks, side_w=STRIP_W):
    """Cluster reference 48x48 ink maps into templates (k-means, numpy only)."""
    if len(inks) == 0:
        return np.zeros((0, side_w, side_w), np.uint8)
    X = np.stack([cv2.resize(ink, (side_w, side_w), interpolation=cv2.INTER_AREA)
                  for ink in inks]).astype(np.float32) / 255.0
    flat = X.reshape(len(X), -1)
    flat = flat - flat.mean(1, keepdims=True)
    flat /= (np.linalg.norm(flat, axis=1, keepdims=True) + 1e-6)
    k = min(64, len(X))
    # k-means++ style init: farthest-first
    idx = [int(np.argmax(flat.std(1)))]
    for _ in range(1, k):
        sims = flat @ flat[idx].T
        pick = int(np.argmin(sims.max(1)))
        if pick in idx:
            remaining = [i for i in range(len(flat)) if i not in idx]
            if not remaining:
                break
            pick = remaining[0]
        idx.append(pick)
    C = flat[idx].copy()
    for _ in range(12):
        sims = flat @ C.T
        lab = np.argmax(sims, 1)
        for c in range(len(C)):
            m = lab == c
            if m.any():
                v = flat[m].mean(0)
                C[c] = v / (np.linalg.norm(v) + 1e-6)
    return C.reshape(len(C), side_w, side_w)


def score_curve(strip_bin, templates, target_h=None):
    """Max NCC score for every window position.

    target_h: expected glyph height AFTER the strip is scaled to STRIP_W; the
    templates are square normalised ink maps, so they are reshaped to the
    manuscript's real glyph aspect instead of being used as squares.
    """
    if strip_bin.size == 0 or len(templates) == 0:
        return np.zeros(strip_bin.shape[0])
    img = strip_bin.astype(np.float32)
    img = cv2.resize(img, (STRIP_W, strip_bin.shape[0]), interpolation=cv2.INTER_AREA)
    n = img.shape[0]
    if target_h is None:
        target_h = max(8, int(round(n * 0.55)))
    best = np.full(n, -1.0, np.float32)
    factors = H_FACTORS
    cache = {}
    for tw in templates:
        key = id(tw)
        for f in factors:
            th = max(6, int(round(target_h * f)))
            if th >= n:
                continue
            kk = (key, th)
            v = cache.get(kk)
            if v is None:
                v = cv2.resize(tw, (STRIP_W, th), interpolation=cv2.INTER_AREA).astype(np.float32)
                cache[kk] = v
            res = cv2.matchTemplate(img, v, cv2.TM_CCOEFF_NORMED).reshape(-1)
            best[:len(res)] = np.maximum(best[:len(res)], res)
    best[best < -0.5] = 0.0
    return np.maximum(best, 0.0)


def dp_cuts(strip_bin, templates, H, *, sim_w=6.0, h_w=2.2, ink_w=0.6,
            ink_curve=None, min_frac=0.62, max_frac=1.55):
    """Return cut positions in STRIP coordinates (ascending, 0..n)."""
    n = strip_bin.shape[0]
    if n < 3 * H or len(templates) == 0:
        return None
    scores = score_curve(strip_bin, templates)
    m = len(scores)
    if m <= 0:
        return None
    # downsample the DP grid to keep it fast at full resolution
    step = max(1, int(round(H / 12.0)))
    g = np.arange(0, n + 1, step)
    if g[-1] != n:
        g = np.append(g, n)
    K = len(g)
    INF = 1e18
    dp = np.full(K, INF, np.float64)
    back = np.zeros(K, np.int32)
    dp[0] = 0.0
    ink_norm = None
    if ink_curve is not None and len(ink_curve) == n:
        ink_norm = ink_curve.astype(np.float64)
        ink_norm = ink_norm / (np.median(ink_norm[ink_norm > 0]) + 1e-6)
    lo = int(np.floor(min_frac * H))
    hi = int(np.ceil(max_frac * H))
    for j in range(1, K):
        yj = int(g[j])
        i0 = max(0, j - int(np.ceil(max_frac * H / step)) - 1)
        for i in range(i0, j):
            h = yj - int(g[i])
            if h < lo or h > hi:
                continue
            s = scores[min(m - 1, int(g[i]))] if int(g[i]) < m else 0.0
            cost = dp[i] - sim_w * s + h_w * (np.log(max(h, 1) / H) ** 2)
            if ink_norm is not None:
                cost += ink_w * ink_norm[min(n - 1, yj)]
            if cost < dp[j]:
                dp[j] = cost
                back[j] = i
    if dp[K - 1] >= INF:
        return None
    cuts = [n]
    j = K - 1
    while j > 0:
        j = int(back[j])
        cuts.append(int(g[j]))
    cuts.reverse()
    return cuts
