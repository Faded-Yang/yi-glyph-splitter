"""Business layer: segment a page, render crops, export tiles, suspect boxes.

Everything here is UI agnostic; app/server.py wraps these calls in jobs.
Reuses the existing CLI core (tools/yi_core) instead of re-implementing it:

    from yi_core import common as C, segment_v1 as S1, export as E

CLI:  python -m app.pipeline --selftest
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import statistics
import sys
import threading
import time
from pathlib import Path

from . import paths, store as store_mod

log = logging.getLogger("yi_segment.pipeline")

# ------------------------------------------------------------- tools on path
_TOOLS_DIR = None


def ensure_tools() -> str:
    """Make tools/ importable and return its path (PyInstaller safe)."""
    global _TOOLS_DIR
    if _TOOLS_DIR is None:
        _TOOLS_DIR = str(paths.find_tools_dir())
    if _TOOLS_DIR not in sys.path:
        sys.path.insert(0, _TOOLS_DIR)
    return _TOOLS_DIR


ensure_tools()
from yi_core import common as C          # noqa: E402
from yi_core import export as E          # noqa: E402
from yi_core import segment_v1 as S1     # noqa: E402

try:
    import cv2
    import numpy as np
    from PIL import Image
except ImportError as exc:  # pragma: no cover - deps are guaranteed
    raise RuntimeError("缺少图像依赖（numpy/opencv/PIL）：%s" % exc)

Image.MAX_IMAGE_PIXELS = None

PREVIEW_W = 1600
TILE_RE = re.compile(r"^\d+\.(png|jpg|jpeg|webp|bmp|tif|tiff)$", re.IGNORECASE)
CLEAN_FILES = ("index.csv", "sheet.png", "overlay.png")
ORDER_VALUES = ("visual", "reading", "id")
FMT_VALUES = ("png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff")
PAGE_MARKER = ".yisegment_page.json"

_OUT_LOCKS = {}
_OUT_LOCKS_GUARD = threading.Lock()


class Canceled(Exception):
    """The caller asked to stop (server turns this into a canceled job)."""


# --------------------------------------------------------------- tiny helpers
def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _cb(progress_cb, frac: float, message: str) -> None:
    if progress_cb is None:
        return
    try:
        progress_cb(max(0.0, min(1.0, float(frac))), str(message))
    except TypeError:
        try:
            progress_cb(max(0.0, min(1.0, float(frac))))
        except Exception:
            pass
    except Exception:
        pass


def _canceled(cancel_cb) -> bool:
    if cancel_cb is None:
        return False
    try:
        return bool(cancel_cb())
    except Exception:
        return False


def check_cancel(cancel_cb) -> None:
    if _canceled(cancel_cb):
        raise Canceled()


def _require_page(store, pid) -> dict:
    entry = store.get_page(pid)
    if not entry:
        raise store_mod.StoreError("页面不存在：%s" % pid)
    return entry


def _clamp_int(v, lo, hi, default=0) -> int:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _image_size(src: str) -> tuple:
    with Image.open(src) as im:
        return im.size


# --------------------------------------------------------------------- preview
def build_preview(store, pid, *, width: int = PREVIEW_W) -> Path:
    """Render the page thumbnail (page only, boxes are drawn by the UI)."""
    entry = _require_page(store, pid)
    src = entry["src"]
    if not os.path.isfile(src):
        raise store_mod.StoreError("源文件不存在：%s" % src)
    rgb = C.load_rgb(src)
    h, w = rgb.shape[:2]
    width = _clamp_int(width, 200, 4000, PREVIEW_W)
    if w > width:
        small = cv2.resize(rgb, (width, max(1, int(round(h * width / float(w))))),
                           interpolation=cv2.INTER_AREA)
    else:
        small = rgb
    if small.ndim == 2:
        small = np.dstack([small, small, small])
    buf = io.BytesIO()
    Image.fromarray(small.astype(np.uint8)).save(buf, format="PNG", compress_level=3)
    store.save_preview_png(pid, buf.getvalue())
    return store.preview_path(pid)


# ---------------------------------------------------------------- process page

# ------------------------------------------------------------------ calibration
def calibration_file() -> str:
    """Learned segmentation params (from a human-corrected page), if any."""
    from . import paths as paths_mod

    cands = [
        paths_mod.data_dir() / "calibration" / "segment_params.json",
        paths_mod.resource_path("data", "calibration", "segment_params.json"),
        paths_mod.app_root().parent / "data" / "calibration" / "segment_params.json",
    ]
    for c in cands:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    return ""


def calibration_params():
    """(overrides dict, source path) loaded from the learned profile."""
    fp = calibration_file()
    if not fp:
        return {}, ""
    try:
        data = json.loads(open(fp, encoding="utf-8").read())
        ov = data.get("params") or {}
        if isinstance(ov, dict):
            return ov, fp
    except Exception:  # noqa: BLE001
        log.warning("calibration file unreadable: %s", fp, exc_info=True)
    return {}, ""


def process_page(store, pid, progress_cb=None, cancel_cb=None, *, force: bool = False) -> dict:
    """Read -> segment_v1.segment_page -> page.json + boxes.json + preview.png.

    force=True re-runs the segmenter even if page.json exists and rebuilds boxes
    from the fresh glyphs (user box edits are overwritten - callers must confirm).
    """
    entry = _require_page(store, pid)
    src = entry["src"]
    if not os.path.isfile(src):
        msg = "源文件不存在：%s" % src
        store.set_error(pid, msg)
        raise store_mod.StoreError(msg)

    t0 = time.time()
    timings = {}
    reused = store.page_json_path(pid).is_file() and not force
    page = None

    if reused:
        _cb(progress_cb, 0.05, "复用已有分割结果")
        page = store.load_page_json(pid)
        timings["load"] = 0.0
        timings["segment"] = 0.0
    else:
        _cb(progress_cb, 0.02, "读取灰度图")
        check_cancel(cancel_cb)
        t = time.time()
        gray = C.load_gray(src)
        timings["load"] = time.time() - t
        check_cancel(cancel_cb)
        _cb(progress_cb, 0.15, "分割中（可能需要十几秒）")
        t = time.time()
        ov, cal_src = calibration_params()
        seg_params = None
        if ov:
            try:
                seg_params = S1.Params(**ov)
                log.info("segmentation uses learned params from %s: %s", cal_src, ov)
            except Exception:  # noqa: BLE001 - bad profile must not break processing
                log.warning("bad calibration params, using defaults", exc_info=True)
        page = S1.segment_page(gray, params=seg_params)
        timings["segment"] = time.time() - t
        check_cancel(cancel_cb)
        page = dict(page or {})
        page["source"] = src
        _cb(progress_cb, 0.72, "保存分割结果")
        store.save_page_json(pid, page)

    # 自动修正「两列并成一列」的列带（只改 page.json 的 columns，不动框）
    try:
        page = dict(page)
        cols_now = ensure_columns(store, pid, page)
        if cols_now:
            page["columns"] = cols_now
    except Exception:  # noqa: BLE001 - 列修正失败不影响主流程
        pass

    width = store_mod._int(page.get("width"), 0) or store_mod._int(entry.get("width"), 0)
    height = store_mod._int(page.get("height"), 0) or store_mod._int(entry.get("height"), 0)
    glyphs = page.get("glyphs") or []

    if force and store.has_boxes_file(pid):
        store.boxes_path(pid).unlink(missing_ok=True)

    boxes, warns = store.write_boxes_if_missing(pid, store_mod.boxes_from_glyphs(
        glyphs, width or None, height or None)[0], width=width, height=height)
    check_cancel(cancel_cb)

    _cb(progress_cb, 0.85, "生成预览图")
    t = time.time()
    if force or not store.has_preview(pid):
        build_preview(store, pid)
    timings["preview"] = time.time() - t
    check_cancel(cancel_cb)

    _cb(progress_cb, 0.97, "更新状态")
    elapsed = time.time() - t0
    entry = store.mark_processed(pid, width=width, height=height,
                                 n_boxes=len(boxes), elapsed=elapsed)
    warning = ""
    if not boxes:
        warning = "未检测到字框（页面可能全白、全黑或扫描方向不对）"
    _cb(progress_cb, 1.0, "完成")
    return {
        "pid": pid,
        "name": entry.get("name") or os.path.basename(src),
        "width": width,
        "height": height,
        "n_boxes": len(boxes),
        "status": entry.get("status"),
        "reused_page_json": bool(reused),
        "warning": warning,
        "warnings": warns,
        "seconds": round(elapsed, 2),
        "timings": {k: round(v, 2) for k, v in timings.items()},
    }


# ------------------------------------------------------- standard-glyph check
def run_refcheck(store, pid, progress_cb=None, cancel_cb=None) -> dict:
    """Score every box against the standard Yi glyph bank (offline, no API)."""
    from . import yi_ref

    entry = _require_page(store, pid)
    src = entry["src"]
    if not os.path.isfile(src):
        msg = "源文件不存在：%s" % src
        _cb(progress_cb, 1.0, msg)
        raise store_mod.StoreError(msg)
    if not yi_ref.available():
        raise store_mod.StoreError("规范字库缺失（data/glyphbank/bank.npz）")
    boxes = store.boxes(pid)
    if not boxes:
        raise store_mod.StoreError("这一页还没有框，先处理或手动框选")

    t0 = time.time()
    _cb(progress_cb, 0.03, "读取灰度图")
    check_cancel(cancel_cb)
    gray = C.load_gray(src)

    # 1) 先把这一页里「人工确认过」的框收进自建字库（更好的笔迹样本）
    try:
        added = harvest_confirmed(store, pid, gray)
    except Exception:  # noqa: BLE001 - 收录失败不影响校验
        added = 0
    if added:
        _cb(progress_cb, 0.12, "收录 %d 个自建字形" % added)

    _cb(progress_cb, 0.20, "对照字库（标准 %d + 自建 %d 个框）" % (len(boxes), 0))
    result = yi_ref.check_boxes(gray, boxes)
    check_cancel(cancel_cb)

    # 2) 统计「本页出现过几次」：按每个框 top1 的字形标签计数
    counts = {}
    for b in result.get("boxes") or []:
        top = b.get("top") or []
        lab = top[0].get("label") if top else ""
        b["label"] = lab or ""
        if lab:
            counts[lab] = counts.get(lab, 0) + 1
    for b in result.get("boxes") or []:
        lab = b.get("label") or ""
        b["label_count"] = int(counts.get(lab, 0))
        for t in b.get("top") or []:
            t["page_count"] = int(counts.get(t.get("label") or "", 0))
    result["label_counts"] = counts

    result["pid"] = pid
    result["seconds"] = round(time.time() - t0, 2)
    result["boxes"] = result.get("boxes") or []
    try:
        from . import userbank
        result["userbank"] = userbank.stats()
    except Exception:  # noqa: BLE001
        result["userbank"] = {"n": 0, "labels": {}}
    store.save_refcheck(pid, result)
    _cb(progress_cb, 1.0, "完成：%d 框，低分 %d" % (result["stats"]["n"], result["stats"]["low"]))
    return result


def page_is_edited(store, pid: str) -> bool:
    """True when the page's boxes differ from the automatic segmentation (i.e. hand-edited)."""
    try:
        boxes = store.boxes(pid)
        page = store.load_page_json(pid)
        auto_n = len(page.get("glyphs") or [])
        return bool(auto_n) and len(boxes) != auto_n
    except Exception:  # noqa: BLE001
        return False


