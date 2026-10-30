"""QC 质检: 叠加图 / 分带高清长条 / 缺陷统计。

契约 (docs/SPEC.md)::

    write_qc(rgb, page, out_dir, strips=True, target_w=2400) -> dict

产物::

    <out_dir>/overlay.png      整页缩略图: 列带灰线 + 彩色字框 + id
    <out_dir>/strips/*.png     原分辨率按列带切的长条(默认每段 ~1500px 高), 画框 + id
    <out_dir>/defects.png      缺陷汇总图(可疑框标红 + 缺陷代号)
    <out_dir>/defects.json     缺陷明细 + 统计 + 参数
    <out_dir>/defects/*.png    可疑框的原分辨率小图(便于逐项复核)

CLI (SPEC 指定用模块形式)::

    python -m yi_core.qc <image> <page.json> <outdir> [--no-strips] [--target-w 2400]

缺陷类别(全部 advisory, 阈值见 defects.json["params"]):
  tiny / huge    面积 < 0.2x / > 4x 中位面积
  touch_col      框左右边贴住列带边界(或越界)  -> 可能跨列粘连
  multi_mass     框内竖直方向有 > 0.35*h 的空白带 -> 可能两字被合成一个
  split_suspect  同列相邻两框: 竖直间隙 < 0.12*h 且水平重叠 > 0.6 -> 可能一字被切成两个
  margin         落在页面边缘 3% 内 -> 多半是装订黑边 / 页边噪声
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import cv2
import numpy as np

try:  # 包内导入 (python -m yi_core.qc)
    from . import common
except ImportError:  # 直接 python qc.py
    _HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(_HERE))
    import yi_core.common as common  # type: ignore

DEFECT_CODES = ("tiny", "huge", "touch_col", "multi_mass", "split_suspect", "margin")
CODE_CN = {
    "tiny": "过小",
    "huge": "过大",
    "touch_col": "贴列边",
    "multi_mass": "两字合一",
    "split_suspect": "一字被切",
    "margin": "页边噪声",
}

# 默认阈值(可被 params= 覆盖, 会原样写进 defects.json)
DEF_PARAMS = {
    "tiny_ratio": 0.2,          # 面积 < tiny_ratio * 中位面积
    "huge_ratio": 4.0,          # 面积 > huge_ratio * 中位面积
    "touch_tol_px": 3,          # 贴边容差(绝对)
    "touch_tol_rel": 0.005,     # 贴边容差(相对列宽)
    "multi_blank_frac": 0.35,   # 空白带 > frac*h 判 multi_mass
    "multi_blank_row_frac": 0.008,  # 行墨量 <= max(2, row_frac*w) 视为空白行
    "multi_blank_close": 5,     # 空白行连通前先闭运算吸收孤立噪点
    "split_gap_frac": 0.12,     # 同列相邻间隙 < frac*min(h)
    "split_overlap": 0.6,       # 水平重叠 / min(w) > 该值
    "margin_frac": 0.03,        # 页面边缘 3% 内
    "empty_ink_frac": 0.006,    # 框内墨占比低于此值 -> 空框(不算 multi_mass, 单独提示)
}

# 图上文字只能用 ASCII: cv2.putText 的 Hershey 字体画不了中文
CODE_EN = {
    "tiny": "tiny", "huge": "huge", "touch_col": "touchcol",
    "multi_mass": "2in1", "split_suspect": "split", "margin": "edge",
}

# defects.png 上打文字标签的类(其余只标红框, 免得文字糊成一片)
_LABEL_CODES = ("multi_mass", "split_suspect", "huge", "tiny")

_PALETTE = [
    (255, 200, 0), (0, 220, 120), (0, 170, 255),
    (255, 120, 255), (120, 255, 255), (200, 120, 0),
]
_RED = (0, 0, 255)
_HDR_BG = (28, 28, 28)


# ------------------------------------------------------------------ 基础工具
def _log(msg: str) -> None:
    """打印摘要; 控制台编码不支持中文时退化成转义, 不让质检因为打印崩掉。"""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def load_page_any(path: str) -> dict:
    """读 page.json, 编码兜底 (utf-8 / utf-8-sig / gbk), 避免别人用 GBK 写盘时质检直接崩。"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return json.load(f)
        except (UnicodeDecodeError, LookupError):
            continue
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8", "replace"))


