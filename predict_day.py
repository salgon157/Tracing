"""
predict_day.py — tenký spouštěč predikčního běhu (žádná plánovací logika)
==========================================================================

Predikce = úplně normální pipeline (prepare → solve → visualize) nad
predikovanými objednávkami z firemního systému, jen s odděleným kořenem dat,
aby se nikdy nemíchala s ostrým provozem:

  vstup:    data/prediction/input/{DEPO}/aktivni/riro-YYYYMMDD-{DEPO}-POB.csv
  prepared: data/prediction/prepared/{DEPO}/
  výsledky: data/prediction/results/{DEPO}/{YYYY-MM-DD}_{HHMM}/
  run log:  data/prediction/results/run_log.jsonl  (ostrá historie zůstává čistá)

Použití:
  python predict_day.py                 # všechna depa, která mají soubor v aktivni/
  python predict_day.py CB MO           # jen vybraná depa
  python predict_day.py --budget 10     # jiný solver budget (default 5 min)
  python predict_day.py --no-visualize  # bez map

Startup testy běží JEDNOU na začátku (ne 2× na každé depo jako v ručním
postupu); --skip-tests je vynechá úplně.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from prepare_inputs_v6 import find_active_riro_file

PY              = sys.executable
PREDICTION_ROOT = Path("data/prediction")
RUN_LOG_REL     = PREDICTION_ROOT / "results" / "run_log.jsonl"
ALL_DEPOTS      = ["CB", "HK", "MO", "PR"]


def _fmt_num(x) -> str:
    """5.0 → '5', 5.5 → '5.5' (stejně jako webui/app/commands.py)."""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def depots_with_input(root: Path = PREDICTION_ROOT) -> list[str]:
    """Depa, která mají CSV v {root}/input/{DEPO}/aktivni/."""
    found = []
    for depot in ALL_DEPOTS:
        aktivni = root / "input" / depot / "aktivni"
        if aktivni.is_dir() and any(
            f.is_file() and f.suffix == ".csv" for f in aktivni.iterdir()
        ):
            found.append(depot)
    return found


def build_depot_commands(depot: str, date_str: str, stamp: str, *,
                         budget_min: float, force_matrix: bool,
                         fresh_osm: bool, visualize: bool,
                         root: Path = PREDICTION_ROOT) -> tuple[list[list[str]], Path]:
    """Složí příkazy pro jedno depo — tytéž jako denní běh, jen pod {root}.
    Vrací (seznam argv, výstupní složka)."""
    orders  = root / "prepared" / depot / f"orders_{depot}_{date_str}.csv"
    out_dir = root / "results" / depot / f"{date_str}_{stamp}"

    prepare = [PY, "prepare_inputs_v6.py", depot, "--data-root", root.as_posix()]

    solve = [PY, "vrp_solver_lines_v6.py",
             "--orders-file", orders.as_posix(),
             "--output-dir", out_dir.as_posix(),
             "--budget-min", _fmt_num(budget_min),
             "--run-log-path", (root / "results" / "run_log.jsonl").as_posix()]
    if force_matrix:
        solve.append("--force-matrix")
    if fresh_osm:
        solve.append("--fresh-osm")

    cmds = [prepare, solve]
    if visualize:
        # NIKDY --open (stejná zásada jako webui)
        vis = [PY, "visualize_routes.py", out_dir.as_posix()]
        if fresh_osm:
            vis.append("--fresh-osm")
        cmds.append(vis)
    return cmds, out_dir


def run_startup_tests_once(skip: bool) -> None:
    """Stejná sada jako prepare_inputs_v6.run_startup_tests, ale jen jednou."""
    if skip:
        return
    print("[TEST] Spouštím startup testy (jednou pro celý predikční běh)...")
    r = subprocess.run(
        [PY, "-m", "pytest", "tests",
         "--ignore", str(Path("tests") / "test_ors_hgv_integration.py"),
         "-x", "-q", "--tb=short", "--no-header"],
    )
    if r.returncode != 0:
        sys.exit("[ABORT] Startup testy selhaly — predikce se nespustí.")
    print()


def read_zone_summary(out_dir: Path) -> dict | None:
    p = out_dir / "zone_summary.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Predikční běh: normální pipeline nad data/prediction/.")
    parser.add_argument("depots", nargs="*",
                        help="Depa (CB HK MO PR). Bez zadání: všechna, která mají "
                             "soubor v data/prediction/input/{DEPO}/aktivni/.")
    parser.add_argument("--budget", type=float, default=5.0,
                        help="Solver budget na jedno depo v minutách (default: 5)")
    parser.add_argument("--force-matrix", action="store_true",
                        help="Předá se solveru (Praha ho obvykle potřebuje)")
    parser.add_argument("--fresh-osm", action="store_true",
                        help="Předá se solveru i vizualizaci (porty 5001/8081)")
    parser.add_argument("--no-visualize", action="store_true",
                        help="Nevytvářet mapy (default: mapy se generují — sklad je používá)")
    parser.add_argument("--skip-tests", action="store_true",
                        help="Přeskočit startup testy úplně")
    args = parser.parse_args()

    if not Path("vrp_solver_lines_v6.py").exists():
        sys.exit("[CHYBA] Spusť z kořene repa (tam, kde je vrp_solver_lines_v6.py).")

    depots = [d.upper() for d in args.depots] or depots_with_input()
    unknown = [d for d in depots if d not in ALL_DEPOTS]
    if unknown:
        sys.exit(f"[CHYBA] Neznámá depa: {', '.join(unknown)}. Platná: {', '.join(ALL_DEPOTS)}")
    if not depots:
        sys.exit("[CHYBA] Žádné depo nemá predikční soubor v "
                 f"{(PREDICTION_ROOT / 'input').as_posix()}/{{DEPO}}/aktivni/.")

    run_startup_tests_once(args.skip_tests)
    # Děti testy neopakují — proběhly (nebo byly vědomě přeskočeny) výše.
    env = {**os.environ, "SKIP_STARTUP_TESTS": "1"}

    stamp = datetime.now().strftime("%H%M")
    print("=" * 64)
    print(f"PREDIKČNÍ BĚH — depa: {', '.join(depots)} | budget {_fmt_num(args.budget)} min/depo | {stamp}")
    print("=" * 64)

    results: list[tuple[str, Path | None, str]] = []   # (depo, out_dir, status)
    for depot in depots:
        try:
            _, date_str = find_active_riro_file(depot, PREDICTION_ROOT / "input")
        except (FileNotFoundError, ValueError) as e:
            print(f"\n[{depot}] PŘESKOČENO — {e}")
            results.append((depot, None, "bez vstupu"))
            continue

        cmds, out_dir = build_depot_commands(
            depot, date_str, stamp,
            budget_min=args.budget, force_matrix=args.force_matrix,
            fresh_osm=args.fresh_osm, visualize=not args.no_visualize,
        )
        status = "ok"
        for cmd in cmds:
            print(f"\n[{depot}] $ {' '.join(cmd[1:])}")
            r = subprocess.run(cmd, env=env)
            if r.returncode != 0:
                status = f"selhalo (krok: {Path(cmd[1]).stem}, kód {r.returncode})"
                break
        results.append((depot, out_dir, status))

    # ── Souhrn ────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print("SOUHRN PREDIKCE")
    print("=" * 64)
    failed = False
    for depot, out_dir, status in results:
        if status != "ok":
            failed = failed or status != "bez vstupu"
            print(f"  {depot}: {status}")
            continue
        s = read_zone_summary(out_dir)
        if s:
            mix = ", ".join(f"{k}×{v}" for k, v in s.get("vehicle_type_mix", {}).items())
            print(f"  {depot}: {s.get('lines_count', '?')} tras ({mix}) | "
                  f"{s.get('total_cost_kc', 0):,.0f} Kč | {s.get('total_km', 0):,.0f} km "
                  f"| → {out_dir.as_posix()}")
        else:
            print(f"  {depot}: hotovo, ale chybí zone_summary.json → {out_dir.as_posix()}")
    print(f"\n  run log: {RUN_LOG_REL.as_posix()}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
