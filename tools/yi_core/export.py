"""Tile exporter for Yi-manuscript page segmentations.

Contract (see docs/SPEC.md):

    export_tiles(rgb, page, out_dir, pad_ratio=0.10, min_pad=6,
                 prefix="", order="reading", fmt="png") -> list[dict]

For every glyph of a *page dict* (produced by segment_v1 / segment_alt) this
crops a padded window out of the *original* RGB page, writes it as
0001.png, 0002.png ... and drops two companions next to the tiles:

* index.csv -- one row per tile, in file-name (= output) order
* sheet.png -- a 13-column contact sheet: light background, ~150 px cells,
  the glyph id printed small underneath each cell (style of the user's
  reference sheet)

The paper colour is preserved verbatim: tiles are plain crops, never binarised.

Coordinates
-----------
All glyph coordinates in the page dict are *original* page pixels with
x1/y1 half-open (Python slice semantics), so the crop is
rgb[y0:y1, x0:x1].  Padding is added around the glyph bbox:

    pad = max(min_pad, round(pad_ratio * max(glyph_w, glyph_h)))

and the padded window is clipped to the page.  index.csv records the
*actual saved window* in x0/y0/x1/y1/w/h (so w/h always match the tile file),
and keeps the raw glyph bbox in the trailing gx0/gy0/gx1/gy1 columns
(plus pad and flags) so nothing is lost -- trailing columns are safe for
DictReader-style consumers and keep the documented 11-column prefix intact.
"""
from __future__ import annotations

import csv
import math
import os
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- sheet style
# Matched against docs/style/style_ref_tiles.jpg (65 tiles, 13 cols x 5 rows):
# plain paper-coloured crops pasted straight on a light background (no white
# cell boxes, no borders), tile top-aligned in a ~165 px slot, thumbnail at
# ~0.70 of the slot, tiny grey file name right under each tile.
SHEET_COLS = 13          # fixed by the user's reference sheet
SHEET_CELL = 150         # slot box that holds one thumbnail (px)
SHEET_THUMB = 0.70       # thumbnail max side = SHEET_THUMB * SHEET_CELL
SHEET_LABEL_H = 20       # strip under the thumbnail holding the small file name
SHEET_GAP_X = 15
SHEET_GAP_Y = 15
SHEET_MARGIN = 20
SHEET_HEADER_H = 30
SHEET_BG = (255, 255, 255)        # white background, same as the reference sheet
SHEET_TEXT = (52, 50, 46)         # small dark file name under each tile
SHEET_HEAD = (52, 50, 46)

# a CJK-capable font first: sheet headers carry the (Chinese) page stem
_FONT_CANDIDATES = ("msyh.ttc", "simhei.ttf", "Deng.ttf", "arial.ttf",
                    "segoeui.ttf", "DejaVuSans.ttf")

CSV_FIELDS = [
    "filename", "id", "col", "row",
    "x0", "y0", "x1", "y1", "w", "h", "ink",
    # extras (trailing, so the documented header prefix stays intact)
    "pad", "gx0", "gy0", "gx1", "gy1", "flags",
    # optional quality fields from the page dict (segment_v1 >= page1 v2)
    "flat", "ink_frac", "core_frac",
]


