"""
benchmark_configs.py — Hledání optimálního rozdělení budgetu pro VRP solver
============================================================================
Spuštění:
  python benchmark_configs.py --blocks-dir data/prepared --time-limit 3600

Co dělá:
  1. Najde všechny orders_block_*.csv v --blocks-dir
  2. Pro každý blok otestuje N konfigurací rozdělení budgetu C/D/E
  3. Zaznamená skutečnou dobu Phase C (přirozený strop) a výslednou cenu
  4. Uloží výsledky do benchmark_results.csv + vypíše doporučení

Proč:
  Phase C má přirozený strop = doba 9 parallel solve úloh (3 seedy × 3 clustery).
  Jakmile doběhnou, zbytek C budgetu je zahozený čas ukradený LNS (Phase D).
  Cílem je zjistit minimální C které stačí, a zbytek dát D.
"""

import csv
import argparse
import subprocess
import sys
import time
import json
import copy
import math
import multiprocessing
import random
import requests
import numpy as np
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from sklearn.cluster import KMeans
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

# ── Import solveru ────────────────────────────────────────────
# Předpokládáme že vrp_solver_lines_v6.py je ve stejné složce.
# Importujeme přímo funkce místo subprocess — přesnější měření.
sys.path.insert(0, str(Path(__file__).parent))
try:
    import vrp_solver_lines_v6 as solver_module
except ImportError:
    print("[CHYBA] vrp_solver_lines_v6.py nenalezen ve stejné složce.")
    sys.exit(1)

# ── Konfigurace benchmark ─────────────────────────────────────

# Testované konfigurace C/D/E (součet musí být 1.0)
# Logika: C je seed solve s přirozeným stropem, D je LNS který vždy využije čas
CONFIGS_TO_TEST = [
    {"name": "current",      "C": 0.30, "D": 0.50, "E": 0.20},
    {"name": "lns_heavy",    "C": 0.20, "D": 0.60, "E": 0.20},
    {"name": "lns_max",      "C": 0.15, "D": 0.70, "E": 0.15},
    {"name": "lns_dominant", "C": 0.10, "D": 0.75, "E": 0.15},
    {"name": "e_heavy",      "C": 0.15, "D": 0.55, "E": 0.30},
]

OUTPUT_CSV = Path("benchmark_results.csv")
OUTPUT_SUMMARY = Path("benchmark_summary.txt")


# ── Spuštění jedné konfigurace na jednom bloku ────────────────

def run_one(orders_file: Path, vehicles_file: Path,
            config: dict, time_budget_sec: int) -> dict:
    """
    Spustí celý solver pipeline s danou konfigurací.
    Vrátí dict s výsledky včetně skutečných časů fází.
    """
    # Nastav konfiguraci v solver modulu
    original_config = copy.deepcopy(solver_module.CONFIG)
    solver_module.CONFIG["budget_phase_C_pct"] = config["C"]
    solver_module.CONFIG["budget_phase_D_pct"] = config["D"]
    solver_module.CONFIG["budget_phase_E_pct"] = config["E"]
    solver_module.CONFIG["total_time_budget_sec"] = time_budget_sec

    result = {
        "block":          orders_file.stem,
        "config_name":    config["name"],
        "C_pct":          config["C"],
        "D_pct":          config["D"],
        "E_pct":          config["E"],
        "total_cost_kc":  None,
        "lines_count":    None,
        "phase_C_actual_sec": None,
        "phase_D_actual_sec": None,
        "phase_E_actual_sec": None,
        "phase_C_budget_sec": None,
        "phase_C_wasted_sec": None,   # klíčová metrika
        "osrm_sec":       None,
        "total_sec":      None,
        "error":          None,
    }

    try:
        t_total_start = time.time()

        # Načti data
        vehicles = solver_module.load_vehicle_types_db(str(vehicles_file))
        orders   = solver_module.load_orders_day(str(orders_file))

        # OSRM matice
        t_osrm = time.time()
        locations = ([(solver_module.DEPOT["lat"], solver_module.DEPOT["lon"])]
                     + [(o["lat"], o["lon"]) for o in orders])
        distances_km, durations_min = solver_module.get_matrix(locations)
        osrm_elapsed = time.time() - t_osrm
        result["osrm_sec"] = round(osrm_elapsed, 1)

        remaining = time_budget_sec - osrm_elapsed
        budget_C  = remaining * config["C"]
        budget_D  = remaining * config["D"]
        budget_E  = remaining * config["E"]
        result["phase_C_budget_sec"] = round(budget_C, 1)

        n_clusters = int(solver_module.CONFIG.get("num_clusters", 3))
        n_workers  = max(1, multiprocessing.cpu_count() - 1)

        # Phase C — měříme skutečný čas
        t_c = time.time()
        state = solver_module.phase_c_best_seed(
            orders, vehicles, distances_km, durations_min,
            n_clusters, int(budget_C), n_workers
        )
        phase_C_actual = time.time() - t_c
        result["phase_C_actual_sec"] = round(phase_C_actual, 1)
        result["phase_C_wasted_sec"] = round(max(0, budget_C - phase_C_actual), 1)

        # Phase D — LNS
        t_d   = time.time()
        state = solver_module.phase_d_lns(
            state, distances_km, durations_min, budget_D, n_workers
        )
        result["phase_D_actual_sec"] = round(time.time() - t_d, 1)

        # Phase E — intenzifikace
        t_e   = time.time()
        state = solver_module.phase_e_intensify(
            state, distances_km, durations_min, budget_E, n_workers
        )
        result["phase_E_actual_sec"] = round(time.time() - t_e, 1)

        routes     = state.all_routes()
        total_cost = state.total_cost
        result["total_cost_kc"] = total_cost
        result["lines_count"]   = len(routes)
        result["total_sec"]     = round(time.time() - t_total_start, 1)

    except Exception as e:
        result["error"] = str(e)[:120]

    finally:
        # Obnov původní konfiguraci
        for k, v in original_config.items():
            solver_module.CONFIG[k] = v

    return result