def _as_rgb(rgb) -> np.ndarray:
    a = np.asarray(rgb)
    if a.ndim == 2:
        a = cv2.cvtColor(a, cv2.COLOR_GRAY2RGB)
    elif a.ndim == 3 and a.shape[2] == 4:
        a = cv2.cvtColor(a, cv2.COLOR_RGBA2RGB)
    if a.dtype != np.uint8:
        a = a.clip(0, 255).astype(np.uint8)
    return a


def _text_w(text, scale, thick=1):
    return cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)[0][0]


def _fit_scale(text, max_w, scale, thick=1, min_scale=0.5):
    """缩小字号让文字塞得进 max_w。"""
    tw = _text_w(text, scale, thick)
    if tw <= max_w or tw <= 0:
        return scale
    return max(min_scale, scale * max_w / float(tw))


def _put_label(img, text, x, y, scale=0.45, color=(255, 255, 255), bg=(0, 0, 0), thick=1):
    """在 (x, y) 处画带底色的文字, 保证在纸色上也读得清。"""
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
    h, w = img.shape[:2]
    x = max(0, min(int(x), w - tw - 2))
    y = max(th + base + 1, min(int(y), h - 3))
    cv2.rectangle(img, (x - 1, y - th - base), (x + tw + 1, y + base - 1), bg, -1)
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def _clip_box(x0, y0, x1, y1, W, H):
    return max(0, x0), max(0, y0), min(W, x1), min(H, y1)


# -------------------------------------------------------------- 归一化输入
def _norm_columns(page: dict, sx: float) -> list:
    out = []
    for k, c in enumerate(page.get("columns") or []):
        try:
            x0 = float(c["x0"]) * sx
            x1 = float(c["x1"]) * sx
        except (KeyError, TypeError, ValueError):
            continue
        if x1 < x0:
            x0, x1 = x1, x0
        out.append({
            "idx": int(c.get("idx", k)),
            "x0": int(round(x0)),
            "x1": int(round(x1)),
            "w": int(round(x1 - x0)),
            "ink": int(c.get("ink") or 0),
        })
    out.sort(key=lambda c: c["x0"])
    return out


def _norm_glyphs(page: dict, sx: float, sy: float, warnings: list) -> list:
    gs = []
    for k, g in enumerate(page.get("glyphs") or []):
        try:
            x0 = float(g["x0"]) * sx
            y0 = float(g["y0"]) * sy
            x1 = float(g["x1"]) * sx
            y1 = float(g["y1"]) * sy
        except (KeyError, TypeError, ValueError):
            warnings.append("glyph[%d] 缺少/非法坐标, 已跳过" % k)
            continue
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        w, h = x1 - x0, y1 - y0
        bbox = max(0, w) * max(0, h)
        area = g.get("area")
        if not isinstance(area, (int, float)) or not np.isfinite(area) or area <= 0:
            area, src = float(bbox), "bbox"
        else:
            area, src = float(area), "area"
        gs.append({
            "id": g.get("id", k + 1),
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "w": w, "h": h, "bbox": bbox, "area": area, "area_src": src,
            "ink": int(g.get("ink") or 0),
            "col": int(g["col"]) if isinstance(g.get("col"), (int, float)) else -1,
            "row": int(g["row"]) if isinstance(g.get("row"), (int, float)) else -1,
            "flags": list(g.get("flags") or []),
        })
    return gs


def _derive_columns(glyphs: list) -> list:
    """没有 columns 时, 按 glyph["col"] 分组的 x 范围反推列带。"""
    groups = {}
    for g in glyphs:
        if g["col"] >= 0:
            lo, hi = groups.get(g["col"], (g["x0"], g["x1"]))
            groups[g["col"]] = (min(lo, g["x0"]), max(hi, g["x1"]))
    out = []
    for idx in sorted(groups):
        x0, x1 = groups[idx]
        out.append({"idx": idx, "x0": x0, "x1": x1, "w": x1 - x0, "ink": 0})
    return out


def _assign_cols(glyphs: list, cols: list, warnings: list) -> None:
    """给每个字框定 col_eff: 优先用 page 里的 col, 否则按 x 中心推断。"""
    by_idx = {c["idx"]: c for c in cols}
    unknown = 0
    for g in glyphs:
        c = by_idx.get(g["col"])
        if c is not None:
            g["col_eff"] = g["col"]
            continue
        g["col_eff"] = None
        cx = 0.5 * (g["x0"] + g["x1"])
        best, best_d = None, None
        for col in cols:
            if col["x0"] <= cx <= col["x1"]:
                best = col["idx"]
                break
            d = min(abs(cx - col["x0"]), abs(cx - col["x1"]))
            if best_d is None or d < best_d:
                best_d, best = d, col["idx"]
        if best is not None:
            g["col_eff"] = best
            if g["col"] >= 0:
                unknown += 1
    if unknown:
        warnings.append("%d 个框的 col 不在 columns 里, 已按 x 中心改判" % unknown)


