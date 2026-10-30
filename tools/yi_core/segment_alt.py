"""Independent alternative segmenter for the Yi manuscript scans.

Cross-validation counterpart of ``segment_v1``.  The philosophy here is
deliberately different from v1:

* v1 models the *layout*  (column projection -> 1-D dynamic program on ink gaps).
* here the only structural prior is **morphological**: the strokes that belong
  to one glyph are closer to each other than one glyph is to the next, so a
  single elliptical closing turns every glyph into one blob.  Blobs that are
  clearly taller than one glyph (two glyphs fused by a stain or by touching)
  are split at the weakest internal row-profile valley.  Columns are read off
  the x-projection and used only for *ordering* and *width sanity checks*, not
  for cutting.

The result is a page dict with the same schema as ``segment_v1`` (see
``docs/SPEC.md``), so ``export`` / ``qc`` / consensus voting can treat both
segmenters interchangeably.

CLI
---
    python -m yi_core.segment_alt <tif> <out.json> [--scale 0.25] [--overlay png]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import numpy as np
import cv2

from . import common as C

# ---------------------------------------------------------------------------
# Defaults.  Lengths are expressed as fractions of the measured column pitch
# px (the horizontal distance between two written columns, ~400 px on the
# reference scan).  They are resolved to pixels by _resolve so the same code
# works on a 1/4 preview and on the full-resolution scan.
# ---------------------------------------------------------------------------
REF_PITCH = 100.0

DEFAULTS = {
    # ---- illumination / binarisation -------------------------------------
    "illum_k": 0.33,          # x px      large box-blur for paper flattening
    "sauvola_win": 0.21,      # x px      local window
    "sauvola_k": 0.25,        # -         threshold sensitivity
    "min_stroke_area": 0.0009,  # x px^2  drop dust before closing
    # ---- morphological grouping ------------------------------------------
    "close_r": 0.055,         # x px      ellipse radius: stitches strokes of one glyph
    "min_blob_area": 0.0016,  # x px^2  drop blobs that are certainly dust
    # ---- blob -> glyph pruning / splitting ------------------------------
    "split_h": 1.75,          # x median glyph height  (split if taller)
    "split_spacing": 0.55,    # x median glyph height  (min distance between cuts)
    "split_valley": 0.18,     # -         cut only where the row ink < this x mean
    "attach_gap": 0.055,      # x px      absorb a small blob this close to a glyph
    "min_frag_area": 0.10,    # x med_area  keep unabsorbed fragments at least this big
    "attach_wide": 0.22,      # x px      max gap for merging an isolated stroke back
    "min_h": 0.30,            # x med_h   reject blobs shorter than this
    "max_h": 2.30,            # x med_h   reject blobs taller than this
    "min_w": 0.16,            # x px
    "max_w": 1.10,            # x px      one glyph never spans more than a column
    "min_ink": 0.004,         # x px^2    reject blobs with almost no ink
    "min_density": 0.045,     # -         ink / bbox area  (stains are hollow)
    # ---- column model ----------------------------------------------------
    "band_thr": 0.10,         # -         fraction of the column-profile max
    "band_merge": 0.10,       # x px      merge sub-bands closer than this
    "band_min_w": 0.35,       # x px      drop slivers / binding shadow
    "band_max_w": 1.45,       # x px      split oversized bands at their minima
}

# parameters that are *not* length fractions of the column pitch
_RAW = {"sauvola_k", "min_density", "band_thr", "split_valley", "min_frag_area",
        # ratios of the median glyph height/width
        "aspect_max",
        # glyph-height ratios (relative to the measured median blob height)
        "min_h", "max_h", "split_h", "split_spacing"}


def _odd(v: float, lo: int = 3) -> int:
    v = int(round(v))
    v = max(lo, v)
    return v if v % 2 else v + 1


def _resolve(params: dict, px: float) -> dict:
    """Turn fractional defaults into concrete pixel values for this scale."""
    out = {}
    merged = dict(DEFAULTS)
    for k, v in params.items():
        if k in merged:
            merged[k] = v
    for k, v in merged.items():
        if k in _RAW:
            out[k] = float(v)
        elif k == "sauvola_win":
            out[k] = _odd(v * px)
        elif k in ("min_stroke_area", "min_blob_area", "min_ink"):
            out[k] = max(4, int(round(v * px * px)))
        else:
            out[k] = max(2, int(round(v * px)))
    out["_pitch"] = float(px)
    return out


# ---------------------------------------------------------------------------
# layout probe: column bands from the x-projection
# ---------------------------------------------------------------------------
def _col_bands(mask, px, par):
    """Ink bands along x (one per written column)."""
    prof = C.smooth1d(C.col_profile(mask), _odd(0.09 * px))
    if prof.max() <= 0:
        return []
    thr = max(1.0, prof.max() * par["band_thr"])
    bands = list(C.runs(prof > thr))
    merged = []
    for a, b in bands:
        if merged and a - merged[-1][1] - 1 < par["band_merge"]:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    out = []
    for a, b in merged:
        w = b - a + 1
        if w < par["band_min_w"]:
            continue
        if w > par["band_max_w"]:
            # binding shadow / marginal note: cut at the deepest internal minima
            k = max(1, int(round(w / px)))
            if k > 1 and w < 2.5 * px:
                seg = prof[a:b + 1]
                cuts = []
                q = max(2, int(px) // 4)
                for i in range(q, len(seg) - q):
                    lo = max(0, i - q)
                    hi = min(len(seg), i + q + 1)
                    if seg[i] == seg[lo:hi].min():
                        cuts.append(i)
                if cuts:
                    keep = sorted(sorted(cuts, key=lambda i: seg[i])[:k - 1])
                    edges = [0] + keep + [len(seg)]
                    for j in range(len(edges) - 1):
                        na, nb = a + edges[j], a + edges[j + 1] - 1
                        if nb - na + 1 >= par["band_min_w"]:
                            out.append((na, nb))
                    continue
                continue
        out.append((a, b))
    return out


def _estimate_pitch(mask, width):
    """Column pitch (px) from the periodic structure of the x-projection.

    Fallback only (band spacing is preferred and more reliable): the profile is
    high-passed so the slow paper gradient cannot dominate the autocorrelation,
    the lag range covers 2..25 columns, and a weak peak is rejected.
    """
    prof = C.smooth1d(C.col_profile(mask).astype(np.float32), _odd(width * 0.004))
    trend = C.smooth1d(prof, _odd(width * 0.08))
    p = (prof - trend).astype(np.float64)
    if p.std() < 1e-6:
        return width * 0.065
    ac = np.correlate(p, p, mode="full")[len(p) - 1:]
    ac = ac / (ac[0] + 1e-9)
    lo = max(6, int(width * 0.04))
    hi = min(len(ac) - 2, int(width * 0.40))
    if hi <= lo + 2:
        return width * 0.065
    seg = ac[lo:hi]
    if float(seg.max()) < 0.12:
        return width * 0.065
    return float(int(np.argmax(seg)) + lo)


def _layout(mask, width, params):
    """Two-pass layout probe: prior pitch -> bands -> measured pitch -> bands.

    The band spacing is used as the pitch measurement because it also covers
    crops that only contain two or three written columns (where the page-wide
    prior 0.065*width would be wrong).
    """
    p0 = width * 0.065
    bands0 = _col_bands(mask, p0, _resolve(params, p0))
    px = p0
    if len(bands0) >= 2:
        centers = [0.5 * (a + b) for a, b in bands0]
        px = float(np.median(np.diff(centers)))
        if not (0.02 * width <= px <= 0.45 * width):
            px = p0
    if px == p0:
        cand = _estimate_pitch(mask, width)
        if 0.02 * width <= cand <= 0.45 * width:
            px = cand
    par = _resolve(params, px)
    bands = _col_bands(mask, px, par)
    if len(bands) < 2 <= len(bands0):
        bands = bands0
    return px, par, bands


# ---------------------------------------------------------------------------
# blob splitting helpers
# ---------------------------------------------------------------------------
def _greedy_minima(prof, n_cuts, spacing, max_val=None):
    """Pick n_cuts low points of prof that are at least spacing apart.

    max_val (optional) rejects valleys that are not deep enough: without it a
    tall single glyph would be sliced up just because it must be split.
    """
    if n_cuts <= 0 or len(prof) < 5:
        return []
    sm = C.smooth1d(prof, _odd(max(3, int(len(prof) * 0.02))))
    cand = [i for i in range(1, len(sm) - 1) if sm[i] <= sm[i - 1] and sm[i] <= sm[i + 1]]
    cand.sort(key=lambda i: sm[i])
    picked = []
    for i in cand:
        if max_val is not None and sm[i] > max_val:
            continue
        if all(abs(i - j) >= spacing for j in picked):
            picked.append(i)
            if len(picked) == n_cuts:
                break
    return sorted(picked)


def _tight_bbox(sub_mask):
    """Tight (x0,y0,x1,y1) of a boolean sub-mask, or None if empty."""
    ys, xs = np.nonzero(sub_mask)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _split_blob(b, mask, work, med_h, par):
    """Split one blob that is clearly taller than a single glyph."""
    x0, y0, x1, y1 = b["x0"], b["y0"], b["x1"], b["y1"]
    # profile the ORIGINAL ink, not the closed mask: the closing is exactly what
    # filled the gap between two fused glyphs, so the valley only survives here
    ink = mask[y0:y1, x0:x1]
    prof = ink.sum(axis=1).astype(np.float32)
    sub = ink
    h = y1 - y0
    n_parts = max(2, min(5, int(round(h / max(med_h, 1.0)))))
    sm = C.smooth1d(prof, _odd(max(3, med_h * 0.15)))
    mean = float(sm.mean()) or 1.0
    cuts = _greedy_minima(sm, n_parts - 1, int(round(par["split_spacing"] * med_h)),
                          max_val=par["split_valley"] * mean)
    if not cuts:
        # no deep internal gap: this is one tall glyph, not two fused ones
        b = dict(b)
        b["flags"] = list(b.get("flags", [])) + ["tall"]
        return [b]
    edges = [0] + sorted(cuts) + [h]
    parts = []
    for j in range(len(edges) - 1):
        a, z = edges[j], edges[j + 1]
        seg = sub[a:z]
        seg_ink = mask[y0 + a:y0 + z, x0:x1]
        ib = _tight_bbox(seg_ink)
        if ib is None:
            continue
        parts.append({
            "x0": x0 + ib[0], "y0": y0 + a + ib[1],
            "x1": x0 + ib[2], "y1": y0 + a + ib[3],
            "area": int(seg.sum()), "ink": int(seg_ink.sum()),
            "flags": ["split"],
        })
    return parts


def _absorb_fragments(units, med_area, par, pitch):
    """Glue tiny leftovers (detached dots) onto the nearest glyph blob.

    A leftover that finds no glyph to attach to is NOT dropped here: it may be
    a real (small) glyph, so it is kept when it is still a substantial blob.
    Dropping them silently cost about 5% of the glyphs on the reference page.
    """
    if not units:
        return units, 0
    gap_max = par["attach_gap"]
    small_t = 0.35 * med_area
    big = [u for u in units if u["area"] >= small_t and u.get("flags") != ["split"]]
    small = [u for u in units if u["area"] < small_t]
    leftover_floor = par["min_frag_area"] * med_area
    n_absorbed = 0
    for s in small:
        best, best_gap = None, None
        scx = 0.5 * (s["x0"] + s["x1"])
        for u in big:
            if u["x1"] < s["x0"] - gap_max or u["x0"] > s["x1"] + gap_max:
                continue
            if u["y1"] < s["y0"] - gap_max or u["y0"] > s["y1"] + gap_max:
                continue
            ucx = 0.5 * (u["x0"] + u["x1"])
            if abs(ucx - scx) > 0.9 * pitch:
                continue
            dx = max(0, max(u["x0"] - s["x1"], s["x0"] - u["x1"]))
            dy = max(0, max(u["y0"] - s["y1"], s["y0"] - u["y1"]))
            gap = max(dx, dy)
            if best_gap is None or gap < best_gap:
                best, best_gap = u, gap
        if best is not None:
            best["x0"] = min(best["x0"], s["x0"])
            best["y0"] = min(best["y0"], s["y0"])
            best["x1"] = max(best["x1"], s["x1"])
            best["y1"] = max(best["y1"], s["y1"])
            best["ink"] = best.get("ink", 0) + s.get("ink", 0)
            best["area"] = best.get("area", 0) + s.get("area", 0)
            fl = best.setdefault("flags", [])
            if "absorbed" not in fl:
                fl.append("absorbed")
            n_absorbed += 1
        elif s["area"] >= leftover_floor:
            big.append(s)          # standalone fragment: keep as a candidate
    big.sort(key=lambda t: (t["y0"], t["x0"]))
    return big, n_absorbed


def _consolidate(glyphs, med_h, med_w, par):
    """Attach stroke-like leftovers (isolated bars / slivers) to their glyph.

    One glyph may be drawn with two to six strokes.  The closing glues most of
    them, but a stroke that sits further away survives as its own blob and would
    become a bogus extra glyph.  Stroke-like units are merged into the nearest
    glyph-like box at most attach_wide away; the rest are dropped as noise.
    """
    if not glyphs:
        return glyphs, 0
    gap_max = par["attach_wide"]

    def glyph_like(d):
        # pieces produced by an explicit valley split are glyphs, never strokes
        if "split" in d.get("flags", ()):
            return True
        w, h = d["w"], d["h"]
        asp = max(w, h) / max(1, min(w, h))
        return (asp <= 2.2 and 0.6 * med_h <= h <= 1.9 * med_h
                and 0.55 * med_w <= w <= 1.6 * med_w)

    big = [d for d in glyphs if glyph_like(d)]
    small = [d for d in glyphs if not glyph_like(d)]
    n_attached = 0
    for s in small:
        scx = 0.5 * (s["x0"] + s["x1"]); scy = 0.5 * (s["y0"] + s["y1"])
        best, best_d = None, None
        for u in big:
            dx = max(0, max(u["x0"] - s["x1"], s["x0"] - u["x1"]))
            dy = max(0, max(u["y0"] - s["y1"], s["y0"] - u["y1"]))
            if max(dx, dy) > gap_max:
                continue
            ucx = 0.5 * (u["x0"] + u["x1"]); ucy = 0.5 * (u["y0"] + u["y1"])
            dist = ((ucx - scx) ** 2 + (ucy - scy) ** 2) ** 0.5
            if best_d is None or dist < best_d:
                best, best_d = u, dist
        if best is None:
            continue
        best["x0"] = min(best["x0"], s["x0"]); best["y0"] = min(best["y0"], s["y0"])
        best["x1"] = max(best["x1"], s["x1"]); best["y1"] = max(best["y1"], s["y1"])
        best["w"] = best["x1"] - best["x0"]; best["h"] = best["y1"] - best["y0"]
        best["ink"] = int(best.get("ink", 0) + s.get("ink", 0))
        best["area"] = int(best["w"] * best["h"])
        fl = best.setdefault("flags", [])
        if "merged_stroke" not in fl:
            fl.append("merged_stroke")
        n_attached += 1
    big.sort(key=lambda t: (t["y0"], t["x0"]))
    return big, n_attached


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def segment_page(gray, *, source="", **params):
    """Segment one page into columns + single-glyph boxes (page-dict contract)."""
    t_start = time.time()
    g = np.ascontiguousarray(gray)
    if g.ndim != 2:
        raise ValueError("segment_page expects a 2-D uint8 grayscale array")
    if g.dtype != np.uint8:
        g = np.clip(g, 0, 255).astype(np.uint8)
    H, W = g.shape

    # ---- 1. illumination flattening + binarisation ------------------------
    px0 = W * 0.065
    par0 = _resolve(params, px0)
    flat = C.flatten_illumination(g, par0["illum_k"])
    # An optional second, denser Sauvola pass can be unioned in (sauvola_k2).
    # Measured on the reference scan: the dense union floods the mould stains
    # and makes the closing fuse neighbouring glyphs, so it is OFF by default.
    mask = C.binarize(flat, "sauvola", par0["sauvola_win"], par0["sauvola_k"])
    if params.get("sauvola_k2") is not None:
        mask |= C.binarize(flat, "sauvola", par0["sauvola_win"], float(params["sauvola_k2"]))
    mask = C.clean_mask(mask, min_area=par0["min_stroke_area"])

    # ---- 2. measure the real layout scale ---------------------------------
    t_layout = time.time()
    px, par, bands = _layout(mask, W, params)

    # ---- 3. morphological grouping of strokes into glyph blobs ------------
    t_morph = time.time()
    r = int(par["close_r"])
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    work = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
    work = C.clean_mask(work > 0, min_area=par["min_blob_area"])
    n_lab, lab, stats, _ = cv2.connectedComponentsWithStats(work.astype(np.uint8), connectivity=8)

    blobs = []
    for i in range(1, n_lab):
        x = int(stats[i, cv2.CC_STAT_LEFT]); y = int(stats[i, cv2.CC_STAT_TOP])
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        a = int(stats[i, cv2.CC_STAT_AREA])
        sub = work[y:y + h, x:x + w]
        ink_sub = mask[y:y + h, x:x + w]
        tb = _tight_bbox(ink_sub)
        if tb is None:
            continue
        # the closed blob only tells us *which* strokes belong together; the
        # box itself is the tight bbox of the original ink, so morphology never
        # inflates a glyph by more than the closing radius
        blobs.append({"x0": x + tb[0], "y0": y + tb[1], "x1": x + tb[2], "y1": y + tb[3],
                      "area": a, "ink": int(ink_sub.sum())})
    del lab

    hs = np.array([b["y1"] - b["y0"] for b in blobs], dtype=np.float32)
    ws = np.array([b["x1"] - b["x0"] for b in blobs], dtype=np.float32)
    areas = np.array([b["area"] for b in blobs], dtype=np.float32)
    med_h = float(np.median(hs)) if hs.size else 0.5 * px
    med_w = float(np.median(ws)) if ws.size else 0.5 * px
    med_area = float(np.median(areas)) if areas.size else 1.0
    med_h = max(med_h, 0.15 * px)

    # ---- 4. prune dust, split fused blobs ---------------------------------
    t_split = time.time()
    diag = {"n_blobs": len(blobs), "drop_dust": 0, "n_split_parts": 0,
            "drop_pre_h": 0, "drop_pre_w": 0, "n_absorbed": 0,
            "drop_h": 0, "drop_w": 0, "drop_density": 0, "drop_offcol": 0}
    units = []
    for b in blobs:
        w = b["x1"] - b["x0"]
        h = b["y1"] - b["y0"]
        if b["ink"] < max(6, int(par["min_ink"])):
            diag["drop_dust"] += 1
            continue
        if h >= par["split_h"] * med_h:
            parts = _split_blob(b, mask, work, med_h, par)
            diag["n_split_parts"] += len(parts)
            units.extend(parts)
        elif h > par["max_h"] * med_h:
            diag["drop_pre_h"] += 1
        elif w > par["max_w"]:
            diag["drop_pre_w"] += 1
        elif w < par["min_w"]:
            diag["drop_dust"] += 1
        else:
            units.append(b)

    units, n_absorbed = _absorb_fragments(units, med_area, par, px)
    diag["n_absorbed"] = n_absorbed

    # ---- 5. final plausibility filter -------------------------------------
    glyphs = []
    for u in units:
        x0, y0, x1, y1 = u["x0"], u["y0"], u["x1"], u["y1"]
        w, h = x1 - x0, y1 - y0
        if h < par["min_h"] * med_h or h > par["max_h"] * med_h:
            diag["drop_h"] += 1
            continue
        if w > par["max_w"] or w < par["min_w"]:
            diag["drop_w"] += 1
            continue
        area = w * h
        if area <= 0 or u["ink"] / area < par["min_density"]:
            diag["drop_density"] += 1
            continue
        flags = list(u.get("flags", []))
        if h > 1.45 * med_h:
            flags.append("tall")
        if h < 0.55 * med_h:
            flags.append("short")
        if w > 0.85 * px:
            flags.append("wide")
        glyphs.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1,
                       "w": w, "h": h, "ink": int(u["ink"]), "area": int(area),
                       "flags": flags})

    # ---- 5b. consolidate stroke leftovers into their parent glyph ----------
    glyphs, n_consolidated = _consolidate(glyphs, med_h, med_w, par)
    diag["n_consolidated"] = n_consolidated

    # ---- 6. column model: assign, order right->left, id in reading order --
    if not bands:
        bands = [(0, W - 1)]
    centers = [0.5 * (a + b) for a, b in bands]
    order = sorted(range(len(bands)), key=lambda i: -centers[i])
    col_info = []
    for cidx, bi in enumerate(order):
        col_info.append({"idx": cidx, "band": bands[bi], "center": centers[bi], "glyphs": []})
    for gl in glyphs:
        cx = 0.5 * (gl["x0"] + gl["x1"])
        dists = [abs(cx - c["center"]) for c in col_info]
        j = int(np.argmin(dists))
        if dists[j] > 0.75 * px:
            diag["drop_offcol"] += 1
            continue                      # marginal note / stray
        gl["col"] = j
        col_info[j]["glyphs"].append(gl)

    out_glyphs = []
    out_cols = []
    for c in col_info:
        items = sorted(c["glyphs"], key=lambda t: t["y0"])
        for ri, gl in enumerate(items):
            gl["row"] = ri
            out_glyphs.append(gl)
        if items:
            bx0 = min(t["x0"] for t in items)
            bx1 = max(t["x1"] for t in items)
            by0 = min(t["y0"] for t in items)
            by1 = max(t["y1"] for t in items)
            ink = int(mask[by0:by1, bx0:bx1].sum())
            out_cols.append({"idx": c["idx"], "x0": bx0, "x1": bx1,
                             "w": bx1 - bx0, "ink": ink,
                             "y0": by0, "y1": by1, "n": len(items)})
        else:
            a, b = c["band"]
            out_cols.append({"idx": c["idx"], "x0": int(a), "x1": int(b) + 1,
                             "w": int(b - a + 1), "ink": 0, "y0": 0, "y1": 0, "n": 0})

    for i, gl in enumerate(out_glyphs):
        gl["id"] = i + 1

    # ---- 7. QC numbers: how much of the body ink landed inside a box --------
    inside = np.zeros((H, W), dtype=bool)
    for gl in out_glyphs:
        inside[gl["y0"]:gl["y1"], gl["x0"]:gl["x1"]] = True
    col_x = np.zeros(W, dtype=bool)
    for c in out_cols:
        col_x[c["x0"]:c["x1"]] = True
    cov_num = int((mask & inside).sum())
    cov_den = int(mask[:, col_x].sum()) if col_x.any() else int(mask.sum())
    coverage = round(cov_num / cov_den, 4) if cov_den else 0.0

    page = {
        "source": source,
        "width": W,
        "height": H,
        "columns": out_cols,
        "glyphs": out_glyphs,
        "params": dict(par),
        "stats": {
            "pitch_x": round(px, 2),
            "median_blob_h": round(med_h, 2),
            "median_blob_w": round(med_w, 2),
            "n_blobs": len(blobs),
            "n_absorbed": n_absorbed,
            "n_glyphs": len(out_glyphs),
            "n_columns": len(out_cols),
            "ink_coverage": coverage,
            "diag": diag,
            "ms_flat_bin": round((t_layout - t_start) * 1000, 1),
            "ms_morph": round((t_split - t_morph) * 1000, 1),
            "ms_total": round((time.time() - t_start) * 1000, 1),
        },
    }
    return page


# ---------------------------------------------------------------------------
# scaling helpers (detect on a preview, report in full-image pixels)
# ---------------------------------------------------------------------------
def scale_page(page, factor):
    """Multiply every pixel coordinate in page by factor."""
    f = float(factor)
    out = dict(page)
    cols = []
    for c in page["columns"]:
        d = dict(c)
        for k in ("x0", "x1", "w", "y0", "y1"):
            if d.get(k) is not None:
                d[k] = int(round(d[k] * f))
        cols.append(d)
    out["columns"] = cols
    gl = []
    for t in page["glyphs"]:
        d = dict(t)
        for k in ("x0", "x1", "y0", "y1", "w", "h", "area"):
            d[k] = int(round(d[k] * f))
        gl.append(d)
    out["glyphs"] = gl
    for k in ("width", "height"):
        out[k] = int(round(page[k] * f))
    st = dict(page.get("stats", {}))
    st["preview_scale"] = 1.0 / f if f else f
    out["stats"] = st
    return out


def _downscale(gray, scale):
    if scale >= 0.999:
        return gray
    h, w = gray.shape[:2]
    return cv2.resize(gray, (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                      interpolation=cv2.INTER_AREA)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m yi_core.segment_alt",
        description="Independent (morphology based) Yi-manuscript glyph segmenter.")
    ap.add_argument("tif", help="source scan (TIF/PNG/JPG)")
    ap.add_argument("out_json", help="output page dict JSON")
    ap.add_argument("--scale", type=float, default=0.25,
                    help="detect on a preview at this scale, report full-res coords (default 0.25)")
    ap.add_argument("--overlay", default=None, help="optional QC overlay PNG")
    ap.add_argument("--target-w", type=int, default=2400, help="overlay width")
    args = ap.parse_args(argv)

    t0 = time.time()
    g = C.load_gray(args.tif)
    full_h, full_w = g.shape
    small = _downscale(g, args.scale)
    print("[alt] loaded %s %dx%d -> preview %dx%d (%.1fs)" % (
        os.path.basename(args.tif), full_w, full_h, small.shape[1], small.shape[0],
        time.time() - t0), file=sys.stderr, flush=True)
    page = segment_page(small, source=args.tif)
    if args.scale < 0.999:
        page = scale_page(page, 1.0 / args.scale)
    page["width"], page["height"] = full_w, full_h
    page["params"]["preview_scale"] = args.scale
    for t in page["glyphs"]:
        t["x0"] = max(0, min(t["x0"], full_w - 1))
        t["x1"] = max(t["x0"] + 1, min(t["x1"], full_w))
        t["y0"] = max(0, min(t["y0"], full_h - 1))
        t["y1"] = max(t["y0"] + 1, min(t["y1"], full_h))
        t["w"] = t["x1"] - t["x0"]
        t["h"] = t["y1"] - t["y0"]
    os.makedirs(os.path.dirname(os.path.abspath(args.out_json)) or ".", exist_ok=True)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(page, f, ensure_ascii=False, indent=1)
    print("[alt] %d glyphs in %d columns -> %s (%.1fs)" % (
        len(page["glyphs"]), len(page["columns"]), args.out_json, time.time() - t0),
        file=sys.stderr, flush=True)

    if args.overlay:
        # downscale while decoding: the full RGB page is ~210 MB in memory
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(args.tif) as im:
            w = int(args.target_w)
            h = max(1, int(round(im.size[1] * w / float(im.size[0]))))
            rgb = np.asarray(im.convert("RGB").resize((w, h), Image.LANCZOS))
        C.draw_overlay(np.ascontiguousarray(rgb), page, args.overlay,
                       target_w=w, draw_ids=False)
        print("[alt] overlay -> %s" % args.overlay, file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
