"""
plan_day.py — predikcí řízené plánování dne (P1 → rezervace → P2 → rozhodnutí)
==============================================================================

Orchestrátor NAD existující pipeline — solver se nemění, flotila se omezuje
generovanými vehicle_types soubory. Starý způsob spouštění (predict_day.py,
ruční prepare+solver) zůstává nedotčený.

  python plan_day.py predict              # celá predikční fáze
  python plan_day.py predict --budget 5   # solver budget per běh (default 5)

Co `predict` udělá (vše na L0 = 100 % nosnosti, okna −5/+25):

  P1   každé depo zvlášť s NEOMEZENÝMI velkými auty
       → „přání": kolik čeho by depo chtělo pro nejlevnější plán
  REZERVACE  velké typy podle přání; přetečené typy ořezané žebříčkem
       podle naloženosti linek (kg); nerezervované kusy = volný pool
  P2   depa SEKVENČNĚ (CB→MO→HK→PR) s budgetem: velká podle rezervací
       a průběžně ubíraná, malá neomezená → skutečná potřeba malých aut
  ROZHODNUTÍ  deficit malých → kg (X_NEED nejméně naložených linek)
       → porušení pro večer: L0 / L1+L2 (103 % + dvojlinky) / +L3 alert

Výstupy:
  data/prediction/results/{DEPO}/{DATE}_{HHMM}_P1|_P2/   (plné solver výstupy)
  data/prediction/results/plan_day/{DATE}_{HHMM}/        (flotily, decision)
  data/prediction/results/decision_{DATE}.json           (stabilní cesta
                                                          pro večerní běh)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import fleet_budget as fb
from predict_day import depots_with_input, run_startup_tests_once, _fmt_num
from prepare_inputs_v6 import find_active_riro_file
from vrp_solver_lines_v6 import find_vehicle_types_file

PY              = sys.executable
PREDICTION_ROOT = Path("data/prediction")
RUN_LOG         = PREDICTION_ROOT / "results" / "run_log.jsonl"
PLAN_DAY_ROOT   = PREDICTION_ROOT / "results" / "plan_day"


# ─────────────────────────────────────────────────────────────────────────────
#  Pomocné
# ─────────────────────────────────────────────────────────────────────────────

def resolve_depots_and_date(requested: list[str]) -> tuple[list[str], str]:
    """Depa v pořadí uzávěrek + JEDNO společné datum z aktivních riro souborů.
    Různá data napříč depy = chyba (predikce dne musí být konzistentní)."""
    present = requested or depots_with_input()
    depots = [d for d in fb.DEPOT_ORDER if d in present]
    skipped = sorted(set(present) - set(depots))
    if skipped:
        raise SystemExit(f"[CHYBA] Neznámá depa: {', '.join(skipped)}. "
                         f"Platná: {', '.join(fb.DEPOT_ORDER)}")
    if not depots:
        raise SystemExit(
            "[CHYBA] Žádné depo nemá predikční soubor v "
            f"{(PREDICTION_ROOT / 'input').as_posix()}/{{DEPO}}/aktivni/.")

    dates = {}
    for depot in depots:
        _, date_str = find_active_riro_file(depot, PREDICTION_ROOT / "input")
        dates[depot] = date_str
    if len(set(dates.values())) > 1:
        listing = ", ".join(f"{d}={v}" for d, v in dates.items())
        raise SystemExit(
            f"[CHYBA] Depa mají různá data závozu: {listing}\n"
            "        Predikce dne potřebuje všechna depa na stejný den.")
    return depots, next(iter(dates.values()))


def run_cmd(cmd: list[str], env: dict, label: str) -> None:
    print(f"\n[{label}] $ {' '.join(cmd[1:])}")
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        raise SystemExit(
            f"[ABORT] Krok '{label}' selhal (kód {result.returncode}) — "
            "predikce dne se zastavuje, nic dalšího se nespouští.")


def build_solver_cmd(depot: str, date_str: str, out_dir: Path,
                     fleet_file: Path, budget_min: float,
                     osm_source: str, force_matrix: bool) -> list[str]:
    orders = PREDICTION_ROOT / "prepared" / depot / f"orders_{depot}_{date_str}.csv"
    cmd = [PY, "vrp_solver_lines_v6.py",
           "--orders-file", orders.as_posix(),
           "--output-dir", out_dir.as_posix(),
           "--budget-min", _fmt_num(budget_min),
           "--run-log-path", RUN_LOG.as_posix(),
           "--vehicle-types-file", fleet_file.as_posix(),
           # L0: 100 % nosnosti (okna −5/+25 jsou default CONFIG)
           "--capacity-multiplier", "1.0"]
    if osm_source:
        cmd += ["--osm-source", osm_source]
    if force_matrix:
        cmd.append("--force-matrix")
    return cmd


def lines_from_run(out_dir: Path) -> list[dict]:
    summary = out_dir / "lines_summary.csv"
    if not summary.exists():
        raise SystemExit(f"[CHYBA] Běh {out_dir} nemá lines_summary.csv — "
                         "solver nedoběhl?")
    return fb.parse_lines_summary(summary)


# ─────────────────────────────────────────────────────────────────────────────
#  Report
# ─────────────────────────────────────────────────────────────────────────────

def format_report(date_str: str, stamp: str, depots: list[str],
                  allocation: dict, p2_by_depot: dict[str, list[dict]],
                  decision: dict, decision_path: Path) -> str:
    lines = ["", "=" * 66,
             f"PLAN_DAY PREDICT — {date_str}  (session {stamp})",
             "=" * 66]

    lines.append("\nP1 přání velkých aut (neomezená flotila):")
    all_types = sorted({t for w in allocation["wishes"].values() for t in w})
    for depot in depots:
        wish = allocation["wishes"].get(depot, {})
        txt = ", ".join(f"{t}×{wish[t]}" for t in all_types if t in wish) or "—"
        lines.append(f"  {depot}: {txt}")

    lines.append("\nRezervace velkých (přetečené typy ořezané žebříčkem kg):")
    for depot in depots:
        res = allocation["reservations"].get(depot, {})
        txt = ", ".join(f"{t}×{n}" for t, n in sorted(res.items())) or "—"
        lines.append(f"  {depot}: {txt}")
    free = {t: n for t, n in allocation["free_pool"].items() if n > 0}
    lines.append(f"  volný pool: "
                 + (", ".join(f"{t}×{n}" for t, n in sorted(free.items())) or "—"))
    for t in allocation["truncated"]:
        lines.append(f"  [!] {t['type']}: přání {t['wanted']} > "
                     f"sklad {t['available']} — ořezáno")

    lines.append("\nP2 (sekvenčně, velká podle rezervací, malá neomezená):")
    for depot in depots:
        depot_lines = p2_by_depot[depot]
        small = [l for l in depot_lines if l["type_code"] in decision["_small_codes"]]
        large = [l for l in depot_lines if l["type_code"] not in decision["_small_codes"]]
        lines.append(f"  {depot}: {len(depot_lines)} linek "
                     f"({len(small)} malých + {len(large)} velkých), "
                     f"{sum(l['total_kg'] for l in depot_lines):,.0f} kg")

    d = decision
    lines += ["", "-" * 66,
              f"Malá auta: potřeba {d['small_need']} | k dispozici "
              f"{d['small_available']} − {d['reserve']} rezerva = {d['usable']} "
              f"| deficit {d['deficit']}"]
    if d["deficit"] > 0:
        lines.append(f"Chybějící kg ({d['x_need']} nejméně naložených linek): "
                     f"{d['missing_kg']:,.0f} z {d['day_kg']:,.0f} kg "
                     f"= {d['missing_pct']:.2f} %")
    verdict = {
        (0, False): "L0 — žádná porušení, dvojlinky vypnuté",
        (1, False): "L1 + L2 — 103 % nosnosti + dvojlinky povolené",
        (1, True):  "L1 + L2 + !!! POTŘEBA L3 (kamiony/rampa — zatím alert) !!!",
    }[(d["level"], d["l3_needed"])]
    lines += [f"ROZHODNUTÍ PRO VEČER: {verdict}",
              f"decision → {decision_path}",
              "=" * 66]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  Fáze predict
# ─────────────────────────────────────────────────────────────────────────────

def main_predict(args: argparse.Namespace) -> None:
    depots, date_str = resolve_depots_and_date([d.upper() for d in args.depots])
    run_startup_tests_once(args.skip_tests)
    env = {**os.environ, "SKIP_STARTUP_TESTS": "1"}

    stamp = datetime.now().strftime("%H%M")
    session = PLAN_DAY_ROOT / f"{date_str}_{stamp}"
    session.mkdir(parents=True, exist_ok=True)

    fleet_path = find_vehicle_types_file()
    fleet_rows = fb.load_fleet_rows(fleet_path)
    small_codes = fb.small_type_codes(fleet_rows)
    avail = fb.available_by_type(fleet_rows)
    large_avail = {t: n for t, n in avail.items() if t not in small_codes}
    small_full = {t: n for t, n in avail.items() if t in small_codes}

    print("=" * 66)
    print(f"PLAN_DAY PREDICT — {date_str} | depa: {', '.join(depots)} | "
          f"budget {_fmt_num(args.budget)} min/běh | session {stamp}")
    print(f"Vozový park: {fleet_path} | malá: {sum(small_full.values())} ks "
          f"| velká: {sum(large_avail.values())} ks")
    print("=" * 66)

    # ── prepare (jednou per depo — P1 i P2 jedou nad týmiž objednávkami) ──
    for depot in depots:
        run_cmd([PY, "prepare_inputs_v6.py", depot,
                 "--data-root", PREDICTION_ROOT.as_posix(), "--prediction"],
                env, f"prepare {depot}")

    # ── P1: neomezená velká ──────────────────────────────────────────────
    p1_fleet = fb.write_fleet_file(fleet_rows, session / "fleet_P1.csv",
                                   fb.p1_overrides(fleet_rows))
    p1_by_depot: dict[str, list[dict]] = {}
    for depot in depots:
        out_dir = PREDICTION_ROOT / "results" / depot / f"{date_str}_{stamp}_P1"
        run_cmd(build_solver_cmd(depot, date_str, out_dir, p1_fleet,
                                 args.budget, args.osm_source,
                                 args.force_matrix),
                env, f"P1 {depot}")
        p1_by_depot[depot] = lines_from_run(out_dir)

    # ── Rezervace velkých ────────────────────────────────────────────────
    allocation = fb.allocate_reservations(p1_by_depot, large_avail)

    # ── P2: sekvenčně s budgetem ─────────────────────────────────────────
    budget = fb.FleetBudget.from_fleet(fleet_rows)
    p2_by_depot: dict[str, list[dict]] = {}
    for depot in depots:
        caps = fb.caps_for_depot(depot, depots, budget,
                                 allocation["reservations"],
                                 small_codes, small_full)
        p2_fleet = fb.write_fleet_file(fleet_rows,
                                       session / f"fleet_P2_{depot}.csv", caps)
        out_dir = PREDICTION_ROOT / "results" / depot / f"{date_str}_{stamp}_P2"
        run_cmd(build_solver_cmd(depot, date_str, out_dir, p2_fleet,
                                 args.budget, args.osm_source,
                                 args.force_matrix),
                env, f"P2 {depot}")
        depot_lines = lines_from_run(out_dir)
        p2_by_depot[depot] = depot_lines
        # z budgetu ubývají jen velká — malá se měří, ne maskují
        used_large = {t: n for t, n in fb.count_by_type(depot_lines).items()
                      if t not in small_codes}
        budget.consume(used_large, context=f"P2 {depot}")

    # ── Rozhodnutí ───────────────────────────────────────────────────────
    all_p2_lines = [l for lines in p2_by_depot.values() for l in lines]
    small_lines = [l for l in all_p2_lines if l["type_code"] in small_codes]
    day_kg = sum(l["total_kg"] for l in all_p2_lines)
    decision = fb.decide_level(small_lines, sum(small_full.values()), day_kg)

    decision_doc = {
        "date": date_str,
        "session": stamp,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "depots": depots,
        "level": decision["level"],
        "dvojlinky": decision["dvojlinky"],
        "l3_needed": decision["l3_needed"],
        "solver_flags": fb.solver_flags_for_level(decision),
        "reservations": allocation["reservations"],
        "free_pool": allocation["free_pool"],
        "wishes_p1": allocation["wishes"],
        "truncated": allocation["truncated"],
        "small": {k: v for k, v in decision.items()
                  if not k.startswith("_")},
        "fleet_file": Path(fleet_path).name,
        "params": {
            "SMALL_FLEET_RESERVE": fb.SMALL_FLEET_RESERVE,
            "L3_THRESHOLD_PCT": fb.L3_THRESHOLD_PCT,
            "UNLIMITED_LARGE_COUNT": fb.UNLIMITED_LARGE_COUNT,
            "SMALL_MAX_KG": fb.SMALL_MAX_KG,
            "budget_min": args.budget,
            "osm_source": args.osm_source,
        },
        "runs": {
            "P1": {d: (PREDICTION_ROOT / "results" / d /
                       f"{date_str}_{stamp}_P1").as_posix() for d in depots},
            "P2": {d: (PREDICTION_ROOT / "results" / d /
                       f"{date_str}_{stamp}_P2").as_posix() for d in depots},
        },
    }
    decision_path = PREDICTION_ROOT / "results" / f"decision_{date_str}.json"
    for target in (decision_path, session / "decision.json"):
        target.write_text(json.dumps(decision_doc, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    decision["_small_codes"] = small_codes
    print(format_report(date_str, stamp, depots, allocation, p2_by_depot,
                        decision, decision_path))


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Predikcí řízené plánování dne (P1 → rezervace → P2 → "
                    "rozhodnutí o porušení).")
    sub = parser.add_subparsers(dest="phase", required=True)

    predict = sub.add_parser(
        "predict", help="Celá predikční fáze; zapíše decision_{DATE}.json")
    predict.add_argument("depots", nargs="*",
                         help="Depa (CB MO HK PR). Bez zadání: všechna s "
                              "predikčním vstupem. Plánuje se v pořadí uzávěrek.")
    predict.add_argument("--budget", type=float, default=5.0,
                         help="Solver budget na jeden běh v minutách "
                              "(default 5; běhů je 2× počet dep)")
    predict.add_argument("--osm-source", choices=["current", "stable"],
                         default="current", help="Routing instance")
    predict.add_argument("--force-matrix", action="store_true",
                         help="Předá se solveru")
    predict.add_argument("--skip-tests", action="store_true",
                         help="Přeskočit startup testy")
    args = parser.parse_args()

    if not Path("vrp_solver_lines_v6.py").exists():
        sys.exit("[CHYBA] Spusť z kořene repa.")
    if args.phase == "predict":
        main_predict(args)


if __name__ == "__main__":
    main()