# ------------------------------------------------------------- 像素级检查
def _ink_row_profile(rgb: np.ndarray, x0, y0, x1, y1):
    """返回 (行墨量 profile, ink 占比); 区域太小或全黑时返回 None。"""
    sub = rgb[y0:y1, x0:x1]
    if sub.size == 0 or sub.shape[0] < 4 or sub.shape[1] < 4:
        return None
    g = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2GRAY)
    paper = float(np.percentile(g, 90.0))
    if paper <= 2.0:
        return None
    mask = g < (paper * 0.72)
    frac = float(mask.mean())
    if frac > 0.92:  # 整框几乎全黑: 多半是黑边/污渍, 不做 massa 判定
        return None
    return mask.sum(axis=1).astype(np.int32), frac


def _longest_blank_run(row_ink: np.ndarray, w: int, p: dict):
    """框内最长的"内部"空白带。

    只认两侧都有墨的空白带(各留 >=5% 框高): 贴着框顶/框底的空白是"框切太松",
    不是"两字合一", 不应该算 multi_mass。
    """
    thresh = max(2, int(round(p["multi_blank_row_frac"] * max(1, w))))
    blank = row_ink <= thresh
    k = int(p["multi_blank_close"])
    if k >= 3 and blank.size >= k:
        ker = np.ones((k, 1), np.uint8)
        closed = cv2.morphologyEx(blank.astype(np.uint8).reshape(-1, 1), cv2.MORPH_CLOSE, ker)
        blank = closed.ravel().astype(bool)
    h = blank.size
    edge = max(2, int(round(0.05 * h)))
    best, best_run = 0, None
    for a, b in common.runs(blank):
        if a < edge or (h - 1 - b) < edge:   # 贴边空白 -> 不算
            continue
        if b - a + 1 > best:
            best, best_run = b - a + 1, (int(a), int(b))
    return best, best_run


