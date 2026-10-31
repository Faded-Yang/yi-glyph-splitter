"""Manuscript glyph model: score boxes by "does this look like a glyph from THESE scans".

The printed 规范彝文 bank does not match this handwriting, and the in-app
learning only tunes a handful of knobs. The hand-cut pages give us ~3400 real
glyphs of the very same hand; this module turns them into a likelihood that can
refine the automatic segmentation.

Model is per user: %LOCALAPPDATA%/yi-segment/glyphbank/refs_manual.npz
(built by scripts/build_manual_refs.py from the hand-cut pages).
"""
from __future__ import annotations

import json
import os

import cv2
import numpy as np

from . import paths

SIDE = 48
COARSE = 16
TOP_CAND = 40


def model_path() -> str:
    return str(paths.data_dir() / "glyphbank" / "refs_manual.npz")


def _unit(v: np.ndarray) -> np.ndarray:
    v = v.reshape(len(v), -1).astype(np.float32)
    v -= v.mean(1, keepdims=True)
    v /= (np.linalg.norm(v, axis=1, keepdims=True) + 1e-6)
    return v


class GlyphModel:
    def __init__(self, f16, f48, pages):
        self.f16 = f16
        self.f48 = f48
        self.pages = pages

    @property
    def size(self) -> int:
        return len(self.f16)

    def similarity(self, ink48: np.ndarray, exclude_page: str = "") -> float:
        if self.size == 0:
            return 0.0
        small = cv2.resize(np.asarray(ink48, np.uint8), (COARSE, COARSE), interpolation=cv2.INTER_AREA)
        v16 = _unit(small.reshape(1, -1))[0]
        v48 = _unit(np.asarray(ink48, np.uint8).reshape(1, -1))[0]
        mask = self._mask(exclude_page)
        if mask is None:
            f16, f48 = self.f16, self.f48
        else:
            f16, f48 = self.f16[mask], self.f48[mask]
        if len(f16) == 0:
            return 0.0
        coarse = f16 @ v16
        take = min(TOP_CAND, len(coarse))
        cand = np.argpartition(-coarse, take - 1)[:take]
        fine = f48[cand] @ v48
        return float(fine.max())

    def _mask(self, exclude_page: str):
        if not exclude_page:
            return None
        m = self.pages != exclude_page
        return m if m.any() else None


_CACHE = {"key": None, "model": None}


def load_model():
    fp = model_path()
    if not os.path.isfile(fp):
        return None
    key = (fp, os.path.getmtime(fp))
    if _CACHE["key"] == key:
        return _CACHE["model"]
    try:
        z = np.load(fp)
        refs48 = z["refs48"]
        refs16 = z["refs16"]
        meta_fp = os.path.splitext(fp)[0] + ".json"
        pages = []
        try:
            meta = json.load(open(meta_fp, encoding="utf-8"))
            for p in meta.get("pages") or []:
                pages.extend([p.get("name") or ""] * int(p.get("n") or 0))
        except Exception:
            pages = [""] * len(refs48)
        if len(pages) != len(refs48):
            pages = [""] * len(refs48)
        model = GlyphModel(_unit(refs16), _unit(refs48), np.asarray(pages, dtype=object))
        _CACHE.update({"key": key, "model": model})
        return model
    except Exception:
        return None


def ink_of(gray, box) -> np.ndarray:
    x0, y0, x1, y1 = [int(v) for v in box]
    tile = np.asarray(gray)[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
    if tile.size == 0:
        return np.zeros((SIDE, SIDE), np.uint8)
    g = tile.astype(np.uint8)
    if g.ndim == 3:
        g = cv2.cvtColor(g, cv2.COLOR_RGB2GRAY)
    thr, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    m = (g < max(60, int(thr))).astype(np.uint8)
    ys, xs = np.nonzero(m)
    if ys.size < 10:
        return np.zeros((SIDE, SIDE), np.uint8)
    crop = (m[ys.min():ys.max() + 1, xs.min():xs.max() + 1] * 255).astype(np.uint8)
    h, w = crop.shape
    s = max(h, w)
    canvas = np.zeros((s, s), np.uint8)
    canvas[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = crop
    return cv2.resize(canvas, (SIDE, SIDE), interpolation=cv2.INTER_AREA)
