"""
webui FastAPI app — tenká API vrstva nad VRP CLI pipeline.

Spuštění z kořene repa:
    python -m uvicorn webui.app.main:app --host 127.0.0.1 --port 8777
(bez --reload — reloader by osiřel běžící solver, viz plán Rizika #1).
"""

from __future__ import annotations

import sys

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import api_jobs, config, history, jobs, scan, staging

app = FastAPI(title="VRP Plánovač — webové rozhraní", version="0.1.0")
app.include_router(api_jobs.router)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "repo_root": config.REPO_ROOT.as_posix(),
        "python_version": sys.version.split()[0],
    }


@app.get("/api/depots")
def depots() -> list[dict]:
    return config.DEPOTS


@app.get("/api/input/{depot}")
def input_files(depot: str) -> list[dict]:
    if depot not in config.DEPOT_CODES:
        raise HTTPException(
            status_code=404,
            detail=f"Neznámé depo '{depot}'. Dostupná: {', '.join(sorted(config.DEPOT_CODES))}",
        )
    return scan.list_input(depot)


@app.post("/api/input/{depot}/upload")
def upload_input(depot: str, file: UploadFile = File(...), force: bool = False) -> dict:
    depot = depot.upper()
    if depot not in config.DEPOT_CODES:
        raise HTTPException(
            status_code=404,
            detail=f"Neznámé depo '{depot}'. Dostupná: "
                   f"{', '.join(sorted(config.DEPOT_CODES))}",
        )
    if jobs.manager.has_active_job_for_depot(depot):
        raise HTTPException(
            status_code=423,
            detail=f"Pro depo {depot} běží nebo čeká úloha — upload je dočasně "
                   f"zablokován. Počkej na dokončení (nebo úlohu zruš).",
        )
    try:
        return staging.stage_upload(depot, file.filename or "", file.file, force=force)
    except staging.StagingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.get("/api/results")
def results() -> dict:
    return scan.scan_results()


def _safe_dir(path: str) -> "object":
    """Přeloží klientskou cestu na existující složku uvnitř data/results."""
    try:
        candidate = scan.resolve_results_path(path)
    except scan.UnsafePath as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail=f"Složka neexistuje: {path}")
    return candidate


@app.get("/api/results/detail")
def results_detail(path: str) -> dict:
    return scan.run_detail(_safe_dir(path))


@app.get("/api/results/file")
def results_file(path: str):
    try:
        candidate = scan.resolve_results_path(path)
    except scan.UnsafePath as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"Soubor neexistuje: {path}")
    media = scan.DOWNLOAD_MEDIA_TYPES.get(candidate.suffix.lower())
    if media is None:
        raise HTTPException(
            status_code=415,
            detail=f"Nepovolený typ souboru '{candidate.suffix}'. "
                   f"Povolené: {', '.join(sorted(scan.DOWNLOAD_MEDIA_TYPES))}",
        )
    return FileResponse(candidate, media_type=media, filename=candidate.name)


@app.get("/api/results/map")
def results_map(path: str):
    map_file = _safe_dir(path) / "routes_map.html"
    if not map_file.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Mapa (routes_map.html) v běhu '{path}' neexistuje. "
                   f"Vygeneruj ji přes vizualizaci.",
        )
    return FileResponse(map_file, media_type="text/html; charset=utf-8")


@app.get("/api/runlog")
def runlog(zone: str | None = None, limit: int = 100) -> list[dict]:
    return history.read_runlog(zone=zone, limit=limit)


@app.get("/api/benchmark/session")
def benchmark_session(path: str) -> dict:
    # Akceptuj "ALL_BENCHMARK/session_x" i holé "session_x".
    candidates = [path]
    if not path.replace("\\", "/").startswith("ALL_BENCHMARK"):
        candidates.append(f"ALL_BENCHMARK/{path}")
    for cand in candidates:
        try:
            d = scan.resolve_results_path(cand)
        except scan.UnsafePath:
            continue
        if d.is_dir():
            return history.read_benchmark_session(d)
    raise HTTPException(status_code=404, detail=f"Benchmark session neexistuje: {path}")


# Statický frontend na "/" — mount MUSÍ být registrován až po /api/* routách,
# jinak by catch-all pohltil API. html=True → "/" servíruje index.html.
app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")