# --------------------------------------------------------------- 缺陷检测
def detect_defects(rgb: np.ndarray, page: dict, p: dict | None = None, px_checks: bool = True):
    """返回 (defects, glyphs, columns, warnings, stats)。纯函数, 不写盘。"""
    params = dict(DEF_PARAMS)
    if p:
        params.update(p)
    rgb = _as_rgb(rgb)
    H, W = rgb.shape[:2]
    if isinstance(page, str):
        page = load_page_any(page)
    pw = int(page.get("width") or W) or W
    ph = int(page.get("height") or H) or H
    sx, sy = W / float(pw), H / float(ph)
    warnings = []
    if abs(sx - 1) > 1e-3 or abs(sy - 1) > 1e-3:
        warnings.append("rgb %dx%d 与 page %dx%d 不一致, 坐标按 sx=%.4f sy=%.4f 缩放" % (W, H, pw, ph, sx, sy))

    glyphs = _norm_glyphs(page, sx, sy, warnings)
    cols = _norm_columns(page, sx)
    if not cols:
        cols = _derive_columns(glyphs)
        if cols:
            warnings.append("page 无 columns, 已按 glyph col 反推 %d 个列带" % len(cols))
    if not cols:
        cols = [{"idx": 0, "x0": 0, "x1": W, "w": W, "ink": 0}]
        warnings.append("page 无 columns 且无 col 信息, 整页当作单列")
    _assign_cols(glyphs, cols, warnings)

    defects = []
    flagged = {}

    def add(code, g, note, **metric):
        rec = {
            "code": code, "cn": CODE_CN.get(code, ""), "id": g["id"],
            "col": g["col_eff"], "box": [g["x0"], g["y0"], g["x1"], g["y1"]],
            "w": g["w"], "h": g["h"], "area": g["area"], "note": note,
        }
        rec.update(metric)
        defects.append(rec)
        flagged.setdefault(g["id"], []).append(code)

    # --- tiny / huge
    areas = np.array([g["area"] for g in glyphs if g["area"] > 0], dtype=np.float64)
    med = float(np.median(areas)) if areas.size else 0.0
    if med > 0:
        for g in glyphs:
            if g["area"] <= 0:
                continue
            r = g["area"] / med
            if r < params["tiny_ratio"]:
                add("tiny", g, "面积 %.0f = %.3fx 中位值 %.0f" % (g["area"], r, med), ratio=round(r, 3))
            elif r > params["huge_ratio"]:
                add("huge", g, "面积 %.0f = %.3fx 中位值 %.0f" % (g["area"], r, med), ratio=round(r, 3))
    else:
        warnings.append("中位面积 = 0, 跳过 tiny/huge")

    # --- touch_col
    by_idx = {c["idx"]: c for c in cols}
    for g in glyphs:
        c = by_idx.get(g["col_eff"])
        if c is None:
            continue
        tol = max(int(params["touch_tol_px"]), int(round(params["touch_tol_rel"] * max(1, c["w"]))))
        left_gap, right_gap = g["x0"] - c["x0"], c["x1"] - g["x1"]
        side, gap = None, None
        if left_gap <= tol:
            side, gap = "L", left_gap
        if right_gap <= tol and (gap is None or right_gap < gap):
            side, gap = "R", right_gap
        if side:
            note = "框%s边距列带边界 %+d px (容差 %d)" % ("左" if side == "L" else "右", gap, tol)
            if gap < 0:
                note += "; 已越出列带, 跨列粘连风险高"
            add("touch_col", g, note, side=side, gap=int(gap), tol=tol)

    # --- multi_mass (需要像素)
    empty_ids = []
    if px_checks and glyphs:
        for g in glyphs:
            x0, y0, x1, y1 = _clip_box(g["x0"], g["y0"], g["x1"], g["y1"], W, H)
            if x1 - x0 < 8 or y1 - y0 < 8:
                continue
            prof = _ink_row_profile(rgb, x0, y0, x1, y1)
            if prof is None:
                continue
            row_ink, frac = prof
            g["ink_frac"] = round(frac, 4)
            hh = y1 - y0
            if frac < params["empty_ink_frac"]:
                empty_ids.append(g["id"])
                continue  # 空框不是"两字合一", 单列提示
            run, span = _longest_blank_run(row_ink, x1 - x0, params)
            if hh > 0 and run > params["multi_blank_frac"] * hh:
                pct = run / float(hh)
                note = "框内 %d px 连续空白带 = %.1f%% 框高 (于 y+%d..y+%d)" % (run, 100.0 * pct, span[0], span[1])
                add("multi_mass", g, note, blank_frac=round(pct, 3), blank_px=int(run))

    # --- split_suspect
    groups = {}
    for g in glyphs:
        groups.setdefault(g["col_eff"], []).append(g)
    for key, arr in groups.items():
        arr = sorted(arr, key=lambda g: (g["y0"], g["x0"]))
        for a, b in zip(arr, arr[1:]):
            if a["h"] <= 0 or b["h"] <= 0:
                continue
            gap = b["y0"] - a["y1"]
            hmin = min(a["h"], b["h"])
            ov = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
            wmin = min(a["w"], b["w"])
            if wmin <= 0:
                continue
            ovr = ov / float(wmin)
            if gap < params["split_gap_frac"] * hmin and ovr > params["split_overlap"]:
                note = "同列相邻: 竖直间隙 %d px (%.1f%% 字高) 水平重叠 %.0f%%" % (gap, 100.0 * gap / hmin, 100.0 * ovr)
                cross = 0
                if px_checks and gap > 0:
                    # 切口带里还有墨 -> 笔画被切断, 基本可确认"一字被切成两个"
                    gx0, gx1 = max(a["x0"], b["x0"]), min(a["x1"], b["x1"])
                    gy0, gy1 = a["y1"], b["y0"]
                    if gx1 > gx0 and gy1 > gy0:
                        if gy1 - gy0 > 48:
                            gy1 = gy0 + 48
                        sub = rgb[gy0:gy1, gx0:gx1]
                        gg = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2GRAY)
                        pap = float(np.percentile(gg, 90.0))
                        if pap > 2.0:
                            cross = int((gg < pap * 0.72).sum())
                    if cross:
                        note += "; 切口带内有墨 %d px (笔画跨框相连)" % cross
                add("split_suspect", b, note, partner=a["id"], gap=int(gap),
                    gap_frac=round(gap / float(hmin), 3), overlap=round(ovr, 3),
                    gap_ink=cross, crosses=bool(cross))

    # --- margin
    mx, my = params["margin_frac"] * W, params["margin_frac"] * H
    for g in glyphs:
        sides = []
        if g["x0"] < mx:
            sides.append("L")
        if g["y0"] < my:
            sides.append("T")
        if g["x1"] > W - mx:
            sides.append("R")
        if g["y1"] > H - my:
            sides.append("B")
        if sides:
            add("margin", g, "落在页面边缘 %.0f%% 内 (%s)" % (100 * params["margin_frac"], "/".join(sides)),
                sides="/".join(sides))

    if empty_ids:
        warnings.append("%d 个框内几乎没有墨(ink<%g%%), 疑似空框/噪声块: ids %s"
                        % (len(empty_ids), 100 * params["empty_ink_frac"],
                           ",".join(str(i) for i in empty_ids[:20])))

    order = {c: i for i, c in enumerate(DEFECT_CODES)}
    defects.sort(key=lambda r: (order.get(r["code"], 99), r["id"]))

    col_counts = {}
    for g in glyphs:
        col_counts[g["col_eff"]] = col_counts.get(g["col_eff"], 0) + 1
    summary = {c: 0 for c in DEFECT_CODES}
    for d in defects:
        summary[d["code"]] += 1
    stats = {
        "width": W, "height": H, "n_columns": len(cols), "n_glyphs": len(glyphs),
        "median_area": round(med, 1),
        "area_src": (glyphs[0]["area_src"] if glyphs else "bbox"),
        "n_suspicious": len(flagged),
        "suspicious_pct": round(100.0 * len(flagged) / len(glyphs), 2) if glyphs else 0.0,
        "summary": summary,
        "n_empty": len(empty_ids),
        "empty_ids": empty_ids,
        "col_counts": {str(k): v for k, v in sorted(col_counts.items(), key=lambda kv: (kv[0] is None, kv[0]))},
    }
    return defects, glyphs, cols, warnings, stats, params


