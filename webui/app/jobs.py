"""
Job engine — striktně JEDEN job současně (solver saturuje CPU).

Model: Job = seřazený seznam kroků, každý = jeden subprocess. Worker daemon
thread bere z fronty, streamuje stdout kroku do společného job.log (offsety
pollingu v BAJTECH kvůli vícebajtové diakritice), persistuje job.json atomicky.

Cancel zabíjí celý strom procesů (multiprocessing děti solveru / child solvery
benchmarku): Windows `taskkill /T /F`, POSIX `killpg`.

Subprocess invarianty (plán): cwd = kořen repa, relativní cesty v argv,
PYTHONIOENCODING=utf-8 + PYTHONUTF8=1, text utf-8 errors=replace, bufsize=1,
Windows CREATE_NEW_PROCESS_GROUP / POSIX start_new_session.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import config


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def cmdline(argv: list[str]) -> str:
    """Lidsky čitelný příkaz (copy-paste reprodukovatelný ručně)."""
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    import shlex
    return shlex.join(argv)


# ── Datové modely ────────────────────────────────────────────────────────────

@dataclass
class Step:
    name: str
    argv: list[str]
    cmdline: str
    status: str = "pending"          # pending|running|success|failed|skipped
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None


@dataclass
class Job:
    id: str
    type: str
    title: str
    status: str = "queued"           # queued|running|success|failed|cancelled|interrupted
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    params: dict = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    env_flags: dict = field(default_factory=dict)
    error_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Job":
        steps = [Step(**s) for s in d.get("steps", [])]
        j = Job(id=d["id"], type=d.get("type", ""), title=d.get("title", ""))
        j.status      = d.get("status", "queued")
        j.created_at  = d.get("created_at", _now_iso())
        j.started_at  = d.get("started_at")
        j.finished_at = d.get("finished_at")
        j.params      = d.get("params", {})
        j.steps       = steps
        j.env_flags   = d.get("env_flags", {})
        j.error_lines = d.get("error_lines", [])
        return j


def new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


# ── Tree-kill ────────────────────────────────────────────────────────────────

def _tree_kill(pid: int) -> None:
    """Zabij proces i všechny jeho potomky."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
        return
    # POSIX: proces běží ve vlastní session (start_new_session) → zabij grupu.
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    # počkej 10 s, pak SIGKILL
    import time
    for _ in range(100):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# ── JobManager ───────────────────────────────────────────────────────────────

