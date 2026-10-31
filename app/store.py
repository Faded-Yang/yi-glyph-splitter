"""ProjectStore: on-disk project state for the app.

Layout (see app/SPEC-APP.md):

    <project>/project.json
    <project>/pages/<pid>/page.json       segmentation result (written once)
    <project>/pages/<pid>/boxes.json      authoritative boxes for export
    <project>/pages/<pid>/proposals.json  AI proposals (never applied automatically)
    <project>/pages/<pid>/preview.png     page thumbnail (no boxes drawn)

Rules that matter (learned the hard way, see yi-segment/AGENTS.md):
  * every JSON read/write is explicit encoding="utf-8" (cp936 default would break)
  * writes are atomic: tmp file + os.replace
  * glyph/box coordinates are original page pixels, x1/y1 half-open
  * boxes.json (user edits) always wins over page.json glyphs

CLI:  python -m app.store --selftest
"""
from __future__ import annotations

import copy
import fnmatch
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Iterable

PROJECT_FILE = "project.json"
PROJECT_VERSION = 1

STATUS_PENDING = "pending"
STATUS_PROCESSED = "processed"
STATUS_REVIEWING = "reviewing"
STATUS_DONE = "done"
STATUS_ERROR = "error"

STATUS_LABELS = {
    STATUS_PENDING: "未处理",
    STATUS_PROCESSED: "已处理",
    STATUS_REVIEWING: "复核中",
    STATUS_DONE: "已完成",
    STATUS_ERROR: "出错",
}
ALL_STATUSES = tuple(STATUS_LABELS)

_STATUS_ALIASES = {
    "new": STATUS_PENDING, "unprocessed": STATUS_PENDING, "todo": STATUS_PENDING,
    "ready": STATUS_PROCESSED, "segmented": STATUS_PROCESSED, "processed": STATUS_PROCESSED,
    "review": STATUS_REVIEWING, "checking": STATUS_REVIEWING, "reviewing": STATUS_REVIEWING,
    "finished": STATUS_DONE, "complete": STATUS_DONE, "completed": STATUS_DONE, "done": STATUS_DONE,
    "failed": STATUS_ERROR, "fail": STATUS_ERROR, "error": STATUS_ERROR,
}

IMAGE_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".webp")
DEFAULT_PATTERNS = ("*.tif", "*.tiff")
MAX_FLAGS = 8
MAX_NOTE = 200
REVIEW_ALLOWED = (0, 1, 2)


class StoreError(Exception):
    """User facing project error; str(exc) is a Chinese sentence."""


