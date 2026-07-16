"""
/api/closures/* — UI vrstva nad existujícími nástroji pro uzavírky.

Čtení = přímo closures.json (read-only). Mutace = subprocess na manage_closures.py
(toggle/remove/test) a closure_map_editor.py (tvorba). Logika uzavírek se nemění.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import closures, config, jobs

router = APIRouter(prefix="/api/closures")


def _run_manage(args: list[str]) -> dict:
    """Synchronně spustí manage_closures.py (rychlé mutace: toggle/remove)."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        r = subprocess.run(
            [sys.executable, "manage_closures.py", *args],
            cwd=str(config.REPO_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(status_code=500, detail=f"manage_closures selhalo: {e}")
    return {"returncode": r.returncode, "output": (r.stdout or "") + (r.stderr or "")}


@router.get("")
def list_all() -> dict:
    return closures.list_closures()


@router.get("/map", response_class=HTMLResponse)
def map_html() -> str:
    return closures.closures_map_html()


@router.post("/{cid}/toggle")
def toggle(cid: str) -> dict:
    res = _run_manage(["toggle", cid])
    res["closures"] = closures.list_closures()["closures"]
    return res


@router.post("/{cid}/remove")
def remove(cid: str) -> dict:
    res = _run_manage(["remove", cid])
    res["closures"] = closures.list_closures()["closures"]
    return res


class TestRequest(BaseModel):
    orders_file: str
    osrm_url: str | None = None
    skip_startup_tests: bool = True
    dry: bool = False


@router.post("/test")
def test(req: TestRequest) -> dict:
    argv = [sys.executable, "manage_closures.py", "test", "--orders-file", req.orders_file]
    if req.osrm_url:
        argv += ["--osrm-url", req.osrm_url]
    step = jobs.Step(name="closure-test", argv=argv, cmdline=jobs.cmdline(argv))
    job = jobs.Job(
        id=jobs.new_job_id(), type="closure_test",
        title=f"Test uzavírek: {req.orders_file}",
        steps=[step], params=req.model_dump(),
        env_flags={"SKIP_STARTUP_TESTS": req.skip_startup_tests},
    )
    if req.dry:
        return job.to_dict()
    return jobs.manager.submit(job).to_dict()


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


@router.post("/editor/start")
def editor_start() -> dict:
    """Spustí existující closure_map_editor.py (port 8765) jako samostatný proces."""
    url = "http://localhost:8765"
    if _port_in_use(8765):
        return {"url": url, "already_running": True}
    # UTF-8 env je nutné — editor při startu tiskne box-drawing znaky, které
    # by na výchozí Windows cp1250 konzoli spadly (UnicodeEncodeError).
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    kwargs = dict(cwd=str(config.REPO_ROOT), env=env,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.DETACHED_PROCESS)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "closure_map_editor.py"], **kwargs)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Nelze spustit editor: {e}")
    return {"url": url, "already_running": False}
