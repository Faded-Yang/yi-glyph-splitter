"""Make packaging/app.ico from the delivered glyph tiles (nice icon, no assets needed)."""
import os
import sys

import cv2
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "packaging", "app.ico")
tiles_dir = os.path.join(ROOT, "out", "page1")


def pick_tile():
    """Prefer a clean, dark-ink, roughly square tile."""
    cands = []
    if os.path.isdir(tiles_dir):
        for name in sorted(os.listdir(tiles_dir)):
            if not (name.endswith(".png") and name[0].isdigit()):
                continue
            p = os.path.join(tiles_dir, name)
            im = cv2.imread(p)
            if im is None:
                continue
            h, w = im.shape[:2]
            if not (140 <= min(h, w) and max(h, w) <= 420):
                continue
            g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
            ink = float((g < 100).mean())
            if 0.06 <= ink <= 0.30:
                cands.append((abs(w / float(h) - 1.0), ink, p))
    if not cands:
        return None
    cands.sort()
    return cands[0][2]


def main():
    src = pick_tile()
    if src is None:
        print("no tile found, writing a placeholder icon")
        base = np.full((256, 256, 3), 245, np.uint8)
    else:
        im = cv2.imread(src)
        h, w = im.shape[:2]
        side = max(h, w)
        pad = int(side * 0.10)
        canvas = np.full((side + 2 * pad, side + 2 * pad, 3), 238, np.uint8)
        y0 = (canvas.shape[0] - h) // 2
        x0 = (canvas.shape[1] - w) // 2
        canvas[y0:y0 + h, x0:x0 + w] = im
        base = cv2.resize(canvas, (256, 256), interpolation=cv2.INTER_AREA)
        print("icon source:", os.path.basename(src))
    rgb = cv2.cvtColor(base, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(rgb)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