# --------------------------------------------------------------- small utils
def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def atomic_write_text(path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def atomic_write_json(path, obj: Any, indent: int = 1) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent))


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def atomic_write_bytes(path, data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def safe_name(name, fallback: str = "page", maxlen: int = 80) -> str:
    """Make a string usable as a file/directory name on Windows."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or ""))
    s = s.strip(" .")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip(" .")
    return s or fallback


def _int(v, default: int = 0) -> int:
    try:
        if isinstance(v, bool):
            return default
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def path_key(path) -> str:
    """Dedupe key for a source path (case-insensitive on Windows)."""
    try:
        p = os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    except (OSError, ValueError):
        p = str(path)
    return os.path.normcase(p)


def is_image(path) -> bool:
    return str(path).lower().endswith(IMAGE_EXTS)


# --------------------------------------------------------------- box helpers
def normalize_box(box, width=None, height=None):
    """Validate one box dict; returns (clean dict without id | None, warnings)."""
    warns = []
    if not isinstance(box, dict):
        return None, ["忽略了一个不是对象的框"]
    x0 = _int(box.get("x0"), 0)
    y0 = _int(box.get("y0"), 0)
    if box.get("x1") is not None:
        x1 = _int(box.get("x1"), x0)
    else:
        x1 = x0 + _int(box.get("w"), 1)
    if box.get("y1") is not None:
        y1 = _int(box.get("y1"), y0)
    else:
        y1 = y0 + _int(box.get("h"), 1)
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0 = max(0, x0)
    y0 = max(0, y0)
    if width:
        x1 = min(_int(width), x1)
    if height:
        y1 = min(_int(height), y1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None, ["丢弃了一个空框或退化框（宽或高小于 2 像素）"]
    flags = box.get("flags")
    clean_flags = []
    if isinstance(flags, str):
        flags = re.split(r"[;,| ]+", flags)
    if isinstance(flags, (list, tuple)):
        for f in flags:
            s = str(f).strip()
            if s and s not in clean_flags:
                clean_flags.append(s[:40])
    review = _int(box.get("review"), 0)
    if review not in REVIEW_ALLOWED:
        review = 2 if review >= 2 else (1 if review == 1 else 0)
    note = str(box.get("note") or "").strip()[:MAX_NOTE]
    # 人工手动标记的颜色（可选）：#rrggbb；非法值直接丢掉
    color = str(box.get("color") or "").strip().lower()
    if color and not re.match(r"^#[0-9a-f]{6}$", color):
        color = ""
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "flags": clean_flags[:MAX_FLAGS], "review": review, "note": note,
            "color": color}, warns


def normalize_boxes(boxes, width=None, height=None):
    """Validate + renumber a full box list.  Returns (boxes, warnings)."""
    if boxes is None:
        boxes = []
    if isinstance(boxes, dict):
        boxes = boxes.get("boxes") or []
    if not isinstance(boxes, (list, tuple)):
        raise StoreError("框数据格式不对：应当是一个数组")
    out = []
    warns = []
    dropped = 0
    for b in boxes:
        nb, w = normalize_box(b, width, height)
        warns.extend(w)
        if nb is None:
            dropped += 1
            continue
        nb["id"] = len(out) + 1
        out.append(nb)
    if dropped:
        warns.append("共丢弃 %d 个无效框" % dropped)
    return out, warns


def boxes_from_glyphs(glyphs, width=None, height=None):
    """Turn page.json glyphs into the initial (review=0) box list."""
    boxes = []
    for i, g in enumerate(glyphs or []):
        if not isinstance(g, dict):
            continue
        b = {"x0": g.get("x0"), "y0": g.get("y0"), "x1": g.get("x1"), "y1": g.get("y1"),
             "flags": list(g.get("flags") or []), "review": 0, "note": "", "color": ""}
        if g.get("id") is not None:
            b["id"] = _int(g.get("id"), i + 1)
        boxes.append(b)
    return normalize_boxes(boxes, width, height)


# --------------------------------------------------------------- the store
class ProjectStore:
    """Thread-safe accessor for one project directory."""

    def __init__(self, root, *, create: bool = True):
        self.root = Path(os.path.abspath(os.path.expanduser(str(root))))
        self._lock = threading.RLock()
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "pages").mkdir(parents=True, exist_ok=True)
        self.project = self._load_project()

    # ------------------------------------------------------------ project file
    def _new_project(self) -> dict:
        return {
            "version": PROJECT_VERSION,
            "name": self.root.name or "项目",
            "created": _now(),
            "updated": _now(),
            "seq": 0,
            "pages": [],
        }

    def _clean_entry(self, entry) -> dict:
        if not isinstance(entry, dict):
            return {}
        e = {
            "id": str(entry.get("id") or ""),
            "name": str(entry.get("name") or ""),
            "src": str(entry.get("src") or ""),
            "status": str(entry.get("status") or STATUS_PENDING),
            "error": str(entry.get("error") or ""),
            "width": _int(entry.get("width"), 0),
            "height": _int(entry.get("height"), 0),
            "n_boxes": _int(entry.get("n_boxes"), 0),
            "reviewed": bool(entry.get("reviewed")),
            "ai_refined": bool(entry.get("ai_refined")),
        }
        st = e["status"].lower()
        e["status"] = _STATUS_ALIASES.get(st, st if st in ALL_STATUSES else STATUS_PENDING)
        for extra in ("created", "exported", "last_export", "n_proposals"):
            if extra in entry:
                e[extra] = entry[extra]
        return e

    def _load_project(self) -> dict:
        p = self.root / PROJECT_FILE
        raw = read_json(p, None)
        if not isinstance(raw, dict) or not isinstance(raw.get("pages"), list):
            proj = self._new_project()
            self.project = proj
            self._save_project(proj)
            return proj
        proj = self._new_project()
        proj.update({k: v for k, v in raw.items() if k not in ("pages",)})
        proj["version"] = _int(raw.get("version"), PROJECT_VERSION) or PROJECT_VERSION
        proj["seq"] = _int(raw.get("seq"), 0)
        proj["pages"] = [self._clean_entry(e) for e in raw.get("pages") or []]
        proj["pages"] = [e for e in proj["pages"] if e.get("id")]
        return proj

    def _save_project(self, proj=None) -> None:
        with self._lock:
            proj = self.project if proj is None else proj
            proj["updated"] = _now()
            atomic_write_json(self.root / PROJECT_FILE, proj)

    def save(self) -> None:
        self._save_project()

    # --------------------------------------------------------------- metadata
    @property
    def name(self) -> str:
        return str(self.project.get("name") or self.root.name)

    def list_pages(self) -> list:
        with self._lock:
            return [dict(e) for e in self.project.get("pages") or []]

    def page_ids(self) -> list:
        return [e["id"] for e in self.project.get("pages") or []]

    def get_page(self, pid) -> dict:
        pid = str(pid)
        with self._lock:
            for e in self.project.get("pages") or []:
                if e["id"] == pid:
                    return dict(e)
        return {}

    def has_page(self, pid) -> bool:
        return bool(self.get_page(pid))

    def update_page(self, pid, **fields) -> dict:
        allowed = {"name", "src", "status", "error", "width", "height", "n_boxes",
                   "reviewed", "ai_refined", "last_export", "n_proposals", "exported"}
        unknown = set(fields) - allowed
        if unknown:
            raise StoreError("不能修改这些页面字段：%s" % ", ".join(sorted(unknown)))
        with self._lock:
            for e in self.project.get("pages") or []:
                if e["id"] == str(pid):
                    for k, v in fields.items():
                        if k in ("width", "height", "n_boxes"):
                            v = _int(v, 0)
                        elif k in ("reviewed", "ai_refined"):
                            v = bool(v)
                        elif k == "status":
                            st = str(v).lower()
                            v = _STATUS_ALIASES.get(st, st if st in ALL_STATUSES else STATUS_PENDING)
                        elif k == "error":
                            v = str(v or "")
                        e[k] = v
                    self._save_project()
                    return dict(e)
        raise StoreError("页面不存在：%s" % pid)

    # ------------------------------------------------------------------ pages
    def _allocate_pid(self) -> str:
        with self._lock:
            self.project["seq"] = _int(self.project.get("seq"), 0) + 1
            seq = self.project["seq"]
        return "p%04d_%s" % (seq, secrets.token_hex(4))

    def page_dir(self, pid) -> Path:
        return self.root / "pages" / safe_name(pid, "page")

    def page_json_path(self, pid) -> Path:
        return self.page_dir(pid) / "page.json"

    def boxes_path(self, pid) -> Path:
        return self.page_dir(pid) / "boxes.json"

    def refcheck_path(self, pid) -> Path:
        return self.page_dir(pid) / "refcheck.json"

    def proposals_path(self, pid) -> Path:
        return self.page_dir(pid) / "proposals.json"

    def preview_path(self, pid) -> Path:
        return self.page_dir(pid) / "preview.png"

    def src(self, pid) -> str:
        return str(self.get_page(pid).get("src") or "")

    def add_paths(self, paths: Iterable) -> tuple:
        """Add source files to the project.  Returns (added pids, skipped records)."""
        added, skipped = [], []
        with self._lock:
            seen = {path_key(e.get("src")) for e in self.project.get("pages") or []}
            for raw in paths or []:
                try:
                    p = os.path.abspath(os.path.expanduser(str(raw)))
                except (OSError, ValueError):
                    skipped.append({"path": str(raw), "reason": "路径不合法"})
                    continue
                if not os.path.isfile(p):
                    skipped.append({"path": p, "reason": "文件不存在"})
                    continue
                if not is_image(p):
                    skipped.append({"path": p, "reason": "不是支持的图片格式（TIF/TIFF/PNG/JPG 等）"})
                    continue
                key = path_key(p)
                if key in seen:
                    skipped.append({"path": p, "reason": "已经在项目里（按真实路径去重）"})
                    continue
                pid = self._allocate_pid()
                entry = {
                    "id": pid,
                    "name": os.path.basename(p),
                    "src": p,
                    "status": STATUS_PENDING,
                    "error": "",
                    "width": 0,
                    "height": 0,
                    "n_boxes": 0,
                    "reviewed": False,
                    "ai_refined": False,
                    "created": _now(),
                }
                self.project.setdefault("pages", []).append(entry)
                seen.add(key)
                added.append(pid)
            if added:
                self._save_project()
        return added, skipped

    def add_folder(self, folder, pattern: str = "", recursive: bool = False) -> tuple:
        folder = os.path.abspath(os.path.expanduser(str(folder or "")))
        if not os.path.isdir(folder):
            raise StoreError("文件夹不存在：%s" % folder)
        pats = [p.strip() for p in re.split(r"[;,|\s]+", str(pattern or "")) if p.strip()]
        if not pats:
            pats = list(DEFAULT_PATTERNS)
        if any("**" in p for p in pats):
            recursive = True
        found = []
        try:
            if recursive:
                walker = os.walk(folder)
                for dirpath, dirnames, filenames in walker:
                    dirnames.sort()
                    for fn in sorted(filenames):
                        full = os.path.join(dirpath, fn)
                        low = fn.lower()
                        for p in pats:
                            if fnmatch.fnmatch(low, p.lower()):
                                found.append(full)
                                break
            else:
                for fn in sorted(os.listdir(folder)):
                    full = os.path.join(folder, fn)
                    if not os.path.isfile(full):
                        continue
                    low = fn.lower()
                    for p in pats:
                        if fnmatch.fnmatch(low, p.lower()):
                            found.append(full)
                            break
        except OSError as exc:
            raise StoreError("读取文件夹失败：%s" % exc)
        if not found:
            raise StoreError("文件夹里没有匹配的图片：%s（匹配规则 %s）" % (folder, ", ".join(pats)))
        return self.add_paths(found)

    def remove_page(self, pid) -> dict:
        pid = str(pid)
        with self._lock:
            pages = self.project.get("pages") or []
            entry = None
            for i, e in enumerate(pages):
                if e["id"] == pid:
                    entry = pages.pop(i)
                    break
            if entry is None:
                raise StoreError("页面不存在：%s" % pid)
            self._save_project()
        d = self.page_dir(pid)
        try:
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
        return entry

    # -------------------------------------------------------- page.json / boxes
    def load_page_json(self, pid) -> dict:
        data = read_json(self.page_json_path(pid), None)
        return data if isinstance(data, dict) else {}

    def save_page_json(self, pid, page: dict) -> dict:
        page = dict(page or {})
        page["width"] = _int(page.get("width"), 0)
        page["height"] = _int(page.get("height"), 0)
        page.setdefault("columns", [])
        page.setdefault("glyphs", [])
        self.page_dir(pid).mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.page_json_path(pid), page)
        self.update_page(pid, width=page["width"], height=page["height"], error="")
        return page

    def dims(self, pid) -> tuple:
        page = self.load_page_json(pid)
        w = _int(page.get("width"), 0)
        h = _int(page.get("height"), 0)
        if w <= 0 or h <= 0:
            e = self.get_page(pid)
            w = w or _int(e.get("width"), 0)
            h = h or _int(e.get("height"), 0)
        return w, h

    def boxes(self, pid) -> list:
        """Authoritative boxes: boxes.json when present, else page.json glyphs."""
        data = read_json(self.boxes_path(pid), None)
        if isinstance(data, dict) and isinstance(data.get("boxes"), list):
            boxes, _ = normalize_boxes(data["boxes"])
            return boxes
        page = self.load_page_json(pid)
        boxes, _ = boxes_from_glyphs(page.get("glyphs") or [], page.get("width"), page.get("height"))
        return boxes

    def has_boxes_file(self, pid) -> bool:
        return self.boxes_path(pid).is_file()

    def save_boxes(self, pid, boxes, *, width=None, height=None) -> tuple:
        """Validate + renumber + persist boxes.json, refresh derived page fields."""
        entry = self.get_page(pid)
        if not entry:
            raise StoreError("页面不存在：%s" % pid)
        w = _int(width, 0) or _int(entry.get("width"), 0)
        h = _int(height, 0) or _int(entry.get("height"), 0)
        bw, bh = self.dims(pid)
        w = w or bw
        h = h or bh
        clean, warns = normalize_boxes(boxes, w or None, h or None)
        payload = {"boxes": clean, "updated": _now()}
        self.page_dir(pid).mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.boxes_path(pid), payload)
        status = self._derive_status(entry, clean, w, h)
        reviewed = bool(clean) and all(b["review"] == 1 for b in clean)
        self.update_page(pid, n_boxes=len(clean), reviewed=reviewed, status=status)
        return clean, warns

    def write_boxes_if_missing(self, pid, boxes, *, width=None, height=None) -> tuple:
        if self.has_boxes_file(pid):
            return self.boxes(pid), []
        return self.save_boxes(pid, boxes, width=width, height=height)

    def _derive_status(self, entry, boxes, width, height) -> str:
        if entry.get("error"):
            return STATUS_ERROR
        if not (width or entry.get("width")) or not (height or entry.get("height")):
            return STATUS_PENDING
        if not boxes:
            return STATUS_PROCESSED
        reviews = [b.get("review", 0) for b in boxes]
        if all(r == 1 for r in reviews):
            return STATUS_DONE
        if any(r != 0 for r in reviews):
            return STATUS_REVIEWING
        return STATUS_PROCESSED

    def mark_processed(self, pid, *, width=0, height=0, n_boxes=None, elapsed=None) -> dict:
        entry = self.get_page(pid)
        if not entry:
            raise StoreError("页面不存在：%s" % pid)
        boxes = self.boxes(pid)
        if n_boxes is None:
            n_boxes = len(boxes)
        status = self._derive_status(entry, boxes, width or entry.get("width"),
                                     height or entry.get("height"))
        fields = {"width": width or entry.get("width") or 0,
                  "height": height or entry.get("height") or 0,
                  "n_boxes": n_boxes, "status": status, "error": "",
                  "reviewed": bool(boxes) and all(b.get("review") == 1 for b in boxes)}
        if elapsed is not None:
            fields["processed_seconds"] = round(float(elapsed), 2)
        # processed_seconds is not an editable field through update_page
        env = {k: v for k, v in fields.items() if k != "processed_seconds"}
        out = self.update_page(pid, **env)
        if elapsed is not None:
            out["processed_seconds"] = round(float(elapsed), 2)
        return out

    def set_error(self, pid, message: str, *, status: str = STATUS_ERROR) -> dict:
        return self.update_page(pid, error=str(message or "未知错误"), status=status)

    # ---------------------------------------------------------------- preview
    def has_preview(self, pid) -> bool:
        return self.preview_path(pid).is_file()

    def save_preview_png(self, pid, png_bytes: bytes) -> Path:
        p = self.preview_path(pid)
        atomic_write_bytes(p, png_bytes)
        return p

    # -------------------------------------------------------------- proposals
    def refcheck(self, pid) -> dict:
        data = read_json(self.refcheck_path(pid), None)
        return data if isinstance(data, dict) else {}

    def save_refcheck(self, pid, result: dict) -> dict:
        self.page_dir(pid).mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.refcheck_path(pid), result)
        return result

    def proposals(self, pid) -> dict:
        data = read_json(self.proposals_path(pid), None)
        if not isinstance(data, dict):
            return {"proposals": [], "meta": {}}
        props = data.get("proposals")
        if not isinstance(props, list):
            props = []
        clean = []
        for i, p in enumerate(props):
            if not isinstance(p, dict):
                continue
            q = dict(p)
            q["index"] = i
            q.setdefault("status", "pending")
            clean.append(q)
        data["proposals"] = clean
        if not isinstance(data.get("meta"), dict):
            data["meta"] = {}
        return data

    def save_proposals(self, pid, proposals, meta=None) -> dict:
        payload = {"proposals": list(proposals or []), "meta": dict(meta or {}),
                   "updated": _now()}
        self.page_dir(pid).mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.proposals_path(pid), payload)
        self.update_page(pid, n_proposals=len(payload["proposals"]))
        return payload

    # ------------------------------------------------------------------ state
    def state_pages(self) -> list:
        out = []
        with self._lock:
            for e in self.project.get("pages") or []:
                d = dict(e)
                d["status_label"] = STATUS_LABELS.get(d.get("status"), d.get("status"))
                d["has_preview"] = self.has_preview(d["id"])
                out.append(d)
        return out


# ---------------------------------------------------------------- self test
def _selftest() -> int:
    import tempfile
    from PIL import Image

    ok = fail = 0
    def check(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("PASS", label, flush=True)
        else:
            fail += 1
            print("FAIL", label, flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="yi_store_"))
    try:
        src_dir = tmp / "in"
        src_dir.mkdir()
        files = []
        for i in range(3):
            p = src_dir / ("scan_%d.TIF" % i)
            Image.new("RGB", (40, 30), (200, 190, 180)).save(p)
            files.append(str(p))
        other = src_dir / "notes.txt"
        other.write_text("x", encoding="utf-8")

        st = ProjectStore(tmp / "proj")
        added, skipped = st.add_paths(files + [str(other), str(tmp / "missing.tif")])
        check(len(added) == 3, "add 3 files")
        check(len(skipped) == 2, "skip txt + missing")
        added2, skipped2 = st.add_paths(files)
        check(added2 == [] and len(skipped2) == 3, "dedupe by real path")
        check(all(len(pid) >= 8 for pid in added), "pid format")
        check(len(set(added)) == 3, "pids unique")

        pid = added[0]
        e = st.get_page(pid)
        check(e["status"] == STATUS_PENDING and e["src"] == files[0], "entry pending")

        # page.json + initial boxes
        page = {"width": 100, "height": 80, "columns": [{"idx": 0, "x0": 0, "x1": 100, "w": 100}],
                "glyphs": [{"id": 1, "x0": 5, "y0": 5, "x1": 30, "y1": 35, "flags": ["clipped"]},
                           {"id": 2, "x0": 40, "y0": 10, "x1": 70, "y1": 40}]}
        st.save_page_json(pid, page)
        boxes = st.boxes(pid)
        check(len(boxes) == 2 and boxes[0]["id"] == 1 and boxes[0]["flags"] == ["clipped"],
              "boxes derived from glyphs")
        check(st.get_page(pid)["width"] == 100, "width recorded")

        clean, warns = st.save_boxes(pid, boxes)
        check(st.has_boxes_file(pid) and len(clean) == 2, "boxes.json written")
        check(st.get_page(pid)["status"] == STATUS_PROCESSED, "status processed")

        # normalization: bad entries dropped, reversed coords fixed, ids renumbered
        raw = [{"x0": 90, "y0": 60, "x1": 10, "y1": 20},
               {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
               {"x0": 5, "y0": 5, "x1": 9, "y1": 9, "review": 9, "flags": "tiny;oversize"}]
        clean, warns = st.save_boxes(pid, raw)
        check(len(clean) == 2, "degenerate box dropped")
        check(clean[0]["x0"] == 10 and clean[0]["x1"] == 90, "reversed coords swapped")
        check(clean[1]["id"] == 2 and clean[1]["review"] == 2, "renumber + review clamp")
        check(clean[1]["flags"] == ["tiny", "oversize"], "flags parsed from string")
        check(st.get_page(pid)["status"] == STATUS_REVIEWING, "status reviewing")

        all_ok = [{"x0": 1, "y0": 2, "x1": 20, "y1": 30, "review": 1}]
        st.save_boxes(pid, all_ok)
        check(st.get_page(pid)["status"] == STATUS_DONE, "status done")

        st.mark_processed(pid, width=100, height=80, n_boxes=1, elapsed=1.5)
        check(st.get_page(pid)["n_boxes"] == 1, "mark_processed")

        st.save_proposals(pid, [{"box_id": 1, "action": "keep", "reason": "ok"}])
        pr = st.proposals(pid)
        check(len(pr["proposals"]) == 1 and pr["proposals"][0]["index"] == 0,
              "proposals read back")

        st.save_preview_png(pid, b"PNGDATA")
        check(st.has_preview(pid), "preview written")

        st.remove_page(pid)
        check(not st.has_page(pid) and not st.page_dir(pid).exists(), "remove page + cache")
        st2 = ProjectStore(tmp / "proj")
        check(len(st2.list_pages()) == 2, "project.json persisted")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("store selftest: %d passed, %d failed" % (ok, fail), flush=True)
    return 0 if fail == 0 else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="app.store", description="项目存储（ProjectStore）")
    ap.add_argument("--selftest", action="store_true", help="跑离线自测")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
