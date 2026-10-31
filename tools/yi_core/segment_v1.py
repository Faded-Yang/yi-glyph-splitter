"""segment_v1 -- main Yi-script glyph segmentation.

PIPELINE
  1. illumination flatten  (large box blur / division) -> even paper, ink stays black
  2. Sauvola binarisation  (integral-image mean/std, window ~ 1 glyph)
  3. component filtering   (area, darkness, bbox size) -> dust / stains / ghost ink / frames
  4. horizontal blocks     (blank row corridors split the page: body vs marginalia)
  5. column bands per block (close the mask vertically -> solid bars -> x-profile peaks)
  6. per-column DP cut     (row ink profile + glyph-height prior; cut cost = ink crossed)
  7. fragment merge        (detached dots / broken strokes glued back to their body)
  8. bbox from real ink    (tight box of the assigned components) + filtering + reading order

All geometry parameters are declared at FULL resolution and scaled by 'base_scale'.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

import cv2
import numpy as np

from . import common as C


@dataclass
class Params:
    # --- preprocessing (full-res pixel units) ---------------------------------
    flatten_ksize: int = 201
    sauvola_win: int = 81
    sauvola_k: float = 0.30
    dark_thr: int = 110          # flattened value below which a pixel counts as real ink
    min_comp_area: int = 90      # px (full-res)
    dark_frac_min: float = 0.28  # fraction of dark pixels inside a component
    border_frac: float = 0.008   # ignore ink closer than this to the page edge
    # --- scanner-edge dark bands (binding shadow / black scan edge) ----------
    edge_clean: bool = True
    edge_band_frac: float = 0.07   # only look at this fraction of the width near a side
    edge_band_ink: float = 0.35    # column ink fraction that makes it a solid dark band
    edge_band_gap: int = 30        # bridge gaps up to this many px inside a band
    max_comp_side: float = 1.75  # drop components with bbox side > this * glyph width
    max_comp_area: float = 6.0   # drop components with area > this * glyph_width^2
    rule_len_ratio: float = 3.0  # drop thin components longer than this * glyph width
    # --- horizontal blocks ---------------------------------------------------
    block_gap_frac: float = 0.85  # blank row corridor (>= this * glyph height) splits blocks
    block_min_ink: float = 0.0005  # ignore blocks with less ink than this * page area
    # --- column detection ----------------------------------------------------
    col_valley_frac: float = 0.40  # valley below this * weaker peak -> real corridor
    min_col_w_ratio: float = 0.25  # min band width as fraction of the median band width
    col_merge_frac: float = 0.28   # bands whose centres differ less than this * gw are one column
    # --- per-column DP -------------------------------------------------------
    shape_w: float = 8.0         # weight of (log(h/H))^2 shape penalty
    cut_base: float = 0.9        # fixed cost of every cut (prevents over-splitting)
    cut_w: float = 2.0           # extra cost proportional to the ink crossed by the cut
    min_h_frac: float = 0.45     # DP search bounds
    max_h_frac: float = 2.10
    h_refine: bool = True        # re-run DP with the median segment height
    h_consensus: bool = True     # clamp each column's H to the page-wide glyph height
    h_consensus_lo: float = 0.72
    h_consensus_hi: float = 1.35
    # --- fragments / filtering ----------------------------------------------
    frag_merge_frac: float = 0.45
    frag_gap_frac: float = 0.55
    min_ink_frac: float = 0.05   # drop glyphs with ink < this * median glyph ink
    ink_frac_max: float = 0.55   # drop "glyphs" whose ink fills more of the box (stain blob)
    flat_veto: float = 110.0     # drop glyphs whose ink is not dark on the flattened image
    core_frac_min: float = 0.15  # drop glyphs with almost no really-dark pixels
    core_thr: float = 60.0       # "really dark" on the flattened image
    # --- post-split: boxes that still hold two glyphs -------------------------
    post_split: bool = True
    ps_h_ratio: float = 1.45     # only try a row split when h > ratio * H
    ps_gap_frac: float = 0.15    # blank band must be at least this * box extent
    ps_part_frac: float = 0.48   # each part must be at least this * H
    ps_w_ratio: float = 1.55     # column split when w > ratio * h
    bbox_clip: float = 0.45      # clip a glyph bbox to band +- this * band width
    # --- learning post-pass: split boxes that are still two glyphs ----------
    tall_split: bool = False     # enabled from data/calibration/segment_params.json
    tall_split_ratio: float = 1.30  # split when h > this * median glyph height
    tall_split_depth: float = 0.65  # valley row ink must be < this * median row ink
    tall_split_min_part: float = 0.35  # each half must be >= this * median height
    wide_split: bool = True          # also split boxes that hold two glyphs side by side
    wide_split_ratio: float = 1.50   # split when w > this * median glyph width
    wide_split_depth: float = 0.65   # col valley ink must be < this * median col ink
    # --- stain blobs (smooth filled patches look nothing like strokes) -------
    tpl_dp: bool = True              # template-likelihood DP (falls back if templates.npz missing)
    tpl_sim_w: float = 6.0           # weight of the glyph-likeness score
    tpl_h_w: float = 2.2             # glyph height prior weight
    tpl_ink_w: float = 0.6           # penalty for cutting through ink
    stain_filter: bool = True
    tighten_boxes: bool = True       # shrink box onto the glyph (drop stray specks)
    blob_solid: float = 0.85         # area / convex-hull area above this = blob-like
    blob_fill: float = 0.35          # ink fraction above this = filled patch
    # --- misc ---------------------------------------------------------------
    verbose: bool = False


# --------------------------------------------------------------- small utils
def _odd(v) -> int:
    v = int(round(v))
    return v if v % 2 else v + 1


def _scaled(p: Params, s: float) -> Params:
    q = copy.deepcopy(p)
    for name in ("flatten_ksize", "sauvola_win", "min_comp_area"):
        setattr(q, name, max(1, int(round(getattr(p, name) * s))))
    q.flatten_ksize = _odd(q.flatten_ksize)
    q.sauvola_win = _odd(q.sauvola_win)
    return q


def _prep(img: np.ndarray, p: Params):
    flat = C.flatten_illumination(img, _odd(p.flatten_ksize))
    mask = C.binarize(flat, "sauvola", win=_odd(p.sauvola_win), k=p.sauvola_k)
    return flat, mask


# ------------------------------------------------------------ components
def _clear_dark_edge_bands(mask: np.ndarray, p: "Params") -> np.ndarray:
    """Wipe solid dark bands hugging the left/right page edge.

    Scans often carry a black scanner edge or the binding shadow; components
    inside it are dark and tall, survive the ink filters, and steal a whole
    column band from the real text (0003_left: ~170 bogus boxes on the strip).
    """
    h, w = mask.shape
    if not p.edge_clean or w < 200:
        return mask
    col = mask.mean(axis=0).astype(np.float32)
    k = max(3, w // 400)
    ker = np.ones(k, np.float32) / float(k)
    col_s = np.convolve(col, ker, mode="same")
    limit = max(8, int(w * p.edge_band_frac))
    out = mask.copy()
    cleared = 0
    for side in ("left", "right"):
        if side == "left":
            lo, hi = 0, min(limit, w)
        else:
            lo, hi = max(0, w - limit), w
        x = lo
        while x < hi:
            if col_s[x] <= p.edge_band_ink:
                x += 1
                continue
            end = x
            gap = 0
            probe = x + 1
            while probe < hi:
                if col_s[probe] > p.edge_band_ink:
                    end = probe
                    gap = 0
                else:
                    gap += 1
                    if gap > p.edge_band_gap:
                        break
                probe += 1
            out[:, x:end + 1] = False
            cleared += end + 1 - x
            x = end + 1
    if cleared and p.verbose:
        print("edge bands cleared: %d px columns" % cleared)
    return out


def _component_stats(mask: np.ndarray, flat: np.ndarray, p: Params):
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return (lab, stats, cent, np.zeros(1, np.int64), np.zeros(1, np.float32),
                np.zeros(1, np.float32))
    areas = stats[:, cv2.CC_STAT_AREA].astype(np.int64)
    cnt = np.bincount(lab[flat < p.dark_thr], minlength=n).astype(np.int64)
    dark_frac = cnt.astype(np.float32) / np.maximum(areas, 1)
    m = mask.astype(bool)
    fsum = np.bincount(lab[m], weights=flat[m].astype(np.float64), minlength=n)
    flat_mean = (fsum / np.maximum(areas, 1)).astype(np.float32)
    return lab, stats, cent, areas, dark_frac, flat_mean


def _component_keep(stats, areas, dark_frac, p: Params, glyph_w: float):
    """Keep only components that could be (part of) a glyph stroke."""
    n = len(areas)
    keep = np.zeros(n, dtype=bool)
    gw = max(glyph_w, 1.0)
    max_side = p.max_comp_side * gw
    max_area = p.max_comp_area * gw * gw
    rule_side = p.rule_len_ratio * gw
    for i in range(1, n):
        a = areas[i]
        if a < p.min_comp_area or a > max_area:
            continue
        if dark_frac[i] < p.dark_frac_min:
            continue
        w = stats[i, cv2.CC_STAT_WIDTH]
        h = stats[i, cv2.CC_STAT_HEIGHT]
        fill = a / float(w * h)
        if max(w, h) > max_side and fill < 0.30:
            continue
        if max(w, h) > rule_side and fill < 0.25:
            continue
        keep[i] = True
    return keep


def _estimate_glyph_w(mask: np.ndarray):
    """Column pitch (= glyph cell width) from the x-profile autocorrelation."""
    prof = C.col_profile(mask).astype(np.float32)
    prof = prof - prof.mean()
    n = len(prof)
    if n < 64:
        return max(64.0, n / 8.0)
    F = np.fft.rfft(prof, n=2 * n)
    ac = np.fft.irfft(F * np.conj(F))[:n]
    ac[0] = 0
    lo = max(8, int(n * 0.01))
    hi = max(lo + 4, int(n * 0.12))
    if hi <= lo + 2:
        return max(64.0, n / 16.0)
    return float(lo + int(np.argmax(ac[lo:hi])))


# ------------------------------------------------------------ blocks
def _runs(mask1d: np.ndarray):
    return C.runs(mask1d)


def _split_blocks(mask: np.ndarray, glyph_w: float, p: Params):
    """Split the page into horizontal blocks separated by blank corridors."""
    rp = C.row_profile(mask) > 0
    blanks = _runs(~rp)
    min_gap = max(8, int(round(p.block_gap_frac * 0.75 * glyph_w)))
    cuts = [0]
    for a, b in blanks:
        lon = b - a + 1
        thr = min_gap if (a > 0 and b < len(rp) - 1) else 1
        if lon >= thr and a > 0:
            cuts.append(int((a + b) // 2))
    cuts.append(mask.shape[0])
    blocks = []
    for i in range(len(cuts) - 1):
        y0, y1 = cuts[i], cuts[i + 1]
        if y1 - y0 < max(8, 0.25 * glyph_w):
            continue
        sub = mask[y0:y1]
        if sub.mean() < p.block_min_ink:
            continue
        blocks.append((y0, y1))
    return blocks


# ------------------------------------------------------------ columns
def _find_columns(mask: np.ndarray, p: Params, glyph_w: float):
    """Column bands of one block: close along y, then peak-pick the x-profile."""
    kh = max(3, int(round(0.95 * glyph_w)))
    closed = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE,
                              np.ones((kh, 1), np.uint8))
    prof = C.col_profile(closed > 0).astype(np.float32)
    sm = C.smooth1d(prof, max(3, int(glyph_w * 0.20) | 1))
    if sm.max() <= 0:
        return []
    try:
        from scipy.signal import find_peaks
        peaks, _ = find_peaks(sm, distance=max(4, int(glyph_w * 0.55)),
                              prominence=0.20 * float(sm.max()))
        peaks = np.asarray(peaks, dtype=np.int64)
        if peaks.size < 2:
            raise RuntimeError
    except Exception:
        thr = max(3.0, sm.max() * 0.05)
        peaks = np.array([int((a + b) / 2) for a, b in _runs(sm > thr)], dtype=np.int64)
    if peaks.size == 0:
        return []
    w_page = len(sm)
    cuts = [0]
    for i in range(len(peaks) - 1):
        a, b = int(peaks[i]), int(peaks[i + 1])
        seg = sm[a:b + 1]
        v = float(seg.min())
        if v <= p.col_valley_frac * min(float(sm[a]), float(sm[b])):
            cuts.append(a + int(np.argmin(seg)))
    cuts.append(w_page)
    bands = [(int(cuts[i]), int(cuts[i + 1])) for i in range(len(cuts) - 1)]
    bands = [(a, b) for a, b in bands if b - a >= max(8, 0.12 * glyph_w)]
    if not bands:
        return []
    return bands


# ------------------------------------------------------------ per-column DP
def _estimate_H(prof: np.ndarray, glyph_w: float) -> float:
    sm = C.smooth1d(prof, 5)
    thr = max(1.0, 0.05 * float(prof.max()) if prof.size else 1.0)
    bh = np.array([b - a + 1 for a, b in _runs(sm > thr)], dtype=np.float32)
    H = float(np.median(bh)) * 1.25 if bh.size >= 3 else glyph_w * 0.55
    return float(np.clip(H, glyph_w * 0.35, glyph_w * 0.95))


def _dp_cuts(prof: np.ndarray, height: int, H: float, p: Params):
    """Partition rows [0,height) into glyph rows (vectorised DP)."""
    ps = C.smooth1d(prof.astype(np.float32), 3)
    nz = ps[ps > 0]
    ref = max(float(np.percentile(nz, 75)) if nz.size else 1.0, 1.0)
    lo = max(1, int(H * p.min_h_frac))
    hi = max(lo + 2, int(H * p.max_h_frac))
    cand = [0]
    cand.extend([y for y in range(1, height - 1)
                 if ps[y] <= ps[y - 1] and ps[y] <= ps[y + 1]])
    cand.append(height)
    cand = np.unique(np.asarray(cand, dtype=np.int64))
    k = len(cand)
    logc = np.log(np.maximum(cand, 1).astype(np.float64))
    cut_pen = p.cut_w * (ps[np.clip(cand, 0, len(ps) - 1)] / ref)
    INF = 1e18
    dp = np.full(k, INF)
    back = np.zeros(k, dtype=np.int32)
    dp[0] = 0.0
    for j in range(1, k):
        yj = cand[j]
        ilo = int(np.searchsorted(cand, yj - hi, "left"))
        ihi = int(np.searchsorted(cand, yj - lo, "right"))
        ihi = min(ihi, j)
        if ihi <= ilo:
            continue
        ii = np.arange(ilo, ihi)
        lr = logc[j] - logc[ii]
        cost = dp[ii] + p.shape_w * lr * lr
        m = int(np.argmin(cost))
        if cost[m] >= INF:
            continue
        dp[j] = cost[m] + (0.0 if j == k - 1 else (p.cut_base + cut_pen[j]))
        back[j] = ii[m]
    if dp[k - 1] >= INF:
        cuts = [0]
        y = H
        while y < height - 1:
            cuts.append(int(round(y)))
            y += H
        cuts.append(height)
        return cuts
    cuts = [int(cand[k - 1])]
    j = k - 1
    while j > 0:
        j = int(back[j])
        cuts.append(int(cand[j]))
    cuts.reverse()
    return cuts


def _segment_column(prof, comps, cbox, areas, cflat, H, p: Params, height: int, pm=None):
    """DP-cut one column and return merged segment boxes (rows relative to block)."""
    cuts = None
    if p.tpl_dp and pm is not None:
        from . import templatedp
        tpl = templatedp.load_default()
        if tpl is not None:
            try:
                cuts = templatedp.dp_cuts(pm, tpl, H, sim_w=p.tpl_sim_w, h_w=p.tpl_h_w,
                                          ink_w=p.tpl_ink_w, ink_curve=prof)
            except Exception:
                cuts = None
    if cuts is None:
        cuts = _dp_cuts(prof, height, H, p)
    if p.h_refine:
        hts = np.diff(np.asarray(cuts, dtype=np.float64))
        hts = hts[hts > 0]
        if hts.size >= 3:
            H2 = float(np.median(hts))
            if 0.6 * H <= H2 <= 1.6 * H:
                H = H2
                cuts = _dp_cuts(prof, height, H, p)
    seg_comp = {}
    for c in comps:
        j = int(np.searchsorted(cuts, c[1], side="right") - 1)
        j = min(max(j, 0), len(cuts) - 2)
        seg_comp.setdefault(j, []).append(c[0])
    segs = []
    for j, cs in seg_comp.items():
        x0 = int(min(cbox[c, 0] for c in cs)); y0 = int(min(cbox[c, 1] for c in cs))
        x1 = int(max(cbox[c, 0] + cbox[c, 2] for c in cs))
        y1 = int(max(cbox[c, 1] + cbox[c, 3] for c in cs))
        ink = int(sum(areas[c] for c in cs))
        fm = float(np.average(cflat[cs], weights=np.maximum(areas[cs], 1)))
        segs.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1,
                     "ink": ink, "flat": fm, "comps": cs})
    segs.sort(key=lambda d: d["y0"])
    merged = []
    for sg in segs:
        if merged:
            prev = merged[-1]
            gap = sg["y0"] - prev["y1"]
            hh = sg["y1"] - sg["y0"]
            hpre = prev["y1"] - prev["y0"]
            ov = min(prev["x1"], sg["x1"]) - max(prev["x0"], sg["x0"])
            ovr = ov / float(max(1, min(prev["x1"] - prev["x0"], sg["x1"] - sg["x0"])))
            if min(hh, hpre) < p.frag_merge_frac * H and gap <= p.frag_gap_frac * H \
                    and ovr > 0.15:
                prev["x0"] = min(prev["x0"], sg["x0"])
                prev["y0"] = min(prev["y0"], sg["y0"])
                prev["x1"] = max(prev["x1"], sg["x1"])
                prev["y1"] = max(prev["y1"], sg["y1"])
                tot = prev["ink"] + sg["ink"]
                prev["flat"] = (prev["flat"] * prev["ink"] + sg["flat"] * sg["ink"]) / max(tot, 1)
                prev["ink"] = tot
                prev["comps"].extend(sg["comps"])
                continue
        merged.append(sg)
    return merged, H


def _tighten_box(mask: np.ndarray, box, H: float, p: "Params"):
    """Shrink a box onto the glyph: drop stray specks that inflate the boundary.

    Strokes of ONE glyph sit within ~0.15*H of each other; a separate dust dot /
    neighbour's stroke tip / stain speck sits farther away or is much smaller.
    Keeping only the main body + nearby sizeable parts, then re-bboxing, stops
    "boxes grow until they swallow other ink".
    """
    x0, y0, x1, y1 = [int(v) for v in box]
    if x1 - x0 < 4 or y1 - y0 < 4 or H <= 0:
        return box
    sub = (mask[y0:y1, x0:x1] > 0).astype(np.uint8)
    if sub.size == 0:
        return box
    n, lab, stats, cent = cv2.connectedComponentsWithStats(sub, connectivity=8)
    if n <= 1:
        return box
    areas = stats[1:, cv2.CC_STAT_AREA].astype(np.float64)
    main = int(np.argmax(areas)) + 1
    m_area = float(areas[main - 1])
    mx0 = stats[main, cv2.CC_STAT_LEFT]
    my0 = stats[main, cv2.CC_STAT_TOP]
    mx1 = mx0 + stats[main, cv2.CC_STAT_WIDTH]
    my1 = my0 + stats[main, cv2.CC_STAT_HEIGHT]
    near = max(4.0, 0.18 * H)
    keep = [main]
    for i in range(1, n):
        if i == main:
            continue
        if float(areas[i - 1]) < max(0.10 * m_area, 0.02 * H * H):
            continue
        gx0 = stats[i, cv2.CC_STAT_LEFT]
        gy0 = stats[i, cv2.CC_STAT_TOP]
        gx1 = gx0 + stats[i, cv2.CC_STAT_WIDTH]
        gy1 = gy0 + stats[i, cv2.CC_STAT_HEIGHT]
        gap = max(mx0 - gx1, gx0 - mx1, my0 - gy1, gy0 - my1, 0)
        if gap <= near:
            keep.append(i)
    sel = np.isin(lab, keep)
    ys, xs = np.nonzero(sel)
    if xs.size == 0:
        return box
    nx0, nx1 = int(xs.min()), int(xs.max()) + 1
    ny0, ny1 = int(ys.min()), int(ys.max()) + 1
    # never grow the box, and keep at least a reasonable body
    nx0 = max(nx0, 0)
    ny0 = max(ny0, 0)
    nx1 = min(nx1, x1 - x0)
    ny1 = min(ny1, y1 - y0)
    if (nx1 - nx0) < 0.45 * (x1 - x0) or (ny1 - ny0) < 0.45 * (y1 - y0):
        return box
    return (x0 + nx0, y0 + ny0, x0 + nx1, y0 + ny1)


def _post_split(box, mask: np.ndarray, H: float, p: Params):
    """Split a box that still contains two glyphs (the DP only cuts along rows).

    A cut is accepted only where the ink mask has a genuinely empty band and
    both resulting parts are at least ps_part_frac * H tall (or wide).
    """
    if not p.post_split or H <= 2:
        return [box]
    queue = [tuple(int(v) for v in box)]
    out = []
    guard = 0
    while queue and guard < 40:
        guard += 1
        x0, y0, x1, y1 = queue.pop()
        hh, ww = y1 - y0, x1 - x0
        if hh < 6 or ww < 6:
            out.append((x0, y0, x1, y1))
            continue
        sub = mask[y0:y1, x0:x1]
        did = False
        if hh > p.ps_h_ratio * H:
            rp = sub.sum(axis=1)
            for a, b in _runs(rp == 0):
                if b - a + 1 < max(2, p.ps_gap_frac * hh):
                    continue
                if a >= p.ps_part_frac * H and (hh - 1 - b) >= p.ps_part_frac * H:
                    cut = (a + b) // 2
                    queue.append((x0, y0, x1, y0 + cut))
                    queue.append((x0, y0 + cut, x1, y1))
                    did = True
                    break
        if did:
            continue
        if ww > p.ps_w_ratio * hh and ww > p.ps_w_ratio * H:
            cp = sub.sum(axis=0)
            for a, b in _runs(cp == 0):
                if b - a + 1 < max(2, p.ps_gap_frac * ww):
                    continue
                if a >= p.ps_part_frac * H and (ww - 1 - b) >= p.ps_part_frac * H:
                    cut = (a + b) // 2
                    queue.append((x0, y0, x0 + cut, y1))
                    queue.append((x0 + cut, y0, x1, y1))
                    did = True
                    break
        if not did:
            out.append((x0, y0, x1, y1))
    return out


# ------------------------------------------------------------------ main
def _tight_box(sub, ox, oy):
    """Tight ink bbox of a mask patch, in page coordinates (None when empty)."""
    ys, xs = np.nonzero(sub)
    if ys.size == 0:
        return None
    return (ox + int(xs.min()), oy + int(ys.min()), ox + int(xs.max()) + 1, oy + int(ys.max()) + 1)


def _split_once(g, mask, med_h, med_w, p):
    """Split a glyph box into two tight boxes when it clearly holds two glyphs.

    Row split: h > tall_split_ratio * med_h, cut at the deepest row-ink valley.
    Col split: w > wide_split_ratio * med_w, cut at the deepest column valley.
    The cut only needs a valley (not an empty band), so touching glyphs split too.
    """
    x0, y0, x1, y1 = int(g.x0), int(g.y0), int(g.x1), int(g.y1)
    sub = mask[y0:y1, x0:x1]
    if sub.size == 0:
        return None
    h, w = sub.shape
    if med_h > 0 and h > p.tall_split_ratio * med_h and h >= 20:
        prof = sub.sum(axis=1).astype(np.float32)
        k = max(3, h // 16)
        ker = np.ones(k, np.float32) / float(k)
        lo, hi = int(h * 0.28), int(h * 0.72)
        seg = np.convolve(prof, ker, mode="same")[lo:hi]
        if seg.size >= 6:
            idx = int(np.argmin(seg))
            med = float(np.median(seg))
            cut = lo + idx
            depth_ok = (med <= 0 or float(seg[idx]) <= p.tall_split_depth * med)
            if min(cut, h - cut) >= p.tall_split_min_part * med_h and depth_ok:
                top = _tight_box(sub[:cut], x0, y0)
                bot = _tight_box(sub[cut:], x0, y0 + cut)
                if top and bot:
                    return top, bot, "split_tall"
    if p.wide_split and med_w > 0 and w > p.wide_split_ratio * med_w and w >= 20:
        prof = sub.sum(axis=0).astype(np.float32)
        k = max(3, w // 16)
        ker = np.ones(k, np.float32) / float(k)
        lo, hi = int(w * 0.30), int(w * 0.70)
        seg = np.convolve(prof, ker, mode="same")[lo:hi]
        if seg.size >= 6:
            idx = int(np.argmin(seg))
            med = float(np.median(seg))
            cut = lo + idx
            depth_ok = (med <= 0 or float(seg[idx]) <= p.wide_split_depth * med)
            if min(cut, w - cut) >= p.tall_split_min_part * med_w and depth_ok:
                left = _tight_box(sub[:, :cut], x0, y0)
                right = _tight_box(sub[:, cut:], x0 + cut, y0)
                if left and right:
                    return left, right, "split_wide"
    return None


def _split_tall_boxes(glyphs, mask, med_h: float, p: "Params", med_w: float = 0.0):
    """Learnable post-pass: split boxes that still hold two glyphs.

    Works on rows (two glyphs stacked) and columns (two glyphs side by side),
    up to two levels deep, and rebuilds tight boxes for both parts.
    """
    if not p.tall_split or med_h <= 0 or not glyphs:
        return glyphs
    out = []
    for g in glyphs:
        stack = [(g, 0)]
        guard = 0
        while stack and guard < 60:
            guard += 1
            cur, depth = stack.pop()
            nxt = None
            if depth < 3:
                nxt = _split_once(cur, mask, med_h, med_w, p)
            if nxt is None:
                out.append(cur)
                continue
            (a, b, tag) = nxt
            ga = copy.copy(cur)
            gb = copy.copy(cur)
            ga.x0, ga.y0, ga.x1, ga.y1 = a
            gb.x0, gb.y0, gb.x1, gb.y1 = b
            ga.flags = list(cur.flags) + [tag]
            gb.flags = list(cur.flags) + [tag]
            for gg in (ga, gb):
                gg.area = int(gg.w * gg.h)
                sub2 = mask[gg.y0:gg.y1, gg.x0:gg.x1]
                gg.ink = int(sub2.sum())
            stack.append((ga, depth + 1))
            stack.append((gb, depth + 1))
    out.sort(key=lambda x: (x.col, x.y0, x.x0))
    return out


def segment_page(gray: np.ndarray, *, base_scale: float = 1.0, params: Params | None = None,
                 debug: dict | None = None) -> dict:
    """Segment one page.  Coordinates in the result are in gray's pixel space."""
    p = Params() if params is None else params
    s = float(base_scale)
    if s != 1.0:
        p = _scaled(p, s)
    h, w = gray.shape[:2]

    flat, mask = _prep(gray, p)
    bm = int(round(min(h, w) * p.border_frac))
    if bm > 0:
        mask[:bm, :] = False
        mask[-bm:, :] = False
        mask[:, :bm] = False
        mask[:, -bm:] = False

    mask = _clear_dark_edge_bands(mask, p)
    lab, stats, cent, areas, dark_frac, cflat = _component_stats(mask, flat, p)
    glyph_w = _estimate_glyph_w(mask)
    keep = _component_keep(stats, areas, dark_frac, p, glyph_w)
    keep[0] = False
    mask2 = np.zeros((h, w), np.uint8)
    lut = np.zeros(len(keep), np.uint8)
    lut[keep] = 1
    mask2 = lut[lab]
    ids = np.flatnonzero(keep)

    cbox = stats[:, [cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP,
                     cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT]].astype(np.int64)
    ccy = cbox[:, 1] + cbox[:, 3] / 2.0
    ccx = cbox[:, 0] + cbox[:, 2] / 2.0

    blocks = _split_blocks(mask2 > 0, glyph_w, p)

    # ---- collect every (block, column band) work item ----------------------
    work = []
    band_list = []
    block_info = []
    for (by0, by1) in blocks:
        sub = mask2[by0:by1]
        bands = _find_columns(sub > 0, p, glyph_w)
        if not bands:
            continue
        kept = []
        for (bx0, bx1) in bands:
            pad_x = int(round(0.10 * (bx1 - bx0 + 1)))
            sx0 = max(0, bx0 - pad_x)
            sx1 = min(w, bx1 + pad_x + 1)
            sel = ids[(ccx[ids] >= sx0) & (ccx[ids] < sx1) &
                      (ccy[ids] >= by0) & (ccy[ids] < by1)]
            if sel.size == 0:
                continue
            pm = np.zeros((by1 - by0, sx1 - sx0), np.uint8)
            loc = lab[by0:by1, sx0:sx1]
            sub_lut = np.zeros(len(keep), np.uint8)
            sub_lut[sel] = 1
            pm[sub_lut[loc] > 0] = 1
            prof = pm.sum(axis=1).astype(np.float32)
            if prof.max() <= 0:
                continue
            work.append({"by0": int(by0), "by1": int(by1),
                         "bx0": int(bx0), "bx1": int(bx1),
                         "prof": prof, "pm": pm,
                         "comps": [(int(c), float(ccy[c]) - by0) for c in sel],
                         "H": _estimate_H(prof, glyph_w)})
            kept.append((int(bx0), int(bx1)))
            band_list.append((int(bx0), int(bx1)))
        block_info.append({"y0": int(by0), "y1": int(by1), "n_cols": len(kept)})

    # ---- pass 1: segment with the per-column height guess ------------------
    for it in work:
        it["segs"], it["H_used"] = _segment_column(
            it["prof"], it["comps"], cbox, areas, cflat, it["H"], p, it["by1"] - it["by0"],
            pm=it.get("pm"))

    # ---- pass 2: one page-wide glyph height, then re-segment outliers ------
    # Handwriting keeps a stable glyph height per page; a column whose local
    # estimate is an outlier (sparse marginalia, dense stains) is the main
    # source of both "two glyphs merged" and "one glyph split" errors.
    allh = [sg["y1"] - sg["y0"] for it in work for sg in it["segs"]]
    H_g = float(np.median(allh)) if allh else glyph_w * 0.55
    if allh and p.h_consensus:
        for it in work:
            Hc = float(np.clip(it["H_used"], p.h_consensus_lo * H_g, p.h_consensus_hi * H_g))
            if abs(Hc - it["H_used"]) > 0.06 * H_g:
                it["segs"], it["H_used"] = _segment_column(
                    it["prof"], it["comps"], cbox, areas, cflat, Hc, p, it["by1"] - it["by0"],
                    pm=it.get("pm"))

    # ---- emit boxes (after the two-glyph post-split) -----------------------
    H_g = float(np.median(allh)) if allh else glyph_w * 0.55
    maskB = mask2 > 0
    raw = []
    for it in work:
        bx0, bx1, by0 = it["bx0"], it["bx1"], it["by0"]
        lim = int(round(p.bbox_clip * (bx1 - bx0 + 1)))
        for sg in it["segs"]:
            box = (sg["x0"], sg["y0"] + by0, sg["x1"], sg["y1"] + by0)
            for (px0, py0, px1, py1) in _post_split(box, maskB, H_g, p):
                clipped = False
                if px0 < bx0 - lim or px1 > bx1 + 1 + lim:
                    px0 = max(px0, bx0 - lim)
                    px1 = min(px1, bx1 + 1 + lim)
                    clipped = True
                sm = maskB[py0:py1, px0:px1]
                area = max(1, (px1 - px0) * (py1 - py0))
                ink = int(sm.sum())
                if ink:
                    fv = flat[py0:py1, px0:px1][sm].astype(np.float32)
                    fmean = float(fv.mean())
                    core = float((fv < p.core_thr).mean())
                else:
                    fmean, core = 255.0, 0.0
                raw.append({"x0": int(px0), "y0": int(py0),
                            "x1": int(px1), "y1": int(py1),
                            "ink": ink, "flat": fmean,
                            "ink_frac": ink / float(area), "core_frac": core,
                            "clipped": clipped, "band": (bx0, bx1)})

    if not raw:
        return {"source": "", "width": int(w), "height": int(h), "base_scale": s,
                "columns": [], "glyphs": [], "params": {}}

    # ---- global columns: cluster the band centres across blocks ------------
    mids = np.array([(a + b) / 2.0 for a, b in band_list], dtype=np.float64)
    order = np.argsort(mids)
    clusters = []
    for i in order:
        m = mids[i]
        if clusters and abs(m - clusters[-1][-1]) <= p.col_merge_frac * glyph_w:
            clusters[-1].append(m)
        else:
            clusters.append([m])
    centres = np.array([float(np.median(c)) for c in clusters])
    # right -> left  =>  reading order index 0 is the rightmost column
    ordered = np.sort(centres)[::-1]
    def col_of(x_center):
        return int(np.argmin(np.abs(ordered - x_center)))
    # column extents from the bands themselves
    col_x = {}
    for (a, b) in band_list:
        ci = col_of((a + b) / 2.0)
        if ci not in col_x:
            col_x[ci] = [a, b]
        else:
            col_x[ci][0] = min(col_x[ci][0], a)
            col_x[ci][1] = max(col_x[ci][1], b)
    columns = [C.Column(idx=i, x0=col_x[i][0], x1=col_x[i][1] + 1)
               for i in sorted(col_x)]

    glyphs = []
    for d in raw:
        ci = col_of((d["band"][0] + d["band"][1]) / 2.0)
        g = C.Glyph(id=0, x0=d["x0"], y0=d["y0"], x1=d["x1"], y1=d["y1"],
                    col=ci, ink=d["ink"])
        g.flat = d["flat"]
        g.ink_frac = d["ink_frac"]
        g.core_frac = d["core_frac"]
        if d["clipped"]:
            g.flags.append("clipped")
        glyphs.append(g)

    # ---- global filtering -------------------------------------------------
    inks = np.array([g.ink for g in glyphs], dtype=np.float32)
    med_ink = float(np.median(inks)) if inks.size else 0.0
    ws = np.array([g.w for g in glyphs], dtype=np.float32)
    hs = np.array([g.h for g in glyphs], dtype=np.float32)
    med_w = float(np.median(ws)) if ws.size else 0.0
    med_h = float(np.median(hs)) if hs.size else 0.0
    out = []
    for g in glyphs:
        if med_ink and g.ink < max(1.0, p.min_ink_frac * med_ink):
            continue
        # not real ink: pale stain texture, a filled blob instead of strokes,
        # or no really-dark pixel anywhere in the box
        if (g.flat > p.flat_veto or g.ink_frac > p.ink_frac_max
                or g.core_frac < p.core_frac_min):
            continue
        if p.stain_filter and g.ink_frac > p.blob_fill:
            # 污渍是「实心光滑块」：凸包填充率高；字形是细笔画，填充率低
            _sm = (mask2[g.y0:g.y1, g.x0:g.x1] > 0).astype(np.uint8)
            if _sm.size:
                _cnts, _ = cv2.findContours(_sm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
                if _cnts:
                    _area = float(_sm.sum())
                    _hull = sum(cv2.contourArea(cv2.convexHull(c)) for c in _cnts if len(c) > 2)
                    if _hull > 0 and _area / _hull > p.blob_solid:
                        continue
        if med_w and g.w > 2.3 * med_w or (med_h and g.h > 2.3 * med_h):
            continue
        if med_w and med_h and g.w < 0.25 * med_w and g.h < 0.32 * med_h:
            continue
        if (med_w and g.w > 1.8 * med_w) or (med_h and g.h > 1.9 * med_h):
            g.flags.append("oversize")
        g.area = int(g.w * g.h)
        out.append(g)
    glyphs = out
    glyphs = _split_tall_boxes(glyphs, mask2 > 0, med_h, p, med_w)  # 补切两字粘连（行/列都能切）
    # 补切可能切出「半截笔画」：把明显小于字形的小块丢掉（不是字，是碎片）
    if glyphs:
        _w = np.array([g.w for g in glyphs], dtype=np.float32)
        _h = np.array([g.h for g in glyphs], dtype=np.float32)
        _ink = np.array([g.ink for g in glyphs], dtype=np.float32)
        mw, mh, mi = float(np.median(_w)), float(np.median(_h)), float(np.median(_ink))
        kept = []
        for g in glyphs:
            if mw and mh and g.w < 0.22 * mw and g.h < 0.34 * mh:
                continue
            if mi and g.ink < 0.14 * mi:
                continue
            kept.append(g)
        glyphs = kept
    # 收紧边界：把旁边的墨点/邻居笔画尖角排除掉，别让框越画越大
    if p.tighten_boxes and glyphs:
        fixed = []
        for g in glyphs:
            nb = _tighten_box(mask2 > 0, (g.x0, g.y0, g.x1, g.y1), max(med_h, 1.0), p)
            if nb != (g.x0, g.y0, g.x1, g.y1):
                g.x0, g.y0, g.x1, g.y1 = nb
                g.area = int(g.w * g.h)
                g.ink = int((mask2[g.y0:g.y1, g.x0:g.x1] > 0).sum())
                g.flags.append("tightened")
            fixed.append(g)
        glyphs = fixed

    # ---- reading order: columns right -> left, rows top -> bottom ---------
    glyphs.sort(key=lambda g: (-g.col, g.y0))
    for i, g in enumerate(glyphs, start=1):
        g.id = i
    by_col = {}
    for g in glyphs:
        by_col.setdefault(g.col, []).append(g)
    for ci, lst in by_col.items():
        lst.sort(key=lambda g: g.y0)
        for r, g in enumerate(lst):
            g.row = r
    for col in columns:
        col.ink = int(sum(g.ink for g in glyphs if g.col == col.idx))

    page = {
        "source": "",
        "width": int(w),
        "height": int(h),
        "base_scale": s,
        "columns": [c.as_dict() for c in columns],
        "glyphs": [g.as_dict() for g in glyphs],
        "params": {"glyph_w_px": glyph_w, "blocks": block_info},
    }
    if debug is not None:
        debug.update({"flat": flat, "mask": mask2, "lab": lab, "keep": keep})
    return page
