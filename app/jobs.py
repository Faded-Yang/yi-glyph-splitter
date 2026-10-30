"""JobManager: background worker threads with progress + cancel.

A job is a plain dict-ish object handed to a callable; the callable updates
progress and can raise JobCanceled (or call job.check()) to stop early.
Everything the API returns is JSON serialisable.

    jid = mgr.submit("process", fn, total=3, message="排队中")
    mgr.get(jid) -> {"id","kind","state","done","total","message","errors",
                     "results","current","created","started","finished"}
    mgr.cancel(jid) -> True/False

CLI:  python -m app.jobs --selftest
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_CANCELED = "canceled"

ALL_STATES = (STATE_RUNNING, STATE_DONE, STATE_ERROR, STATE_CANCELED)


class JobCanceled(Exception):
    """Raised inside a job body when the user cancelled the job."""


def jsonable(obj):
    """Best-effort conversion so job payloads always survive json.dumps."""
    try:
        json.dumps(obj, ensure_ascii=False)
        return obj
    except (TypeError, ValueError):
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def _now() -> float:
    return time.time()


class Job:
    def __init__(self, jid: str, kind: str, *, total: int = 0, message: str = "", meta=None):
        self.id = jid
        self.kind = kind
        self.state = STATE_RUNNING
        self.total = int(total or 0)
        self.done = 0
        self.message = str(message or "")
        self.errors = []
        self.results = []
        self.current = None
        self.meta = dict(meta or {})
        self.created = _now()
        self.started = 0.0
        self.finished = 0.0
        self.cancel_requested = False
        self._lock = threading.RLock()

    # -------------------------------------------------------------- callbacks
    def update(self, *, done=None, total=None, message=None, current=None) -> None:
        with self._lock:
            if done is not None:
                self.done = max(0, int(done))
            if total is not None:
                # some callbacks report a per-batch total; the job total only grows
                self.total = max(self.total, max(0, int(total)))
            if self.total:
                self.done = min(self.done, self.total)
            if message is not None:
                self.message = str(message)
            if current is not None:
                if isinstance(current, dict):
                    cur = dict(current)
                    if "frac" in cur:
                        try:
                            cur["frac"] = round(min(1.0, max(0.0, float(cur["frac"]))), 4)
                        except (TypeError, ValueError):
                            cur.pop("frac", None)
                    self.current = jsonable(cur)
                else:
                    self.current = jsonable(current)

    def progress(self, done, total=None, message=None) -> None:
        self.update(done=done, total=total, message=message)

    def add_error(self, message: str) -> None:
        with self._lock:
            self.errors.append(str(message))

    def add_result(self, record) -> None:
        with self._lock:
            self.results.append(jsonable(record))

    def cancel(self) -> bool:
        with self._lock:
            if self.state != STATE_RUNNING:
                return False
            self.cancel_requested = True
            return True

    def clear_current(self) -> None:
        with self._lock:
            self.current = None

    def is_canceled(self) -> bool:
        return self.cancel_requested

    def check(self) -> None:
        if self.cancel_requested:
            raise JobCanceled()

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "state": self.state,
                "done": self.done,
                "total": self.total,
                "message": self.message,
                "errors": list(self.errors),
                "results": list(self.results),
                "current": self.current,
                "meta": dict(self.meta),
                "cancel_requested": self.cancel_requested,
                "created": self.created,
                "started": self.started or None,
                "finished": self.finished or None,
                "elapsed": round((self.finished or _now()) - (self.started or _now()), 3)
                if self.started else None,
            }


class JobManager:
    def __init__(self, *, keep_finished: int = 60):
        self._jobs = {}
        self._lock = threading.RLock()
        self.keep_finished = int(keep_finished)

    # -------------------------------------------------------------- submit/run
    def submit(self, kind: str, fn, *, total: int = 0, message: str = "", meta=None) -> str:
        """Start fn(job) in a daemon thread; returns the job id."""
        jid = uuid.uuid4().hex[:12]
        job = Job(jid, kind, total=total, message=message, meta=meta)
        with self._lock:
            self._jobs[jid] = job
            self._prune_locked()
        t = threading.Thread(target=self._run, args=(job, fn), name="job-" + jid, daemon=True)
        job.thread = t
        t.start()
        return jid

    def _run(self, job: Job, fn) -> None:
        job.started = _now()
        try:
            result = fn(job)
            with job._lock:
                if isinstance(result, list):
                    job.results.extend(jsonable(result))
                if job.cancel_requested:
                    job.state = STATE_CANCELED
                    job.message = job.message or "已取消"
                else:
                    job.state = STATE_DONE
                    if not job.message:
                        job.message = "完成"
                    if job.total and job.done < job.total:
                        job.done = job.total
        except JobCanceled:
            with job._lock:
                job.state = STATE_CANCELED
                job.message = "已取消"
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            msg = str(exc) or exc.__class__.__name__
            with job._lock:
                job.state = STATE_ERROR
                job.errors.append(msg)
                job.message = "失败：" + msg
                job.meta["traceback"] = traceback.format_exc(limit=8)
        finally:
            job.finished = _now()
            with job._lock:
                job.current = None

    def _prune_locked(self) -> None:
        finished = [(j.finished or 0, jid) for jid, j in self._jobs.items()
                    if j.state in (STATE_DONE, STATE_ERROR, STATE_CANCELED)]
        if len(finished) > self.keep_finished:
            finished.sort()
            for _, jid in finished[: len(finished) - self.keep_finished]:
                self._jobs.pop(jid, None)

    # ------------------------------------------------------------------ reads
    def get(self, jid) -> dict:
        with self._lock:
            job = self._jobs.get(str(jid))
        return job.to_dict() if job else {}

    def list(self, limit: int = 20) -> list:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
        return [j.to_dict() for j in jobs[: max(1, int(limit or 1))]]

    def has_running(self, kind: str = "") -> bool:
        with self._lock:
            for j in self._jobs.values():
                if j.state == STATE_RUNNING and (not kind or j.kind == kind):
                    return True
        return False

    def cancel(self, jid) -> bool:
        with self._lock:
            job = self._jobs.get(str(jid))
        return job.cancel() if job else False

    # ------------------------------------------------------------------ waits
    def wait(self, jid, timeout: float = 60.0, poll: float = 0.05) -> dict:
        """Block until the job stops (used by selftests / scripts only)."""
        deadline = _now() + float(timeout)
        while True:
            job = self.get(jid)
            if not job:
                raise KeyError(jid)
            if job["state"] != STATE_RUNNING:
                return job
            if _now() >= deadline:
                return job
            time.sleep(poll)


# ---------------------------------------------------------------- self test
def _selftest() -> int:
    ok = fail = 0
    def check(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("PASS", label, flush=True)
        else:
            fail += 1
            print("FAIL", label, flush=True)

    mgr = JobManager()

    def quick(job):
        for i in range(5):
            job.check()
            job.progress(i + 1, 5, "step %d" % (i + 1))
            job.add_result({"i": i})
            time.sleep(0.01)
        return [{"i": 99}]

    jid = mgr.submit("demo", quick, total=5, message="starting")
    got = mgr.wait(jid, timeout=10)
    check(got["state"] == STATE_DONE, "job done")
    check(got["done"] == 5 and got["total"] == 5, "progress reached total")
    check(len(got["results"]) == 6, "streamed + returned results")
    check(got["current"] is None, "current cleared")
    check(json.dumps(got, ensure_ascii=False), "json serialisable")

    def slow(job):
        for i in range(200):
            job.check()
            job.update(done=i, total=200, message="working",
                       current={"pid": "p1", "frac": i / 200.0})
            time.sleep(0.01)

    jid2 = mgr.submit("slow", slow, total=200)
    time.sleep(0.08)
    check(mgr.cancel(jid2), "cancel accepted")
    got2 = mgr.wait(jid2, timeout=10)
    check(got2["state"] == STATE_CANCELED, "job canceled")
    check(mgr.cancel(jid2) is False, "cancel terminal job refused")

    def boom(job):
        job.update(done=1, total=2, message="almost")
        raise ValueError("模拟失败")

    jid3 = mgr.submit("boom", boom, total=2)
    got3 = mgr.wait(jid3, timeout=10)
    check(got3["state"] == STATE_ERROR, "job error state")
    check(any("模拟失败" in e for e in got3["errors"]), "error text kept")

    check(len(mgr.list()) >= 3, "list jobs")
    check(mgr.get("nope") == {}, "unknown job -> empty")
    print("jobs selftest: %d passed, %d failed" % (ok, fail), flush=True)
    return 0 if fail == 0 else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="app.jobs", description="后台任务管理器")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