def harvest_confirmed(store, pid: str, gray=None) -> int:
    """Add the page's human-confirmed boxes (review=1) to the user glyph bank.

    Their own handwriting is a better template than the printed manual, and the
    bank grows every time they confirm boxes + re-run the check.
    """
    from . import userbank
    from . import yi_ref

    try:
        boxes = store.boxes(pid)
    except Exception:  # noqa: BLE001
        return 0
    # 用户明确要求：绿色(已确认)、蓝色(一般)、红色(可疑) 全部收录。
    # 每条的 review / refLevel 都记在 meta 里，以后要按颜色筛选也有依据。
    confirmed = list(boxes)
    if not confirmed:
        return 0
    levels = {}
    try:
        rc = store.refcheck(pid)
        for rb in rc.get("boxes") or []:
            levels[rb.get("id")] = rb.get("level") or ""
    except Exception:  # noqa: BLE001
        pass
    if gray is None:
        gray = page_gray(store, pid)
    entry = _require_page(store, pid)
    page_name = entry.get("name") or pid
    glyphs = []
    for b in confirmed:
        x0, y0, x1, y1 = int(b["x0"]), int(b["y0"]), int(b["x1"]), int(b["y1"])
        tile = gray[y0:y1, x0:x1]
        if tile.size == 0:
            continue
        ink = yi_ref.norm_ink(tile)
        if int((ink > 0).sum()) < 20:
            continue
        try:
            m = yi_ref.match(ink, k=1)
            lab = (m.get("top") or [{}])[0].get("label") or ""
        except Exception:  # noqa: BLE001
            lab = ""
        glyphs.append({"ink48": ink, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "label": lab,
                       "review": int(b.get("review") or 0),
                       "level": str(levels.get(b.get("id")) or "")})
    res = userbank.add_glyphs(glyphs, page=page_name)
    return int(res.get("added") or 0)


