"""Shared utilities for the Yi-script glyph segmentation toolkit.

Everything here is page-size agnostic: the target scans are ~6354x11042 8-bit
RGB TIFs, so all operations must stream / use integral-image filters rather
than huge temporary float stacks where possible.
"""
from __future__ import annotations

import json
import os
import hashlib
from dataclasses import dataclass, asdict, field
from typing import Iterable, Sequence

import numpy as np
import cv2

cv2.setNumThreads(max(1, (os.cpu_count() or 4) - 1))

Image_MAX = None


# ---------------------------------------------------------------- IO
def load_gray(path: str) -> np.ndarray:
    """Load a scan as 8-bit grayscale (uint8, HxW).

    Handles RGB / RGBA / 16-bit / bilevel TIF via PIL, then converts.
    """
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        if im.mode in ("I;16", "I;16B", "I;16L", "I"):
            a = np.asarray(im, dtype=np.uint16)
            if a.max() > 255:
                a = (a.astype(np.float32) / (a.max() / 255.0)).clip(0, 255)
            g = a.astype(np.uint8)
        elif im.mode == "L":
            g = np.asarray(im, dtype=np.uint8)
        elif im.mode in ("1",):
            g = np.asarray(im.convert("L"), dtype=np.uint8)
        else:
            g = np.asarray(im.convert("L"), dtype=np.uint8)
    return np.ascontiguousarray(g)


def load_rgb(path: str) -> np.ndarray:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        return np.ascontiguousarray(np.asarray(im.convert("RGB"), dtype=np.uint8))


def file_sig(path: str) -> str:
    st = os.stat(path)
    h = hashlib.sha1()
    h.update(os.path.basename(path).encode("utf-8"))
    h.update(str(st.st_size).encode())
    return h.hexdigest()[:16]


# ------------------------------------------------- illumination / binarize
def flatten_illumination(gray: np.ndarray, ksize: int = 129) -> np.ndarray:
    """Divide out the slowly varying paper brightness.

    Returns uint8 where ~128 is paper and darker is ink; contrast stretched by
    a robust percentile window.  ksize must exceed glyph stroke but be far
    smaller than the page so it tracks the stain/shadow gradient.
    """
    g = gray.astype(np.float32)
    k = ksize if ksize % 2 else ksize + 1
    # large-kernel median is too slow on 70 MP images: use repeated box blur
    bg = cv2.blur(g, (k, k))
    bg = cv2.blur(bg, (k, k))
    bg = np.maximum(bg, 1.0)
    r = g / bg
    lo = float(np.percentile(r, 2.0))
    hi = float(np.percentile(r, 60.0))
    if hi <= lo:
        hi = lo + 1e-3
    out = (r - lo) / (hi - lo)
    out = (out * 255.0).clip(0, 255)
    return out.astype(np.uint8)


def _mean_std(img_u8: np.ndarray, win: int):
    f = img_u8.astype(np.float32)
    m = cv2.boxFilter(f, ddepth=-1, ksize=(win, win), normalize=True,
                      borderType=cv2.BORDER_REFLECT)
    m2 = cv2.boxFilter(f * f, ddepth=-1, ksize=(win, win), normalize=True,
                       borderType=cv2.BORDER_REFLECT)
    return m, np.sqrt(np.maximum(m2 - m * m, 0.0))


def binarize(gray: np.ndarray, method: str = "sauvola", win: int = 61,
             k: float = 0.34, offset: float = 0.0) -> np.ndarray:
    """Ink mask (True == ink) for 8-bit grayscale.

    method: 'sauvola' | 'niblack' | 'otsu' | 'adaptive'
    """
    if method == "otsu":
        _, m = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        return m > 0
    if method == "adaptive":
        m = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                  cv2.THRESH_BINARY_INV, max(3, win | 1), 12)
        return m > 0
    g = gray.astype(np.float32)
    mean, std = _mean_std(gray, max(3, win | 1))
    if method == "sauvola":
        R = 128.0
        thr = mean * (1.0 + k * (std / R - 1.0))
    elif method == "niblack":
        thr = mean + k * std
    else:
        raise ValueError(method)
    thr = thr + offset
    return g < thr


