"""
Čisté buildery argv (bez I/O, unit-testovatelné).

Reprodukují PŘESNĚ příkazy z WORKFLOW.md, s relativními cestami (cwd = kořen
repa), aby byl každý příkaz ručně copy-paste spustitelný. argv[0] je konkrétní
interpreter (sys.executable) kvůli spolehlivému spuštění; skript + argumenty
odpovídají WORKFLOW.md token po tokenu.
"""

from __future__ import annotations

import sys

PY = sys.executable


def _fmt_num(x) -> str:
    """5.0 → '5', 5.5 → '5.5' (aby příkaz vypadal jako ve WORKFLOW.md)."""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def orders_rel_path(depot: str, date: str) -> str:
    return f"data/prepared/{depot}/orders_{depot}_{date}.csv"


def build_prepare(depot: str) -> list[str]:
    return [PY, "prepare_inputs_v6.py", depot]


def build_solve(depot: str, date: str, *,
                budget_min=None, force_matrix: bool = False,
                fresh_osm: bool = False,
                allow_profile_fallback: bool = False) -> list[str]:
    argv = [PY, "vrp_solver_lines_v6.py", "--orders-file", orders_rel_path(depot, date)]
    if budget_min is not None:
        argv += ["--budget-min", _fmt_num(budget_min)]
    if force_matrix:
        argv.append("--force-matrix")
    if allow_profile_fallback:
        argv.append("--allow-profile-fallback")
    if fresh_osm:
        argv.append("--fresh-osm")
    return argv


def build_visualize(result_rel_dir: str, *,
                    no_osrm: bool = False, fresh_osm: bool = False) -> list[str]:
    # NIKDY --open (otevřel by prohlížeč z procesu serveru → rozbil by headless).
    argv = [PY, "visualize_routes.py", result_rel_dir]
    if no_osrm:
        argv.append("--no-osrm")
    if fresh_osm:
        argv.append("--fresh-osm")
    return argv


def build_all_depots(*, date=None, depots=None, budget_min=None, budget_ratios=None,
                     clusters=None, workers=None, seed_restarts=None,
                     force_matrix: bool = False, fresh_osm: bool = False,
                     dry_run: bool = False, run_startup_tests: bool = False) -> list[str]:
    """Předává jen vyplněné flagy — ostatní nechává na defaultech skriptu."""
    argv = [PY, "vrp_solver_lines_all_depots_v6.py"]
    if date:                     argv += ["--date", str(date)]
    if depots:                   argv += ["--depots", str(depots)]
    if budget_min is not None:   argv += ["--budget-min", _fmt_num(budget_min)]
    if budget_ratios:            argv += ["--budget-ratios", str(budget_ratios)]
    if clusters:                 argv += ["--clusters", str(clusters)]
    if workers is not None:      argv += ["--workers", str(workers)]
    if seed_restarts is not None: argv += ["--seed-restarts", str(seed_restarts)]
    if force_matrix:             argv.append("--force-matrix")
    if fresh_osm:                argv.append("--fresh-osm")
    if dry_run:                  argv.append("--dry-run")
    if run_startup_tests:        argv.append("--run-startup-tests")
    return argv


def build_benchmark(*, budget_min=None, preset=None, date=None, depots=None,
                    cluster_factors=None, budget_profiles=None, seed_restarts=None,
                    workers=None, pause_sec=None, only=None, list_only: bool = False,
                    dry_run: bool = False, force_matrix: bool = False,
                    fresh_osm: bool = False, run_startup_tests: bool = False,
                    stop_on_failure: bool = False) -> list[str]:
    """
    Poziční budget_min XOR --preset (právě jedno). Ostatní flagy volitelné.
    Vyhodí ValueError při porušení exkluzivity.
    """
    if (budget_min is None) == (preset is None):
        raise ValueError("Zadej právě jedno: budget_min NEBO preset.")
    argv = [PY, "benchmark_all_depots_solver_v6.py"]
    if budget_min is not None:
        argv.append(_fmt_num(budget_min))        # POZIČNÍ argument
    else:
        argv += ["--preset", str(preset)]
    if date:                     argv += ["--date", str(date)]
    if depots:                   argv += ["--depots", str(depots)]
    if cluster_factors:          argv += ["--cluster-factors", str(cluster_factors)]
    if budget_profiles:          argv += ["--budget-profiles", str(budget_profiles)]
    if seed_restarts is not None: argv += ["--seed-restarts", str(seed_restarts)]
    if workers is not None:      argv += ["--workers", str(workers)]
    if pause_sec is not None:    argv += ["--pause-sec", _fmt_num(pause_sec)]
    if only:                     argv += ["--only", str(only)]
    if list_only:                argv.append("--list-only")
    if dry_run:                  argv.append("--dry-run")
    if force_matrix:             argv.append("--force-matrix")
    if fresh_osm:                argv.append("--fresh-osm")
    if run_startup_tests:        argv.append("--run-startup-tests")
    if stop_on_failure:          argv.append("--stop-on-failure")
    return argv