# --------------------------------------------------------------- 渲染函数
def _render_overlay(rgb, glyphs, cols, out_path, target_w):
    h, w = rgb.shape[:2]
    s = target_w / float(w)
    view = {
        "columns": [{"idx": c["idx"], "x0": c["x0"], "x1": c["x1"]} for c in cols],
        "glyphs": [{"id": g["id"], "x0": g["x0"], "y0": g["y0"], "x1": g["x1"], "y1": g["y1"],
                    "col": (g["col_eff"] if g["col_eff"] is not None else 0)} for g in glyphs],
    }
    common.draw_overlay(rgb, view, out_path, target_w=target_w, draw_ids=True)
    return s


def _render_defects_png(rgb, glyphs, cols, flagged_ids, summary, out_path, target_w):
    h, w = rgb.shape[:2]
    small = cv2.resize(rgb, (int(target_w), max(1, int(round(h * target_w / float(w))))),
                       interpolation=cv2.INTER_AREA)
    small = cv2.cvtColor(small, cv2.COLOR_RGB2BGR)
    fsc = max(0.55, target_w / 1600.0)      # 顶栏字号随缩略图宽度缩放
    l1 = "defects  page %dx%d  cols %d  glyphs %d  suspicious %d  (%d hits)" % (
        w, h, len(cols), len(glyphs), len(flagged_ids), sum(summary.values()))
    l2 = "   ".join("%s %d" % (CODE_EN.get(c, c), summary.get(c, 0)) for c in DEFECT_CODES)
    f1 = _fit_scale(l1, target_w - 16, fsc)
    f2 = _fit_scale(l2, target_w - 16, fsc * 0.9)
    (_, h1), b1 = cv2.getTextSize(l1, cv2.FONT_HERSHEY_SIMPLEX, f1, 1)
    (_, h2), b2 = cv2.getTextSize(l2, cv2.FONT_HERSHEY_SIMPLEX, f2, 1)
    y1 = 4 + h1 + b1
    y2 = y1 + h2 + b2 + 2
    hdr = y2 + 6
    canvas = np.full((small.shape[0] + hdr, small.shape[1], 3), _HDR_BG[0], np.uint8)
    canvas[hdr:] = small
    s = target_w / float(w)
    for c in cols:
        cv2.line(canvas, (int(c["x0"] * s), hdr), (int(c["x0"] * s), canvas.shape[0] - 1), (70, 70, 70), 1)
        cv2.line(canvas, (int(c["x1"] * s), hdr), (int(c["x1"] * s), canvas.shape[0] - 1), (70, 70, 70), 1)
    for g in glyphs:
        col = _PALETTE[(g["col_eff"] or 0) % len(_PALETTE)]
        box = (int(g["x0"] * s), int(g["y0"] * s) + hdr, int(g["x1"] * s), int(g["y1"] * s) + hdr)
        if g["id"] in flagged_ids:
            cv2.rectangle(canvas, box[:2], box[2:], _RED, 3)
        else:
            cv2.rectangle(canvas, box[:2], box[2:], col, 1)
    # 可疑框的代号(放最后画, 压在上面); cv2.putText 只支持 ASCII, 中文只进 json
    # 只给"要动手修的"类打标签, touch_col/margin 数量多且低优先, 只标红框免得糊成一片
    lsc = max(0.4, 0.4 * target_w / 1200.0)
    for g in glyphs:
        if g["id"] not in flagged_ids:
            continue
        codes = [c for c in flagged_ids[g["id"]] if c in _LABEL_CODES]
        if not codes:
            continue
        x, y = int(g["x0"] * s), int(g["y0"] * s) + hdr
        _put_label(canvas, "%d:%s" % (g["id"], "+".join(CODE_EN.get(c, c) for c in codes)),
                   x, y - 3, lsc, (255, 255, 255), _RED)
    _put_label(canvas, l1, 8, y1, f1, (255, 255, 255), _HDR_BG)
    _put_label(canvas, l2, 8, y2, f2, (200, 220, 255), _HDR_BG)
    cv2.imwrite(out_path, canvas)
    return out_path