# --------------------------------------------------- per-box reference scoring
# Cached page grayscale so a single-box re-check costs ~5 ms instead of re-reading
# a 210 MB scan. Keep at most 2 pages (each ~70 MB).
_GRAY_CACHE: dict = {}


def page_gray(store, pid: str):
    """Grayscale page, cached per page (keyed by src + mtime)."""
    src = store.src(pid)
    if not src or not os.path.isfile(src):
        raise store_mod.StoreError("源文件不存在：%s" % src)
    key = (src, os.path.getmtime(src))
    ent = _GRAY_CACHE.get(pid)
    if ent and ent[0] == key:
        return ent[1]
    gray = C.load_gray(src)
    _GRAY_CACHE[pid] = (key, gray)
    while len(_GRAY_CACHE) > 2:
        _GRAY_CACHE.pop(next(iter(_GRAY_CACHE)))
    return gray


def score_boxes(store, pid: str, boxes) -> list:
    """Reference match for the given boxes (one page load, N cheap crops)."""
    from . import yi_ref

    if not yi_ref.available():
        raise store_mod.StoreError("规范字库缺失（data/glyphbank/bank.npz）")
    gray = page_gray(store, pid)
    out = []
    for b in boxes or []:
        x0, y0 = int(b.get("x0", 0)), int(b.get("y0", 0))
        x1, y1 = int(b.get("x1", 0)), int(b.get("y1", 0))
        h, w = gray.shape[:2]
        x0, x1 = max(0, min(w, x0)), max(0, min(w, x1))
        y0, y1 = max(0, min(h, y0)), max(0, min(h, y1))
        if x1 - x0 < 3 or y1 - y0 < 3:
            out.append({"id": b.get("id"), "score": 0.0, "level": "low", "top": []})
            continue
        m = yi_ref.score_tile(gray[y0:y1, x0:x1])
        score = float(m.get("score") or 0.0)
        level = ("strong" if score >= yi_ref.STRONG else
                 "good" if score >= yi_ref.GOOD else
                 "warn" if score >= yi_ref.WARN else "low")
        out.append({"id": b.get("id"), "score": round(score, 4), "level": level,
                    "top": m.get("top") or []})
    return out


