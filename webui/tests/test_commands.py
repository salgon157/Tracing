"""
Buildery argv — token po tokenu proti WORKFLOW.md příkazům.

WORKFLOW.md kanonické příkazy:
  python prepare_inputs_v6.py CB
  python vrp_solver_lines_v6.py --orders-file data/prepared/CB/orders_CB_YYYY-MM-DD.csv
  python visualize_routes.py data/results/CB/YYYY-MM-DD/   (── ale NIKDY --open)

argv[0] je konkrétní interpreter (sys.executable) — testujeme argv[1:]
(skript + argumenty), což je to, co musí sedět s WORKFLOW.md.

Spouštět:  python -m pytest webui/tests -q
"""

import pytest

from webui.app import commands


def test_build_prepare_matches_workflow():
    argv = commands.build_prepare("CB")
    assert argv[1:] == ["prepare_inputs_v6.py", "CB"]


def test_build_solve_minimal_matches_workflow():
    argv = commands.build_solve("CB", "2026-04-29")
    assert argv[1:] == [
        "vrp_solver_lines_v6.py",
        "--orders-file", "data/prepared/CB/orders_CB_2026-04-29.csv",
    ]


def test_build_solve_all_flags():
    argv = commands.build_solve(
        "PR", "2026-04-29", budget_min=5, force_matrix=True,
        fresh_osm=True, allow_profile_fallback=True)
    s = argv[1:]
    assert s[0] == "vrp_solver_lines_v6.py"
    assert s[1] == "--orders-file"
    assert s[2] == "data/prepared/PR/orders_PR_2026-04-29.csv"
    assert "--budget-min" in s and s[s.index("--budget-min") + 1] == "5"
    assert "--force-matrix" in s
    assert "--fresh-osm" in s
    assert "--allow-profile-fallback" in s


def test_budget_min_float_renders_clean():
    # 5.0 → '5', 5.5 → '5.5'
    assert commands.build_solve("CB", "2026-04-29", budget_min=5.0)[-1] == "5"
    assert commands.build_solve("CB", "2026-04-29", budget_min=5.5)[-1] == "5.5"


def test_build_visualize_never_open():
    argv = commands.build_visualize("data/results/CB/2026-04-29")
    assert "--open" not in argv
    assert argv[1:] == ["visualize_routes.py", "data/results/CB/2026-04-29"]


def test_build_visualize_flags_no_open():
    argv = commands.build_visualize(
        "data/results/CB/2026-04-29", no_osrm=True, fresh_osm=True)
    assert "--open" not in argv
    assert "--no-osrm" in argv
    assert "--fresh-osm" in argv


def test_orders_rel_path_relative():
    # Relativní cesta (cwd = kořen repa) → ručně reprodukovatelné.
    p = commands.orders_rel_path("MO", "2026-04-29")
    assert p == "data/prepared/MO/orders_MO_2026-04-29.csv"
    assert not p.startswith("/") and ":" not in p


# ── all-depots ───────────────────────────────────────────────────────────────

def test_all_depots_minimal():
    argv = commands.build_all_depots()
    assert argv[1:] == ["vrp_solver_lines_all_depots_v6.py"]


def test_all_depots_only_filled_flags():
    argv = commands.build_all_depots(date="2026-04-29", budget_min=5, dry_run=True)
    s = argv[1:]
    assert s == ["vrp_solver_lines_all_depots_v6.py",
                 "--date", "2026-04-29", "--budget-min", "5", "--dry-run"]
    # nevyplněné flagy se NEpředávají (defaulty nechány na skriptu)
    assert "--depots" not in s and "--clusters" not in s


def test_all_depots_full():
    argv = commands.build_all_depots(
        date="2026-04-29", depots="CB,MO", budget_ratios="0.35,0.25,0.40",
        clusters="8", workers=4, seed_restarts=3, force_matrix=True,
        fresh_osm=True, run_startup_tests=True)
    s = argv[1:]
    assert "--depots" in s and s[s.index("--depots") + 1] == "CB,MO"
    assert "--budget-ratios" in s and s[s.index("--budget-ratios") + 1] == "0.35,0.25,0.40"
    assert "--force-matrix" in s and "--fresh-osm" in s and "--run-startup-tests" in s


# ── benchmark: poziční budget_min XOR --preset ──────────────────────────────

def test_benchmark_positional_budget_min():
    argv = commands.build_benchmark(budget_min=30)
    assert argv[1:] == ["benchmark_all_depots_solver_v6.py", "30"]   # poziční, ne --budget-min


def test_benchmark_preset():
    argv = commands.build_benchmark(preset="tomorrow_4h")
    assert argv[1:] == ["benchmark_all_depots_solver_v6.py", "--preset", "tomorrow_4h"]


def test_benchmark_requires_exactly_one():
    with pytest.raises(ValueError):
        commands.build_benchmark()                       # nic
    with pytest.raises(ValueError):
        commands.build_benchmark(budget_min=30, preset="x")   # obojí


def test_benchmark_list_only_and_flags():
    argv = commands.build_benchmark(budget_min=30, list_only=True, date="2026-04-29",
                                    depots="CB,MO", force_matrix=True)
    s = argv[1:]
    assert s[0] == "benchmark_all_depots_solver_v6.py"
    assert s[1] == "30"                                  # poziční hned za skriptem
    assert "--list-only" in s and "--force-matrix" in s
    assert "--date" in s and s[s.index("--date") + 1] == "2026-04-29"
