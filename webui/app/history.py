"""
Parsování historie běhů (run_log.jsonl) a benchmark sessionů.

Vše tolerantní: vadné/rozepsané řádky se tiše přeskakují, nikdy 500.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from . import config


def read_runlog(zone: str | None = None, limit: int = 100,
                path: Path | None = None) -> list[dict]:
    """
    Načte run_log.jsonl, zploští na přehledové sloupce, nejnovější první.
    Vadné JSON řádky přeskakuje. `path` injektovatelný pro testy.
    """
    log_path = path or config.RUN_LOG_PATH
    if not log_path.exists():
        return []

    flat: list[dict] = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # vadný řádek tiše přeskočit
                inp = rec.get("input", {}) or {}
                res = rec.get("results", {}) or {}
                cfg = rec.get("config", {}) or {}
                z = inp.get("zone")
                if zone and z != zone:
                    continue
                budget_sec = cfg.get("total_time_budget_sec")
                budget_min = (round(budget_sec / 60, 1)
                              if isinstance(budget_sec, (int, float)) else None)
                flat.append({
                    "run_id":        rec.get("run_id"),
                    "zone":          z,
                    "delivery_date": inp.get("delivery_date"),
                    "orders_count":  inp.get("orders_count"),
                    "budget_min":    budget_min,
                    "lines_count":   res.get("lines_count"),
                    "total_cost_kc": res.get("total_cost_kc"),
                    "total_km":      res.get("total_km"),
                    "total_hours":   res.get("total_hours"),
                    "elapsed_min":   res.get("elapsed_min"),
                    "output_dir":    res.get("output_dir"),
                    "raw":           rec,
                })
    except OSError:
        return []

    flat.reverse()          # jsonl je append (nejstarší první) → otoč
    return flat[:limit]


def _read_runs_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                rows.append(dict(row))
    except (OSError, csv.Error):
        pass                # vrať co máme (session se možná píše)
    return rows


def _read_runs_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def read_benchmark_session(session_dir: Path) -> dict:
    """Plán + běhy sessionu. Preferuje CSV, fallback na JSONL. Tolerantní."""
    plan = None
    plan_file = session_dir / "benchmark_plan.json"
    if plan_file.exists():
        try:
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            plan = None

    runs = _read_runs_csv(session_dir / "benchmark_runs.csv")
    if not runs:
        runs = _read_runs_jsonl(session_dir / "benchmark_runs.jsonl")

    return {
        "path": session_dir.relative_to(config.RESULTS_ROOT).as_posix(),
        "name": session_dir.name,
        "plan": plan,
        "runs": runs,
    }
