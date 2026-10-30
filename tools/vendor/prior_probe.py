"""Prior-art probe: does morphological stroke-repair + projection columns
actually work on our Yi manuscript scans?  Numbers + PNG for the report.

Scope: tools/vendor (own file).  Read-only use of yi_core.common.
Run:  python tools/vendor/prior_probe.py
Outputs: scratch/prior/*.png + stdout stats.
"""
import os, sys, json
import numpy as np, cv2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from yi_core import common as C

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(ROOT, "scratch", "prior")
os.makedirs(OUT, exist_ok=True)


def cc_count(mask, min_area=20):
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    ok = st[1:, cv2.CC_STAT_AREA] >= min_area
    return lab, st, ok


def glyph_grid_stats(lab, st, ok, cw=300, ch=350):
    """How many stroke fragments share one 300x350 glyph cell?"""
    ids = np.flatnonzero(ok) + 1
    if ids.size == 0:
        return None
    cells = {}
    for i in ids:
        cx = st[i, cv2.CC_STAT_LEFT] + st[i, cv2.CC_STAT_WIDTH] / 2
        cy = st[i, cv2.CC_STAT_TOP] + st[i, cv2.CC_STAT_HEIGHT] / 2
        cells.setdefault((int(cx // cw), int(cy // ch)), 0)
        cells[(int(cx // cw), int(cy // ch))] += 1
    v = np.array(list(cells.values()))
    return dict(cells=len(v), mean=float(v.mean()), p50=float(np.median(v)),
                p90=float(np.percentile(v, 90)), hist={int(k): int((v == k).sum()) for k in np.unique(v)})


def morph_sweep(mask):
    rows = []
    base_lab, base_st, base_ok = cc_count(mask)
    n0 = int(base_ok.sum())
    for k in (3, 5, 7, 9, 11, 13):
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, ker)
        lab, st, ok = cc_count(m)
        idx = np.flatnonzero(ok) + 1
        w = st[idx, cv2.CC_STAT_WIDTH] if idx.size else np.array([0])
        h = st[idx, cv2.CC_STAT_HEIGHT] if idx.size else np.array([0])
        rows.append(dict(k=k, n_cc=int(ok.sum()),
                         merge_rate=round(1 - ok.sum() / max(n0, 1), 3),
                         wide=int((w > 430).sum()), tall=int((h > 480).sum()),
                         med_w=float(np.median(w)), med_h=float(np.median(h))))
    return n0, rows


def main():
    crop = C.load_gray(os.path.join(ROOT, "scratch", "crop_center.png"))
    print("crop_center shape", crop.shape)
    flat = C.flatten_illumination(crop, ksize=129)
    rep = {"crop_shape": list(crop.shape)}

    for name, kw in (("sauvola_k34", dict(method="sauvola", win=61, k=0.34)),
                     ("sauvola_k25", dict(method="sauvola", win=61, k=0.25))):
        m = C.binarize(flat, **kw)
        m = C.clean_mask(m, min_area=20)
        lab, st, ok = cc_count(m, 20)
        n0, rows = morph_sweep(m)
        rep[name] = dict(ink_ratio=round(float(m.mean()), 4), n_cc=n0,
                         grid=glyph_grid_stats(lab, st, ok), morph=rows)
        print("\n==", name, "ink", round(float(m.mean()), 4), "n_cc", n0)
        print("  grid(cells of 300x350):", rep[name]["grid"])
        for r in rows:
            print("  close k=%2d  n_cc=%5d merge=%+.3f med_w=%5.0f med_h=%5.0f wide=%3d tall=%3d"
                  % (r["k"], r["n_cc"], r["merge_rate"], r["med_w"], r["med_h"], r["wide"], r["tall"]))
        rep[name]["_mask"] = m

    # ---- whole-page column profile on 1/4 scale
    sg = np.load(os.path.join(ROOT, "scratch", "small_gray.npy"))
    fg = C.flatten_illumination(sg, ksize=61)
    pm = C.binarize(fg, method="sauvola", win=25, k=0.30)
    pm = C.clean_mask(pm, min_area=15)
    cp = C.smooth1d(C.col_profile(pm), 9)
    thr = 0.12 * cp.max()
    cols = []
    for a, b in C.runs(cp > thr):
        if b - a >= 20:                      # >=80px at 1x
            cols.append((int(a * 4), int(b * 4), int((b - a) * 4)))
    gaps = [cols[i + 1][0] - cols[i][1] for i in range(len(cols) - 1)]
    rep["page"] = dict(shape=list(sg.shape), n_columns=len(cols),
                       col_widths_px=[c[2] for c in cols],
                       gaps_px=gaps,
                       pitch_px=[int((cols[i + 1][0] + cols[i][0]) / 2 - (cols[i][0] + cols[i][1]) / 2) for i in range(len(cols) - 1)])
    print("\n== page columns:", rep["page"])

    # ---- visual: crop 720x720 with masks + closing
    x0, y0, w = crop.shape[1] // 2 - 360, crop.shape[0] // 2 - 360, 720
    sub = crop[y0:y0 + w, x0:x0 + w]
    sflat = flat[y0:y0 + w, x0:x0 + w]
    sm = rep["sauvola_k34"]["_mask"][y0:y0 + w, x0:x0 + w].astype(np.uint8)
    panels = []
    base = cv2.cvtColor(sub, cv2.COLOR_GRAY2BGR)
    panels.append(base)
    kers = [(3, 3), (7, 7), (11, 11)]
    for k in kers:
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, k)
        mc = cv2.morphologyEx(sm, cv2.MORPH_CLOSE, ker)
        vis = np.full_like(base, 255)
        vis[mc > 0] = (0, 0, 0)
        n, lab, st, _ = cv2.connectedComponentsWithStats(mc, connectivity=8)
        cnt = 0
        for i in range(1, n):
            if st[i, cv2.CC_STAT_AREA] < 20:
                continue
            cnt += 1
            x, y, ww, hh = st[i, cv2.CC_STAT_LEFT], st[i, cv2.CC_STAT_TOP], st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
            cv2.rectangle(vis, (x, y), (x + ww, y + hh), (0, 0, 200), 2)
        cv2.putText(vis, "close %dx%d  cc=%d" % (k[0], k[1], cnt), (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 0, 0), 2)
        panels.append(vis)
    strip = np.hstack(panels)
    p = os.path.join(OUT, "morph_compare.png")
    cv2.imwrite(p, strip)
    print("wrote", p, strip.shape)

    for k in ("sauvola_k34", "sauvola_k25"):
        rep[k].pop("_mask", None)
    jp = os.path.join(OUT, "probe_stats.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print("wrote", jp)


if __name__ == "__main__":
    main()