class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.RLock()
        self._current_proc: subprocess.Popen | None = None
        self._current_job_id: str | None = None
        self._cancel_requested = False
        self._cancelled_ids: set[str] = set()

        config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self._recover_on_start()

        self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                        name="webui-job-worker")
        self._worker.start()

    # -- perzistence --
    def _job_dir(self, job_id: str) -> Path:
        return config.JOBS_DIR / job_id

    def _persist(self, job: Job) -> None:
        d = self._job_dir(job.id)
        d.mkdir(parents=True, exist_ok=True)
        target = d / "job.json"
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, target)

    def _recover_on_start(self) -> None:
        """Načti existující joby; osiřelé running/queued → interrupted."""
        for entry in config.JOBS_DIR.glob("*/job.json"):
            try:
                job = Job.from_dict(json.loads(entry.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if job.status in ("running", "queued"):
                job.status = "interrupted"
                for s in job.steps:
                    if s.status == "running":
                        s.status = "failed"
                self._persist(job)
            self._jobs[job.id] = job

    # -- veřejné API --
    def submit(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
            self._persist(job)
            self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def has_active_job_for_depot(self, depot: str) -> bool:
        """True pokud běží/čeká job pro dané depo (blokuje upload)."""
        depot = depot.upper()
        for j in self._jobs.values():
            if j.status not in ("running", "queued"):
                continue
            p = j.params or {}
            if str(p.get("depot", "")).upper() == depot:
                return True
            deps = p.get("depots")
            if isinstance(deps, list) and depot in [str(x).upper() for x in deps]:
                return True
        return False

    def list(self, limit: int = 50) -> list[Job]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def read_log(self, job_id: str, offset: int = 0) -> dict:
        log_path = self._job_dir(job_id) / "job.log"
        if not log_path.exists():
            return {"offset": offset, "content": ""}
        try:
            with open(log_path, "rb") as f:
                f.seek(max(0, offset))
                data = f.read()
        except OSError:
            return {"offset": offset, "content": ""}
        return {
            "offset":  (max(0, offset)) + len(data),
            "content": data.decode("utf-8", errors="replace"),
        }

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status not in ("running", "queued"):
                return job                      # nic ke zrušení
            if self._current_job_id == job_id and self._current_proc is not None:
                self._cancel_requested = True
                proc = self._current_proc
            else:
                self._cancelled_ids.add(job_id)  # ve frontě, worker ho přeskočí
                proc = None
        if proc is not None and proc.poll() is None:
            _tree_kill(proc.pid)
        return job

    # -- worker --
    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run_job(job_id)
            finally:
                self._queue.task_done()

    def _run_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        with self._lock:
            if job_id in self._cancelled_ids:
                self._cancelled_ids.discard(job_id)
                job.status = "cancelled"
                job.finished_at = _now_iso()
                self._persist(job)
                return
            self._current_job_id = job_id
            self._cancel_requested = False

        job.status = "running"
        job.started_at = _now_iso()
        self._persist(job)

        log_path = self._job_dir(job_id) / "job.log"
        ok = True
        try:
            with open(log_path, "ab") as log_fh:
                for step in job.steps:
                    if self._cancel_requested:
                        break
                    ok = self._run_step(job, step, log_fh)
                    if not ok:
                        break
                for step in job.steps:              # zbylé kroky
                    if step.status == "pending":
                        step.status = "skipped"
                if self._cancel_requested:
                    job.status = "cancelled"
                    log_fh.write("\n[WEBUI] Job zrušen uživatelem\n".encode("utf-8"))
                    log_fh.flush()
                else:
                    job.status = "success" if ok else "failed"
                    if job.status == "success":
                        try:
                            self._post_process(job)
                        except Exception:       # noqa: BLE001 — detekce je best-effort
                            pass
        except Exception as e:                      # noqa: BLE001 — nikdy nespadnout
            job.status = "failed"
            job.error_lines.append(f"[WEBUI] Interní chyba jobu: {e}")
        finally:
            with self._lock:
                self._current_proc = None
                self._current_job_id = None
            job.finished_at = _now_iso()
            self._persist(job)

    def _post_process(self, job: Job) -> None:
        """Po úspěchu benchmarku detekuj vytvořený session dir (odkaz pro UI)."""
        if job.type != "benchmark":
            return
        bench = config.RESULTS_ROOT / "ALL_BENCHMARK"
        try:
            subdirs = [p for p in bench.iterdir()
                       if p.is_dir() and (p / "benchmark_plan.json").exists()]
        except OSError:
            return
        if subdirs:
            newest = max(subdirs, key=lambda p: p.stat().st_mtime)
            job.params["session_dir_detected"] = \
                newest.relative_to(config.RESULTS_ROOT).as_posix()

    def _run_step(self, job: Job, step: Step, log_fh) -> bool:
        step.status = "running"
        step.started_at = _now_iso()
        self._persist(job)

        log_fh.write(f"\n===== STEP {step.name}: {step.cmdline} =====\n".encode("utf-8"))
        log_fh.flush()

        # PYTHONUNBUFFERED=1 → skripty vypláznou stdout okamžitě (řádek po řádku),
        # jako v terminálu. Bez toho Python při psaní do roury blokově bufruje
        # a výstup dlouhého kroku (solve) by naskočil až v dávkách / na konci.
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
               "PYTHONUNBUFFERED": "1"}
        for k, v in job.env_flags.items():
            if v:
                env[k] = "1"

        kwargs = dict(
            cwd=str(config.REPO_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env,
        )
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(step.argv, **kwargs)
        except (OSError, ValueError) as e:
            step.status = "failed"
            step.finished_at = _now_iso()
            job.error_lines.append(f"[WEBUI] Nelze spustit krok '{step.name}': {e}")
            self._persist(job)
            return False

        step.pid = proc.pid
        with self._lock:
            self._current_proc = proc
        self._persist(job)

        tail: deque[str] = deque(maxlen=50)
        assert proc.stdout is not None
        for line in proc.stdout:
            log_fh.write(line.encode("utf-8"))
            log_fh.flush()
            tail.append(line.rstrip("\n"))
        proc.wait()

        with self._lock:
            self._current_proc = None
        step.exit_code = proc.returncode
        step.finished_at = _now_iso()
        step.status = "success" if proc.returncode == 0 else "failed"

        if step.status == "failed" and not self._cancel_requested:
            errs = [ln for ln in tail
                    if "[CHYBA]" in ln or "[ABORT]" in ln or "[ERROR]" in ln]
            job.error_lines.extend(errs if errs else list(tail)[-5:])
        self._persist(job)
        return step.status == "success"


# Singleton — vytvoří se při importu (spustí worker + recovery).
manager = JobManager()


# ── Buildery jobů ────────────────────────────────────────────────────────────

def build_selftest_job() -> Job:
    """Mini UTF-8 / živý-streaming / cancel smoke test (~15 s)."""
    code = (
        "import sys, time\n"
        "print('Příliš žluťoučký kůň — UTF-8 OK', flush=True)\n"
        "for i in range(15):\n"
        "    print(f'tick {i+1}/15', flush=True)\n"
        "    time.sleep(1)\n"
        "print('selftest hotovo', flush=True)\n"
    )
    argv = [sys.executable, "-c", code]
    step = Step(name="selftest", argv=argv,
                cmdline="python -c \"<selftest 15s>\"")
    return Job(id=new_job_id(), type="selftest",
               title="Testovací úloha (UTF-8 + cancel)", steps=[step])
