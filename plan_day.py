"""
plan_day.py — predikcí řízené plánování dne (P1 → rezervace → P2 → rozhodnutí)
==============================================================================

Orchestrátor NAD existující pipeline — solver se nemění, flotila se omezuje
generovanými vehicle_types soubory. Starý způsob spouštění (predict_day.py,
ruční prepare+solver) zůstává nedotčený.

  python plan_day.py predict              # celá predikční fáze
  python plan_day.py predict --budget 5   # solver budget per běh (default 5)

Co `predict` udělá (vše na L0 = 100 % nosnosti, okna −5/+25):

  P1   každé depo zvlášť s CELÝM skladem (žádné nafukování počtů)
       → „přání": kolik čeho by depo chtělo pro nejlevnější plán;
       přetečení se pozná samo (jeden kamion, tři depa → tři přání)
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
import time
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

def resolve_depots_and_date(requested: list[str],
                            root: Path = PREDICTION_ROOT) -> tuple[list[str], str]:
    """Depa v pořadí uzávěrek + JEDNO společné datum z aktivních riro souborů.
    Různá data napříč depy = chyba (plánování dne musí být konzistentní)."""
    present = requested or depots_with_input(root=root)
    depots = [d for d in fb.DEPOT_ORDER if d in present]
    skipped = sorted(set(present) - set(depots))
    if skipped:
        raise SystemExit(f"[CHYBA] Neznámá depa: {', '.join(skipped)}. "
                         f"Platná: {', '.join(fb.DEPOT_ORDER)}")
    if not depots:
        raise SystemExit(
            "[CHYBA] Žádné depo nemá riro soubor v "
            f"{(root / 'input').as_posix()}/{{DEPO}}/aktivni/.")

    dates = {}
    for depot in depots:
        _, date_str = find_active_riro_file(depot, root / "input")
        dates[depot] = date_str
    if len(set(dates.values())) > 1:
        listing = ", ".join(f"{d}={v}" for d, v in dates.items())
        raise SystemExit(
            f"[CHYBA] Depa mají různá data závozu: {listing}\n"
            "        Plánování dne potřebuje všechna depa na stejný den.")
    return depots, next(iter(dates.values()))


def step_header(title: str, detail: str = "") -> None:
    """Oddělovač kroku — ať se v dlouhém výstupu solveru dá zorientovat."""
    print("\n" + "▶" + "─" * 65)
    print(f"▶ {title}")
    if detail:
        print(f"▶ {detail}")
    print("▶" + "─" * 65)


def run_cmd(cmd: list[str], env: dict, label: str) -> float:
    """Spustí podproces, vrátí dobu běhu v sekundách."""
    print(f"  $ {' '.join(cmd[1:])}")
    started = time.time()
    result = subprocess.run(cmd, env=env)
    elapsed = time.time() - started
    if result.returncode != 0:
        raise SystemExit(
            f"[ABORT] Krok '{label}' selhal (kód {result.returncode}) — "
            "běh se zastavuje, nic dalšího se nespouští.")
    return elapsed


def fmt_mix(counts: dict[str, int], small_codes: set[str] | None = None) -> str:
    """{'TYPE_02': 14} -> 'TYPE_02×14'; malá/velká odděleně, když je znám klíč."""
    if not counts:
        return "—"
    items = sorted(counts.items())
    if small_codes is None:
        return ", ".join(f"{t}×{n}" for t, n in items)
    small = [f"{t}×{n}" for t, n in items if t in small_codes]
    large = [f"{t}×{n}" for t, n in items if t not in small_codes]
    parts = []
    if small:
        parts.append("malá " + " ".join(small))
    if large:
        parts.append("velká " + " ".join(large))
    return " | ".join(parts) or "—"


def summarize_run(lines: list[dict], small_codes: set[str],
                  elapsed: float, prefix: str = "  →") -> None:
    """Krátká bilance jednoho solver běhu — hned pod jeho výstupem."""
    used = fb.vehicles_used_by_type(lines)
    kg = sum(l["total_kg"] for l in lines)
    doubles = sum(1 for l in lines if l.get("double_run"))
    print(f"{prefix} {len(lines)} linek"
          + (f" (z toho {doubles} dvojlinek)" if doubles else "")
          + f", {kg:,.0f} kg  |  {fmt_mix(used, small_codes)}"
          + f"  |  {elapsed / 60:.1f} min")


def build_solver_cmd(depot: str, date_str: str, out_dir: Path,
                     fleet_file: Path, budget_min: float,
                     osm_source: str, force_matrix: bool,
                     seed_finalists: str = "") -> list[str]:
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
    # Bez zadání se nepředává nic → solver jede na CONFIG (dnes "auto")
    if seed_finalists:
        cmd += ["--seed-finalists", seed_finalists]
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

    lines.append("\nP1 přání velkých aut (každé depo s celým skladem):")
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

    t_start = time.time()

    # ── prepare (jednou per depo — P1 i P2 jedou nad týmiž objednávkami) ──
    step_header(f"PŘÍPRAVA DAT — {len(depots)} dep",
                "riro → orders CSV (los podle historie + koeficient kg)")
    for i, depot in enumerate(depots, 1):
        print(f"\n  [{i}/{len(depots)}] prepare {depot}")
        run_cmd([PY, "prepare_inputs_v6.py", depot,
                 "--data-root", PREDICTION_ROOT.as_posix(), "--prediction"],
                env, f"prepare {depot}")

    # ── P1: každé depo samostatně s CELÝM skladem ────────────────────────
    # Žádné nafukování počtů: přetečení se pozná samo (jeden kamion,
    # tři depa → tři přání), a P1 tak prohledává stejný prostor jako P2.
    p1_fleet = Path(fleet_path)
    step_header(f"P1 — přání dep ({len(depots)} běhů)",
                "každé depo samostatně s CELÝM skladem "
                "· L0 (100 %, okna −5/+25)")
    p1_by_depot: dict[str, list[dict]] = {}
    for i, depot in enumerate(depots, 1):
        print(f"\n  [{i}/{len(depots)}] P1 {depot}")
        out_dir = PREDICTION_ROOT / "results" / depot / f"{date_str}_{stamp}_P1"
        elapsed = run_cmd(build_solver_cmd(depot, date_str, out_dir, p1_fleet,
                                           args.budget, args.osm_source,
                                           args.force_matrix,
                                           args.seed_finalists),
                          env, f"P1 {depot}")
        p1_by_depot[depot] = lines_from_run(out_dir)
        summarize_run(p1_by_depot[depot], small_codes, elapsed)

    # ── Rezervace velkých ────────────────────────────────────────────────
    allocation = fb.allocate_reservations(p1_by_depot, large_avail)
    step_header("REZERVACE VELKÝCH AUT",
                "přetečené typy ořezané žebříčkem podle naloženosti linek")
    for depot in depots:
        wish = allocation["wishes"].get(depot, {})
        res = allocation["reservations"].get(depot, {})
        changed = "" if wish == res else "   ← ořezáno"
        print(f"  {depot}: přání {fmt_mix(wish):<28} → rezervace "
              f"{fmt_mix(res)}{changed}")
    free = {t: n for t, n in allocation["free_pool"].items() if n > 0}
    print(f"  volný pool (bere kdokoli): {fmt_mix(free)}")
    for t in allocation["truncated"]:
        print(f"  [!] {t['type']}: přání {t['wanted']} > sklad {t['available']}")

    # ── P2: sekvenčně s budgetem ─────────────────────────────────────────
    step_header(f"P2 — generálka s ubíráním ({len(depots)} běhů)",
                f"pořadí uzávěrek {' → '.join(depots)} · velká podle rezervací, "
                f"malá neomezená (deficit se MĚŘÍ)")
    budget = fb.FleetBudget.from_fleet(fleet_rows)
    p2_by_depot: dict[str, list[dict]] = {}
    for i, depot in enumerate(depots):
        # chráněná = depa, která v sekvenci teprve přijdou
        protected = depots[i + 1:]
        caps = fb.caps_for_depot(depot, protected, budget,
                                 allocation["reservations"],
                                 small_codes, small_full)
        large_caps = {t: n for t, n in caps.items()
                      if t not in small_codes and n > 0}
        print(f"\n  [{i + 1}/{len(depots)}] P2 {depot}"
              f"   velká k dispozici: {fmt_mix(large_caps)}"
              + (f"   (chráněno pro {', '.join(protected)})" if protected else ""))
        p2_fleet = fb.write_fleet_file(fleet_rows,
                                       session / f"fleet_P2_{depot}.csv", caps)
        out_dir = PREDICTION_ROOT / "results" / depot / f"{date_str}_{stamp}_P2"
        elapsed = run_cmd(build_solver_cmd(depot, date_str, out_dir, p2_fleet,
                                           args.budget, args.osm_source,
                                           args.force_matrix,
                                           args.seed_finalists),
                          env, f"P2 {depot}")
        depot_lines = lines_from_run(out_dir)
        p2_by_depot[depot] = depot_lines
        summarize_run(depot_lines, small_codes, elapsed)
        # z budgetu ubývají jen velká — malá se měří, ne maskují
        used_large = {t: n for t, n in fb.vehicles_used_by_type(depot_lines).items()
                      if t not in small_codes}
        budget.consume(used_large, context=f"P2 {depot}")
        rest = {t: n for t, n in budget.remaining.items()
                if t not in small_codes and n > 0}
        print(f"  → zbývá velkých: {fmt_mix(rest)}")

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
    print(f"Celková doba: {(time.time() - t_start) / 60:.1f} min "
          f"({2 * len(depots)} solver běhů)")


# ─────────────────────────────────────────────────────────────────────────────
#  Fáze real — večerní ostrý běh podle decision
# ─────────────────────────────────────────────────────────────────────────────

REAL_ROOT = Path("data")


def load_decision(date_str: str) -> dict:
    path = PREDICTION_ROOT / "results" / f"decision_{date_str}.json"
    if not path.exists():
        raise SystemExit(
            f"[CHYBA] Chybí {path} — ostrý běh se řídí predikcí.\n"
            f"        Nejdřív spusť: python plan_day.py predict")
    return json.loads(path.read_text(encoding="utf-8"))


def real_state_dir(date_str: str, label: str = "") -> Path:
    suffix = f"_{label}" if label else ""
    return REAL_ROOT / "results" / "plan_day" / f"{date_str}{suffix}"


def load_real_state(state_path: Path, fleet_rows: list[dict],
                    decision: dict) -> dict:
    """Stav večera: zbytek flotily + hotová depa + aktuální level.
    Existuje-li (běh po částech — dřívější depa jsou definitivní),
    pokračuje se z něj; jinak start z plné flotily a decision levelu."""
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        print(f"[STAV] Navazuji na {state_path} — hotová depa: "
              f"{', '.join(state['planned']) or 'žádná'}")
        return state
    return {
        "remaining": fb.available_by_type(fleet_rows),
        "planned": [],
        "flags": dict(decision["solver_flags"]),
        "escalated": False,
    }


def save_real_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def build_real_solver_cmd(depot: str, date_str: str, fleet_file: Path,
                          flags: dict, args: argparse.Namespace) -> list[str]:
    orders = REAL_ROOT / "prepared" / depot / f"orders_{depot}_{date_str}.csv"
    cmd = [PY, "vrp_solver_lines_v6.py",
           "--orders-file", orders.as_posix(),
           "--budget-min", _fmt_num(args.budget),
           "--vehicle-types-file", fleet_file.as_posix(),
           "--capacity-multiplier", _fmt_num(flags["capacity_multiplier"])]
    if flags.get("double_runs"):
        cmd.append("--double-runs")
    if args.label:
        out = REAL_ROOT / "results" / depot / f"{date_str}_{args.label}"
        cmd += ["--output-dir", out.as_posix()]
    if args.run_log_path:
        cmd += ["--run-log-path", args.run_log_path]
    if args.osm_source:
        cmd += ["--osm-source", args.osm_source]
    if args.force_matrix:
        cmd.append("--force-matrix")
    # Bez zadání se nepředává nic → solver jede na CONFIG (dnes "auto")
    if getattr(args, "seed_finalists", ""):
        cmd += ["--seed-finalists", args.seed_finalists]
    return cmd


def real_out_dir(depot: str, date_str: str, label: str) -> Path:
    suffix = f"_{label}" if label else ""
    return REAL_ROOT / "results" / depot / f"{date_str}{suffix}"


def _flags_label(flags: dict) -> str:
    return ("L1+L2 (103 % + dvojlinky)" if flags.get("double_runs")
            else "L0 (100 %, bez porušení)")


def main_real(args: argparse.Namespace) -> None:
    depots, date_str = resolve_depots_and_date(
        [d.upper() for d in args.depots], root=REAL_ROOT)
    decision = load_decision(date_str)
    if decision["date"] != date_str:
        raise SystemExit(f"[CHYBA] decision je pro {decision['date']}, "
                         f"aktivní data pro {date_str}.")
    run_startup_tests_once(args.skip_tests)
    env = {**os.environ, "SKIP_STARTUP_TESTS": "1"}

    fleet_path = find_vehicle_types_file()
    fleet_rows = fb.load_fleet_rows(fleet_path)
    small_codes = fb.small_type_codes(fleet_rows)
    reservations = decision.get("reservations", {})

    state_dir = real_state_dir(date_str, args.label)
    state_path = state_dir / "state.json"
    state = load_real_state(state_path, fleet_rows, decision)
    budget = fb.FleetBudget(remaining=dict(state["remaining"]))
    flags = dict(state["flags"])
    to_plan = [d for d in depots if d not in state["planned"]]

    print("=" * 66)
    print(f"PLAN_DAY REAL — {date_str} | depa: {', '.join(to_plan)} "
          f"| level: {_flags_label(flags)}")
    if decision.get("l3_needed"):
        print("[!] PREDIKCE HLÁSÍ POTŘEBU L3 (kamiony/rampa) — zatím není "
              "postavené, den může skončit alertem.")
    print(f"Vozový park: {fleet_path} | zbytek: "
          + ", ".join(f"{t}×{n}" for t, n in sorted(budget.remaining.items())
                      if n > 0))
    print("=" * 66)

    # Rezervace chrání KAŽDÉ depo dne, které ještě neplánovalo — podle
    # decision, ne podle toho, co je zrovna v příkazu. Jinak by běh
    # `real MO` po depech mohl sníst kamion rezervovaný pro PR.
    day_depots = decision.get("depots") or depots

    t_start = time.time()

    for i, depot in enumerate(to_plan, 1):
        protected = [d for d in day_depots
                     if d != depot and d not in state["planned"]]
        caps = fb.caps_for_depot(depot, protected, budget, reservations,
                                 small_codes, small_full=None)
        avail_small = sum(n for t, n in caps.items() if t in small_codes)
        large_caps = {t: n for t, n in caps.items()
                      if t not in small_codes and n > 0}
        step_header(f"[{i}/{len(to_plan)}] DEPO {depot} — {_flags_label(flags)}",
                    f"k dispozici: malá {avail_small} ks, "
                    f"velká {fmt_mix(large_caps)}"
                    + (f" · chráněno pro {', '.join(protected)}"
                       if protected else " · poslední depo, bere vše"))

        run_cmd([PY, "prepare_inputs_v6.py", depot], env, f"prepare {depot}")

        fleet_file = fb.write_fleet_file(fleet_rows,
                                         state_dir / f"fleet_{depot}.csv",
                                         caps)

        planned_ok = False
        while True:
            cmd = build_real_solver_cmd(depot, date_str, fleet_file, flags, args)
            print(f"\n  solver ({_flags_label(flags)}):")
            print(f"  $ {' '.join(cmd[1:])}")
            t_solve = time.time()
            rc = subprocess.run(cmd, env=env).returncode
            solve_min = (time.time() - t_solve) / 60
            if rc == 0:
                planned_ok = True
                break
            harder = fb.escalate_flags(flags)
            if harder is None:
                break
            print("\n" + "!" * 66)
            print(f"[ESKALACE] {depot} nevyšlo na {_flags_label(flags)} — "
                  f"zvyšuji na {_flags_label(harder)} (platí i pro další depa)")
            print("!" * 66)
            flags = harder
            state["flags"] = flags
            state["escalated"] = True
            save_real_state(state_path, state)

        if not planned_ok:
            raise SystemExit(
                "\n" + "=" * 66 + "\n"
                f"[ALERT] Depo {depot} nejde naplánovat ani s "
                f"{_flags_label(flags)}.\n"
                f"Vyšší porušení (L3 — kamiony/rampa) zatím není postavené.\n"
                f"Zbytek flotily: "
                + ", ".join(f"{t}×{n}" for t, n in sorted(budget.remaining.items()))
                + f"\nHotová depa ({', '.join(state['planned']) or 'žádná'}) "
                  "jsou definitivní; tohle a další depa čekají na člověka.\n"
                + "=" * 66)

        out_dir = real_out_dir(depot, date_str, args.label)
        lines = lines_from_run(out_dir)
        used = fb.vehicles_used_by_type(lines)
        budget.consume(used, context=f"real {depot}")
        state["remaining"] = budget.remaining
        state["planned"].append(depot)
        save_real_state(state_path, state)

        summarize_run(lines, small_codes, solve_min * 60, prefix=f"\n  ✓ {depot}:")
        rest_small = sum(n for t, n in budget.remaining.items()
                         if t in small_codes)
        rest_large = {t: n for t, n in budget.remaining.items()
                      if t not in small_codes and n > 0}
        print(f"  → zbývá pro další depa: malá {rest_small} ks, "
              f"velká {fmt_mix(rest_large)}")
        print(f"  → plán: {out_dir}")

        if not args.no_visualize:
            run_cmd([PY, "visualize_routes.py", out_dir.as_posix()]
                    + (["--osm-source", args.osm_source] if args.osm_source else []),
                    env, f"mapa {depot}")

    print("\n" + "=" * 66)
    print(f"PLAN_DAY REAL — {date_str} DOKONČEN | level: {_flags_label(flags)}"
          + (" | BĚHEM DNE ESKALOVÁNO" if state.get("escalated") else ""))
    print(f"Naplánováno: {', '.join(state['planned'])}"
          + (f"  |  zbývá: {', '.join(d for d in day_depots if d not in state['planned'])}"
             if any(d not in state["planned"] for d in day_depots) else ""))
    print("Zbytek flotily: "
          + fmt_mix({t: n for t, n in budget.remaining.items() if n > 0},
                    small_codes))
    print(f"Celková doba: {(time.time() - t_start) / 60:.1f} min")
    print(f"Stav: {state_path}")
    print("=" * 66)


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
    predict.add_argument("--seed-finalists", default="",
                         choices=["", "auto", "1", "2", "3"],
                         help="Vynutit počet finalistů fáze E. Bez zadání "
                              "jede solver na svém defaultu (CONFIG: auto). "
                              "'1' = chování před 11.8.2026 — na srovnávací "
                              "běhy.")

    real = sub.add_parser(
        "real", help="Večerní ostrý běh: sekvence dep podle decision, "
                     "flotila ubývá, eskalace při selhání")
    real.add_argument("depots", nargs="*",
                      help="Depa (CB MO HK PR). Bez zadání: všechna s ostrým "
                           "vstupem. Jede se v pořadí uzávěrek; už hotová "
                           "depa (stav) se přeskakují.")
    real.add_argument("--budget", type=float, default=30.0,
                      help="Solver budget na depo v minutách (default 30 "
                           "— ostrý provoz)")
    real.add_argument("--osm-source", choices=["current", "stable"],
                      default="current", help="Routing instance")
    real.add_argument("--force-matrix", action="store_true",
                      help="Předá se solveru")
    real.add_argument("--no-visualize", action="store_true",
                      help="Nevytvářet mapy")
    real.add_argument("--skip-tests", action="store_true",
                      help="Přeskočit startup testy")
    real.add_argument("--label", default="",
                      help="Přípona výstupů a stavu — testovací běh vedle "
                           "ostrého (results/{D}/{DATE}_{label})")
    real.add_argument("--run-log-path", default="",
                      help="Vlastní run log (testy); default = ostrý log")
    real.add_argument("--seed-finalists", default="",
                      choices=["", "auto", "1", "2", "3"],
                      help="Vynutit počet finalistů fáze E. Bez zadání jede "
                           "solver na svém defaultu (CONFIG: auto). "
                           "'1' = chování před 11.8.2026 — na srovnávací běhy.")
    args = parser.parse_args()

    if not Path("vrp_solver_lines_v6.py").exists():
        sys.exit("[CHYBA] Spusť z kořene repa.")
    if args.phase == "predict":
        main_predict(args)
    elif args.phase == "real":
        main_real(args)


if __name__ == "__main__":
    main()
