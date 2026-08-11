"""
compare_seed_finalists.py — férové A/B srovnání --seed-finalists 1 vs N
=======================================================================

Solver není reprodukovatelný (GLS zastavuje wall-clock limit, ne počet
kroků), proto jeden pár běhů nedokazuje nic. Metodika:

  - varianty se STŘÍDAJÍ v čase (A,B,A,B…) — zpomalení stroje během testu
    dopadne na obě varianty stejně
  - ≥3 opakování na variantu; vyhodnocuje se MEDIÁN a NEJHORŠÍ běh
    (smysl finalistů není zlepšit průměr, ale zabránit propadákům)
  - vlastní output-dir (data/results/_ab_finalists/, gitignored) i vlastní
    run log — ostrá data ani historie se nedotknou

Příklady:

  # predikční data 10. 8., plný sklad, 3 opakování na variantu, 5 min/běh
  python compare_seed_finalists.py --date 2026-08-10

  # jen problémová depa, flotily přesně jako v P2 seanci 19:59
  python compare_seed_finalists.py --date 2026-08-10 --depots HK PR \
      --fleet-template data/prediction/results/plan_day/2026-08-10_1959/fleet_P2_{DEPO}.csv
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PY         = sys.executable
DEPOTS_ALL = ["CB", "MO", "HK", "PR"]
AB_ROOT    = Path("data/results/_ab_finalists")


def run_one(orders: Path, out_dir: Path, fleet_file: Path, budget: float,
            finalists: str, osm: str, run_log: Path, console_log: Path,
            env: dict) -> dict:
    cmd = [PY, "vrp_solver_lines_v6.py",
           "--orders-file", orders.as_posix(),
           "--output-dir", out_dir.as_posix(),
           "--budget-min", f"{budget:g}",
           "--run-log-path", run_log.as_posix(),
           "--vehicle-types-file", fleet_file.as_posix(),
           "--seed-finalists", finalists,
           "--osm-source", osm]
    t0 = time.time()
    with open(console_log, "w", encoding="utf-8") as lf:
        rc = subprocess.run(cmd, env=env, stdout=lf,
                            stderr=subprocess.STDOUT).returncode
    if rc != 0:
        raise SystemExit(f"[ABORT] Běh {out_dir.name} selhal (kód {rc}) — "
                         f"viz {console_log}")
    z = json.loads((out_dir / "zone_summary.json").read_text(encoding="utf-8"))
    return {"cost": z["total_cost_kc"], "lines": z["lines_count"],
            "km": z["total_km"], "min": round((time.time() - t0) / 60, 1)}


def summary_table(results: dict, finalists_b: str) -> str:
    lines = ["", "=" * 72,
             f"SOUHRN — A (finalisté 1 = dosavadní) vs B (finalisté {finalists_b})",
             "=" * 72]
    tot = {"A": 0.0, "B": 0.0}
    for depot, r in results.items():
        lines.append(f"\n{depot}:")
        med = {}
        for v in ("A", "B"):
            costs = [x["cost"] for x in r[v]]
            med[v] = statistics.median(costs)
            tot[v] += med[v]
            lines.append(
                f"  {v}: " + " / ".join(f"{c:,.0f}" for c in costs)
                + f"  |  medián {med[v]:,.0f}  |  nejhorší {max(costs):,.0f}"
                + f"  |  linky {'/'.join(str(x['lines']) for x in r[v])}")
        d_med   = med["B"] - med["A"]
        d_worst = (max(x["cost"] for x in r["B"])
                   - max(x["cost"] for x in r["A"]))
        lines.append(f"  Δ (B−A): medián {d_med:+,.0f} Kč | "
                     f"nejhorší {d_worst:+,.0f} Kč")
    lines += [f"\nCELKEM Δ mediánů (B−A): {tot['B'] - tot['A']:+,.0f} Kč "
              "(záporné = finalisté levnější)",
              "=" * 72]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="A/B test --seed-finalists: střídané běhy, medián + "
                    "nejhorší, vlastní výstupy i run log.")
    ap.add_argument("--date", required=True, help="Datum závozu (YYYY-MM-DD)")
    ap.add_argument("--depots", nargs="*", default=DEPOTS_ALL,
                    help=f"Depa (default: {' '.join(DEPOTS_ALL)})")
    ap.add_argument("--data-root", default="data/prediction",
                    help="Kořen s prepared/{DEPO}/orders_… "
                         "(default: predikční data; ostrá = data)")
    ap.add_argument("--budget", type=float, default=5.0,
                    help="Solver budget na běh v minutách (default 5)")
    ap.add_argument("--reps", type=int, default=3,
                    help="Opakování NA VARIANTU (default 3 → 6 běhů/depo)")
    ap.add_argument("--finalists", default="3",
                    help="Varianta B: hodnota --seed-finalists (default 3; "
                         "A je vždy 1)")
    ap.add_argument("--fleet-file", default="",
                    help="Jeden vozový park pro všechna depa "
                         "(default: soubor ze static)")
    ap.add_argument("--fleet-template", default="",
                    help="Cesta s {DEPO} pro per-depo flotily, "
                         "např. …/fleet_P2_{DEPO}.csv")
    ap.add_argument("--osm-source", choices=["current", "stable"],
                    default="current")
    ap.add_argument("--skip-tests", action="store_true",
                    help="Přeskočit startup testy")
    args = ap.parse_args()

    if not Path("vrp_solver_lines_v6.py").exists():
        sys.exit("[CHYBA] Spusť z kořene repa.")

    from predict_day import run_startup_tests_once
    from vrp_solver_lines_v6 import find_vehicle_types_file
    run_startup_tests_once(args.skip_tests)
    env = {**os.environ, "SKIP_STARTUP_TESTS": "1", "PYTHONIOENCODING": "utf-8"}

    depots = [d.upper() for d in args.depots]

    def fleet_for(depot: str) -> Path:
        if args.fleet_template:
            return Path(args.fleet_template.replace("{DEPO}", depot))
        if args.fleet_file:
            return Path(args.fleet_file)
        return Path(find_vehicle_types_file())

    # Kontroly vstupů PŘED prvním during-run selháním
    for depot in depots:
        orders = Path(args.data_root) / "prepared" / depot \
            / f"orders_{depot}_{args.date}.csv"
        if not orders.exists():
            sys.exit(f"[CHYBA] Chybí {orders} — nejdřív prepare pro {depot}.")
        if not fleet_for(depot).exists():
            sys.exit(f"[CHYBA] Chybí vozový park {fleet_for(depot)}.")

    stamp = datetime.now().strftime("%H%M")
    root = AB_ROOT / f"{args.date}_{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    run_log = root / "run_log.jsonl"

    total_runs = len(depots) * args.reps * 2
    print("=" * 72)
    print(f"A/B TEST --seed-finalists 1 vs {args.finalists} | {args.date} | "
          f"depa: {', '.join(depots)}")
    print(f"{args.reps}× na variantu, střídání A,B | budget {args.budget:g} "
          f"min/běh | {total_runs} běhů → odhad "
          f"~{total_runs * (args.budget + 1):.0f} min")
    print(f"Výstupy: {root} (gitignored, ostrá data netknutá)")
    print("=" * 72)

    t_start = time.time()
    results: dict = {d: {"A": [], "B": []} for d in depots}
    for depot in depots:
        orders = Path(args.data_root) / "prepared" / depot \
            / f"orders_{depot}_{args.date}.csv"
        fleet = fleet_for(depot)
        for rep in range(1, args.reps + 1):
            for variant, fin in (("A", "1"), ("B", args.finalists)):
                name = f"{depot}_rep{rep}_{variant}"
                print(f"[{name}] finalisté={fin} …", end="", flush=True)
                out = run_one(orders, root / name, fleet, args.budget, fin,
                              args.osm_source, run_log,
                              root / f"{name}.log", env)
                results[depot][variant].append(out)
                print(f" {out['lines']} linek | {out['cost']:,.0f} Kč | "
                      f"{out['min']} min")

    print(summary_table(results, args.finalists))
    (root / "summary.json").write_text(
        json.dumps({"date": args.date, "budget_min": args.budget,
                    "finalists_b": args.finalists, "results": results},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Detail: {root / 'summary.json'}")
    print(f"Celková doba: {(time.time() - t_start) / 60:.0f} min")


if __name__ == "__main__":
    main()