# -------------------------------------------------------------------- cropping
def render_crop(store, pid, x0, y0, x1, y1, scale: float = 2.0, *, max_side: int = 4000) -> bytes:
    """Crop the original page and return PNG bytes (editor detail view)."""
    entry = _require_page(store, pid)
    src = entry["src"]
    if not os.path.isfile(src):
        raise store_mod.StoreError("源文件不存在：%s" % src)

    W, H = store.dims(pid)
    if W <= 0 or H <= 0:
        W, H = _image_size(src)
        if W <= 0 or H <= 0:
            raise store_mod.StoreError("无法读取页面尺寸")
    a, b = int(round(float(x0))), int(round(float(y0)))
    c, d = int(round(float(x1))), int(round(float(y1)))
    if c < a:
        a, c = c, a
    if d < b:
        b, d = d, b
    a, c = max(0, min(W, a)), max(0, min(W, c))
    b, d = max(0, min(H, b)), max(0, min(H, d))
    if c - a < 2 or d - b < 2:
        raise store_mod.StoreError("裁剪区域太小或超出页面范围")
    try:
        s = float(scale)
    except (TypeError, ValueError):
        s = 2.0
    s = max(0.1, min(8.0, s))

    with Image.open(src) as im:
        rgb = im.convert("RGB")
        crop = rgb.crop((a, b, c, d))
    ow = max(1, int(round((c - a) * s)))
    oh = max(1, int(round((d - b) * s)))
    if max(ow, oh) > max_side:
        f = max_side / float(max(ow, oh))
        ow = max(1, int(round(ow * f)))
        oh = max(1, int(round(oh * f)))
    if (ow, oh) != crop.size:
        resample = Image.LANCZOS if s >= 1.0 else Image.BILINEAR
        crop = crop.resize((ow, oh), resample)
    buf = io.BytesIO()
    crop.save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


# --------------------------------------------------------------------- export
def _clean_tiles(out_dir: Path) -> list:
    """Remove stale tiles / index / sheet before a fresh export (see AGENTS.md)."""
    removed = []
    if not out_dir.is_dir():
        return removed
    for child in out_dir.iterdir():
        if not child.is_file():
            continue
        name = child.name
        if name in CLEAN_FILES or TILE_RE.match(name):
            try:
                child.unlink()
                removed.append(name)
            except OSError:
                pass
    return removed


def _lock_for(path: Path):
    key = os.path.normcase(os.path.abspath(str(path)))
    with _OUT_LOCKS_GUARD:
        if key not in _OUT_LOCKS:
            _OUT_LOCKS[key] = threading.Lock()
        return key, _OUT_LOCKS[key]


def default_export_dir(store) -> Path:
    return Path(store.root) / "exports"