def _split_span(y0, y1, strip_h, spans, max_extra=1200):
    """把 [y0,y1) 切成 ~strip_h 的窗口, 尽量不切断任何框。"""
    out = []
    a = y0
    while a < y1:
        b = min(y1, a + strip_h)
        if b < y1:
            for _ in range(8):
                cross = [g1 for (g0, g1) in spans if g0 < b < g1]
                if not cross:
                    break
                nb = min(y1, max(cross) + 4)
                if nb <= b or nb - a > strip_h + max_extra:
                    break
                b = nb
        out.append((a, b))
        a = b
    # 合并过短的末段(但别让单条超过 1.25*strip_h)
    if len(out) >= 2 and (out[-1][1] - out[-1][0]) < 0.5 * strip_h \
            and (out[-1][1] - out[-2][0]) <= 1.25 * strip_h:
        a, _ = out[-2]
        out[-2:] = [(a, out[-1][1])]
    return out


def _write_strips(rgb, glyphs, cols, flagged_ids, out_dir, strip_h, max_strips):
    strips_dir = os.path.join(out_dir, "strips")
    os.makedirs(strips_dir, exist_ok=True)
    H, W = rgb.shape[:2]
    by_col = {}
    for g in glyphs:
        by_col.setdefault(g["col_eff"], []).append(g)
    written = 0
    names = []
    for c in cols:
        arr = by_col.get(c["idx"]) or []
        if not arr:
            continue
        pad = 16
        x0 = max(0, c["x0"] - pad)
        x1 = min(W, c["x1"] + pad)
        if x1 - x0 < 8:
            continue
        spans = [(g["y0"], g["y1"]) for g in arr]
        y_lo = max(0, min(s[0] for s in spans) - 24)
        y_hi = min(H, max(s[1] for s in spans) + 24)
        for (ya, yb) in _split_span(y_lo, y_hi, strip_h, spans):
            if written >= max_strips:
                return strips_dir, written, names
            crop = rgb[ya:yb, x0:x1]
            if crop.size == 0:
                continue
            img = cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_RGB2BGR)
            cw = img.shape[1]
            sc = max(0.45, min(1.6, cw / 380.0))   # 字号跟着条宽走(真实页列带 ~400px)
            hdr = int(round(10 + 16 * sc))
            canvas = np.full((img.shape[0] + hdr, cw, 3), _HDR_BG[0], np.uint8)
            canvas[hdr:] = img
            # 列带边界(灰线) + 顶部 L/R 小标
            for bx, tag, tx in ((c["x0"], "L", None), (c["x1"], "R", None)):
                px = bx - x0
                if 0 <= px < cw:
                    cv2.line(canvas, (px, hdr), (px, canvas.shape[0] - 1), (90, 90, 90), 1)
                    _put_label(canvas, tag, px + 2 if tag == "L" else px - 2 - int(9 * sc),
                               int(round(10 * sc + 4)), sc * 0.85, (220, 220, 220), (40, 40, 40))
            used = [g for g in arr if g["y1"] > ya and g["y0"] < yb]
            n_flag = 0
            for g in used:
                col = _PALETTE[(c["idx"]) % len(_PALETTE)]
                gx0, gy0 = g["x0"] - x0, g["y0"] - ya + hdr
                gx1, gy1 = g["x1"] - x0, g["y1"] - ya + hdr
                ty = max(hdr + int(11 * sc), gy0) + 1
                if g["id"] in flagged_ids:
                    n_flag += 1
                    cv2.rectangle(canvas, (max(0, gx0), max(hdr, gy0)),
                                  (min(cw - 1, gx1), min(canvas.shape[0] - 1, gy1)), _RED, max(2, int(2 * sc)))
                    _put_label(canvas, "%d %s" % (g["id"], "+".join(CODE_EN.get(c2, c2) for c2 in flagged_ids[g["id"]])),
                               max(0, gx0) + 2, ty, sc, (255, 255, 255), _RED)
                else:
                    cv2.rectangle(canvas, (max(0, gx0), max(hdr, gy0)),
                                  (min(cw - 1, gx1), min(canvas.shape[0] - 1, gy1)), col, max(1, int(2 * sc)))
                    _put_label(canvas, str(g["id"]), max(0, gx0) + 2, ty, sc, (20, 20, 20), col)
            ids = [g["id"] for g in used]
            head = "c%d  y[%d,%d)  ids %s..%s  n=%d  flagged=%d  x %d..%d @1:1" % (
                c["idx"], ya, yb, min(ids) if ids else "-", max(ids) if ids else "-",
                len(used), n_flag, x0, x1)
            if _text_w(head, sc * 0.92) > cw - 8:  # 太长就丢掉 x 范围
                head = "c%d  y[%d,%d)  ids %s..%s  n=%d  flagged=%d" % (
                    c["idx"], ya, yb, min(ids) if ids else "-", max(ids) if ids else "-",
                    len(used), n_flag)
            hsc = _fit_scale(head, cw - 8, sc * 0.92)
            _put_label(canvas, head, 4, int(round(4 + 10 * sc)), hsc,
                       (255, 255, 255), _HDR_BG)
            name = "c%02d_y%05d-%05d.png" % (c["idx"], ya, yb)
            cv2.imwrite(os.path.join(strips_dir, name), canvas,
                        [int(cv2.IMWRITE_PNG_COMPRESSION), 3])
            written += 1
            names.append(name)
    return strips_dir, written, names


