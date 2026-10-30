"""Batch CLI: page scan(s) -> page.json -> glyph tiles + contact sheet.

Usage
-----
    python tools/yi_segment.py --input <tif|dir> --out <dir> [--jobs N] [--resume]
                               [--limit N] [--scale F] [--order reading|visual]
                               [--no-tiles] [--pad-ratio F] [--min-pad N]
                               [--fmt png] [--segment-module module|file.py]

Per page:
    load_gray -> segment_v1.segment_page -> <out>/<page>/page.json
              -> export.export_tiles -> <out>/<page>/tiles/{0001.png,...,index.csv,sheet.png}

Options worth knowing
---------------------
--resume   skip pages whose page.json + tiles/index.csv already exist.  If only
           page.json is present (crash during tiling) it re-uses the json and
           just re-exports, so a crash does not redo segmentation.
--limit N  process only the first N pages (smoke test).
--scale F  segment on a F-times downscaled page; boxes are mapped back to the
           original page, so tiles are always cut from full resolution.
--jobs N   spawn workers (default: 1 for a single file, up to 4 for a folder,
           because one 70 MP page peaks around 1-1.5 GB per worker).
--segment-module
           override the algorithm module: a dotted module name or a path to a
           .py file (used to smoke-test the exporter before segment_v1 exists).
           The env var YI_SEGMENT_MODULE sets the default.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import multiprocessing as mp
import os
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# page stems are Chinese on this material; make progress lines survive any console
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np  # noqa: E402

from yi_core import export as yi_export  # noqa: E402
from yi_core.common import load_gray, load_rgb  # noqa: E402

DEFAULT_SEGMENT_MODULE = os.environ.get("YI_SEGMENT_MODULE") or "yi_core.segment_v1"
PAGE_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".webp")
DEFAULT_JOBS_FOLDER = 4


# ----------------------------------------------------------------- helpers
def natural_key(path: str):
    """Sort 0001.tif, 0002.tif, 10.tif in human order."""
    name = os.path.basename(path).lower()
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def collect_pages(inp: str, pattern: str | None = None) -> list:
    if os.path.isfile(inp):
        return [os.path.abspath(inp)]
    if not os.path.isdir(inp):
        raise SystemExit(f"--input not found: {inp}")
    rx = re.compile(pattern, re.IGNORECASE) if pattern else None
    out = []
    for name in os.listdir(inp):
        p = os.path.join(inp, name)
        if not os.path.isfile(p):
            continue
        if rx is not None:
            if not rx.search(name):
                continue
        elif os.path.splitext(name)[1].lower() not in PAGE_EXTS:
            continue
        out.append(os.path.abspath(p))
    out.sort(key=natural_key)
    return out


def load_segment_module(spec: str):
    """Import the segmenter: a dotted module name, or a path to a .py file."""
    if not spec:
        spec = DEFAULT_SEGMENT_MODULE
    looks_like_path = spec.endswith(".py") or ("/" in spec) or ("\\" in spec)
    if looks_like_path:
        path = spec if os.path.isabs(spec) else os.path.abspath(spec)
        if not os.path.exists(path):
            raise SystemExit(f"segment module file not found: {path}")
        modname = "_yi_seg_" + re.sub(r"\W+", "_", os.path.splitext(os.path.basename(path))[0])
        spec_obj = importlib.util.spec_from_file_location(modname, path)
        if spec_obj is None or spec_obj.loader is None:
            raise SystemExit(f"cannot load segment module: {path}")
        mod = importlib.util.module_from_spec(spec_obj)
        sys.modules[modname] = mod
        spec_obj.loader.exec_module(mod)
        return mod
    if spec not in sys.modules:
        importlib.import_module(spec)
    mod = sys.modules[spec]
    if not hasattr(mod, "segment_page"):
        raise SystemExit(f"segment module {spec!r} has no segment_page()")
    return mod


def _clean_tiles(tiles_dir: str) -> int:
    """Remove tile files written by an earlier run (never page.json/overlay.png).

    Only tile-looking names are touched: ^\d+\.[A-Za-z0-9]+$ plus index.csv /
    sheet.png.  Returns how many files were removed.
    """
    import re as _re
    if not os.path.isdir(tiles_dir):
        return 0
    tile_re = _re.compile(r"^\d+\.[A-Za-z0-9]+$")
    dropped = 0
    for name in os.listdir(tiles_dir):
        p = os.path.join(tiles_dir, name)
        if not os.path.isfile(p):
            continue
        if name in ("index.csv", "sheet.png") or tile_re.match(name):
            try:
                os.remove(p)
                dropped += 1
            except OSError:
                pass
    return dropped


def load_json_any(path: str):
    """Load a JSON page dict whatever the text encoding (utf-8 / gbk / bom)."""
    with open(path, "rb") as f:
        raw = f.read()
    last = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "cp936"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, ValueError) as exc:
            last = exc
    raise ValueError("cannot decode/parse %s: %s" % (path, last))


def _write_json_atomic(path: str, obj) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def _rescale_page(page: dict, inv: float, width: int, height: int) -> dict:
    """Map boxes from the downscaled segmentation back into original pixels."""
    def sx(v):
        return int(round(float(v) * inv))

    glyphs = []
    for g in page.get("glyphs") or []:
        x0, y0 = sx(g["x0"]), sx(g["y0"])
        x1, y1 = sx(g["x1"]), sx(g["y1"])
        x0 = max(0, min(x0, width - 1))
        y0 = max(0, min(y0, height - 1))
        x1 = max(x0 + 1, min(x1, width))
        y1 = max(y0 + 1, min(y1, height))
        g = dict(g)
        g.update({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "w": x1 - x0, "h": y1 - y0})
        glyphs.append(g)
    cols = []
    for c in page.get("columns") or []:
        c = dict(c)
        c["x0"] = max(0, min(sx(c["x0"]), width - 1))
        c["x1"] = max(c["x0"] + 1, min(sx(c["x1"]), width))
        if "w" in c:
            c["w"] = c["x1"] - c["x0"]
        cols.append(c)
    page = dict(page)
    page["glyphs"] = glyphs
    page["columns"] = cols
    page["width"] = int(width)
    page["height"] = int(height)
    return page


# ------------------------------------------------------------------ worker
def process_page(task: dict) -> dict:
    """Segment one page and export its tiles. Runs in a spawned worker."""
    t0 = time.time()
    path = task["path"]
    stem = task["stem"]
    page_dir = task["page_dir"]
    out_root = task["out_root"]
    res = {"index": task["index"], "stem": stem, "ok": False, "skipped": False,
           "page": os.path.relpath(page_dir, out_root), "glyphs": 0,
           "tiles": 0, "seconds": 0.0, "error": "", "log": ""}

    try:
        os.makedirs(page_dir, exist_ok=True)
        pj = os.path.join(page_dir, "page.json")
        tiles_dir = page_dir if task.get("flat") else os.path.join(page_dir, "tiles")
        need_tiles = not task["no_tiles"]
        have_page = os.path.isfile(pj)
        have_tiles = os.path.isfile(os.path.join(tiles_dir, "index.csv"))

        page = None
        pj_mode = task.get("page_json")
        if task["resume"] and have_page and not pj_mode:
            cached = load_json_any(pj)
            params = cached.get("params") or {}
            same_scale = abs(float(params.get("scale", 1.0)) - float(task["scale"])) < 1e-6
            same_mod = str(params.get("segment_module", task["segment_module"])) == str(task["segment_module"])
            if same_scale and same_mod:
                page = cached
                if need_tiles and have_tiles:
                    res.update(skipped=True, ok=True, glyphs=len(page.get("glyphs") or []))
                    return res
                res["reused_json"] = True   # tiling only
            else:
                diffs = []
                if not same_scale:
                    diffs.append("scale %s->%s" % (params.get("scale"), task["scale"]))
                if not same_mod:
                    diffs.append("module %s->%s" % (params.get("segment_module"), task["segment_module"]))
                res["restale"] = ", ".join(diffs) or "params changed"

        if page is None and pj_mode:
            # --page-json: trust an existing page dict (rgb is not re-segmented)
            page = load_json_any(pj_mode)
            page["source"] = page.get("source") or path
            page["name"] = stem
            params = dict(page.get("params") or {})
            params.setdefault("scale", float(task["scale"]))
            params["segment_module"] = "__page_json__"
            params["page_json"] = os.path.abspath(pj_mode)
            page["params"] = params
            _write_json_atomic(pj, page)
            res["reused_json"] = True
            res["from_json"] = True

        if page is None:
            seg = load_segment_module(task["segment_module"])
            gray = load_gray(path)
            height, width = gray.shape[:2]
            scale = float(task["scale"])
            if scale != 1.0:
                import cv2
                small = cv2.resize(
                    gray,
                    (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                    interpolation=cv2.INTER_AREA,
                )
                page = seg.segment_page(small)
                page = _rescale_page(page, 1.0 / scale, width, height)
            else:
                page = seg.segment_page(gray)
                page["width"], page["height"] = int(width), int(height)
            del gray  # free ~70 MB before pulling the RGB page

            page["source"] = path
            page["name"] = stem
            params = dict(page.get("params") or {})
            params.setdefault("scale", scale)
            params.setdefault("segment_module", task["segment_module"])
            page["params"] = params
            _write_json_atomic(pj, page)

        res["glyphs"] = len(page.get("glyphs") or [])

        # Only trust pre-existing tiles when the json they came from was re-used;
        # a fresh segmentation always re-exports.  Without --resume we start from
        # a clean tiles dir so stale files from a previous run cannot linger.
        skip_export = bool(task["resume"] and have_tiles and res.get("reused_json")
                          and not res.get("from_json"))
        if need_tiles and not skip_export:
            # rewriting all tiles: drop leftovers from any previous run first
            # (only tile-like files; page.json / overlay.png are never touched)
            res["cleaned"] = _clean_tiles(tiles_dir)
            rgb = load_rgb(path)
            records = yi_export.export_tiles(
                rgb, page, tiles_dir,
                pad_ratio=float(task["pad_ratio"]),
                min_pad=int(task["min_pad"]),
                order=task["order"],
                fmt=task["fmt"],
                sheet=task["sheet"],
                sheet_cols=int(task["sheet_cols"]),
                cell=int(task["cell"]),
                sheet_header=(("%s    %d glyphs    order=%s"
                               % (stem, len(page.get("glyphs") or []), task["order"]))
                              if task["sheet_header"] else None),
            )
            res["tiles"] = len(records)
            try:
                import re as _re2
                tile_re2 = _re2.compile(r"^\d+\.[A-Za-z0-9]+$")
                res["dir_tiles"] = sum(1 for n in os.listdir(tiles_dir)
                                       if tile_re2.match(n))
            except OSError:
                res["dir_tiles"] = -1
            try:
                _write_json_atomic(os.path.join(page_dir, "export_params.json"), {
                    "source_image": path,
                    "source_json": (os.path.abspath(pj_mode) if pj_mode else None),
                    "glyphs": res["glyphs"],
                    "tiles": res["tiles"],
                    "pad_ratio": float(task["pad_ratio"]),
                    "min_pad": int(task["min_pad"]),
                    "order": task["order"],
                    "fmt": task["fmt"],
                    "scale": float(task["scale"]),
                    "sheet": bool(task["sheet"]),
                })
            except OSError:
                pass
            del rgb

        res["ok"] = True
    except Exception as exc:  # noqa: BLE001 - report, do not kill the batch
        res["error"] = f"{type(exc).__name__}: {exc}"
        try:
            os.makedirs(page_dir, exist_ok=True)
            log = os.path.join(page_dir, "error.log")
            with open(log, "w", encoding="utf-8") as f:
                f.write(f"page: {path}\n")
                f.write(f"segment: {task['segment_module']}\n\n")
                f.write(traceback.format_exc())
            res["log"] = os.path.relpath(log, out_root)
        except Exception:
            pass
    res["seconds"] = round(time.time() - t0, 2)
    return res


# -------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yi_segment",
        description="Yi manuscript page scans -> per-glyph tiles (batch, resumable).",
    )
    p.add_argument("--input", required=True, help="a TIF page or a directory of pages")
    p.add_argument("--out", required=True, help="output root directory")
    p.add_argument("--jobs", type=int, default=0,
                   help="worker processes (default: 1 for one file, up to %d for a folder)" % DEFAULT_JOBS_FOLDER)
    p.add_argument("--resume", action="store_true",
                   help="skip pages that already have page.json + tiles/index.csv")
    p.add_argument("--limit", type=int, default=0, help="process at most N pages")
    p.add_argument("--scale", type=float, default=1.0,
                   help="segment on a downscaled page (coords mapped back); default 1.0")
    p.add_argument("--order", default="reading", choices=("reading", "visual", "id"),
                   help="tile numbering: reading = columns right-to-left (default), "
                        "visual = columns left-to-right, id = follow page-dict glyph ids")
    p.add_argument("--no-tiles", action="store_true", help="only write page.json")
    p.add_argument("--pad-ratio", type=float, default=0.10)
    p.add_argument("--min-pad", type=int, default=6)
    p.add_argument("--fmt", default="png", choices=("png", "jpg", "jpeg", "webp"),
                   help="tile image format (default png)")
    p.add_argument("--jpg", action="store_true", help="shorthand for --fmt jpg")
    p.add_argument("--flat", action="store_true",
                   help="write tiles/index.csv/sheet.png straight into <out>/<page>/ "
                        "instead of <out>/<page>/tiles/")
    p.add_argument("--no-sheet", action="store_true", help="skip sheet.png")
    p.add_argument("--sheet-header", action="store_true",
                   help="add a one-line header (page + count) to sheet.png")
    p.add_argument("--sheet-cols", type=int, default=13)
    p.add_argument("--cell", type=int, default=150)
    p.add_argument("--segment-module", default=DEFAULT_SEGMENT_MODULE,
                   help="module name or .py path (default: %s)" % DEFAULT_SEGMENT_MODULE)
    p.add_argument("--pattern", default=None,
                   help="regex for file names when --input is a directory")
    p.add_argument("--page-json", default=None,
                   help="re-use an existing page dict instead of segmenting: a .json file "
                        "(single input page) or a directory holding <stem>.json")
    p.add_argument("--page-name", default=None,
                   help="output dir name for a single input page (e.g. page1 gives out/page1)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.jpg and args.fmt == "png":
        args.fmt = "jpg"
    if args.jobs < 0:
        raise SystemExit("--jobs must be >= 0")
    if not (0.01 <= args.scale <= 4.0):
        raise SystemExit("--scale must be in [0.01, 4.0]")

    all_pages = collect_pages(args.input, args.pattern)
    n_all = len(all_pages)
    pages = all_pages[: args.limit] if (args.limit and args.limit > 0) else all_pages
    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)
    if not pages:
        print(f"no pages found under {args.input!r}", flush=True)
        return 2

    # fail fast on a broken segmenter instead of crashing every worker
    # (skipped with --page-json: that path never calls segment_page)
    if not args.page_json:
        try:
            load_segment_module(args.segment_module)
        except SystemExit:
            raise
        except Exception as exc:
            raise SystemExit(f"cannot import segment module {args.segment_module!r}: {exc}")

    if args.jobs and args.jobs > 0:
        jobs = args.jobs
    elif os.path.isdir(args.input):
        jobs = min(DEFAULT_JOBS_FOLDER, max(1, (os.cpu_count() or 4) - 1))
    else:
        jobs = 1
    jobs = max(1, min(jobs, len(pages)))

    if args.page_name and len(pages) > 1:
        raise SystemExit("--page-name only makes sense for a single input page")
    if args.page_json and os.path.isfile(args.page_json) and len(pages) > 1:
        raise SystemExit("--page-json FILE needs a single input page (use a directory for batches)")
    if args.page_json and not os.path.exists(args.page_json):
        raise SystemExit("--page-json not found: %s" % args.page_json)

    tasks = []
    for i, path in enumerate(pages, start=1):
        stem = os.path.splitext(os.path.basename(path))[0]
        pj_src = None
        if args.page_json:
            if os.path.isdir(args.page_json):
                cand = os.path.join(args.page_json, stem + ".json")
                if not os.path.isfile(cand):
                    raise SystemExit("--page-json dir has no %s" % (stem + ".json"))
                pj_src = os.path.abspath(cand)
            else:
                pj_src = os.path.abspath(args.page_json)
        tasks.append({
            "index": i, "path": path, "stem": stem,
            "page_json": pj_src,
            "page_dir": os.path.join(out_root, args.page_name if (args.page_name and len(pages) == 1) else stem),
            "out_root": out_root,
            "resume": bool(args.resume),
            "scale": float(args.scale),
            "order": args.order,
            "no_tiles": bool(args.no_tiles),
            "pad_ratio": float(args.pad_ratio),
            "min_pad": int(args.min_pad),
            "fmt": args.fmt,
            "flat": bool(args.flat),
            "sheet": not args.no_sheet,
            "sheet_header": bool(args.sheet_header),
            "sheet_cols": int(args.sheet_cols),
            "cell": int(args.cell),
            "segment_module": args.segment_module,
        })

    print("input   : %s" % os.path.abspath(args.input), flush=True)
    print("pages   : %d%s" % (len(pages), "" if len(pages) == n_all else " (limited from %d)" % n_all), flush=True)
    print("out     : %s" % out_root, flush=True)
    print("segment : %s" % args.segment_module, flush=True)
    print("jobs    : %d   scale: %g   order: %s   tiles: %s"
          % (jobs, args.scale, args.order, "off" if args.no_tiles else "on"), flush=True)

    total = len(tasks)
    skipped = failed = tiled = 0
    glyphs_total = 0
    t_start = time.time()

    def report(res: dict) -> None:
        nonlocal skipped, failed, tiled, glyphs_total
        tag = "[%d/%d]" % (res["index"], total)
        if res["skipped"]:
            skipped += 1
            print("%s %-28s SKIP (page.json + tiles present)" % (tag, res["stem"]), flush=True)
            return
        if not res["ok"]:
            failed += 1
            print("%s %-28s FAILED: %s" % (tag, res["stem"], res["error"]), flush=True)
            if res["log"]:
                print("           traceback -> %s" % res["log"], flush=True)
            return
        tiled += 1
        glyphs_total += res["glyphs"]
        note = ""
        if res.get("restale"):
            note = "  (re-segmented: cached page.json was %s)" % res["restale"]
        if res.get("cleaned"):
            note += "  (cleaned %d stale tile files)" % res["cleaned"]
        if res.get("dir_tiles", res["tiles"]) != res["tiles"]:
            note += "  [WARN dir has %d tile files for %d glyphs]" % (res["dir_tiles"], res["tiles"])
        if res.get("from_json"):
            note += "  (from --page-json)"
        elif res.get("reused_json"):
            note += "  (re-used page.json)"
        print("%s %-28s glyphs=%-4d tiles=%-4d %6.2fs -> %s%s"
              % (tag, res["stem"], res["glyphs"], res["tiles"], res["seconds"], res["page"], note),
              flush=True)

    if jobs <= 1:
        for task in tasks:
            report(process_page(task))
    else:
        ctx = mp.get_context("spawn")
        pool = ctx.Pool(jobs, maxtasksperchild=1)
        try:
            for res in pool.imap_unordered(process_page, tasks):
                report(res)
        except KeyboardInterrupt:
            pool.terminate()
            print("interrupted", flush=True)
            return 130
        finally:
            pool.close()
            pool.join()

    dt = time.time() - t_start
    print("done    : %d ok, %d skipped, %d failed, %d glyphs, %.1fs (%.1f pages/min)"
          % (tiled, skipped, failed, glyphs_total, dt,
             (tiled + skipped + failed) / max(1e-6, dt / 60.0)), flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