def _page_columns(page: dict, boxes: list) -> list:
    cols = []
    for i, c in enumerate(page.get("columns") or []):
        try:
            cols.append({"idx": int(c.get("idx", i)), "x0": int(c["x0"]), "x1": int(c["x1"])})
        except (KeyError, TypeError, ValueError):
            continue
    if cols:
        return cols
    # fallback: cluster box x-centres into reading bands
    if not boxes:
        return []
    centers = sorted((b["x0"] + b["x1"]) / 2.0 for b in boxes)
    widths = sorted(b["x1"] - b["x0"] for b in boxes)
    med_w = widths[len(widths) // 2] if widths else 0
    thr = max(30.0, med_w * 1.25)
    groups = [[centers[0]]]
    for cx in centers[1:]:
        if cx - groups[-1][-1] <= thr:
            groups[-1].append(cx)
        else:
            groups.append([cx])
    out = []
    for i, g in enumerate(groups):
        pad = med_w / 2.0
        out.append({"idx": i, "x0": int(max(0, g[0] - pad)), "x1": int(g[-1] + pad)})
    return out



def _ref_low_boxes(store, pid) -> list:
    """Boxes that the last reference-check marked as low (matched by geometry)."""
    data = store.refcheck(pid)
    items = data.get("boxes") or []
    out = []
    for it in items:
        if str(it.get("level")) != "low":
            continue
        box = it.get("box")
        if not box or len(box) != 4:
            continue
        out.append((float(box[0]), float(box[1]), float(box[2]), float(box[3])))
    return out


def _is_low_box(b, ref_low) -> bool:
    ax0, ay0, ax1, ay1 = float(b["x0"]), float(b["y0"]), float(b["x1"]), float(b["y1"])
    for (bx0, by0, bx1, by1) in ref_low:
        ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        iy = max(0.0, min(ay1, by1) - max(ay0, by0))
        inter = ix * iy
        if inter <= 0:
            continue
        union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
        if union > 0 and inter / union >= 0.6:
            return True
    return False



# ---------------------------------------------------------------- columns
def _ink_profile(gray, x0: int, x1: int):
    """Per-x ink amount (dark pixels counted down the page) for a column band."""
    import numpy as np

    h, w = gray.shape[:2]
    x0 = max(0, min(w - 1, int(x0)))
    x1 = max(x0 + 1, min(w, int(x1)))
    band = gray[:, x0:x1]
    ink = (band < 120).astype(np.float32)
    return ink.sum(axis=0)


def _band_valley(prof, x0: int):
    """Best interior cut for a too-wide band: deepest ink valley, else midpoint."""
    import numpy as np

    w = len(prof)
    if w < 80:
        return None
    lo, hi = int(w * 0.30), int(w * 0.70)
    seg = prof[lo:hi]
    if seg.size == 0:
        return None
    k = max(3, int(w * 0.03))
    ker = np.ones(k, np.float32) / float(k)
    seg = np.convolve(seg, ker, mode='same')
    idx = int(np.argmin(seg))
    med = float(np.median(seg))
    if med <= 0 or float(seg[idx]) > 0.85 * med:
        return x0 + (w // 2)
    return x0 + lo + idx


def refine_columns(store, pid, page: dict) -> dict:
    """Split over-wide column bands (two columns merged into one) at ink valleys.

    Only touches page.json['columns'] -- boxes and glyphs are untouched, so all
    manual box edits survive.  Idempotent (columns_refined flag).
    """
    cols = []
    for i, c in enumerate(page.get("columns") or []):
        try:
            x0, x1 = int(c["x0"]), int(c["x1"])
        except (KeyError, TypeError, ValueError):
            continue
        if x1 - x0 >= 20:
            cols.append((x0, x1))
    if len(cols) < 2:
        return page
    cols.sort(key=lambda t: t[0])
    widths = sorted(b - a for a, b in cols)
    med = widths[len(widths) // 2]
    if med <= 0:
        return page
    gray = None
    changed = False
    for _ in range(10):
        wide = [i for i, (a, b) in enumerate(cols) if (b - a) > 1.55 * med and (b - a) > 120]
        if not wide:
            break
        i = max(wide, key=lambda k: cols[k][1] - cols[k][0])
        a, b = cols[i]
        if gray is None:
            gray = page_gray(store, pid)
        prof = _ink_profile(gray, a, b)
        cut = _band_valley(prof, a)
        min_part = max(140, int(0.45 * med))
        if cut is None or not (a + min_part <= cut <= b - min_part):
            cut = (a + b) // 2
        if (cut - a) < min_part or (b - cut) < min_part:
            break          # 再切就会切出细条，放弃
        cols[i:i + 1] = [(a, cut), (cut, b)]
        changed = True
    if not changed:
        return page
    page = dict(page)
    page["columns"] = [{"idx": i, "x0": a, "x1": b, "w": b - a}
                       for i, (a, b) in enumerate(sorted(cols, key=lambda t: t[0]))]
    page["columns_refined"] = True
    store.save_page_json(pid, page)
    return page


def ensure_columns(store, pid, page: dict = None) -> list:
    """Page columns, auto-splitting merged bands once (unless the user edited them)."""
    if page is None:
        page = store.load_page_json(pid)
    cols = page.get("columns") or []
    if not cols:
        return cols
    if page.get("columns_manual") or page.get("columns_refined"):
        return _merge_slivers(_sorted_columns(cols))
    page = refine_columns(store, pid, page)
    return _merge_slivers(_sorted_columns(page.get("columns") or []))


def _merge_slivers(cols, min_w: int = 90) -> list:
    """Merge columns narrower than min_w into their wider neighbour (segmenter noise bands)."""
    cols = [dict(c) for c in cols]
    merged = False
    while len(cols) > 1:
        widths = [c["x1"] - c["x0"] for c in cols]
        k = min(range(len(cols)), key=lambda i: widths[i])
        if widths[k] >= min_w:
            break
        if k == 0:
            nb = 1
        elif k == len(cols) - 1:
            nb = k - 1
        else:
            nb = k - 1 if widths[k - 1] >= widths[k + 1] else k + 1
        cols[nb]["x0"] = min(cols[nb]["x0"], cols[k]["x0"])
        cols[nb]["x1"] = max(cols[nb]["x1"], cols[k]["x1"])
        del cols[k]
        merged = True
    out = []
    for c in cols:
        out.append({"idx": 0, "x0": int(c["x0"]), "x1": int(c["x1"])})
    out.sort(key=lambda c: c["x0"])
    for i, c in enumerate(out):
        c["idx"] = i
    return out if merged else cols


def _sorted_columns(cols) -> list:
    """Canonical left->right order (segment_v1 emits right->left; the UI assumes left->right)."""
    out = []
    for c in cols or []:
        try:
            out.append({"idx": 0, "x0": int(c["x0"]), "x1": int(c["x1"])})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda c: c["x0"])
    for i, c in enumerate(out):
        c["idx"] = i
    return out


def page_dict_for_export(store, pid, boxes) -> dict:
    """Assemble the minimal page dict export_tiles() needs, from current boxes."""
    page = store.load_page_json(pid)
    W, H = store.dims(pid)
    if W <= 0 or H <= 0:
        src = store.src(pid)
        W, H = _image_size(src)
    ensure_columns(store, pid, page)
    cols = _page_columns(page, boxes)
    centers = [(c["x0"] + c["x1"]) / 2.0 for c in cols]

    def col_of(box):
        if not cols:
            return 0
        cx = (box["x0"] + box["x1"]) / 2.0
        best, bestd = 0, None
        for i, c in enumerate(cols):
            d = abs(centers[i] - cx)
            if bestd is None or d < bestd:
                best, bestd = i, d
        return best

    ref_low = _ref_low_boxes(store, pid)
    glyphs = []
    for b in boxes:            # 每一框都导出，可疑框也只是标记，不做过滤
        flags = list(b.get("flags") or [])
        review = store_mod._int(b.get("review"), 0)
        if review == 1:
            flags.append("confirmed")
        elif review == 2:
            flags.append("suspect")
        if ref_low and _is_low_box(b, ref_low):
            flags.append("lowref")
        g = {"id": store_mod._int(b.get("id"), 1), "x0": b["x0"], "y0": b["y0"],
             "x1": b["x1"], "y1": b["y1"], "flags": flags,
             "col": col_of(b)}
        glyphs.append(g)
    return {"width": W, "height": H, "columns": cols, "glyphs": glyphs}


def export_page(store, pid, out_dir=None, *, pad_ratio: float = 0.10, min_pad: int = 6,
                order: str = "visual", fmt: str = "png", prefix: str = "",
                start: int = 1, digits: int = 0, sheet: bool = True, quality: int = 95,
                progress_cb=None,
                cancel_cb=None) -> dict:
    """Write <out>/<page name>/{0001.png..., index.csv, sheet.png}."""
    entry = _require_page(store, pid)
    src = entry["src"]
    if not os.path.isfile(src):
        raise store_mod.StoreError("源文件不存在：%s" % src)
    boxes = store.boxes(pid)
    if not boxes:
        raise store_mod.StoreError("这一页还没有任何框，无法导出（请先处理或手动加框）")

    order = str(order or "visual").lower()
    if order not in ORDER_VALUES:
        order = "visual"
    fmt = str(fmt or "png").lower().lstrip(".")
    if fmt not in FMT_VALUES:
        fmt = "png"
    try:
        pad_ratio = max(0.0, min(0.5, float(pad_ratio)))
    except (TypeError, ValueError):
        pad_ratio = 0.10
    min_pad = _clamp_int(min_pad, 0, 200, 6)

    root = Path(out_dir) if out_dir else default_export_dir(store)
    root = Path(os.path.abspath(os.path.expanduser(str(root))))
    if root.exists() and not root.is_dir():
        raise store_mod.StoreError("导出路径不是文件夹：%s" % root)
    root.mkdir(parents=True, exist_ok=True)

    stem = store_mod.safe_name(os.path.splitext(entry.get("name") or pid)[0] or pid, "page")
    page_out = root / stem
    marker = page_out / PAGE_MARKER
    prev = store_mod.read_json(marker, None)
    if isinstance(prev, dict) and prev.get("pid") not in (None, pid) and page_out.is_dir():
        page_out = root / (stem + "_" + str(pid)[-4:])
        marker = page_out / PAGE_MARKER
    page_out.mkdir(parents=True, exist_ok=True)

    key, lock = _lock_for(page_out)
    if not lock.acquire(blocking=False):
        raise store_mod.StoreError("输出目录正在被另一个导出任务使用：%s" % page_out)
    try:
        _clean_tiles(page_out)
        store_mod.atomic_write_json(marker, {"pid": pid, "name": entry.get("name") or "",
                                             "updated": _now()})
        _cb(progress_cb, 0.05, "准备导出")
        check_cancel(cancel_cb)
        page = page_dict_for_export(store, pid, boxes)
        _cb(progress_cb, 0.15, "读取原图")
        t = time.time()
        rgb = C.load_rgb(src)
        _cb(progress_cb, 0.35, "裁切 %d 个框" % len(boxes))
        t2 = time.time()
        records = E.export_tiles(rgb, page, str(page_out), pad_ratio=pad_ratio,
                                 min_pad=min_pad, order=order, fmt=fmt,
                                 prefix=prefix, start=start, digits=digits,
                                 sheet=sheet, quality=quality)
        export_seconds = time.time() - t2
        check_cancel(cancel_cb)
        _cb(progress_cb, 0.9, "校验导出结果")
        # 文件名前缀/起始编号可自定义，所以按「记录里的文件名」校验，不用固定正则
        wanted = {str(r.get("filename")) for r in records if r.get("filename")}
        tile_files = [p.name for p in page_out.iterdir()
                      if p.is_file() and (p.name in wanted if wanted else TILE_RE.match(p.name))]
        index_csv = page_out / "index.csv"
        csv_rows = 0
        if index_csv.is_file():
            with open(index_csv, "r", encoding="utf-8-sig", newline="") as f:
                csv_rows = max(0, sum(1 for _ in csv.DictReader(f)))
        n = len(records)
        if not (n == len(tile_files) == csv_rows):
            raise store_mod.StoreError(
                "导出校验失败：tiles=%d, index.csv 行=%d, 记录=%d" % (len(tile_files), csv_rows, n))
        entry2 = store.update_page(pid, last_export={"out": str(page_out), "n": n,
                                                     "order": order, "fmt": fmt,
                                                     "at": _now(),
                                                     "pad_ratio": pad_ratio,
                                                     "min_pad": min_pad})
        _cb(progress_cb, 1.0, "完成")
        return {
            "pid": pid,
            "name": entry.get("name") or "",
            "out": str(page_out),
            "n_tiles": n,
            "index_csv": str(index_csv),
            "sheet_png": str(page_out / "sheet.png"),
            "order": order,
            "fmt": fmt,
            "pad_ratio": pad_ratio,
            "min_pad": min_pad,
            "seconds": round(time.time() - t, 2),
            "export_seconds": round(export_seconds, 2),
            "status": (entry2 or {}).get("status"),
        }
    finally:
        lock.release()


# ------------------------------------------------------------- suspect heuristics
SUSPECT_FLAGS = ("tiny", "oversize", "clipped", "stain", "broken", "merge", "split", "merged")


def suspect_detail(boxes) -> dict:
    """Explainable heuristics for AI review targets: {box_id: [reasons]}."""
    boxes = [b for b in (boxes or []) if isinstance(b, dict)]
    if not boxes:
        return {}
    ws = [max(1, b["x1"] - b["x0"]) for b in boxes]
    hs = [max(1, b["y1"] - b["y0"]) for b in boxes]
    med_w = statistics.median(ws)
    med_h = statistics.median(hs)
    med_area = med_w * med_h
    out = {}
    for b in boxes:
        w = max(1, b["x1"] - b["x0"])
        h = max(1, b["y1"] - b["y0"])
        reasons = []
        flags = [str(f).lower() for f in (b.get("flags") or [])]
        for f in flags:
            if f in SUSPECT_FLAGS:
                reasons.append("标记：" + f)
        if h < 0.5 * med_h:
            reasons.append("高度偏小（%.0f%% 中位高）" % (100.0 * h / med_h))
        elif h > 1.8 * med_h:
            reasons.append("高度偏大（%.0f%% 中位高）" % (100.0 * h / med_h))
        if w < 0.5 * med_w:
            reasons.append("宽度偏小（%.0f%% 中位宽）" % (100.0 * w / med_w))
        elif w > 1.9 * med_w:
            reasons.append("宽度偏大（%.0f%% 中位宽）" % (100.0 * w / med_w))
        area = w * h
        if med_area and area > 2.6 * med_area:
            reasons.append("面积偏大")
        elif med_area and area < 0.35 * med_area:
            reasons.append("面积偏小")
        if w / float(h) > 2.2:
            reasons.append("极扁（宽高比 %.1f）" % (w / float(h)))
        if h / float(w) > 4.0:
            reasons.append("极窄（高宽比 %.1f）" % (h / float(w)))
        if reasons:
            out[int(b.get("id") or 0)] = reasons
    return out


def suspect_ids(boxes) -> list:
    return sorted(suspect_detail(boxes).keys())


# ---------------------------------------------------------------- self test
def _selftest() -> int:
    import tempfile
    import shutil
    from PIL import Image, ImageDraw

    ok = fail = 0
    def check(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("PASS", label, flush=True)
        else:
            fail += 1
            print("FAIL", label, flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="yi_pipeline_"))
    try:
        # Prefer a real page: quarter-scale gray cache left over from the CLI work
        # exercises the genuine segmenter fast.  Fall back to a synthetic page so
        # the selftest still works on a machine without that cache.
        src = tmp / "page_small.png"
        real = False
        cache = Path(paths.app_root()) / "scratch" / "gray_1.npy"
        if cache.is_file():
            try:
                g = np.load(str(cache), mmap_mode="r")
                small = cv2.resize(np.asarray(g), (g.shape[1] // 4, g.shape[0] // 4),
                                   interpolation=cv2.INTER_AREA)
                Image.fromarray(small).save(src)
                real = True
                check(True, "quarter-scale page built from gray cache")
            except Exception as exc:
                print("WARN gray cache unusable: %s" % exc, flush=True)
        if not real:
            img = Image.new("RGB", (600, 900), (205, 196, 186))
            d = ImageDraw.Draw(img)
            for col in range(2):
                for row in range(3):
                    x0, y0 = 80 + col * 240, 100 + row * 240
                    d.ellipse([x0, y0, x0 + 120, y0 + 150], outline=(25, 20, 18), width=7)
            img.save(src)
            check(True, "synthetic fallback page built")

        st = store_mod.ProjectStore(tmp / "proj")
        added, _ = st.add_paths([str(src)])
        check(len(added) == 1, "add page")
        pid = added[0]

        t0 = time.time()
        res = process_page(st, pid)
        check(st.has_preview(pid), "preview.png written")
        check(res["width"] > 0 and res["height"] > 0, "page size recorded")
        check(st.get_page(pid)["status"] in ("processed", "reviewing", "done"),
              "status after process: " + str(st.get_page(pid)["status"]))
        if real:
            check(res["n_boxes"] > 50,
                  "real page segmented: %d boxes in %.1fs" % (res["n_boxes"], time.time() - t0))

        png = render_crop(st, pid, 50, 70, 250, 300, 2.0)
        check(png[:8] == b"\x89PNG\r\n\x1a\n", "crop is PNG")
        with Image.open(io.BytesIO(png)) as im:
            check(abs(im.size[0] - 400) <= 2 and abs(im.size[1] - 460) <= 2,
                  "crop scaled 2x (%s)" % (im.size,))

        boxes = st.boxes(pid)
        if not boxes:
            w, h = res["width"], res["height"]
            manual = [{"x0": 40 + i * 30, "y0": 60 + j * 40, "x1": 60 + i * 30, "y1": 90 + j * 40}
                      for i in range(4) for j in range(3)]
            boxes, _ = st.save_boxes(pid, manual)
            check(len(boxes) == 12, "manual boxes for export test")
        det = suspect_detail(boxes)
        check(isinstance(det, dict), "suspect_detail returns dict")
        weird = [{"id": 1, "x0": 0, "y0": 0, "x1": 600, "y1": 20, "flags": []}]
        check(1 in suspect_detail(weird), "flat box flagged suspect")
        check(suspect_ids(weird) == [1], "suspect_ids list")
        check(sorted(suspect_ids(boxes)) == sorted(suspect_detail(boxes)),
              "suspect_ids consistent with detail")

        n = len(boxes)
        out = tmp / "out"
        exp = export_page(st, pid, out, pad_ratio=0.1, min_pad=6, order="visual", fmt="png")
        check(exp["n_tiles"] == n, "tile count == box count (%d)" % n)
        check(Path(exp["index_csv"]).is_file() and Path(exp["sheet_png"]).is_file(),
              "index.csv + sheet.png")
        tile_pngs = [p for p in Path(exp["out"]).iterdir() if TILE_RE.match(p.name)]
        check(len(tile_pngs) == n, "tiles on disk")
        exp2 = export_page(st, pid, out, order="reading", fmt="jpg")
        jpgs = [p for p in Path(exp2["out"]).iterdir() if p.suffix == ".jpg"]
        old_pngs = [p for p in Path(exp2["out"]).iterdir()
                    if p.suffix == ".png" and p.name != "sheet.png"]
        check(len(jpgs) == exp2["n_tiles"], "jpg export")
        check(len(old_pngs) == 0, "stale png tiles cleaned")
        exp3 = export_page(st, pid, out, order="id", fmt="png")
        check(exp3["out"] == exp["out"], "same page reuses export dir")
        cols = page_dict_for_export(st, pid, st.boxes(pid))["columns"]
        check(len(cols) >= 1, "export page dict has columns (%d)" % len(cols))

        # empty box list must refuse to export
        st.save_boxes(pid, [])
        try:
            export_page(st, pid, out)
            check(False, "empty boxes export refused")
        except store_mod.StoreError:
            check(True, "empty boxes export refused")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("pipeline selftest: %d passed, %d failed" % (ok, fail), flush=True)
    return 0 if fail == 0 else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="app.pipeline", description="分割/预览/导出业务层")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
