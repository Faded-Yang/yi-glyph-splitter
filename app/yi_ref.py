"""Standard-Yi glyph reference bank ("规范彝文字形库") matching.

The bank is built from 《规范彝文单字图形识别手册》 (data/glyphbank/bank.npz):
1428 catalogue classes, ~5700 normalised 48x48 ink images.

It answers one question for every segmentation box:
    "does this crop look like ONE standard Yi character, and which one?"
That is what a reader who does not know Yi script needs in order to trust a cut.
"""
from __future__ import annotations

import io
import json
import math
import os
import threading
from typing import Any, Optional, Sequence

import cv2
import numpy as np
from PIL import Image

from . import paths
from . import userbank

SIDE = 48
COARSE = 16
TOP_K = 60          # coarse candidates re-ranked at 48x48
SHOW_K = 3          # how many reference glyphs to show in the UI
STRONG = 0.62       # >= this: matches a standard character very well (green box)
GOOD = 0.50         # >= this: clearly matches a standard character
WARN = 0.40         # >= this: plausible
LOW = 0.38          # < this: suspicious (merge / fragment / junk)

USER_OFFSET = 1_000_000   # user-bank indices exposed to the UI start here
_BANK: Optional[dict] = None
_LOCK = threading.Lock()


def bank_file() -> str:
    """Resource path of the glyph bank (works inside the PyInstaller exe).

    Accepts bank.npz (local build) or bank.npz.b64 (the text-only copy kept in git)."""
    names = ("bank.npz", "bank.npz.b64")
    roots = (paths.resource_path("data", "glyphbank"),
             paths.app_root().parent.joinpath("data", "glyphbank"))
    for root in roots:
        for name in names:
            cand = root.joinpath(name)
            if os.path.isfile(str(cand)):
                return str(cand)
    return ""


def available() -> bool:
    return bool(bank_file())


def load() -> dict:
    global _BANK
    with _LOCK:
        if _BANK is not None:
            return _BANK
        fp = bank_file()
        if not fp:
            raise FileNotFoundError("字形库 bank.npz 缺失（data/glyphbank/）")
        if str(fp).lower().endswith(".b64"):
            import base64
            import io as _io
            raw = base64.b64decode(open(fp, "rb").read())
            z = np.load(_io.BytesIO(raw))
        else:
            z = np.load(fp)
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
        _BANK = {"refs48": refs48, "refs16": refs16, "labels": labels,
                 "f16": f16, "f48": f48, "n": n}
        return _BANK