def _write_defect_crops(rgb, glyphs, flagged_ids, out_dir, max_crops):
    d = os.path.join(out_dir, "defects")
    os.makedirs(d, exist_ok=True)
    H, W = rgb.shape[:2]
    by_id = {g["id"]: g for g in glyphs}
    n = 0
    for gid in sorted(flagged_ids):
        if n >= max_crops:
            break
        g = by_id.get(gid)
        if g is None:
            continue
        x0, y0, x1, y1 = _clip_box(g["x0"] - 8, g["y0"] - 8, g["x1"] + 8, g["y1"] + 8, W, H)
        if x1 <= x0 or y1 <= y0:
            continue
        crop = cv2.cvtColor(np.ascontiguousarray(rgb[y0:y1, x0:x1]), cv2.COLOR_RGB2BGR)
        codes = flagged_ids[gid]
        cv2.imwrite(os.path.join(d, "%05d_%s.png" % (gid, "_".join(codes))), crop)
        n += 1
    return d, n


# ------------------------------------------------------------------ 主入口
def write_qc(rgb, page, out_dir, strips=True, target_w=2400, *, strip_h=1500,
             crops=True, max_crops=400, max_strips=600, full=False,
             params=None, px_checks=True, verbose=True) -> dict:
    """质检: 写 overlay / 分带长条 / defects 图 + defects.json, 并打印摘要。"""
    t0 = time.time()
    os.makedirs(out_dir, exist_ok=True)
    rgb = _as_rgb(rgb)
    if isinstance(page, str):
        page = load_page_any(page)

    defects, glyphs, cols, warnings, stats, params_used = detect_defects(
        rgb, page, p=params, px_checks=px_checks)
    flagged = {}
    for d in defects:
        flagged.setdefault(d["id"], []).append(d["code"])

    out = {"out_dir": os.path.abspath(out_dir)}

    over = os.path.join(out_dir, "overlay.png")
    _render_overlay(rgb, glyphs, cols, over, target_w)
    out["overlay"] = over

    strips_dir, n_strips, names = (None, 0, [])
    if strips:
        strips_dir, n_strips, names = _write_strips(rgb, glyphs, cols, flagged, out_dir, strip_h, max_strips)
    out["strips_dir"] = strips_dir
    out["n_strips"] = n_strips

    dpng = os.path.join(out_dir, "defects.png")
    _render_defects_png(rgb, glyphs, cols, flagged, stats["summary"], dpng, target_w)
    out["defects_png"] = dpng

    crop_dir, n_crops = (None, 0)
    if crops and flagged:
        crop_dir, n_crops = _write_defect_crops(rgb, glyphs, flagged, out_dir, max_crops)
    out["defects_dir"] = crop_dir
    out["n_crops"] = n_crops

    payload = {
        "out_dir": out["out_dir"],
        "source": page.get("source"),
        "stats": stats,
        "params": params_used,
        "warnings": warnings,
        "defects": defects,
        "flagged": {str(k): v for k, v in sorted(flagged.items())},
        "artifacts": {
            "overlay": over,
            "defects_png": dpng,
            "strips_dir": strips_dir,
            "strips": names[:50],
            "n_strips": n_strips,
            "defects_dir": crop_dir,
            "n_crops": n_crops,
        },
    }
    if full:
        payload["glyphs"] = [
            {"id": g["id"], "col": g["col_eff"], "box": [g["x0"], g["y0"], g["x1"], g["y1"]],
             "w": g["w"], "h": g["h"], "area": g["area"], "ink_frac": g.get("ink_frac")}
            for g in glyphs
        ]
    jpath = os.path.join(out_dir, "defects.json")
    tmp = jpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, jpath)
    out["defects_json"] = jpath
    out["stats"] = stats
    out.update(stats)
    out["defects"] = defects
    out["flagged"] = flagged
    out["warnings"] = warnings
    out["elapsed_s"] = round(time.time() - t0, 2)

    if verbose:
        _log("[qc] page %dx%d  cols %d  glyphs %d  median_area %.0f (%s)"
             % (stats["width"], stats["height"], stats["n_columns"], stats["n_glyphs"],
                stats["median_area"], stats["area_src"]))
        _log("     可疑 %d 个字框 (%.2f%%)   %.2fs"
             % (stats["n_suspicious"], stats["suspicious_pct"], out["elapsed_s"]))
        for c in DEFECT_CODES:
            _log("       %-14s %-8s %d" % (c, CODE_CN[c], stats["summary"][c]))
        if stats["n_empty"]:
            _log("       空框(框内无墨)      %d" % stats["n_empty"])
        for wmsg in warnings:
            _log("     ! " + wmsg)
        _log("     产物: " + out["out_dir"])
    return out


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Yi 切字质检: 叠加图 / 分带长条 / 缺陷统计")
    ap.add_argument("image", help="原图 (tif/png/jpg) 或 .npy 灰度数组")
    ap.add_argument("page", help="page.json")
    ap.add_argument("outdir", help="输出目录")
    ap.add_argument("--target-w", type=int, default=2400, help="缩略图宽度 (默认 2400)")
    ap.add_argument("--strip-h", type=int, default=1500, help="分带每段高度 (默认 1500, 原分辨率)")
    ap.add_argument("--no-strips", action="store_true", help="不产分带长条")
    ap.add_argument("--no-px", action="store_true", help="跳过像素级检查(multi_mass)")
    ap.add_argument("--no-crops", action="store_true", help="不产可疑框小图")
    ap.add_argument("--full", action="store_true", help="defects.json 里带上每个框的度量")
    a = ap.parse_args(argv)
    try:  # 控制台编码不支持中文时不要抛 UnicodeEncodeError
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:
        pass

    img = a.image
    if img.lower().endswith(".npy"):
        arr = np.load(img)
        rgb = _as_rgb(arr)
        src = "%s (npy %s)" % (img, arr.shape)
    else:
        rgb = common.load_rgb(img)
        src = img
    page = load_page_any(a.page)
    page.setdefault("source", src)
    res = write_qc(rgb, page, a.outdir, strips=not a.no_strips, target_w=a.target_w,
                   strip_h=a.strip_h, crops=not a.no_crops, px_checks=not a.no_px, full=a.full)
    return 0 if not res.get("stats", {}).get("n_glyphs") == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
