"""Glyph-library manager core: cluster exported tiles, edit boxes, re-export pages.

Data on disk (out/<page>/):
    index.csv            one row per exported tile (filename,id,col,row,x0..gy1,...)
    NNN.png              the tile actually exported (crop of the scan with 10% pad)
    .yisegment_page.json {pid, name, updated}

The manager keeps its own state in out/.glyphmgr/ :
    classes.json   [{id, n, members:[{page, file, page_id, idx}]}]
    undo.json      last action (for one-step undo)

Every edit rewrites the affected page: tiles are re-cropped from the scan,
renumbered 001..N in reading order, and index.csv is regenerated.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import time

import cv2
import numpy as np
from PIL import Image


def load_rgb(fp):
    """Read an image as RGB uint8 (the exporter writes colour tiles)."""
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(fp) as im:
        return np.ascontiguousarray(np.asarray(im.convert("RGB"), dtype=np.uint8))


def load_gray(fp):
    """Read an image as uint8 grayscale (PIL handles TIFF/PNG reliably here)."""
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(fp) as im:
        if im.mode in ("I;16", "I;16B", "I;16L", "I"):
            a = np.asarray(im, dtype=np.uint16)
            if a.max() > 255:
                a = (a.astype(np.float32) / (a.max() / 255.0)).clip(0, 255)
            return a.astype(np.uint8)
        return np.asarray(im.convert("L"), dtype=np.uint8)

SIDE = 48
COARSE = 16
CLUSTER_SIM = 0.64
PAD_RATIO = 0.1
MIN_PAD = 6


# ------------------------------------------------------------------ io helpers
def _read_json(fp, default):
    try:
        return json.loads(open(fp, encoding="utf-8").read())
    except Exception:
        return default


def _write_json(fp, data):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    last = None
    for _ in range(20):
        try:
            os.replace(tmp, fp)
            return
        except PermissionError as exc:   # OneDrive / AV briefly locks the file
            last = exc
            import time as _time
            _time.sleep(0.15)
    raise last


_BANK = {"key": None, "data": None}


def _bank_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [os.path.join(os.path.dirname(here), "data", "glyphbank"),
             os.path.join(here, "data", "glyphbank"),
             os.path.join(os.getcwd(), "data", "glyphbank"),
             os.path.join(os.getcwd(), "..", "data", "glyphbank")]
    import sys as _sys
    if getattr(_sys, "frozen", False):
        roots.insert(0, os.path.join(os.path.dirname(_sys.executable), "data", "glyphbank"))
        roots.insert(1, os.path.join(os.path.dirname(_sys.executable), "_internal", "data", "glyphbank"))
    out = []
    for root in roots:
        for name in ("bank.npz", "bank.npz.b64"):
            fp = os.path.join(root, name)
            if os.path.isfile(fp):
                out.append(fp)
    return out


def _load_bank():
    """(f16, f48, labels) of the printed standard bank, or None."""
    import base64
    import io as _io
    for fp in _bank_paths():
        try:
            if fp.lower().endswith(".b64"):
                raw = base64.b64decode(open(fp, "rb").read())
                z = np.load(_io.BytesIO(raw))
            else:
                z = np.load(fp)
        except Exception:
            continue
        try:
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
            return f16, f48, labels
        except Exception:
            continue
    return None


def unit(rows):
    v = np.asarray(rows, np.float32).reshape(len(rows), -1)
    v = v - v.mean(1, keepdims=True)
    v /= (np.linalg.norm(v, axis=1, keepdims=True) + 1e-6)
    return v


def norm_ink(gray):
    if gray is None or np.asarray(gray).size == 0:
        return np.zeros((SIDE, SIDE), np.uint8)
    g = np.asarray(gray)
    if g.ndim == 3:
        g = cv2.cvtColor(g, cv2.COLOR_RGB2GRAY)
    g = g.astype(np.uint8)
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


def tight_box(gray, box):
    x0, y0, x1, y1 = [int(v) for v in box]
    tile = np.asarray(gray)[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
    if tile.size == 0:
        return None
    g = tile.astype(np.uint8)
    if g.ndim == 3:
        g = cv2.cvtColor(g, cv2.COLOR_RGB2GRAY)
    thr, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    m = (g < max(60, int(thr))).astype(np.uint8)
    ys, xs = np.nonzero(m)
    if ys.size < 10:
        return None
    return (x0 + int(xs.min()), y0 + int(ys.min()), x0 + int(xs.max()) + 1, y0 + int(ys.max()) + 1)


# ------------------------------------------------------------------ page model
class Page:
    def __init__(self, root, name):
        self.root = root
        self.name = name
        self.dir = os.path.join(root, name)
        self.csv_path = os.path.join(self.dir, "index.csv")
        self.marker = _read_json(os.path.join(self.dir, ".yisegment_page.json"), {})
        self.rows = []
        if os.path.isfile(self.csv_path):
            with open(self.csv_path, encoding="utf-8-sig", newline="") as f:
                self.rows = list(csv.DictReader(f))
        self._gray = None
        self._rgb = None

    # -- scan
    def tif_path(self):
        name = self.marker.get("name") or ""
        if not name:
            name = os.path.basename(self.name) + ".tif"
        scans = os.path.join(os.path.dirname(os.path.abspath(self.root)), "scans")
        cand = os.path.join(scans, os.path.basename(name))
        return cand if os.path.isfile(cand) else ""

    def gray(self):
        if self._gray is not None:
            return self._gray
        fp = self.tif_path()
        if not fp:
            raise RuntimeError("找不到源 TIF：%s" % self.name)
        self._gray = load_gray(fp)
        return self._gray

    def rgb(self):
        if self._rgb is not None:
            return self._rgb
        fp = self.tif_path()
        if not fp:
            raise RuntimeError("找不到源 TIF：%s" % self.name)
        self._rgb = load_rgb(fp)
        return self._rgb

    def tile_path(self, filename):
        return os.path.join(self.dir, filename)

    def glyph_boxes(self):
        """Tight glyph boxes (gx0..gy1), i.e. WITHOUT the export padding."""
        out = []
        for r in self.rows:
            if r.get("gx0") not in (None, ""):
                out.append((int(float(r["gx0"])), int(float(r["gy0"])),
                            int(float(r["gx1"])), int(float(r["gy1"]))))
            else:
                out.append((int(float(r["x0"])), int(float(r["y0"])),
                            int(float(r["x1"])), int(float(r["y1"]))))
        return out

    def tile_rects(self):
        return [(int(float(r["x0"])), int(float(r["y0"])), int(float(r["x1"])), int(float(r["y1"])))
                for r in self.rows]

    # -- export
    def reexport(self, boxes, backup=True):
        """Rewrite this page's tiles + csv from the given TIGHT glyph boxes."""
        import re as _re
        rgb = self.rgb()
        if backup:
            bak = os.path.join(self.root, ".glyphmgr", "backup", self.name, time.strftime("%Y%m%d-%H%M%S"))
            os.makedirs(bak, exist_ok=True)
            for fn in os.listdir(self.dir):
                if fn.lower().endswith(".png") or fn == "index.csv":
                    shutil.copy2(os.path.join(self.dir, fn), os.path.join(bak, fn))
        # clean only the numeric tiles + the contact sheet (keep marker / other files)
        for fn in os.listdir(self.dir):
            if _re.match(r"^\d+\.(png|jpg|webp)$", fn, _re.I) or fn == "sheet.png":
                try:
                    os.remove(os.path.join(self.dir, fn))
                except OSError:
                    pass
        h_img, w_img = rgb.shape[:2]
        rows = []
        for i, (gx0, gy0, gx1, gy1) in enumerate(boxes, start=1):
            gx0 = max(0, min(w_img - 1, gx0)); gx1 = max(gx0 + 1, min(w_img, gx1))
            gy0 = max(0, min(h_img - 1, gy0)); gy1 = max(gy0 + 1, min(h_img, gy1))
            gw, gh = gx1 - gx0, gy1 - gy0
            pad = max(MIN_PAD, int(round(PAD_RATIO * max(gw, gh))))
            x0 = max(0, gx0 - pad); y0 = max(0, gy0 - pad)
            x1 = min(w_img, gx1 + pad); y1 = min(h_img, gy1 + pad)
            tile = rgb[y0:y1, x0:x1]
            fn = "%03d.png" % i
            Image.fromarray(np.ascontiguousarray(tile)).save(os.path.join(self.dir, fn))
            rows.append({"filename": fn, "id": i, "col": -1, "row": -1,
                         "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                         "w": x1 - x0, "h": y1 - y0, "ink": 0, "pad": pad,
                         "gx0": gx0, "gy0": gy0, "gx1": gx1, "gy1": gy1,
                         "flags": "", "flat": "", "ink_frac": "", "core_frac": ""})
        fields = ["filename", "id", "col", "row", "x0", "y0", "x1", "y1", "w", "h", "ink", "pad",
                  "gx0", "gy0", "gx1", "gy1", "flags", "flat", "ink_frac", "core_frac"]
        with open(self.csv_path, "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=fields)
            wr.writeheader()
            for r in rows:
                wr.writerow(r)
        self.rows = rows
        self._gray = None
        self._rgb = rgb
        try:
            self.make_sheet(rows)
        except Exception:
            pass
        return len(rows)

    def make_sheet(self, rows=None):
        """Rebuild sheet.png: a simple contact sheet of the tiles (13 per row)."""
        rows = rows or self.rows
        if not rows:
            return
        cols = 13
        slot_w, slot_h, max_side, gap = 165, 175, 105, 6
        n = len(rows)
        nrows = (n + cols - 1) // cols
        sheet = Image.new("RGB", (cols * slot_w, nrows * slot_h), (255, 255, 255))
        for i, r in enumerate(rows):
            fp = os.path.join(self.dir, r["filename"])
            if not os.path.isfile(fp):
                continue
            try:
                im = Image.open(fp).convert("RGB")
            except Exception:
                continue
            im.thumbnail((max_side, max_side), Image.LANCZOS)
            cx = (i % cols) * slot_w + (slot_w - im.width) // 2
            cy = (i // cols) * slot_h + gap + (max_side - im.height) // 2
            sheet.paste(im, (cx, cy))
        sheet.save(os.path.join(self.dir, "sheet.png"))


# ------------------------------------------------------------------ library
class Library:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.state_dir = os.path.join(self.root, ".glyphmgr")
        os.makedirs(self.state_dir, exist_ok=True)
        self.pages = {}
        for name in sorted(os.listdir(self.root)):
            d = os.path.join(self.root, name)
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "index.csv")):
                self.pages[name] = Page(self.root, name)
        self.manual = _read_json(os.path.join(self.state_dir, "manual.json"),
                                 {"moves": {}, "labels": {}, "next_new": 1})
        if not isinstance(self.manual, dict):
            self.manual = {"moves": {}, "labels": {}, "next_new": 1}
        self.manual.setdefault("moves", {})
        self.manual.setdefault("labels", {})
        self.manual.setdefault("next_new", 1)
        loaded = _read_json(os.path.join(self.state_dir, "classes.json"), None)
        if isinstance(loaded, dict):
            loaded = loaded.get("classes") or []
        self._base = loaded if isinstance(loaded, list) else None
        if self._base is not None:
            try:
                self._migrate_manual()
            except Exception:
                pass
        self.classes = self._overlay(self._base) if self._base is not None else None

    # -- features + classes
    def build_classes(self, force=False, sim=CLUSTER_SIM):
        if self.classes and not force:
            return self.classes
        items = []
        for name, page in self.pages.items():
            for i, row in enumerate(page.rows, start=1):
                fp = page.tile_path(row["filename"])
                if not os.path.isfile(fp):
                    continue
                try:
                    img = load_gray(fp)
                except Exception:
                    continue
                items.append({"page": name, "file": row["filename"], "idx": i,
                              "ink48": norm_ink(img)})
        if not items:
            self.classes = []
            return self.classes
        for it in items:
            it["small"] = cv2.resize(it["ink48"], (COARSE, COARSE), interpolation=cv2.INTER_AREA)
        f16 = unit(np.stack([it["small"] for it in items]))
        f48 = unit(np.stack([it["ink48"] for it in items]))
        clusters = []          # list of lists of item indices
        reps = []              # representative 48-vector per cluster
        reps16 = []
        for i in range(len(items)):
            best, best_sim = -1, sim
            if reps:
                sims = np.asarray(reps16) @ f16[i]
                j = int(np.argmax(sims))
                if sims[j] > best_sim:
                    best, best_sim = j, float(sims[j])
            if best < 0:
                reps.append(f48[i]); reps16.append(f16[i]); clusters.append([])
                best = len(clusters) - 1
            clusters[best].append(i)
        out = []
        for cid, members in enumerate(clusters):
            out.append({"id": cid, "n": len(members),
                        "members": [{"page": items[i]["page"], "file": items[i]["file"],
                                     "idx": items[i]["idx"]} for i in members]})
        out.sort(key=lambda c: -c["n"])
        base = out
        self._base = base
        self.classes = self._apply_manual(base)
        _write_json(os.path.join(self.state_dir, "classes.json"),
                    {"built": time.strftime("%Y-%m-%d %H:%M"), "sim": sim,
                     "pages": list(self.pages), "classes": base})
        return self.classes

    def class_members(self, cid):
        for c in self.classes or []:
            if int(c["id"]) == int(cid):
                return c["members"]
        return []

    # -- edits
    def _log_undo(self, page_name, boxes_before):
        _write_json(os.path.join(self.state_dir, "undo.json"),
                    {"page": page_name, "boxes": boxes_before,
                     "at": time.strftime("%Y-%m-%d %H:%M:%S")})

    def undo(self):
        data = _read_json(os.path.join(self.state_dir, "undo.json"), None)
        if not data:
            return {"ok": False, "error": "没有可撤销的操作"}
        page = self.pages.get(data["page"])
        if page is None:
            return {"ok": False, "error": "页面不在库里：%s" % data["page"]}
        page.reexport([tuple(b) for b in data["boxes"]])
        os.remove(os.path.join(self.state_dir, "undo.json"))
        return {"ok": True, "page": data["page"], "n": len(data["boxes"])}

    def split_glyph(self, page_name, idx, rel=0.5, axis="h", apply_class=None):
        """Split glyph #idx of a page at relative position rel.

        axis='h' cuts across (two glyphs stacked), 'v' cuts sideways.
        apply_class: when set, the same relative cut is applied to every member
        of that class (across pages).
        """
        page = self.pages.get(page_name)
        if page is None:
            return {"ok": False, "error": "页面不存在：%s" % page_name}
        boxes = page.glyph_boxes()
        if not (1 <= idx <= len(boxes)):
            return {"ok": False, "error": "框号越界"}
        before = [list(b) for b in boxes]
        targets = [(page_name, idx)]
        if apply_class is not None:
            targets = [(m["page"], m["idx"]) for m in self.class_members(apply_class)]
        changed = []
        per_page = {}
        for pname, i in targets:
            p = self.pages.get(pname)
            if p is None:
                continue
            bs = p.glyph_boxes()
            if not (1 <= i <= len(bs)):
                continue
            per_page.setdefault(pname, bs)
            x0, y0, x1, y1 = per_page[pname][i - 1]
            gray = p.gray()
            if axis == "h":
                cut = y0 + int(round((y1 - y0) * rel))
                a = tight_box(gray, (x0, y0, x1, cut)) or (x0, y0, x1, cut)
                b = tight_box(gray, (x0, cut, x1, y1)) or (x0, cut, x1, y1)
            else:
                cut = x0 + int(round((x1 - x0) * rel))
                a = tight_box(gray, (x0, y0, cut, y1)) or (x0, y0, cut, y1)
                b = tight_box(gray, (cut, y0, x1, y1)) or (cut, y0, x1, y1)
            per_page[pname][i - 1:i] = [a, b]
            changed.append({"page": pname, "idx": i})
        for pname, bs in per_page.items():
            self.pages[pname].reexport(bs)
        if changed:
            self._log_undo(page_name, before)
        self.build_classes(force=True)
        return {"ok": True, "changed": changed, "note": "已切分 %d 个框" % len(changed)}

    def merge_glyph(self, page_name, idx, with_next=True):
        """Merge glyph #idx with its neighbour below (or above) in the same page."""
        page = self.pages.get(page_name)
        if page is None:
            return {"ok": False, "error": "页面不存在"}
        boxes = page.glyph_boxes()
        j = idx if with_next else idx - 1
        if not (1 <= j < len(boxes) + (0 if with_next else 0)) or j < 1 or j >= len(boxes):
            return {"ok": False, "error": "没有可合并的邻框"}
        before = [list(b) for b in boxes]
        a, b = boxes[j - 1], boxes[j]
        u = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
        new_box = tight_box(page.gray(), u) or u
        boxes[j - 1:j + 1] = [new_box]
        page.reexport(boxes)
        self._log_undo(page_name, before)
        self.build_classes(force=True)
        return {"ok": True, "page": page_name, "merged": [j, j + 1], "n": len(boxes)}

    # -- query
    def _overlay(self, base):
        if base is None:
            return None
        return self._apply_manual(base)

    def refresh_classes(self):
        """Re-apply manual moves/labels WITHOUT re-running the clustering (fast)."""
        if self._base is None:
            self.build_classes(force=True)
            return self.classes
        self.classes = self._overlay(self._base)
        return self.classes

    # -- manual classification (v2: anchor based, survives re-clustering) -----
    @staticmethod
    def _anchor(m):
        return "%s|%s" % (m.get("page"), m.get("file"))

    def _save_manual(self):
        self.manual["version"] = 2
        _write_json(os.path.join(self.state_dir, "manual.json"), self.manual)

    def _migrate_manual(self):
        """v1 keys (auto:N / new:M) -> v2 anchors so re-clustering keeps edits."""
        base = self._base or []
        moves = self.manual.get("moves") or {}
        labels = self.manual.get("labels") or {}
        needs = (self.manual.get("version") != 2
                 or any(str(v).startswith(("auto:", "new:")) for v in moves.values())
                 or any(str(k).startswith(("auto:", "new:")) for k in labels))
        if not needs:
            return
        id2anchor = {}
        for c in base:
            ms = c.get("members") or []
            if ms:
                id2anchor[int(c["id"])] = self._anchor(ms[0])
        groups = {}
        for gk, t in moves.items():
            if str(t).startswith("new:"):
                groups.setdefault(str(t), gk)
        new_moves = {}
        for gk, t in moves.items():
            t = str(t)
            if t.startswith("auto:"):
                try:
                    aid = int(t.split(":", 1)[1])
                except ValueError:
                    continue
                a = id2anchor.get(aid)
                if a:
                    new_moves[gk] = "A:" + a
            elif t.startswith("new:"):
                a = groups.get(t) or gk
                new_moves[gk] = "M:" + a
            else:
                new_moves[gk] = t
        new_labels = {}
        for k, v in labels.items():
            k = str(k)
            if k.startswith("auto:"):
                try:
                    aid = int(k.split(":", 1)[1])
                except ValueError:
                    continue
                a = id2anchor.get(aid)
                if a:
                    new_labels[a] = v
            elif k.startswith("new:"):
                a = groups.get(k)
                if a:
                    new_labels[a] = v
            else:
                new_labels[k] = v
        self.manual["moves"] = new_moves
        self.manual["labels"] = new_labels
        self.manual["version"] = 2
        self._save_manual()

    def _apply_manual(self, classes):
        """Overlay anchor-based moves/labels on the automatic classes."""
        base = classes or []
        moves = self.manual.get("moves") or {}
        labels = self.manual.get("labels") or {}
        member_base = {}
        base_key = {}
        for c in base:
            members = list(c.get("members") or [])
            if not members:
                continue
            bid = int(c["id"])
            base_key[bid] = "A:" + self._anchor(members[0])
            for m in members:
                member_base[self._anchor(m)] = bid

        def resolve(akey, depth=0):
            if depth > 8:
                return None
            t = moves.get(akey)
            if not t:
                return base_key.get(member_base.get(akey))
            t = str(t)
            if t.startswith("A:"):
                return resolve(t[2:], depth + 1)
            if t.startswith("M:"):
                return t
            return None

        groups = {}
        for akey, bid in member_base.items():
            g = resolve(akey) or base_key.get(bid)
            if g:
                groups.setdefault(g, []).append(akey)
        out = []
        for i, (g, anchors) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1]))):
            members = []
            for a in anchors:
                page, _, file = a.partition("|")
                members.append({"page": page, "file": file})
            label = ""
            for a in anchors:
                if a in labels:
                    label = labels[a]
                    break
            out.append({"id": i, "key": g, "n": len(members), "members": members,
                        "auto": 0, "label": label})
        return out

    def assign(self, items, target="", label=""):
        """Move glyphs into an existing class or a brand-new manual class."""
        moves = self.manual["moves"]
        items = [it for it in (items or []) if it.get("page") and it.get("file")]
        if not items:
            return {"ok": True, "moved": 0, "target": str(target or "")}
        tgt = str(target or "")
        if not tgt or tgt == "new":
            tgt = "M:" + self._anchor(items[0])
        elif not (tgt.startswith("A:") or tgt.startswith("M:")):
            tgt = "A:" + tgt
        for it in items:
            moves[self._anchor(it)] = tgt
        if label:
            self.manual["labels"][tgt[2:] if tgt.startswith(("A:", "M:")) else tgt] = str(label)
        self._save_manual()
        self.refresh_classes()
        return {"ok": True, "moved": len(items), "target": tgt}

    def unassign(self, items):
        moves = self.manual["moves"]
        n = 0
        for it in items or []:
            k = self._anchor(it)
            if k in moves:
                del moves[k]
                n += 1
        self._save_manual()
        self.refresh_classes()
        return {"ok": True, "reverted": n}

    def merge_classes(self, src_key, dst_key):
        keyed = {c["key"]: c for c in self.classes or []}
        if src_key not in keyed or dst_key not in keyed:
            return {"ok": False, "error": "类别不存在"}
        for m in keyed[src_key]["members"]:
            self.manual["moves"][self._anchor(m)] = dst_key
        self._save_manual()
        self.refresh_classes()
        return {"ok": True, "merged": len(keyed[src_key]["members"])}

    def set_label(self, key, label):
        label = str(label or "")
        cls = next((c for c in (self.classes or []) if c.get("key") == str(key)), None)
        if cls and cls.get("members"):
            a = self._anchor(cls["members"][0])
            if label:
                self.manual["labels"][a] = label
            else:
                self.manual["labels"].pop(a, None)
        else:
            a = str(key)
            if label:
                self.manual["labels"][a] = label
            else:
                self.manual["labels"].pop(a, None)
        self._save_manual()
        self.refresh_classes()
        return {"ok": True}

    def autolabel(self, limit=800, min_score=0.42):
        bank = _load_bank()
        if bank is None:
            return {"ok": False, "error": "找不到规范字库 data/glyphbank/bank.npz"}
        f16, f48, labels = bank
        done = 0
        for c in self.classes or []:
            members = c.get("members") or []
            if not members:
                continue
            first = self._anchor(members[0])
            if first in (self.manual.get("labels") or {}):
                continue
            inks = []
            for m in members[:3]:
                page = self.pages.get(m["page"])
                if page is None:
                    continue
                fp = page.tile_path(m["file"])
                if not os.path.isfile(fp):
                    continue
                try:
                    img = load_gray(fp)
                except Exception:
                    continue
                ink = norm_ink(img)
                if int((ink > 0).sum()) >= 20:
                    inks.append(ink)
            if not inks:
                continue
            rep = np.mean(np.stack(inks).astype(np.float32), axis=0).astype(np.uint8)
            v16 = unit(cv2.resize(rep, (COARSE, COARSE), interpolation=cv2.INTER_AREA).reshape(1, -1))[0]
            v48 = unit(rep.reshape(1, -1))[0]
            coarse = f16 @ v16
            take = min(60, len(coarse))
            cand = np.argpartition(-coarse, take - 1)[:take]
            fine = f48[cand] @ v48
            j = int(np.argmax(fine))
            score = float(fine[j])
            if score >= min_score:
                self.manual["labels"][first] = "%s（%.2f）" % (str(labels[int(cand[j])]), score)
                done += 1
            if done >= limit:
                break
        self._save_manual()
        self.refresh_classes()
        return {"ok": True, "labeled": done, "total": len(self.classes or [])}

    def _class_mean_ink(self, c, k=3):
        inks = []
        for m in c["members"][:k]:
            page = self.pages.get(m["page"])
            if page is None:
                continue
            fp = page.tile_path(m["file"])
            if not os.path.isfile(fp):
                continue
            try:
                inks.append(norm_ink(load_gray(fp)))
            except Exception:
                continue
        if not inks:
            return None
        return np.mean(np.stack(inks).astype(np.float32), axis=0).astype(np.uint8)

    def _class_feature(self, c):
        ink = self._class_mean_ink(c)
        if ink is None:
            return None
        return unit(cv2.resize(ink, (SIDE, SIDE), interpolation=cv2.INTER_AREA).reshape(1, -1))[0]

    def merge_small(self, min_n=3, thr=0.35):
        """Merge classes with <min_n members into the most similar bigger class."""
        classes = self.classes or []
        small = [c for c in classes if c["n"] < int(min_n)]
        big = [c for c in classes if c["n"] >= int(min_n)]
        if not small or not big:
            return {"ok": True, "merged": 0, "classes": len(classes)}
        big_nodes = [(c, self._class_feature(c)) for c in big]
        big_nodes = [(c, f) for c, f in big_nodes if f is not None]
        if not big_nodes:
            return {"ok": True, "merged": 0, "classes": len(classes)}
        FB = np.stack([f for _, f in big_nodes])
        merged = 0
        for c in small:
            f = self._class_feature(c)
            if f is None:
                continue
            sims = FB @ f
            j = int(np.argmax(sims))
            if float(sims[j]) < thr:
                continue
            if self.merge_classes(c.get("key"), big_nodes[j][0].get("key")).get("ok"):
                merged += 1
        self.refresh_classes()
        return {"ok": True, "merged": merged, "classes": len(self.classes or [])}

    def summary(self):
        return {"pages": [{"name": n, "n": len(p.rows)} for n, p in self.pages.items()],
                "classes": len(self.classes or []),
                "glyphs": sum(len(p.rows) for p in self.pages.values())}

    def glyph(self, page_name, file_or_idx):
        page = self.pages.get(page_name)
        if page is None:
            return None
        for i, row in enumerate(page.rows, start=1):
            if str(row["filename"]) == str(file_or_idx) or str(i) == str(file_or_idx):
                return {"page": page_name, "file": row["filename"], "idx": i,
                        "box": [int(float(row["gx0"])), int(float(row["gy0"])),
                                int(float(row["gx1"])), int(float(row["gy1"]))],
                        "tile_box": [int(float(row["x0"])), int(float(row["y0"])),
                                     int(float(row["x1"])), int(float(row["y1"]))]}
        return None