# ------------------------------------------------------------------ shapes
def norm_ink(gray: np.ndarray, side: int = SIDE) -> np.ndarray:
    """Crop to ink, pad to square, resize -> uint8 ink map (255 = ink)."""
    g = np.asarray(gray)
    if g.ndim == 3:
        g = cv2.cvtColor(g, cv2.COLOR_RGB2GRAY) if g.shape[2] == 3 else g[:, :, 0]
    g = g.astype(np.uint8)
    if g.size == 0:
        return np.zeros((side, side), np.uint8)
    thr, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink = (g < max(60, int(thr))).astype(np.uint8) * 255
    ys, xs = np.nonzero(ink)
    if ys.size < 10:
        return np.zeros((side, side), np.uint8)
    crop = ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = crop.shape
    s = max(h, w)
    canvas = np.zeros((s, s), np.uint8)
    canvas[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = crop
    return np.asarray(Image.fromarray(canvas).resize((side, side), Image.LANCZOS), np.uint8)


def _unit(vec: np.ndarray) -> Optional[np.ndarray]:
    v = vec.astype(np.float32)
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    if n < 1e-6:
        return None
    return v / n


def _user_candidates(v16, v48, k: int, exclude=None) -> list:
    """Best matches from the user's own confirmed cuts (empty when none).

    exclude: set of (x0,y0,x1,y1) that must not be used as a match -- the box
    itself is in the bank, and matching a glyph to itself would always score 1.0.
    """
    ub = userbank.load()
    if not ub.get("n"):
        return []
    entries = ub.get("entries") or []
    skip = set(exclude or ())
    coarse = ub["f16"] @ v16
    take = min(max(8, k * 3), ub["n"])
    cand = np.argpartition(-coarse, take - 1)[:take]
    fine = ub["f48"][cand] @ v48
    order = np.argsort(-fine)[:k]
    counts = userbank.label_counts()
    out = []
    for o in order:
        i = int(cand[o])
        if skip and i < len(entries):
            e = entries[i] or {}
            key = (e.get("x0"), e.get("y0"), e.get("x1"), e.get("y1"))
            if key in skip:
                continue
        lab = str(ub["labels"][i]) if i < len(ub["labels"]) else ""
        out.append({"idx": USER_OFFSET + i, "label": lab,
                    "score": round(float(fine[o]), 4),
                    "src": "user", "count": int(counts.get(lab, 0))})
    return out


def match(ink48: np.ndarray, k: int = SHOW_K, exclude=None) -> dict:
    """Return {score, best, top:[{idx,label,score,src,count}]} for one ink map.

    Candidates come from the standard bank AND the user's own confirmed glyphs;
    the best of both wins, and each candidate carries how many times that glyph
    has been collected (count) so the UI can show 出现过 N 次.
    """
    bank = load()
    q16 = np.asarray(Image.fromarray(ink48).resize((COARSE, COARSE), Image.LANCZOS), np.uint8)
    v16 = _unit(q16.ravel())
    v48 = _unit(ink48.ravel())
    if v16 is None or v48 is None:
        return {"score": 0.0, "best": -1, "top": []}
    coarse = bank["f16"] @ v16
    take = min(TOP_K, bank["n"])
    cand = np.argpartition(-coarse, take - 1)[:take]
    fine = bank["f48"][cand] @ v48
    order = np.argsort(-fine)[:max(1, k)]
    top = [{"idx": int(cand[o]), "label": str(bank["labels"][cand[o]]),
            "score": round(float(fine[o]), 4), "src": "std", "count": 0} for o in order]
    std_top = list(top)
    std_score = float(std_top[0]["score"])
    std_best = std_top[0]

    # 自建库（你切好的字形）才是这本手稿的字体；标准库只是兜底。
    # 得分 = max(标准库分, 自建库相似度 × 重复出现数权重)
    repeat, sim, user_score, cid = 0, 0.0, 0.0, None
    user_cands = []
    try:
        user_cands = _user_candidates(v16, v48, max(1, k), exclude)
    except Exception:
        user_cands = []
    if user_cands:
        ub_best = max(user_cands, key=lambda t: t["score"])
        sim = float(ub_best["score"])
        try:
            from . import userbank as _ub
            st = _ub.stats()
            i = int(ub_best["idx"]) - USER_OFFSET
            entries = _ub.load().get("entries") or []
            cid = (entries[i] or {}).get("cluster") if i < len(entries) else None
            repeat = int((st.get("cluster_sizes") or {}).get(cid, 1))
        except Exception:
            repeat = 1
        f = min(1.0, math.log2(1 + max(0, repeat)) / math.log2(7.0))
        user_score = sim * (0.75 + 0.25 * f)

    score = round(min(0.99, max(std_score, user_score)), 4)
    bonus = round(score - std_score, 4)
    if user_score > std_score:
        best = dict(std_best)
        best["score"] = score
        best["src"] = "user"
        best["repeat"] = repeat
        best["sim"] = round(sim, 4)
        best["cluster"] = cid
        best["bonus"] = bonus
        best_idx = best["idx"]
    else:
        best = dict(std_best)
        best["repeat"] = repeat
        best["sim"] = round(sim, 4)
        best["cluster"] = cid
        best["bonus"] = bonus
        best_idx = best["idx"]
    top = std_top + user_cands
    for t in top:
        if t["idx"] == best_idx:
            t["score"] = score
    top.sort(key=lambda t: -t["score"])
    top = top[:max(1, k)]
    return {"score": score, "best": best_idx, "top": top,
            "repeat": repeat, "sim": round(sim, 4), "cluster": cid,
            "bonus": bonus, "std_score": std_score,
            "user_score": round(user_score, 4)}


def score_tile(gray: np.ndarray) -> dict:
    return match(norm_ink(gray))


def ref_png(idx: int, scale: int = 4) -> bytes:
    """Reference glyph as a black-on-white PNG (for the UI thumbnails)."""
    if int(idx) >= USER_OFFSET:
        ub = userbank.load()
        i = int(idx) - USER_OFFSET
        if not (0 <= i < ub["n"]):
            raise IndexError(idx)
        ink = ub["refs48"][i]
    else:
        bank = load()
        if not (0 <= idx < bank["n"]):
            raise IndexError(idx)
        ink = bank["refs48"][idx]
    img = 255 - ink                       # ink -> black
    img = cv2.resize(img, (SIDE * scale, SIDE * scale), interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("encode failed")
    return buf.tobytes()


def ref_label(idx: int) -> str:
    if int(idx) >= USER_OFFSET:
        ub = userbank.load()
        i = int(idx) - USER_OFFSET
        return str(ub["labels"][i]) if i < len(ub["labels"]) else ""
    return str(load()["labels"][idx])


# ------------------------------------------------------------------ page check
def check_boxes(page_gray: np.ndarray, boxes: Sequence[dict], *,
                gap_frac: float = 0.45, merge_margin: float = 0.08) -> dict:
    """Score every box; propose merges for pairs whose union looks much more like a glyph.

    Returns {"boxes":[{id,score,level,top,flags}], "proposals":[{action,...}], "stats":{...}}
    """
    if not available():
        raise FileNotFoundError("字形库未安装")
    out_boxes = []
    cache: dict = {}

    def at(box):
        key = (box["x0"], box["y0"], box["x1"], box["y1"])
        if key not in cache:
            tile = page_gray[int(box["y0"]):int(box["y1"]), int(box["x0"]):int(box["x1"])]
            cache[key] = match(norm_ink(tile), exclude=set([key]))
        return cache[key]

    for b in boxes:
        m = at(b)
        score = m["score"]
        level = ("strong" if score >= STRONG else
                 "good" if score >= GOOD else
                 "warn" if score >= WARN else "low")
        flags = list(b.get("flags") or [])
        user_best = max([t.get("score", 0.0) for t in (m.get("top") or []) if t.get("src") == "user"] or [0.0])
        if level == "low" and user_best >= 0.78:
            level = "warn"          # 自建字库里有非常像的字形：是字，只是不像印刷体
            flags.append("userbank")
        out_boxes.append({"id": b.get("id"), "score": score, "level": level,
                          "top": m["top"], "user_best": round(float(user_best), 4),
                          "repeat": int(m.get("repeat") or 0),
                          "bonus": float(m.get("bonus") or 0.0),
                          "std_score": float(m.get("std_score") or score),
                          "box": [int(b["x0"]), int(b["y0"]), int(b["x1"]), int(b["y1"])],
                          "flags": flags})

    # --- merge proposals: adjacent boxes whose union matches markedly better
    props = []
    by_col: dict = {}
    for i, b in enumerate(boxes):
        by_col.setdefault(b.get("col"), []).append(i)
    for col, idxs in by_col.items():
        idxs.sort(key=lambda i: boxes[i]["y0"])
        for a, c in zip(idxs, idxs[1:]):
            A, B = boxes[a], boxes[c]
            ha, hb = A["y1"] - A["y0"], B["y1"] - B["y0"]
            gap = B["y0"] - A["y1"]
            if gap > gap_frac * min(ha, hb):
                continue
            merged = {"x0": min(A["x0"], B["x0"]), "y0": A["y0"],
                      "x1": max(A["x1"], B["x1"]), "y1": B["y1"]}
            w, h = merged["x1"] - merged["x0"], merged["y1"] - merged["y0"]
            if w > 1.6 * max(h, 1):
                continue
            sm = at(merged)
            sa, sb = at(A)["score"], at(B)["score"]
            gain = sm["score"] - max(sa, sb)
            if gain >= merge_margin and sm["score"] >= GOOD:
                props.append({"action": "merge", "ids": [A.get("id"), B.get("id")],
                              "before": [[A["x0"], A["y0"], A["x1"], A["y1"]],
                                         [B["x0"], B["y0"], B["x1"], B["y1"]]],
                              "after": [[merged["x0"], merged["y0"], merged["x1"], merged["y1"]]],
                              "score": sm["score"], "gain": round(gain, 4),
                              "reason": "合并后更像规范字 %.2f（分开 %.2f/%.2f）" % (sm["score"], sa, sb)})

    scores = np.array([b["score"] for b in out_boxes], np.float32) if out_boxes else np.zeros(0, np.float32)
    low = int((scores < LOW).sum()) if scores.size else 0
    stats = {"n": len(out_boxes), "low": low,
             "median": round(float(np.median(scores)), 4) if scores.size else 0.0,
             "good": int((scores >= GOOD).sum()) if scores.size else 0,
             "proposals": len(props), "thresholds": {"good": GOOD, "warn": WARN, "low": LOW}}
    return {"boxes": out_boxes, "proposals": props, "stats": stats}


def label_map() -> dict:
    """Normalised glyph index -> catalogue code (for grouping results)."""
    bank = load()
    return {i: str(bank["labels"][i]) for i in range(bank["n"])}


if __name__ == "__main__":  # tiny CLI: score one image file
    import argparse
    ap = argparse.ArgumentParser(description="规范彝文字形库匹配（自测）")
    ap.add_argument("image")
    args = ap.parse_args()
    g = np.asarray(Image.open(args.image).convert("L"), np.uint8)
    r = score_tile(g)
    print(json.dumps(r, ensure_ascii=False, indent=1))
