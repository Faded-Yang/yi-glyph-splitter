"""End-to-end API selftest against a real page (task A acceptance run).

    python -m app.selftest_api                 # default TIF, temp project
    python -m app.selftest_api --tif PATH --project DIR --keep
    python -m app.selftest_api --skip-tif       # only cheap checks

It drives the real FastAPI app through fastapi.testclient.TestClient and walks
the whole flow from SPEC-APP.md: state -> add TIF -> process (poll job) ->
page detail -> preview -> crop -> PUT boxes -> export -> jobs/cancel -> errors.
Exit code 0 only when every check passes.  Long steps (segmentation, export)
print flushed progress so a background run can be watched with job_output.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_TIF = (r"A:\Downloads\xwechat_files\wxid_0vb7dipk5onr22_9677\msg\file"
               r"\2026-10\彝族扫描3-0002_right.tif")
PAGE_W, PAGE_H = 6354, 11042


class Runner:
    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.skips = 0
        self.timings = {}

    def check(self, cond, label, extra=""):
        if cond:
            self.ok += 1
            print("PASS  %s%s" % (label, ("  [" + extra + "]") if extra else ""), flush=True)
            return True
        self.fail += 1
        print("FAIL  %s%s" % (label, ("  [" + extra + "]") if extra else ""), flush=True)
        return False

    def skip(self, label, why):
        self.skips += 1
        print("SKIP  %s  (%s)" % (label, why), flush=True)

    def step(self, title):
        print("\n== %s ==" % title, flush=True)


def wait_job(client, runner, jid, timeout, label):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        r = client.get("/api/jobs/%s" % jid)
        if r.status_code != 200:
            raise RuntimeError("job %s: HTTP %s %s" % (jid, r.status_code, r.text[:200]))
        job = r.json()
        key = (job.get("state"), job.get("done"), job.get("total"), job.get("message"))
        if key != last:
            last = key
            elapsed = time.time() - t0
            print("      job %-6s %s/%s %-40s %.1fs" %
                  (job.get("state"), job.get("done"), job.get("total"),
                   (job.get("message") or "")[:40], elapsed), flush=True)
        if job.get("state") != "running":
            runner.timings[label] = round(time.time() - t0, 1)
            return job
        time.sleep(0.5)
    raise TimeoutError("记录 %s：任务 %s 超过 %.0fs 仍未结束" % (label, jid, timeout))


def png_size(data: bytes):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.selftest_api", description="后端端到端自测")
    ap.add_argument("--tif", default=DEFAULT_TIF)
    ap.add_argument("--project", default="")
    ap.add_argument("--keep", action="store_true", help="保留临时项目目录")
    ap.add_argument("--skip-tif", action="store_true", help="跳过需要真 TIF 的步骤")
    ap.add_argument("--timeout", type=float, default=420.0)
    args = ap.parse_args(argv)

    tmp = Path(args.project) if args.project else Path(tempfile.mkdtemp(prefix="yi_e2e_"))
    tmp.mkdir(parents=True, exist_ok=True)
    # Isolate config/logs/projects: this must happen before the first app.* call
    # that touches paths (AppConfig.load), so the real user config stays untouched.
    home = tmp / "_home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["YI_SEGMENT_HOME"] = str(home)
    os.environ["YI_SEGMENT_NO_DIALOG"] = "1"
    from fastapi.testclient import TestClient
    from . import server as server_mod

    runner = Runner()
    project_dir = tmp / "project"
    tif = str(args.tif)
    tif_ok = os.path.isfile(tif)
    if args.skip_tif:
        tif_ok = False
    print("project : %s" % project_dir, flush=True)
    print("tif     : %s (%s)" % (tif, "found" if tif_ok else "MISSING - TIF steps will be skipped"),
          flush=True)
    t_start = time.time()

    try:
        app = server_mod.create_app(str(project_dir))
        client = TestClient(app)

        # ------------------------------------------------------------- health
        runner.step("health / state")
        r = client.get("/api/health")
        runner.check(r.status_code == 200 and r.json().get("ok"), "GET /api/health")
        print("      health: %s" % json.dumps(r.json(), ensure_ascii=False), flush=True)
        r = client.get("/api/state")
        state = r.json()
        runner.check(r.status_code == 200, "GET /api/state")
        runner.check(state.get("pages") == [], "empty project has no pages")
        runner.check("settings" in state and "defaults" in state["settings"], "state.settings")
        ai_available = bool(r.json().get("ai_available"))
        print("      ai_available: %s" % ai_available, flush=True)

        # --------------------------------------------------------------- add
        runner.step("add pages (real TIF, dedupe)")
        if not tif_ok:
            runner.skip("pages/add with TIF", "TIF not found / --skip-tif")
            pid = None
            n_boxes = 0
        else:
            r = client.post("/api/pages/add", json={"paths": [tif, tif]})
            body = r.json()
            runner.check(r.status_code == 200 and len(body.get("added", [])) == 1,
                         "POST /api/pages/add added exactly 1", str(body.get("added")))
            runner.check(len(body.get("skipped", [])) == 1, "second add deduped by real path")
            pid = (body.get("added") or [None])[0]
            state = client.get("/api/state").json()
            page = state["pages"][0]
            runner.check(page["status"] == "pending", "new page status pending")
            runner.check(page["src"] == os.path.abspath(tif), "src recorded absolute")

            # --------------------------------------------------------- process
            runner.step("process page (segment_v1)")
            t0 = time.time()
            r = client.post("/api/pages/process", json={"ids": [pid]})
            runner.check(r.status_code == 200 and r.json().get("job_id"),
                         "POST /api/pages/process")
            job = wait_job(client, runner, r.json()["job_id"], args.timeout, "process")
            runner.check(job["state"] == "done", "process job done", job["state"])
            runner.check(job["done"] == 1 and job["total"] == 1, "job progress 1/1")
            res = (job.get("results") or [{}])[0]
            runner.check(res.get("ok") is True, "process result ok", str(res.get("error", "")))
            n_boxes = int(res.get("n_boxes") or 0)
            runner.check(n_boxes > 100, "segmented %d boxes" % n_boxes)
            print("      process seconds=%.1f timings=%s" %
                  (time.time() - t0, json.dumps(res.get("timings"), ensure_ascii=False)), flush=True)
            state = client.get("/api/state").json()
            page = state["pages"][0]
            runner.check(page["width"] == PAGE_W and page["height"] == PAGE_H,
                         "page size %dx%d" % (page["width"], page["height"]))
            runner.check(page["n_boxes"] == n_boxes, "state n_boxes matches")
            runner.check(page["status"] == "processed", "status processed", page["status"])

            # ------------------------------------------------------ page detail
            runner.step("page detail / preview / crop")
            r = client.get("/api/pages/%s" % pid)
            detail = r.json()
            runner.check(r.status_code == 200, "GET /api/pages/{pid}")
            boxes = detail["boxes"]
            runner.check(len(boxes) == n_boxes, "detail boxes == n_boxes")
            runner.check(detail["page"]["width"] == PAGE_W, "detail page.width")
            runner.check(all(k in boxes[0] for k in
                             ("id", "x0", "y0", "x1", "y1", "flags", "review", "note")),
                         "box fields complete")
            runner.check([b["id"] for b in boxes] == list(range(1, len(boxes) + 1)),
                         "box ids 1..N")

            r = client.get("/api/pages/%s/preview.png" % pid)
            runner.check(r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n",
                         "preview.png is PNG", "%d bytes" % len(r.content))
            if r.status_code == 200:
                w, h = png_size(r.content)
                runner.check(w == 1600, "preview width 1600", "%dx%d" % (w, h))
                runner.check(abs(h - round(PAGE_H * 1600.0 / PAGE_W)) <= 2,
                             "preview aspect kept")
            r = client.get("/api/pages/%s/preview.png?w=800&boxes=0" % pid)
            if r.status_code == 200:
                w2, _ = png_size(r.content)
                runner.check(w2 == 800, "preview?w=800 resized", str(w2))

            r = client.get("/api/pages/%s/crop?x0=100&y0=200&x1=400&y1=500&scale=2" % pid)
            runner.check(r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n",
                         "GET crop -> PNG")
            if r.status_code == 200:
                runner.check(png_size(r.content) == (600, 600), "crop scaled 2x",
                             str(png_size(r.content)))
            r = client.get("/api/pages/%s/crop?x0=5000&y0=10000&x1=9000&y1=12000" % pid)
            runner.check(r.status_code == 200, "crop clamped outside page")
            r = client.get("/api/pages/%s/crop?x0=100&y0=100&x1=100&y1=100" % pid)
            runner.check(r.status_code == 400 and "裁剪" in r.json()["detail"],
                         "degenerate crop -> 400 chinese")
            r = client.get("/api/pages/%s/crop" % pid)
            runner.check(r.status_code == 400, "crop without params -> 400")

            # -------------------------------------------------------- put boxes
            runner.step("PUT boxes (edit / validation / persistence)")
            edited = [dict(b) for b in boxes]
            b0 = edited[0]
            edited[0] = {"x0": b0["x0"] + 3, "y0": b0["y0"] + 3,
                         "x1": b0["x1"] - 3, "y1": b0["y1"] - 3,
                         "flags": list(b0["flags"]), "review": 1, "note": "自测编辑"}
            r = client.put("/api/pages/%s/boxes" % pid, json=edited)
            put = r.json()
            runner.check(r.status_code == 200, "PUT /api/pages/{pid}/boxes")
            runner.check(len(put["boxes"]) == n_boxes, "same box count")
            runner.check(put["boxes"][0]["x0"] == b0["x0"] + 3, "edit kept (x0+3)")
            runner.check(put["boxes"][0]["note"] == "自测编辑", "note kept")
            runner.check(put["boxes"][0]["review"] == 1, "review kept")
            runner.check(put["boxes"][0]["id"] == 1 and put["boxes"][-1]["id"] == n_boxes,
                         "ids renumbered 1..N")
            runner.check(put["status"] == "reviewing", "status -> reviewing", put["status"])
            runner.check(put["warnings"] == [], "no warnings for valid boxes", str(put["warnings"]))
            detail2 = client.get("/api/pages/%s" % pid).json()
            runner.check(detail2["boxes"][0]["x0"] == b0["x0"] + 3, "boxes persisted to disk")
            runner.check(detail2["boxes"][0]["review"] == 1, "review persisted")

            r = client.put("/api/pages/%s/boxes" % pid,
                           json={"boxes": edited + [{"x0": 0, "y0": 0, "x1": 1, "y1": 1},
                                                   {"x0": 90, "y0": 90, "x1": 10, "y1": 10}]})
            bad = r.json()
            runner.check(len(bad["boxes"]) == n_boxes + 1, "degenerate dropped, reversed kept")
            runner.check(len(bad["warnings"]) >= 1, "warnings reported", str(bad["warnings"]))

            # restore the original box table (needed for export expectations)
            r = client.put("/api/pages/%s/boxes" % pid, json=boxes)
            runner.check(r.status_code == 200 and len(r.json()["boxes"]) == n_boxes,
                         "restore original boxes")

            # --------------------------------------------------------- export
            runner.step("export page")
            out = tmp / "export"
            r = client.post("/api/pages/%s/export" % pid,
                            json={"out": str(out), "pad_ratio": 0.10, "min_pad": 6,
                                  "order": "visual", "fmt": "png"})
            runner.check(r.status_code == 200, "POST export -> job", r.text[:120])
            job = wait_job(client, runner, r.json()["job_id"], args.timeout, "export")
            runner.check(job["state"] == "done", "export job done", str(job.get("errors")))
            eres = (job.get("results") or [{}])[0]
            runner.check(eres.get("ok") is True, "export result ok", str(eres.get("error", "")))
            page_out = Path(eres.get("out") or "")
            n_tiles = int(eres.get("n_tiles") or 0)
            runner.check(n_tiles == n_boxes, "exported %d tiles == %d boxes" % (n_tiles, n_boxes))
            runner.check(page_out.is_dir(), "export dir exists", str(page_out))
            tiles = sorted(p.name for p in page_out.iterdir()
                           if p.suffix == ".png" and p.name != "sheet.png")
            runner.check(len(tiles) == n_boxes, "tile files on disk", str(len(tiles)))
            nums = sorted(int(p.split(".")[0]) for p in tiles if p.split(".")[0].isdigit())
            runner.check(nums == list(range(1, n_boxes + 1)),
                         "tile names zero-padded 1..N", "first=%s" % (tiles[0] if tiles else "-"))
            csv_path = page_out / "index.csv"
            rows = 0
            if csv_path.is_file():
                with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                    rows = sum(1 for _ in csv.DictReader(f))
            runner.check(rows == n_boxes, "index.csv rows == n_boxes", str(rows))
            runner.check((page_out / "sheet.png").is_file(), "sheet.png written")
            runner.check((page_out / "sheet.png").stat().st_size > 1000, "sheet.png not empty")
            # first tile really is the padded crop of the first box
            rec = None
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                rec = next(csv.DictReader(f))
            tile0 = page_out / rec["filename"]
            runner.check(tile0.is_file(), "index.csv filename exists")
            if tile0.is_file():
                runner.check(png_size(tile0.read_bytes()) == (int(rec["w"]), int(rec["h"])),
                             "tile size == index.csv w/h")
            # second export with a different order/fmt must clean old tiles
            r = client.post("/api/pages/%s/export" % pid,
                            json={"out": str(out), "order": "id", "fmt": "jpg"})
            job = wait_job(client, runner, r.json()["job_id"], args.timeout, "export-jpg")
            runner.check(job["state"] == "done", "export jpg done", str(job.get("errors")))
            jpgs = [p for p in page_out.iterdir() if p.suffix == ".jpg"]
            stale_png = [p for p in page_out.iterdir()
                         if p.suffix == ".png" and p.name != "sheet.png"]
            runner.check(len(jpgs) == n_boxes, "jpg tiles", str(len(jpgs)))
            runner.check(stale_png == [], "stale png tiles cleaned", str(len(stale_png)))
            # project-wide export
            r = client.post("/api/export", json={"out": str(tmp / "export_all")})
            job = wait_job(client, runner, r.json()["job_id"], args.timeout, "export-all")
            runner.check(job["state"] == "done" and
                         (job.get("results") or [{}])[0].get("ok") is True,
                         "POST /api/export (project wide)")

            # ----------------------------------------------------- cancel test
            runner.step("job cancel")
            r = client.post("/api/pages/process", json={"ids": "all", "force": True})
            jid = r.json()["job_id"]
            time.sleep(0.4)
            rc = client.post("/api/jobs/%s/cancel" % jid)
            runner.check(rc.status_code == 200 and rc.json().get("ok") is True,
                         "cancel request accepted")
            job = wait_job(client, runner, jid, 120, "cancel")
            runner.check(job["state"] == "canceled", "job canceled", job["state"])
            runner.check(client.get("/api/pages/%s" % pid).status_code == 200,
                         "page still readable after cancel")
            runner.check(client.get("/api/state").json()["pages"][0]["status"] != "error",
                         "cancel did not mark page error")

            # ------------------------------------------------ errors / bad input
            runner.step("errors / bad input")
            r = client.get("/api/pages/nope")
            runner.check(r.status_code == 404 and "不存在" in r.json()["detail"],
                         "unknown page -> 404 chinese")
            r = client.get("/api/jobs/nope")
            runner.check(r.status_code == 404, "unknown job -> 404")
            r = client.get("/api/nope")
            runner.check(r.status_code == 404 and r.json()["detail"].startswith("接口不存在"),
                         "unknown api -> json 404")
            r = client.get("/api/pages/%s/crop?x0=abc&y0=1&x1=2&y1=3" % pid)
            runner.check(r.status_code == 422 and "不合法" in r.json()["detail"],
                         "bad query param -> chinese 422", r.text[:120])
            r = client.post("/api/pages/add", json={"nonsense": 1})
            runner.check(r.status_code == 400, "add without paths/folder -> 400")
            r = client.put("/api/pages/%s/boxes" % pid, json=None)
            runner.check(r.status_code in (400, 422), "PUT boxes with empty body -> error")
            r = client.post("/api/pages/%s/export" % pid, json={"out": str(tmp / "x"), "fmt": "bogus"})
            job = wait_job(client, runner, r.json()["job_id"], 120, "export-bogus")
            runner.check(job["state"] == "done", "bogus fmt normalized, export still ok",
                         str(job.get("errors")))

            # ------------------------------------------------------------ AI
            runner.step("AI proposals (mock / no key)")
            pr = client.get("/api/pages/%s/proposals" % pid)
            runner.check(pr.status_code == 200 and isinstance(pr.json().get("proposals"), list),
                         "GET proposals empty ok")
            if ai_available:
                r = client.post("/api/pages/%s/ai-refine" % pid,
                                json={"ids": "suspect", "mode": "suspect"})
                job = wait_job(client, runner, r.json()["job_id"], 240, "ai-refine")
                if job["state"] == "done":
                    res = (job.get("results") or [{}])[0]
                    n_props = int(res.get("n_proposals") or 0)
                    runner.check(n_props >= 0, "ai-refine produced %d proposals" % n_props)
                    pr = client.get("/api/pages/%s/proposals" % pid).json()
                    runner.check(len(pr["proposals"]) == n_props, "proposals.json matches job")
                    if n_props:
                        first = pr["proposals"][0]
                        runner.check("box_id" in first and "action" in first
                                     and "after" in first and first["status"] == "pending",
                                     "proposal fields", str(first.get("action")))
                        r = client.post("/api/pages/%s/proposals" % pid,
                                        json={"apply_all": True})
                        runner.check(r.status_code == 200, "POST proposals apply_all")
                        runner.check(isinstance(r.json().get("boxes"), list),
                                     "boxes returned after apply")
                        props_after = client.get("/api/pages/%s/proposals" % pid).json()
                        runner.check(all(p["status"] in ("accepted", "rejected")
                                         for p in props_after["proposals"]),
                                     "proposal statuses updated")
                else:
                    runner.check(job["state"] == "error" and
                                 any("AI" in e or "api" in e.lower() or "key" in e.lower()
                                     for e in job.get("errors") or []),
                                 "ai-refine error is explained", str(job.get("errors")))
            else:
                runner.skip("ai-refine", "app/ai_refine.py not importable yet")
                r = client.post("/api/pages/%s/ai-refine" % pid, json={"ids": "all"})
                runner.check(r.status_code == 200, "ai-refine accepted while module missing")
                job = wait_job(client, runner, r.json()["job_id"], 60, "ai-refine-missing")
                runner.check(job["state"] == "error" and job["errors"],
                             "missing ai module -> job error (no crash)")

            # ------------------------------------------------------------ delete
            runner.step("delete page")
            r = client.delete("/api/pages/%s" % pid)
            runner.check(r.status_code == 200 and r.json()["pages"] == [],
                         "DELETE /api/pages/{pid}")
            runner.check(not (project_dir / "pages" / pid).exists(), "page cache removed")
            runner.check(os.path.isfile(tif), "source TIF untouched")
        # ------------------------------------------------------------ project
        runner.step("project.json")
        pj = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        runner.check(pj.get("version") == 1, "project.json version 1")
        runner.check(isinstance(pj.get("pages"), list), "project.json pages list")
        runner.check(pj.get("name"), "project name")
        print("      project.json: %s" %
              json.dumps({k: v for k, v in pj.items() if k != "pages"}, ensure_ascii=False),
              flush=True)

        # ------------------------------------------------------------ final state
        runner.step("final state")
        state = client.get("/api/state").json()
        runner.check(state["project"]["path"] == str(project_dir.resolve()),
                     "state project path")
        print("      backend state: pages=%d files_present=%s" %
              (len(state["pages"]), (project_dir / "project.json").is_file()), flush=True)
    finally:
        os.environ.pop("YI_SEGMENT_NO_DIALOG", None)
        os.environ.pop("YI_SEGMENT_HOME", None)
        elapsed = time.time() - t_start
        print("\n==== summary ====", flush=True)
        print("checks: %d passed, %d failed, %d skipped | total %.1fs"
              % (runner.ok, runner.fail, runner.skips, elapsed), flush=True)
        print("job timings: %s" % json.dumps(runner.timings, ensure_ascii=False), flush=True)
        print("project dir: %s" % project_dir, flush=True)
        if args.keep or not args.project:
            print("kept: %s" % project_dir if args.keep else
                  "temporary project kept for inspection: %s" % project_dir, flush=True)
        if not args.keep:
            pass  # leave the temp dir: the caller may want to inspect tiles
    return 0 if runner.fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