# ── Hlavní benchmark smyčka ───────────────────────────────────

def find_blocks(blocks_dir: Path) -> list:
    """Najde všechny orders_block_*.csv soubory."""
    files = sorted(blocks_dir.glob("orders_block_*.csv"))
    if not files:
        # Fallback: jakýkoli orders_*.csv
        files = sorted(blocks_dir.glob("orders_*.csv"))
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blocks-dir",     default="data/prepared",
                        help="Složka s orders_block_*.csv soubory")
    parser.add_argument("--vehicles-file",  default="data/static/vehicle_types.csv")
    parser.add_argument("--time-limit",     type=int, default=3600,
                        help="Celkový budget v sekundách (default: 3600 = 60 min)")
    parser.add_argument("--blocks",         nargs="*", default=None,
                        help="Omez na konkrétní bloky (např. --blocks 211 212)")
    args = parser.parse_args()

    blocks_dir   = Path(args.blocks_dir)
    vehicles_file = Path(args.vehicles_file)
    time_budget  = args.time_limit

    if not blocks_dir.exists():
        print(f"[CHYBA] {blocks_dir} neexistuje.")
        sys.exit(1)

    all_blocks = find_blocks(blocks_dir)
    if args.blocks:
        all_blocks = [b for b in all_blocks
                      if any(bid in b.stem for bid in args.blocks)]

    if not all_blocks:
        print(f"[CHYBA] Žádné bloky nenalezeny v {blocks_dir}")
        sys.exit(1)

    n_total = len(all_blocks) * len(CONFIGS_TO_TEST)
    print("=" * 65)
    print(f"Benchmark: {len(all_blocks)} bloků × "
          f"{len(CONFIGS_TO_TEST)} konfigurací = {n_total} spuštění")
    print(f"Budget per spuštění: {time_budget // 60} min")
    print(f"Odhadovaný celkový čas: "
          f"{n_total * time_budget / 3600:.1f} hodin")
    print("=" * 65)
    print("\nTip: Omez na 1–2 bloky pro rychlý test:")
    print(f"  python benchmark_configs.py --blocks 211 212\n")

    all_results = []
    run_idx = 0

    for block_file in all_blocks:
        print(f"\n{'─' * 65}")
        print(f"Blok: {block_file.stem}")
        print(f"{'─' * 65}")

        block_results = []
        for config in CONFIGS_TO_TEST:
            run_idx += 1
            print(f"\n[{run_idx}/{n_total}] Config '{config['name']}' "
                  f"(C={config['C']} D={config['D']} E={config['E']})...")

            result = run_one(block_file, vehicles_file, config, time_budget)
            block_results.append(result)
            all_results.append(result)

            if result["error"]:
                print(f"  [!] CHYBA: {result['error']}")
            else:
                wasted = result["phase_C_wasted_sec"]
                print(f"  Cena: {result['total_cost_kc']:,.0f} Kč | "
                      f"Lines: {result['lines_count']} | "
                      f"C skutečně: {result['phase_C_actual_sec']}s "
                      f"(zahozeno: {wasted}s) | "
                      f"D: {result['phase_D_actual_sec']}s | "
                      f"E: {result['phase_E_actual_sec']}s")

        # Průběžné mini-srovnání pro tento blok
        valid = [r for r in block_results if r["total_cost_kc"] is not None]
        if valid:
            best  = min(valid, key=lambda r: r["total_cost_kc"])
            worst = max(valid, key=lambda r: r["total_cost_kc"])
            diff  = worst["total_cost_kc"] - best["total_cost_kc"]
            print(f"\n  → Nejlepší: '{best['config_name']}' "
                  f"({best['total_cost_kc']:,.0f} Kč)")
            print(f"  → Rozdíl best/worst: {diff:,.0f} Kč "
                  f"({100*diff/best['total_cost_kc']:.1f} %)")
            c_natural = min(r["phase_C_actual_sec"] for r in valid
                            if r["phase_C_actual_sec"] is not None)
            print(f"  → Přirozený strop Phase C: ~{c_natural:.0f} sec "
                  f"({c_natural/60:.1f} min)")

    # Ulož CSV
    if all_results:
        fieldnames = list(all_results[0].keys())
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(all_results)
        print(f"\n\nVýsledky uloženy: {OUTPUT_CSV}")

    # Souhrnná analýza
    _print_summary(all_results, time_budget)