def _num(v) -> str:
    """Render an optional numeric quality field for CSV ("" when absent)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return "%.6g" % v
    return str(v)


# ------------------------------------------------------------------ helpers
def _font(size: int):
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


def _as_image(arr: np.ndarray) -> Image.Image:
    if arr.ndim == 2:
        return Image.fromarray(arr).convert("RGB")
    if arr.shape[2] == 4:
        return Image.fromarray(arr).convert("RGB")
    return Image.fromarray(arr)


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _glyph_box(g: dict):
    """Raw glyph bbox (x0, y0, x1, y1); accepts w/h-only or x1/y1-only dicts."""
    x0 = _int(g.get("x0"))
    y0 = _int(g.get("y0"))
    if g.get("x1") is not None:
        x1 = _int(g.get("x1"))
    elif g.get("w") is not None:
        x1 = x0 + _int(g.get("w"))
    else:
        return None
    if g.get("y1") is not None:
        y1 = _int(g.get("y1"))
    elif g.get("h") is not None:
        y1 = y0 + _int(g.get("h"))
    else:
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def column_centers(page: dict, glyphs: list) -> dict:
    """Map column index -> x centre, from page["columns"] (+ glyph fallback).

    Needed for output ordering: "reading" wants columns right-to-left, so the
    sorter has to know where each *column* is, not only where a glyph is.
    """
    centers = {}
    for i, c in enumerate(page.get("columns") or []):
        try:
            idx = _int(c.get("idx"), i)
            centers[idx] = 0.5 * (float(c["x0"]) + float(c["x1"]))
        except Exception:
            continue
    # Derive centres for columns that only appear as glyph["col"].
    acc = {}
    for g in glyphs:
        col = g.get("col")
        box = _glyph_box(g)
        if col is None or box is None:
            continue
        acc.setdefault(_int(col), []).append(0.5 * (box[0] + box[2]))
    for col, xs in acc.items():
        if col not in centers:
            centers[col] = float(sum(xs) / len(xs))
    return centers


def _sort_glyphs(glyphs: list, col_cx: dict, order: str) -> list:
    """Order glyphs for output.

    "id"      tile N is glyph id N (the segmenter's own reading order; safest,
              the page dict is the single source of truth for reading order)
    "reading" columns right-to-left, top-to-bottom (SPEC's geometric reading)
    "visual"  columns left-to-right, top-to-bottom
    """
    if order not in ("reading", "visual", "id"):
        raise ValueError(f"order must be 'reading', 'visual' or 'id', got {order!r}")
    if order == "id":
        return sorted((g for g in glyphs if _glyph_box(g) is not None),
                      key=lambda g: _int(g.get("id"), 0))

    keyed = []
    for g in glyphs:
        box = _glyph_box(g)
        if box is None:
            continue
        col = g.get("col")
        cx = col_cx.get(_int(col)) if col is not None else None
        if cx is None:
            cx = 0.5 * (box[0] + box[2])
        y0 = box[1]
        sign = -1.0 if order == "reading" else 1.0
        # cx is identical for all glyphs of one column -> second key splits rows
        keyed.append(((sign * cx, y0, box[0]), g))
    keyed.sort(key=lambda kv: kv[0])
    return [g for _, g in keyed]


def _fit(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    s = min(box_w / float(w), box_h / float(h))
    nw = max(1, int(round(w * s)))
    nh = max(1, int(round(h * s)))
    return img.resize((nw, nh), Image.LANCZOS)


def write_sheet(thumbs: list, out_path: str,
                cols: int = SHEET_COLS, cell: int = SHEET_CELL,
                header: str | None = None) -> str:
    """13-column contact sheet, small file name printed under each tile.

    Tiles are pasted as-is (paper colour kept) on a near-white background; no
    white cell boxes / borders, top-aligned per row, like the reference sheet.
    """
    n = len(thumbs)
    cols = max(1, int(cols))
    ncols = min(cols, n) if n else cols
    rows = max(1, int(math.ceil(n / float(cols))))
    thumb_max = max(1, int(round(cell * SHEET_THUMB)))
    slot_w = cell + SHEET_GAP_X
    slot_h = cell + SHEET_LABEL_H + SHEET_GAP_Y
    head_h = SHEET_HEADER_H if header else 0
    W = SHEET_MARGIN * 2 + ncols * slot_w - SHEET_GAP_X
    H = SHEET_MARGIN * 2 + head_h + rows * slot_h - SHEET_GAP_Y

    sheet = Image.new("RGB", (max(1, W), max(1, H)), SHEET_BG)
    d = ImageDraw.Draw(sheet)
    f_head = _font(15)
    f_label = _font(13)

    if header:
        d.text((SHEET_MARGIN, SHEET_MARGIN - 2), header, font=f_head, fill=SHEET_HEAD)

    if not n:
        d.text((SHEET_MARGIN, SHEET_MARGIN + head_h + 8), "(no glyphs)",
               font=f_head, fill=SHEET_TEXT)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        sheet.save(out_path)
        return out_path

    for i, item in enumerate(thumbs):
        thumb, label = item
        r, c = divmod(i, cols)
        x = SHEET_MARGIN + c * slot_w
        y = SHEET_MARGIN + head_h + r * slot_h
        t = _fit(thumb, thumb_max, thumb_max)
        px = x + (slot_w - SHEET_GAP_X - t.size[0]) // 2
        sheet.paste(t, (px, y))
        # small file name right under the thumbnail, same y for the whole row
        try:
            tw = d.textlength(label, font=f_label)
        except Exception:
            tw = 6 * len(label)
        d.text((x + (slot_w - SHEET_GAP_X - tw) / 2.0, y + thumb_max + 4),
               label, font=f_label, fill=SHEET_TEXT)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    sheet.save(out_path)
    return out_path


# --------------------------------------------------------------- main export
def export_tiles(rgb, page: dict, out_dir: str,
                 pad_ratio: float = 0.10, min_pad: int = 6,
                 prefix: str = "", order: str = "reading", fmt: str = "png",
                 *, sheet: bool = True, sheet_cols: int = SHEET_COLS,
                 cell: int = SHEET_CELL, sheet_name: str = "sheet.png",
                 sheet_header: str | None = None) -> list:
    """Crop every glyph of page out of rgb and write tiles + index + sheet.

    Parameters
    ----------
    rgb : np.ndarray
        Original page pixels (HxWx3 uint8; HxW grayscale is also accepted).
    page : dict
        Page dict per docs/SPEC.md (glyphs carry original-pixel bboxes).
    out_dir : str
        Destination directory for tiles / index.csv / sheet.png.
    pad_ratio, min_pad
        Padding around each glyph bbox (see module docstring).
    prefix, fmt
        File-name prefix and image format/extension ("png", "jpg", ...).
    order
        "reading" = columns right-to-left, top-to-bottom inside a column
        (default); "visual" = columns left-to-right; "id" = follow the page
        dict ids (the segmenter's reading order).

    Returns
    -------
    list[dict]
        One record per written tile (same fields as index.csv), in file order.
    """
    # prefix is sometimes mistaken for order by positional callers;
    # accept that quietly instead of producing names like "reading_0001.png".
    if prefix in ("reading", "visual") and order == "reading":
        prefix, order = "", prefix

    order = (order or "reading").lower()
    fmt = (fmt or "png").lower().lstrip(".")
    if fmt not in ("png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"):
        raise ValueError(f"unsupported fmt {fmt!r}")

    if rgb is None or getattr(rgb, "size", 0) == 0:
        raise ValueError("rgb is empty")
    h_img, w_img = rgb.shape[:2]

    glyphs = list(page.get("glyphs") or [])
    col_cx = column_centers(page, glyphs)
    ordered = _sort_glyphs(glyphs, col_cx, order)

    os.makedirs(out_dir, exist_ok=True)
    n = len(ordered)
    ndigits = max(3, len(str(max(1, n))))

    records = []
    thumbs = []
    for i, g in enumerate(ordered, start=1):
        box = _glyph_box(g)
        if box is None:
            continue
        gx0, gy0, gx1, gy1 = box
        gw, gh = gx1 - gx0, gy1 - gy0
        pad = max(int(min_pad), int(round(pad_ratio * max(gw, gh))))
        x0 = max(0, gx0 - pad)
        y0 = max(0, gy0 - pad)
        x1 = min(w_img, gx1 + pad)
        y1 = min(h_img, gy1 + pad)
        if x1 <= x0 or y1 <= y0:
            continue
        arr = rgb[y0:y1, x0:x1]
        if arr.size == 0:
            continue

        filename = f"{prefix}{i:0{ndigits}d}.{fmt}"
        path = os.path.join(out_dir, filename)
        img = _as_image(arr) if arr.ndim == 3 else Image.fromarray(arr)
        if img.size != (x1 - x0, y1 - y0):  # never write a mis-shaped tile
            raise RuntimeError("tile %s: PIL size %s != box %s"
                               % (filename, img.size, (x1 - x0, y1 - y0)))
        if fmt in ("jpg", "jpeg"):
            img.convert("RGB").save(path, quality=95, subsampling=0)
        else:
            img.save(path)

        rec = {
            "filename": filename,
            "id": _int(g.get("id"), i),
            "col": _int(g.get("col"), -1),
            "row": _int(g.get("row"), -1),
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "w": x1 - x0, "h": y1 - y0,
            "ink": _int(g.get("ink"), 0),
            "pad": pad,
            "gx0": gx0, "gy0": gy0, "gx1": gx1, "gy1": gy1,
            "flags": ";".join(str(f) for f in (g.get("flags") or [])),
            "flat": _num(g.get("flat")),
            "ink_frac": _num(g.get("ink_frac")),
            "core_frac": _num(g.get("core_frac")),
        }
        records.append(rec)
        # reference sheet prints the tile file name under each tile, not just the id
        thumbs.append((img if img.mode == "RGB" else img.convert("RGB"),
                       filename))

    _write_index_csv(records, os.path.join(out_dir, "index.csv"))
    if sheet:
        if sheet_header is None:
            pass  # reference style: tiles + file names only, no header line
        write_sheet(thumbs, os.path.join(out_dir, sheet_name),
                    cols=sheet_cols, cell=cell, header=sheet_header)
    return records


def _write_index_csv(records: list, path: str) -> str:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(r)
    return path
