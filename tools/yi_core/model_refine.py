"""Refine automatic boxes with the manuscript glyph model.

The segmenter works on ink alone; correctness of a box is "does this look like a
glyph of this manuscript".  Given the hand-cut glyph bank (GlyphModel), this pass

  * splits boxes that hold two glyphs when BOTH halves look like glyphs,
  * merges neighbouring fragments when their UNION looks like one glyph,
  * drops boxes whose content looks like nothing in the bank.

It is used by app/pipeline.py after segmentation and by the benchmark script.
"""
from __future__ import annotations

import numpy as np

from .glyphmodel import ink_of


def _tight(gray, box):
    """Tight ink bbox inside box (None when empty)."""
    import cv2
    x0, y0, x1, y1 = [int(v) for v in box]
    tile = np.asarray(gray)[y0:y1, x0:x1]
    if tile.size == 0:
        return None
    g = tile.astype(np.uint8)
    if g.ndim == 3:
        g = cv2.cvtColor(g, cv2.COLOR_RGB2GRAY)
    thr, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    m = (g < max(60, int(thr))).astype(np.uint8)
    ys, xs = np.nonzero(m)
    if ys.size < 20:
        return None
    return (x0 + int(xs.min()), y0 + int(ys.min()), x0 + int(xs.max()) + 1, y0 + int(ys.max()) + 1)


def _valleys(prof, lo_frac=0.30, hi_frac=0.70, n=3):
    """Indices of the n deepest smoothed valleys inside the central part."""
    prof = np.asarray(prof, np.float32)
    if prof.size < 12:
        return []
    k = max(3, prof.size // 14)
    ker = np.ones(k, np.float32) / k
    sm = np.convolve(prof, ker, mode="same")
    lo, hi = int(prof.size * lo_frac), int(prof.size * hi_frac)
    if hi - lo < 4:
        return []
    seg = sm[lo:hi]
    order = np.argsort(seg)[:n]
    return [lo + int(i) for i in order]


def _tight_mask(m, ox, oy):
    ys, xs = np.nonzero(m)
    if ys.size < 20:
        return None
    return (ox + int(xs.min()), oy + int(ys.min()), ox + int(xs.max()) + 1, oy + int(ys.max()) + 1)


def _bin_crop(gray, box):
    import cv2
    x0, y0, x1, y1 = [int(v) for v in box]
    tile = np.asarray(gray)[y0:y1, x0:x1]
    if tile.size == 0:
        return None
    g = tile.astype(np.uint8)
    if g.ndim == 3:
        g = cv2.cvtColor(g, cv2.COLOR_RGB2GRAY)
    thr, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return (g < max(60, int(thr))).astype(np.uint8)


def _split_candidates(gray, box, med_w, med_h):
    """Yield (axis, partA, partB) candidate splits with tight part boxes."""
    x0, y0, x1, y1 = [int(v) for v in box]
    m = _bin_crop(gray, box)
    if m is None:
        return
    h, w = m.shape
    if h > 1.28 * med_h and h >= 20:
        prof = m.sum(axis=1)
        for cut in _valleys(prof, 0.28, 0.72):
            if cut < 0.30 * med_h or (h - cut) < 0.30 * med_h:
                continue
            a = _tight_mask(m[:cut], x0, y0)
            b = _tight_mask(m[cut:], x0, y0 + cut)
            if a and b:
                yield ("h", a, b)
    if w > 1.30 * med_w and w >= 20:
        prof = m.sum(axis=0)
        for cut in _valleys(prof, 0.30, 0.70):
            if cut < 0.30 * med_w or (w - cut) < 0.30 * med_w:
                continue
            a = _tight_mask(m[:, :cut], x0, y0)
            b = _tight_mask(m[:, cut:], x0 + cut, y0)
            if a and b:
                yield ("w", a, b)


def refine_boxes(gray, boxes, model, page_name="", *, med_w=None, med_h=None,
                 split_gain=0.03, merge_gain=0.05, drop_sim=0.40, verbose=False):
    """Return refined boxes (list of tuples) + per-box similarity list."""
    if model is None or getattr(model, "size", 0) == 0 or not boxes:
        return list(boxes), []
    boxes = [(int(b[0]), int(b[1]), int(b[2]), int(b[3])) for b in boxes]
    hs = np.array([b[3] - b[1] for b in boxes], float)
    ws = np.array([b[2] - b[0] for b in boxes], float)
    med_h = float(med_h or np.median(hs))
    med_w = float(med_w or np.median(ws))
    if med_h <= 0 or med_w <= 0:
        return boxes, []

    sims = [model.similarity(ink_of(gray, b), page_name) for b in boxes]

    # 1) split two-glyph boxes when both halves are glyph-like
    out = []
    for b, s in zip(boxes, sims):
        best = None
        for axis, pa, pb in _split_candidates(gray, b, med_w, med_h):
            sa = model.similarity(ink_of(gray, pa), page_name)
            sb = model.similarity(ink_of(gray, pb), page_name)
            score = min(sa, sb)
            if score < max(0.52, s + split_gain):
                continue
            if abs((pa[3] - pa[1]) - (pb[3] - pb[1])) > 0.9 * max(med_h, 1):
                continue
            if best is None or score > best[0]:
                best = (score, pa, pb)
        if best is not None:
            out.extend([best[1], best[2]])
        else:
            out.append(b)

    # 2) drop boxes that look like nothing
    kept = []
    for b in out:
        s = model.similarity(ink_of(gray, b), page_name)
        h, w = b[3] - b[1], b[2] - b[0]
        if s < drop_sim and (h < 0.60 * med_h or w < 0.50 * med_w):
            continue
        if s < drop_sim - 0.06:
            continue
        kept.append((b, s))
    kept.sort(key=lambda t: (t[0][1], t[0][0]))

    # 3) merge neighbouring fragments whose union is more glyph-like
    merged = []
    for b, s in kept:
        if merged:
            prev, ps = merged[-1]
            gap = b[1] - prev[3]
            ov = min(prev[2], b[2]) - max(prev[0], b[0])
            ovr = ov / float(max(1, min(prev[2] - prev[0], b[2] - b[0])))
            if -0.15 * med_h <= gap <= 0.30 * med_h and ovr > 0.35:
                u = (min(prev[0], b[0]), min(prev[1], b[1]), max(prev[2], b[2]), max(prev[3], b[3]))
                su = model.similarity(ink_of(gray, u), page_name)
                if su > max(ps, s) + merge_gain and (u[3] - u[1]) <= 1.8 * med_h:
                    merged[-1] = (u, su)
                    continue
        merged.append((b, s))

    return [t[0] for t in merged], [t[1] for t in merged]
