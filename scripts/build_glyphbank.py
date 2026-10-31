"""Build the standard-Yi glyph bank from 《规范彝文单字图形识别手册》 PDF.

    python scripts/build_glyphbank.py --pdf "A:\...\规范彝文单字图形识别手册.pdf"

Output: data/glyphbank/bank.npz  (+ bank_meta.json). 1428 classes, ~5700 glyphs.
Packaged with the app; the repo also keeps a base64 copy (bank.npz.b64) so a
fresh clone works without the PDF.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os

import numpy as np
from PIL import Image

SIDE = 48


def norm_glyph(gray: np.ndarray, side: int = SIDE, ink_thr: int = 190) -> np.ndarray:
    """dark-on-light image -> centred square ink map (255 = ink)."""
    ink = (gray < ink_thr).astype(np.uint8) * 255
    if ink.max() == 0:
        ink = (255 - gray).astype(np.uint8)
        ink = np.where(ink > 40, 255, 0).astype(np.uint8)
    ys, xs = np.nonzero(ink)
    if ys.size == 0:
        return np.zeros((side, side), np.uint8)
    crop = ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = crop.shape
    s = max(h, w)
    canvas = np.zeros((s, s), np.uint8)
    canvas[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = crop
    return np.asarray(Image.fromarray(canvas).resize((side, side), Image.LANCZOS), np.uint8)


def build(pdf_path: str, out_dir: str) -> dict:
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    items = []
    per_page = []
    for pno in range(1, doc.page_count):
        page = doc[pno]
        W, H = page.rect.width, page.rect.height
        cw, ch = W / 4.0, H / 4.0
        codes = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    t = (span.get("text") or "").strip()
                    if t.isdigit() and len(t) >= 5:
                        x0, y0, _, _ = span["bbox"]
                        codes.append((t, x0, y0))
        if not codes:
            continue
        grid = [((c[1] + 8) // cw, (c[2] + 12) // ch, c[0]) for c in codes]

        def assign(cx, cy):
            gx, gy = cx // cw, cy // ch
            return min(grid, key=lambda g: abs(gx - g[0]) + abs(gy - g[1]))[2]

        n_ok = 0
        for info in page.get_images(full=True):
            xref = info[0]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                continue
            for r in rects:
                try:
                    data = doc.extract_image(xref)
                    im = Image.open(io.BytesIO(data["image"])).convert("L")
                except Exception:
                    continue
                code = assign((r.x0 + r.x1) / 2.0, (r.y0 + r.y1) / 2.0)
                items.append((code, norm_glyph(np.asarray(im, np.uint8))))
                n_ok += 1
        per_page.append({"page": pno, "n": n_ok})

    if not items:
        raise SystemExit("没有从 PDF 里抽到字形（检查版面是否 4x4 目录式）")
    refs48 = np.stack([v for _, v in items])
    labels = np.array([c for c, _ in items])
    refs16 = np.stack([
        np.asarray(Image.fromarray(v).resize((16, 16), Image.LANCZOS), np.uint8) for v in refs48
    ])
    os.makedirs(out_dir, exist_ok=True)
    npz = os.path.join(out_dir, "bank.npz")
    np.savez_compressed(npz, refs48=refs48, refs16=refs16, labels=labels)
    with open(npz + ".b64", "w", encoding="ascii") as f:
        f.write(base64.b64encode(open(npz, "rb").read()).decode("ascii"))
    meta = {"n": len(items), "codes": len(set(labels.tolist())), "side": SIDE, "per_page": per_page}
    with open(os.path.join(out_dir, "bank_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    print("glyphs:", meta["n"], "classes:", meta["codes"], "->", npz, os.path.getsize(npz), "bytes")
    return meta


def main():
    ap = argparse.ArgumentParser(description="Build the standard-Yi glyph bank")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                  "data", "glyphbank"))
    args = ap.parse_args()
    build(args.pdf, args.out)


if __name__ == "__main__":
    main()
