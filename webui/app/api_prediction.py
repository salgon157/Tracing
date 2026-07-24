"""
/api/prediction/* — UI vrstva nad predict_day.py + compare_prediction.py.

Spuštění = job (subprocess na existující skripty, žádná logika navíc).
Čtení = přímo predikční strom (data/prediction/results) a comparison.jsonl.
Ostrá historie i produkční data zůstávají netknuté.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import commands, config, jobs

router = APIRouter(prefix="/api/prediction")


def _step(name: str, argv: list[str]) -> jobs.Step:
    return jobs.Step(name=name, argv=argv, cmdline=jobs.cmdline(argv))


# ── Čtení JSONL (tolerantní) ────────────────────────────────────────────────

def _read_jsonl(path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _dir_leaf(rec: dict) -> str:
    out = str(rec.get("results", {}).get("output_dir", ""))
    return out.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


# ── Spuštění ────────────────────────────────────────────────────────────────

class PredictRequest(BaseModel):
    depots: list[str] = []                # prázdné = všechna s predikčním souborem
    budget_min: float | None = None
    osm_source: str = "current"
    visualize: bool = True
    skip_startup_tests: bool = False
    dry: bool = False


class CompareRequest(BaseModel):
    date: str | None = None
    depots: str | None = None
    pred_stamp: str | None = None
    dry: bool = False


@router.post("/run")
def run_prediction(req: PredictRequest) -> dict:
    depots = [d.strip().upper() for d in req.depots if d.strip()]
    bad = [d for d in depots if d not in config.DEPOT_CODES]
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"Neznámá depa: {', '.join(bad)}")
    argv = commands.build_predict_day(
        depots=depots, budget_min=req.budget_min,
        no_visualize=not req.visualize, osm_source=req.osm_source,
        skip_tests=req.skip_startup_tests)
    job = jobs.Job(
        id=jobs.new_job_id(), type="prediction",
        title=f"Predikce {', '.join(depots) or 'všechna depa'}",
        steps=[_step("predict-day", argv)], params=req.model_dump(),
        # predict_day.py spouští startup testy jednou sám; ať je child solvery
        # neopakují, drží SKIP na 1 (skript testy pustí ve svém procesu).
        env_flags={"SKIP_STARTUP_TESTS": True},
    )
    if req.dry:
        return job.to_dict()
    return jobs.manager.submit(job).to_dict()


@router.post("/compare")
def run_compare(req: CompareRequest) -> dict:
    argv = commands.build_compare_prediction(
        date=req.date, depots=req.depots, pred_stamp=req.pred_stamp)
    job = jobs.Job(
        id=jobs.new_job_id(), type="compare_prediction",
        title=f"Porovnání predikce {req.date or ''}".strip(),
        steps=[_step("compare", argv)], params=req.model_dump(),
        env_flags={"SKIP_STARTUP_TESTS": True},
    )
    if req.dry:
        return job.to_dict()
    return jobs.manager.submit(job).to_dict()


# ── Čtení výsledků ──────────────────────────────────────────────────────────

@router.get("/runs")
def prediction_runs() -> list[dict]:
    """Predikční běhy z data/prediction/results/run_log.jsonl, nejnovější první."""
    recs = _read_jsonl(config.PREDICTION_RUN_LOG)
    out = []
    for r in recs:
        inp, res = r.get("input", {}), r.get("results", {})
        out.append({
            "run_id":       r.get("run_id"),
            "zone":         inp.get("zone"),
            "date":         inp.get("delivery_date") or "",
            "stamp":        _dir_leaf(r).split("_")[-1] if "_" in _dir_leaf(r) else "",
            "orders":       inp.get("orders_count"),
            "lines":        res.get("lines_count"),
            "vehicle_mix":  res.get("vehicle_type_mix", {}),
            "cost_kc":      res.get("total_cost_kc"),
            "total_km":     res.get("total_km"),
            "output_dir":   res.get("output_dir", ""),
        })
    out.reverse()
    return out


@router.get("/comparison")
def comparison(date: str | None = None) -> list[dict]:
    """Poslední porovnání predikce×realita z comparison.jsonl."""
    recs = _read_jsonl(config.PREDICTION_COMPARISON)
    if date:
        recs = [r for r in recs if r.get("date") == date]
    recs.sort(key=lambda r: (str(r.get("date", "")), str(r.get("zone", ""))))
    return recs