def clean_mask(mask: np.ndarray, min_area: int = 12, open_every: int = 0) -> np.ndarray:
    """Drop specks (dust) from a boolean ink mask."""
    m = mask.astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    keep = np.zeros(n, dtype=bool)
    for i in range(1, n):
        keep[i] = stats[i, cv2.CC_STAT_AREA] >= min_area
    keep[0] = False
    return keep[lab]


# ------------------------------------------------------- structure helpers
def runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive [start, end] index runs where flags is True."""
    f = np.asarray(flags, dtype=np.int8)
    d = np.diff(np.concatenate(([0], f, [0])))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1) - 1
    return [(int(a), int(b)) for a, b in zip(starts, ends)]


def col_profile(mask: np.ndarray) -> np.ndarray:
    return mask.sum(axis=0).astype(np.int32)


def row_profile(mask: np.ndarray) -> np.ndarray:
    return mask.sum(axis=1).astype(np.int32)


def smooth1d(v: np.ndarray, win: int) -> np.ndarray:
    if win <= 1:
        return v.astype(np.float32)
    k = np.ones(win, dtype=np.float32) / win
    return np.convolve(v.astype(np.float32), k, mode="same")


def cc_filter(mask: np.ndarray, min_area: int, max_area: int | None = None):
    """Connected components of a mask, returning stats/labels."""
    m = mask.astype(np.uint8)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, connectivity=8)
    keep = np.zeros(n, dtype=bool)
    for i in range(1, n):
        a = stats[i, cv2.CC_STAT_AREA]
        if a >= min_area and (max_area is None or a <= max_area):
            keep[i] = True
    keep[0] = False
    return lab, stats, cent, keep


# ------------------------------------------------------------- datatypes
@dataclass
class Glyph:
    id: int
    x0: int
    y0: int
    x1: int
    y1: int
    col: int = -1
    row: int = -1
    ink: int = 0
    area: int = 0
    flat: float = 0.0      # mean flattened-intensity of the ink (low = really dark)
    ink_frac: float = 0.0  # ink / bbox area
    core_frac: float = 1.0 # fraction of ink pixels that are really dark
    flags: list = field(default_factory=list)

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["w"] = self.w
        d["h"] = self.h
        return d


@dataclass
class Column:
    idx: int
    x0: int
    x1: int
    ink: int = 0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["w"] = self.x1 - self.x0
        return d


def save_page_json(path: str, *, source: str, size, columns, glyphs, params=None, extra=None):
    page = {
        "source": source,
        "width": int(size[0]),
        "height": int(size[1]),
        "columns": [c.as_dict() for c in columns],
        "glyphs": [g.as_dict() for g in glyphs],
        "params": params or {},
    }
    if extra:
        page.update(extra)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(page, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return page


def load_page_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------ rendering
BOX_COLORS = [(0, 220, 255), (80, 220, 120), (255, 190, 70)]


def draw_overlay(rgb: np.ndarray, page: dict, out_path: str, target_w: int = 2400,
                 draw_ids: bool = True, pad: int = 0) -> None:
    """Render boxes over a downscaled copy of the page (for visual QC)."""
    h, w = rgb.shape[:2]
    s = target_w / float(w)
    small = cv2.resize(rgb, (target_w, max(1, int(round(h * s)))), interpolation=cv2.INTER_AREA)
    small = cv2.cvtColor(small, cv2.COLOR_RGB2BGR)
    for c in page.get("columns", []):
        x0 = int(c["x0"] * s)
        x1 = int(c["x1"] * s)
        cv2.line(small, (x0, 0), (x0, small.shape[0] - 1), (60, 60, 60), 1)
        cv2.line(small, (x1, 0), (x1, small.shape[0] - 1), (60, 60, 60), 1)
    for g in page.get("glyphs", []):
        col = BOX_COLORS[g.get("col", 0) % len(BOX_COLORS)]
        x0 = int(g["x0"] * s); y0 = int(g["y0"] * s)
        x1 = int(g["x1"] * s); y1 = int(g["y1"] * s)
        cv2.rectangle(small, (x0, y0), (x1, y1), col, 2)
        if draw_ids:
            cv2.putText(small, str(g["id"]), (x0 + 2, max(12, y0 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 255), 1, cv2.LINE_AA)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    cv2.imwrite(out_path, small)


def pmap(*args, **kwargs):
    """Cross-platform parallel map (spawn context, safe with cv2)."""
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    with ctx.Pool(kwargs.pop("processes", None)) as pool:
        return pool.starmap(args[0], args[1], **kwargs)
