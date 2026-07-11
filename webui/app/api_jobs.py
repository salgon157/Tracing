"""
/api/jobs/* routy — správa a sledování jobů.

Command-specific endpointy (daily / all-depots / benchmark / visualize) se
doplňují v krocích 6 a 8; tady jsou generické: selftest, list, get, log, cancel.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import commands, config, jobs, scan

router = APIRouter(prefix="/api/jobs")


def _step(name: str, argv: list[str]) -> jobs.Step:
    return jobs.Step(name=name, argv=argv, cmdline=jobs.cmdline(argv))


def _finish(job: jobs.Job, dry: bool) -> dict:
    if dry:
        return job.to_dict()
    return jobs.manager.submit(job).to_dict()


class DailyRequest(BaseModel):
    depot: str
    budget_min: float | None = None
    force_matrix: bool = False
    fresh_osm: bool = False
    allow_profile_fallback: bool = False
    skip_startup_tests: bool = False
    visualize: bool = True
    dry: bool = False


class VisualizeRequest(BaseModel):
    path: str
    no_osrm: bool = False
    fresh_osm: bool = False
    dry: bool = False


class AllDepotsRequest(BaseModel):
    date: str | None = None
    depots: str | None = None            # "CB,MO,HK,PR"
    budget_min: float | None = None
    budget_ratios: str | None = None     # "0.35,0.25,0.40"
    clusters: str | None = None          # "auto" nebo číslo
    workers: int | None = None
    seed_restarts: int | None = None
    force_matrix: bool = False
    fresh_osm: bool = False
    dry_run: bool = False
    run_startup_tests: bool = False
    skip_startup_tests: bool = False
    dry: bool = False


class BenchmarkRequest(BaseModel):
    budget_min: float | None = None
    preset: str | None = None
    date: str | None = None
    depots: str | None = None
    cluster_factors: str | None = None   # "0.75,1.0,1.25"
    budget_profiles: str | None = None   # "combined_lns,normal_no_lns"
    seed_restarts: int | None = None
    workers: int | None = None
    pause_sec: float | None = None
    only: str | None = None
    list_only: bool = False
    dry_run: bool = False
    force_matrix: bool = False
    fresh_osm: bool = False
    run_startup_tests: bool = False
    stop_on_failure: bool = False
    skip_startup_tests: bool = False
    dry: bool = False


def _validate_depots(depots: str) -> str:
    toks = [t.strip().upper() for t in depots.split(",") if t.strip()]
    bad = [t for t in toks if t not in config.DEPOT_CODES]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"Neznámá depa: {', '.join(bad)}. "
                   f"Povolená: {', '.join(sorted(config.DEPOT_CODES))}",
        )
    return ",".join(toks)


def _validate_ratios(ratios: str) -> str:
    parts = [p.strip() for p in ratios.split(",")]
    if len(parts) != 3:
        raise HTTPException(status_code=400,
                            detail="budget_ratios musí být tři čísla C,D,E (např. 0.35,0.25,0.40).")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="budget_ratios musí být čísla, např. 0.35,0.25,0.40.")
    if abs(sum(nums) - 1.0) > 0.001:
        raise HTTPException(status_code=400,
                            detail=f"budget_ratios musí dát součet 1.0 (teď {sum(nums):.3f}).")
    return ratios


@router.get("")
def list_jobs(limit: int = 50) -> list[dict]:
    return [j.to_dict() for j in jobs.manager.list(limit=limit)]


@router.post("/selftest")
def selftest(body: dict | None = None) -> dict:
    job = jobs.build_selftest_job()
    if body and body.get("dry"):
        return job.to_dict()
    return jobs.manager.submit(job).to_dict()


@router.post("/daily")
def daily(req: DailyRequest) -> dict:
    depot = req.depot.upper()
    if depot not in config.DEPOT_CODES:
        raise HTTPException(
            status_code=400,
            detail=f"Neznámé depo '{req.depot}'. Dostupná: "
                   f"{', '.join(sorted(config.DEPOT_CODES))}",
        )
    # Zrcadlí pravidlo prepare: v aktivni/ musí být PRÁVĚ JEDEN RiRo soubor.
    files = scan.list_input(depot)
    if len(files) != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"V data/input/{depot}/aktivni/ musí být právě jeden "
                           f"RiRo soubor (nalezeno {len(files)}).",
                "files": [f["name"] for f in files],
            },
        )
    date = files[0]["date"]
    if not date:
        raise HTTPException(
            status_code=400,
            detail=f"Z názvu '{files[0]['name']}' nelze odvodit datum "
                   f"(očekávám riro-YYYYMMDD-...).",
        )

    steps = [
        _step("prepare", commands.build_prepare(depot)),
        _step("solve", commands.build_solve(
            depot, date, budget_min=req.budget_min, force_matrix=req.force_matrix,
            fresh_osm=req.fresh_osm, allow_profile_fallback=req.allow_profile_fallback)),
    ]
    if req.visualize:
        steps.append(_step("visualize", commands.build_visualize(
            f"data/results/{depot}/{date}", fresh_osm=req.fresh_osm)))

    job = jobs.Job(
        id=jobs.new_job_id(), type="daily",
        title=f"Denní běh {depot} {date}",
        steps=steps, params=req.model_dump(),
        env_flags={"SKIP_STARTUP_TESTS": req.skip_startup_tests},
    )
    return _finish(job, req.dry)


@router.post("/visualize")
def visualize(req: VisualizeRequest) -> dict:
    try:
        d = scan.resolve_results_path(req.path)
    except scan.UnsafePath as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"Složka neexistuje: {req.path}")
    # Cesta pro visualize_routes.py musí být relativní ke kořeni repa (cwd),
    # tj. 'data/results/CB/...'. Label do titulku je zkrácený (rel k results).
    arg_path = d.relative_to(config.REPO_ROOT).as_posix()
    label    = d.relative_to(config.RESULTS_ROOT).as_posix()
    job = jobs.Job(
        id=jobs.new_job_id(), type="visualize",
        title=f"Vizualizace {label}",
        steps=[_step("visualize", commands.build_visualize(
            arg_path, no_osrm=req.no_osrm, fresh_osm=req.fresh_osm))],
        params=req.model_dump(),
    )
    return _finish(job, req.dry)


@router.post("/all-depots")
def all_depots(req: AllDepotsRequest) -> dict:
    depots = _validate_depots(req.depots) if req.depots else None
    ratios = _validate_ratios(req.budget_ratios) if req.budget_ratios else None
    argv = commands.build_all_depots(
        date=req.date, depots=depots, budget_min=req.budget_min,
        budget_ratios=ratios, clusters=req.clusters, workers=req.workers,
        seed_restarts=req.seed_restarts, force_matrix=req.force_matrix,
        fresh_osm=req.fresh_osm, dry_run=req.dry_run,
        run_startup_tests=req.run_startup_tests)
    job = jobs.Job(
        id=jobs.new_job_id(), type="all_depots",
        title=f"Všechna depa {req.date or 'nejnovější'}",
        steps=[_step("all-depots", argv)], params=req.model_dump(),
        env_flags={"SKIP_STARTUP_TESTS": req.skip_startup_tests},
    )
    return _finish(job, req.dry)


@router.post("/benchmark")
def benchmark(req: BenchmarkRequest) -> dict:
    depots = _validate_depots(req.depots) if req.depots else None
    try:
        argv = commands.build_benchmark(
            budget_min=req.budget_min, preset=req.preset, date=req.date,
            depots=depots, cluster_factors=req.cluster_factors,
            budget_profiles=req.budget_profiles, seed_restarts=req.seed_restarts,
            workers=req.workers, pause_sec=req.pause_sec, only=req.only,
            list_only=req.list_only, dry_run=req.dry_run,
            force_matrix=req.force_matrix, fresh_osm=req.fresh_osm,
            run_startup_tests=req.run_startup_tests,
            stop_on_failure=req.stop_on_failure)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    label = f"preset {req.preset}" if req.preset else f"{commands._fmt_num(req.budget_min)} min"
    job = jobs.Job(
        id=jobs.new_job_id(), type="benchmark",
        title=f"Benchmark {label}",
        steps=[_step("benchmark", argv)], params=req.model_dump(),
        env_flags={"SKIP_STARTUP_TESTS": req.skip_startup_tests},
    )
    return _finish(job, req.dry)


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' neexistuje")
    return job.to_dict()


@router.get("/{job_id}/log")
def get_job_log(job_id: str, offset: int = 0) -> dict:
    if jobs.manager.get(job_id) is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' neexistuje")
    return jobs.manager.read_log(job_id, offset=offset)


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = jobs.manager.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' neexistuje")
    return job.to_dict()