def _print_summary(results: list, time_budget: int):
    valid = [r for r in results if r["total_cost_kc"] is not None]
    if not valid:
        print("\nŽádné validní výsledky pro souhrn.")
        return

    print("\n" + "=" * 65)
    print("SOUHRNNÁ ANALÝZA")
    print("=" * 65)

    # Průměrný přirozený strop Phase C
    c_actuals = [r["phase_C_actual_sec"] for r in valid
                 if r["phase_C_actual_sec"] is not None]
    if c_actuals:
        avg_c = sum(c_actuals) / len(c_actuals)
        max_c = max(c_actuals)
        print(f"\nPřirozený strop Phase C:")
        print(f"  Průměr: {avg_c:.0f} sec ({avg_c/60:.1f} min)")
        print(f"  Maximum: {max_c:.0f} sec ({max_c/60:.1f} min)")
        pct_needed = max_c / (time_budget * 0.9)  # 0.9 = zbytek po OSRM
        print(f"  Doporučené minimum budget_phase_C_pct: {pct_needed:.2f} "
              f"(= {max_c:.0f} sec z {time_budget * 0.9:.0f} sec zbývajících)")

    # Průměrná cena per konfigurace
    print(f"\nPrůměrná cena per konfigurace (nižší = lepší):")
    config_costs = {}
    for r in valid:
        name = r["config_name"]
        config_costs.setdefault(name, []).append(r["total_cost_kc"])

    ranked = sorted(config_costs.items(),
                    key=lambda x: sum(x[1]) / len(x[1]))
    best_avg = sum(ranked[0][1]) / len(ranked[0][1])

    for name, costs in ranked:
        avg  = sum(costs) / len(costs)
        diff = avg - best_avg
        bars = "█" * int(10 * avg / (best_avg * 1.1))
        print(f"  {name:16s}  {avg:>10,.0f} Kč  "
              f"(+{diff:,.0f} Kč vs best)  {bars}")

    best_config = ranked[0][0]
    print(f"\n→ DOPORUČENÁ KONFIGURACE: '{best_config}'")

    cfg = next(c for c in CONFIGS_TO_TEST if c["name"] == best_config)
    print(f"   budget_phase_C_pct: {cfg['C']}")
    print(f"   budget_phase_D_pct: {cfg['D']}")
    print(f"   budget_phase_E_pct: {cfg['E']}")

    # Zapiš summary
    lines = []
    lines.append("BENCHMARK SUMMARY\n")
    lines.append(f"Budget: {time_budget} sec\n\n")
    for name, costs in ranked:
        avg = sum(costs) / len(costs)
        lines.append(f"{name}: {avg:,.0f} Kč avg\n")
    lines.append(f"\nDoporučení: {best_config}\n")
    if c_actuals:
        lines.append(f"Phase C přirozený strop: max {max_c:.0f} sec\n")
        lines.append(f"Doporučené budget_phase_C_pct: {pct_needed:.2f}\n")

    OUTPUT_SUMMARY.write_text("".join(lines), encoding="utf-8")
    print(f"\nSouhrn uložen: {OUTPUT_SUMMARY}")


if __name__ == "__main__":
    main()
