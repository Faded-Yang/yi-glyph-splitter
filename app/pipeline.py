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
import os
import re
import statistics
import sys
import threading
import time
from pathlib import Path

from . import paths, store as store_mod

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
        page = S1.segment_page(gray)
        timings["segment"] = time.time() - t
        check_cancel(cancel_cb)
        page = dict(page or {})
        page["source"] = src
        _cb(progress_cb, 0.72, "保存分割结果")
        store.save_page_json(pid, page)

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


def page_dict_for_export(store, pid, boxes) -> dict:
    """Assemble the minimal page dict export_tiles() needs, from current boxes."""
    page = store.load_page_json(pid)
    W, H = store.dims(pid)
    if W <= 0 or H <= 0:
        src = store.src(pid)
        W, H = _image_size(src)
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

    glyphs = []
    for b in boxes:
        g = {"id": store_mod._int(b.get("id"), 1), "x0": b["x0"], "y0": b["y0"],
             "x1": b["x1"], "y1": b["y1"], "flags": list(b.get("flags") or []),
             "col": col_of(b)}
        glyphs.append(g)
    return {"width": W, "height": H, "columns": cols, "glyphs": glyphs}


def export_page(store, pid, out_dir=None, *, pad_ratio: float = 0.10, min_pad: int = 6,
                order: str = "visual", fmt: str = "png", progress_cb=None,
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
                                 min_pad=min_pad, order=order, fmt=fmt)
        export_seconds = time.time() - t2
        check_cancel(cancel_cb)
        _cb(progress_cb, 0.9, "校验导出结果")
        tile_files = [p.name for p in page_out.iterdir()
                      if p.is_file() and TILE_RE.match(p.name)]
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
