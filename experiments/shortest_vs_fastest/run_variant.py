"""
FÁZE 2 — plný běh PRODUKČNÍHO solveru proti zvolené routing instanci.

Klíčové: solver se sem IMPORTUJE, nekopíruje. Je to bit za bit tentýž program
jako v produkci — jen mu v paměti tohoto procesu podstrčíme jinou routing URL.
Na disku se nemění NIC (osm_routing.py zůstává netknutý).

Jak: solver bere URL z OSM_PRESETS["stable"|"current"]. Přepíšeme preset
"current" na experimentální port a solver spustíme s --fresh-osm.

Použití (z experiments/shortest_vs_fastest/):
  python run_variant.py --orders-file ..\\..\\data\\prepared\\CB\\orders_CB_2026-07-17.csv \\
                        --variant fastest  --budget-min 5
  python run_variant.py --orders-file ..\\..\\data\\prepared\\CB\\orders_CB_2026-07-17.csv \\
                        --variant shortest --budget-min 5

Výstupy i run log jdou do results/ zde — do data/results/ se nesahá.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

EXP_DIR   = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[1]

VARIANT_URLS = {
    "fastest":  "http://localhost:5000",   # produkční stable (jen čteme)
    "shortest": "http://localhost:5002",   # experimentální instance
}
# ORS je v OBOU variantách stejné - drží se konstantní, aby jedinou proměnnou
# zůstal profil pro 'driving'. Uzavírky (2 aktivní) přes něj počítají objízdky
# (apply_closures_to_matrix dělá "exact ORS avoid-route replacement"), takže
# kontejner ors-hgv MUSÍ běžet: docker start ors-hgv
ORS_URL = "http://localhost:8080"
# Flotila bez kamionů → potřeba jen profil 'driving' (žádná druhá ORS instance).
DEFAULT_FLEET = EXP_DIR / "fleet" / "vehicle_types_no_hgv.csv"


def main() -> None:
    p = argparse.ArgumentParser(description="Fáze 2: produkční solver proti zvolené instanci.")
    p.add_argument("--orders-file", required=True)
    p.add_argument("--variant", choices=sorted(VARIANT_URLS), required=True)
    p.add_argument("--budget-min", type=float, default=5.0)
    p.add_argument("--vehicle-types-file", default=str(DEFAULT_FLEET))
    p.add_argument("--force-matrix", action="store_true", default=True,
                   help="default zapnuto (CB/PR narážejí na limit nedosažitelných párů)")
    args = p.parse_args()

    url = VARIANT_URLS[args.variant]
    orders_file = Path(args.orders_file).resolve()
    fleet_file = Path(args.vehicle_types_file).resolve()
    if not orders_file.exists():
        sys.exit(f"[CHYBA] Orders soubor neexistuje: {orders_file}")
    if not fleet_file.exists():
        sys.exit(f"[CHYBA] Flotila neexistuje: {fleet_file}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = EXP_DIR / "results" / f"{orders_file.stem}_{args.variant}_{stamp}"
    run_log = EXP_DIR / "results" / "run_log.jsonl"

    # Solver čte relativní cesty vůči kořeni repa (data/static/closures.json apod.)
    os.chdir(REPO_ROOT)
    sys.path.insert(0, str(REPO_ROOT))

    # Startup + routing testy přeskakujeme:
    #  - unit testy už běží v produkci (292 zelených)
    #  - integrační test ORS-vs-OSRM porovnává shodu vzdáleností, což u varianty
    #    'shortest' NEMŮŽE projít (jiné vzdálenosti jsou celý smysl experimentu)
    #  - obě OSRM instance jsou ověřené ručně
    os.environ["SKIP_STARTUP_TESTS"] = "1"

    # ── Podstrčení URL: JEN v paměti tohoto procesu ──────────────────────
    import osm_routing
    osm_routing.OSM_PRESETS["current"] = {
        "osrm_url": url,
        # driving = testovaná proměnná; driving-hgv = konstantní produkční ORS
        # (potřebné pro objízdky uzavírek, viz ORS_URL výše)
        "osrm_urls": {"driving": url, "driving-hgv": ORS_URL},
    }

    import vrp_solver_lines_v6 as solver

    print("=" * 70)
    print(f"VARIANTA: {args.variant.upper()}  →  driving = {url}")
    print(f"          ORS (uzavírky, konstantní) = {ORS_URL}")
    print(f"orders:   {orders_file.name}")
    print(f"flotila:  {fleet_file.name}")
    print(f"výstup:   {out_dir}")
    print("[i] Startup/routing testy přeskočeny (viz komentář v run_variant.py).")
    print("=" * 70)

    sys.argv = [
        "vrp_solver_lines_v6.py",
        "--orders-file", str(orders_file),
        "--vehicle-types-file", str(fleet_file),
        "--output-dir", str(out_dir),
        "--run-log-path", str(run_log),
        "--budget-min", str(args.budget_min),
        "--fresh-osm",                      # → použije přepsaný preset "current"
    ]
    if args.force_matrix:
        sys.argv.append("--force-matrix")

    solver.main()

    print(f"\nHOTOVO → {out_dir}")
    print(f"Porovnej zone_summary.json obou variant (total_cost_kc, total_km, total_hours).")


if __name__ == "__main__":
    main()
