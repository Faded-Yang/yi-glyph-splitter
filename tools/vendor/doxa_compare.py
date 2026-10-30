"""doxapy (Doxa, CC0) vs our Sauvola: ink continuity + false-ink on stains.

Three probe tiles are auto-picked from the native-res crops by measurable
criteria (faint strokes / blotchy stain / paper-only), then every algorithm
binarizes the *flattened* image with identical preprocessing.

Run: python tools/vendor/doxa_compare.py
Out: scratch/prior/doxa_compare.png, scratch/prior/doxa_compare.json
"""
import os, sys, json, time
import numpy as np, cv2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from yi_core import common as C
import doxapy

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(ROOT, "scratch", "prior")
os.makedirs(OUT, exist_ok=True)
T = 512

ALGOS = [("SAUVOLA", doxapy.Binarization.SAUVOLA), ("SU", doxapy.Binarization.SU),
         ("WAN", doxapy.Binarization.WAN), ("GATOS", doxapy.Binarization.GATOS),
         ("ISAUVOLA", doxapy.Binarization.ISAUVOLA)]


def doxa(img_u8, alg, win=None):
    """doxapy 0.9.2 API: initialize(input) then to_binary(out, params) -> None.
    Output array holds 0/255, 255 == ink."""
    b = doxapy.Binarization(alg)
    b.initialize(np.ascontiguousarray(img_u8, dtype=np.uint8))
    out = np.zeros(img_u8.shape, np.uint8)
    b.to_binary(out, {} if win is None else {"window_radius": int(win)})
    # doxapy writes 0 == foreground(ink), 255 == background (verified on real crops)
    return out == 0


def pick_tiles(flat, crop):
    """Return 3 named tiles: faint ink, blotchy stain, paper-only."""
    H, Wd = flat.shape
    best = {}
    cand = [(y, x) for y in range(0, H - T, T) for x in range(0, Wd - T, T)]
    for y, x in cand:
        t = flat[y:y + T, x:x + T]
        m = C.binarize(t, method="sauvola", win=61, k=0.34)
        ink = float(m.mean())
        strong = float((t < 60).mean())          # solid black strokes
        mid = float(((t > 90) & (t < 150)).mean())  # faint grey (bleed-through/stain)
        if 0.02 < ink < 0.20 and strong > 0.002:
            faint_score = mid / max(strong, 1e-3)     # lots of faint, little solid
            if faint_score > best.get("faint", (0,))[0]:
                best["faint"] = (faint_score, y, x)
        if mid > best.get("stain", (0,))[0]:
            best["stain"] = (mid, y, x)
        if ink < best.get("paper", (9,))[0]:
            best["paper"] = (ink, y, x)
    return {k: (v[1], v[2]) for k, v in best.items()}


def stats(mask, min_area=20):
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    idx = np.flatnonzero(st[1:, cv2.CC_STAT_AREA] >= min_area) + 1
    cell = {}
    for i in idx:
        cx = st[i, cv2.CC_STAT_LEFT] + st[i, cv2.CC_STAT_WIDTH] / 2
        cy = st[i, cv2.CC_STAT_TOP] + st[i, cv2.CC_STAT_HEIGHT] / 2
        k = (int(cx // 300), int(cy // 350))
        cell[k] = cell.get(k, 0) + 1
    v = np.array(list(cell.values())) if cell else np.array([0])
    return dict(ink=round(float(mask.mean()), 4), n_cc=int(idx.size),
                cc_per_cell=round(float(v.mean()), 2), cells=int(v.size))


def main():
    crops = {n: C.load_gray(os.path.join(ROOT, "scratch", n + ".png")) for n in ("crop_center", "crop_right")}
    rep = {"algo_win": {}, "tiles": {}, "timing_s_per_512": {}}
    panels = {}
    for cname, crop in crops.items():
        flat = C.flatten_illumination(crop, ksize=129)
        tiles = pick_tiles(flat, crop)
        for tname, (y, x) in tiles.items():
            key = cname + ":" + tname
            sub_flat = flat[y:y + T, x:x + T]
            sub_raw = crop[y:y + T, x:x + T]
            row = {}
            # baseline: our current common.binarize
            row["ours_sauvola61_k34"] = stats(C.binarize(sub_flat, method="sauvola", win=61, k=0.34))
            row["ours_sauvola61_k25"] = stats(C.binarize(sub_flat, method="sauvola", win=61, k=0.25))
            for nm, alg in ALGOS:
                best = None
                for win in (None, 15, 25):
                    try:
                        t0 = time.time(); mk = doxa(sub_flat, alg, win); dt = time.time() - t0
                    except Exception as e:
                        row[nm] = {"err": str(e)[:60]}; best = None; break
                    s = stats(mk); s["win"] = win; s["ms"] = round(dt * 1000, 1)
                    rep["timing_s_per_512"].setdefault(nm, []).append(dt)
                    best = s if best is None or abs(s["ink"] - 0.09) < abs(best["ink"] - 0.09) else best
                    if tname == "paper":
                        pass
                if best:
                    row[nm] = best
            rep["tiles"][key] = row
            panels.setdefault(tname, []).append((key, sub_raw, sub_flat, row))
            print("\n==", key, "(y,x)=", (y, x))
            for k, v in row.items():
                print("   %-22s %s" % (k, v))
    # speed on full page: extrapolate from 512 tile
    for nm, lst in rep["timing_s_per_512"].items():
        rep["algo_win"][nm] = round(float(np.median(lst)) * 27.6, 2)   # 70MP / 0.26MP
    print("\n== est. whole-page seconds (70MP, host CPU, from 512^2 median):", rep["algo_win"])
    with open(os.path.join(OUT, "doxa_compare.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    # visual for the 'faint' tile: raw / ours / each algo at chosen win
    for tname, lst in panels.items():
        key, raw, sub_flat, row = lst[0]
        imgs = [cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)]
        mk = C.binarize(sub_flat, method="sauvola", win=61, k=0.34).astype(np.uint8)
        v = np.full((T, T, 3), 255, np.uint8); v[mk > 0] = (0, 0, 0)
        cv2.putText(v, "ours sauv61k.34", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 200), 2); imgs.append(v)
        for nm, alg in ALGOS:
            try:
                mk = doxa(sub_flat, alg, row[nm].get("win")).astype(np.uint8)
            except Exception:
                continue
            v = np.full((T, T, 3), 255, np.uint8); v[mk > 0] = (0, 0, 0)
            cv2.putText(v, nm, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 200), 2); imgs.append(v)
        p = os.path.join(OUT, "doxa_%s.png" % tname)
        cv2.imwrite(p, np.hstack(imgs)); print("wrote", p)
    print("wrote", os.path.join(OUT, "doxa_compare.json"))


if __name__ == "__main__":
    main()
