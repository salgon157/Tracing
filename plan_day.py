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
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import fleet_budget as fb
import l3_planner as l3
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


def print_uninflated_cost(out_dir: Path, lines_count: int, delta: int,
                          prefix: str = "  →") -> float:
    """
    Kolik by plán stál bez zdražení výjezdu (#2). Jen výpis do konzole —
    soubory nesou navýšené ceny (decision má deltu, přepočet je triviální).
    Každá linka platí přesně jeden start (dvojlinka = 2 řádky lines_summary),
    takže odpočet = delta × počet linek.
    """
    if not delta:
        return 0.0
    summary = out_dir / "zone_summary.json"
    cost = json.loads(summary.read_text(encoding="utf-8"))["total_cost_kc"]
    real_cost = cost - delta * lines_count
    print(f"{prefix} nenavýšená cena aut = {cost:,.0f} − ({delta} × "
          f"{lines_count} linek) = {real_cost:,.0f} Kč")
    return real_cost


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
#  L3 — kamiony, hgv matice (sdílené predict + l3)
# ─────────────────────────────────────────────────────────────────────────────

def l3_truck_units(fleet_rows: list[dict], counts_by_type: dict[str, int]
                   ) -> list[dict]:
    """Kamiony (nad horní mezí „středních") rozbalené per kus:
    [{type_code, max_kg, cost_per_km, start_cost}], největší první."""
    units = []
    for r in fleet_rows:
        t = r["type_code"].strip()
        if float(r["max_kg"]) <= fb.MEDIUM_KG_RANGE[1]:
            continue
        for _ in range(int(counts_by_type.get(t, 0))):
            units.append({"type_code": t, "max_kg": float(r["max_kg"]),
                          "cost_per_km": float(r["cost_per_km"]),
                          "start_cost": float(r.get("start_cost_kc", 0) or 0)})
    return sorted(units, key=lambda u: -u["max_kg"])


def l3_hgv_matrix(locations: list[dict], osm_source: str):
    """(dist_km, dur_min) přes ORS driving-hgv pro sklad + lokace, nebo None
    když routing není k dispozici (výběr pak padá na záložní greedy)."""
    from osm_routing import apply_osm_source
    from vrp_solver_lines_v6 import CONFIG as SOLVER_CONFIG, DEPOT, get_matrix
    if osm_source:
        apply_osm_source(SOLVER_CONFIG, osm_source)
    pts = [(DEPOT["lat"], DEPOT["lon"])] + [(l["lat"], l["lon"]) for l in locations]
    try:
        return get_matrix(pts, profile="driving-hgv")
    except SystemExit as e:
        print(f"  [!] hgv matice není k dispozici: {str(e).strip().splitlines()[0][:90]}")
        return None


def l3_trucks_by_type_from_bins(units: list[dict], bins: list[list]) -> dict[str, int]:
    out: dict[str, int] = {}
    for u, b in zip(units, bins):
        if b:
            out[u["type_code"]] = out.get(u["type_code"], 0) + 1
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Report
# ─────────────────────────────────────────────────────────────────────────────

def format_report(date_str: str, stamp: str, depots: list[str],
                  allocation: dict, p2_by_depot: dict[str, list[dict]],
                  decision: dict, decision_path: Path,
                  esc: dict | None = None,
                  l3_block: dict | None = None) -> str:
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

    if esc and esc.get("delta"):
        lines.append(f"\nZdražení výjezdu (#2): +{esc['delta']} Kč všem typům "
                     f"(chybí {esc['missing_small']} malých, střední na "
                     f"{esc['medium_usage']:.0%}) — platí pro P2 i večer; "
                     f"ceny v plánech jsou o deltu×linky vyšší než skutečné")

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
    if d["level"] == 0:
        verdict = "L0 — žádná porušení, dvojlinky vypnuté"
    elif not d["l3_needed"]:
        verdict = "L1 + L2 — 103 % nosnosti + dvojlinky povolené"
    elif l3_block:
        verdict = (f"L1 + L2 + L3 — kamion předem: "
                   f"{len(l3_block['orders'])} objednávek / "
                   f"{l3_block['selected_kg']:,.0f} kg "
                   f"({l3_block['trucks_used']}× 18t)")
        if l3_block["exhausted"]:
            verdict += "  [sjízdné rampové kg nestačily na chybějící]"
    else:
        verdict = ("L1 + L2 + !!! POTŘEBA L3, ale po P2 nezbyl kamion — "
                   "alert, člověk rozhodne !!!")
    lines += [f"ROZHODNUTÍ PRO VEČER: {verdict}"]
    if l3_block and l3_block.get("routes"):
        lines.append("  odhad tras kamionu (hgv matice, výběr VRP):")
        lines.append(l3.format_routes(l3_block["routes"]))
    lines += [f"decision → {decision_path}",
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

    # ── Zdražení výjezdu (#2): chybí malá a střední stojí? ───────────────
    # Vyhodnocuje se z P1; delta platí už pro P2 (a večer pro real).
    esc = fb.start_cost_escalation(p1_by_depot, fleet_rows)
    if esc["delta"]:
        print(f"\n  [#2] Chybí {esc['missing_small']} malých aut a střední "
              f"jedou jen na {esc['medium_usage']:.0%} "
              f"({esc['medium_used']}/{esc['medium_available']}) → "
              f"výjezd VŠECH aut +{esc['delta']} Kč (P2 i večerní běh)")
    elif esc["missing_small"] > 0:
        print(f"\n  [#2] Chybí {esc['missing_small']} malých, ale zdražení "
              f"se nezapíná (limit >{fb.START_COST_TRIGGER_MISSING} chybějících "
              f"a střední pod {fb.MEDIUM_USAGE_TRIGGER:.0%}; "
              f"teď {esc['medium_usage']:.0%})")

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
                                       session / f"fleet_P2_{depot}.csv", caps,
                                       start_cost_delta=esc["delta"])
        out_dir = PREDICTION_ROOT / "results" / depot / f"{date_str}_{stamp}_P2"
        elapsed = run_cmd(build_solver_cmd(depot, date_str, out_dir, p2_fleet,
                                           args.budget, args.osm_source,
                                           args.force_matrix,
                                           args.seed_finalists),
                          env, f"P2 {depot}")
        depot_lines = lines_from_run(out_dir)
        p2_by_depot[depot] = depot_lines
        summarize_run(depot_lines, small_codes, elapsed)
        print_uninflated_cost(out_dir, len(depot_lines), esc["delta"])
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

    # ── L3: výběr rampových objednávek pro kamion (jen když L1+L2 nestačí)
    l3_block = None
    if decision["l3_needed"]:
        step_header("L3 — VÝBĚR PRO KAMION",
                    "rampové SKUTEČNÉ objednávky napříč depy; kamiony 18t "
                    "zbylé po P2")
        # kamion = auto nad horní mezí "středních" (dnes TYPE_05/06)
        truck_units = l3_truck_units(fleet_rows, budget.remaining)
        if not truck_units:
            print("  [!] Po P2 nezbyl žádný kamion 18t — L3 se NEKONÁ, "
                  "den zůstává na L1+L2 (deficit se nezmenší; večer může skončit alertem)")
        else:
            candidates = l3.load_l3_candidates(
                PREDICTION_ROOT / "prepared", depots, date_str)
            target = l3.l3_target_kg(decision["missing_kg"])
            print(f"  kandidátů (rampa, skutečné): {len(candidates)} lokací / "
                  f"{sum(c['kg'] for c in candidates):,.0f} kg | chybí "
                  f"{decision['missing_kg']:,.0f} kg, cíl (strop) {target:,.0f} kg | "
                  f"kamiony k dispozici: "
                  + ", ".join(f"{u['type_code']} {u['max_kg']:,.0f} kg"
                              for u in truck_units))
            # HLAVNÍ cesta: VRP nad reálnou hgv maticí — vybírá jen SJÍZDNÉ
            # smyčky (denní jízda, pauzy, okno). Bez ORS záložní greedy.
            mats = l3_hgv_matrix(candidates, args.osm_source) if candidates else None
            if mats is not None:
                rules = l3.driver_rules()
                sel = l3.select_locations_vrp(
                    candidates, mats[0], mats[1], truck_units,
                    target, decision["missing_kg"], driver=rules)
            else:
                print("  [!] Bez hgv matice — ZÁLOŽNÍ greedy výběr, sjízdnost "
                      "trasy se ověří až večer (plan_day l3)")
                rules = None
                sel = l3.select_locations(candidates, target,
                                          [u["max_kg"] for u in truck_units])
            # bins jsou v pořadí truck_units (kapacity desc) → typy kamionů
            trucks_by_type = l3_trucks_by_type_from_bins(truck_units, sel["bins"])
            l3_block = l3.build_l3_decision_block(
                sel, decision["missing_kg"], trucks_by_type)
            print(f"  vybráno ({sel.get('method', 'greedy')}): "
                  f"{len(sel['selected'])} lokací / "
                  f"{len(l3_block['orders'])} objednávek / "
                  f"{sel['selected_kg']:,.0f} kg → kamiony {fmt_mix(trucks_by_type)}"
                  + (f" | λ = {sel['kg_value_kc']:g} Kč/kg"
                     if sel.get("kg_value_kc") else ""))
            if sel.get("routes"):
                print(l3.format_routes(sel["routes"],
                                       rules["max_drive_h"] if rules else None))
            if sel.get("dropped"):
                print(f"  vynecháno (nevyplatí se / nevejde se): "
                      f"{len(sel['dropped'])} lokací / "
                      f"{sum(c['kg'] for c in sel['dropped']):,.0f} kg")
            if l3_block["exhausted"]:
                print(f"  [!] Sjízdné rampové lokace nedaly chybějící kg "
                      f"({sel['selected_kg']:,.0f} < {decision['missing_kg']:,.0f}) "
                      f"— zbytek deficitu řeší L1+L2")
            for loc in sel["selected"]:
                print(f"    {loc['depot']} {loc['location_code']:<24} "
                      f"{loc['kg']:>8,.1f} kg  {loc['customer_name'][:40]}")

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
        "start_cost": esc,
        "l3": l3_block,
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
                        decision, decision_path, esc=esc, l3_block=l3_block))
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
    pokračuje se z něj; jinak start z plné flotily a decision levelu.
    Kamiony vyhrazené pro L3 se odečtou hned na startu — depa s nimi
    nesmí počítat (kamion jede ráno svou trasu)."""
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        print(f"[STAV] Navazuji na {state_path} — hotová depa: "
              f"{', '.join(state['planned']) or 'žádná'}")
        return state
    remaining = fb.available_by_type(fleet_rows)
    for t, n in (decision.get("l3") or {}).get("trucks", {}).items():
        remaining[t] = max(0, remaining.get(t, 0) - int(n))
    return {
        "remaining": remaining,
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
    # Zdražení výjezdu (#2) rozhodnuté predikcí platí i večer
    start_cost_delta = int(decision.get("start_cost", {}).get("delta", 0))

    state_dir = real_state_dir(date_str, args.label)
    state_path = state_dir / "state.json"
    state = load_real_state(state_path, fleet_rows, decision)
    budget = fb.FleetBudget(remaining=dict(state["remaining"]))
    flags = dict(state["flags"])
    to_plan = [d for d in depots if d not in state["planned"]]

    print("=" * 66)
    print(f"PLAN_DAY REAL — {date_str} | depa: {', '.join(to_plan)} "
          f"| level: {_flags_label(flags)}")
    if start_cost_delta:
        print(f"[#2] Výjezd všech aut +{start_cost_delta} Kč (rozhodnuto "
              f"predikcí — chybí malá, střední stojí); skutečná cena se "
              f"dopočítává pod každým depem")
    l3_block_real = decision.get("l3")
    l3_excludes = l3.orders_by_depot(l3_block_real) if l3_block_real else {}
    if l3_block_real:
        print(f"[L3] Kamion předem: {len(l3_block_real['orders'])} objednávek "
              f"/ {l3_block_real['selected_kg']:,.0f} kg se vyřadí z dep; "
              f"kamiony {', '.join(f'{t}×{n}' for t, n in sorted(l3_block_real['trucks'].items()))} "
              f"jsou mimo budget. Po posledním depu spusť: "
              f"python plan_day.py l3")
    if decision.get("l3_needed") and not l3_block_real:
        print("[!] PREDIKCE HLÁSÍ POTŘEBU L3, ale kamion se nevybral (po P2 "
              "nezbyl / rampové lokace nedaly nic sjízdného) — den jede na "
              "L1+L2 a může skončit alertem.")
    print(f"Vozový park: {fleet_path} | zbytek: "
          + ", ".join(f"{t}×{n}" for t, n in sorted(budget.remaining.items())
                      if n > 0))
    print("=" * 66)

    # Rezervace chrání KAŽDÉ depo dne, které ještě neplánovalo — podle
    # decision, ne podle toho, co je zrovna v příkazu. Jinak by běh
    # `real MO` po depech mohl sníst kamion rezervovaný pro PR.
    day_depots = decision.get("depots") or depots

    t_start = time.time()
    day_uninflated = 0.0
    day_lines_count = 0

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

        prep_cmd = [PY, "prepare_inputs_v6.py", depot]
        if l3_excludes.get(depot):
            excl_path = state_dir / f"l3_exclude_{depot}.json"
            excl_path.parent.mkdir(parents=True, exist_ok=True)
            excl_path.write_text(json.dumps(l3_excludes[depot]),
                                 encoding="utf-8")
            prep_cmd += ["--exclude-orders-file", excl_path.as_posix()]
        run_cmd(prep_cmd, env, f"prepare {depot}")

        fleet_file = fb.write_fleet_file(fleet_rows,
                                         state_dir / f"fleet_{depot}.csv",
                                         caps,
                                         start_cost_delta=start_cost_delta)

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
                f"L3 (kamion předem) se rozhoduje odpoledne v predikci — večer "
                f"už není kam eskalovat.\n"
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
        day_uninflated += print_uninflated_cost(out_dir, len(lines),
                                                start_cost_delta)
        day_lines_count += len(lines)
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
    if start_cost_delta and day_lines_count:
        print(f"[#2] Skutečná (nenavýšená) cena dep tohoto běhu: "
              f"{day_uninflated:,.0f} Kč "
              f"(odečteno {start_cost_delta} × {day_lines_count} linek)")
    print(f"Celková doba: {(time.time() - t_start) / 60:.1f} min")
    print(f"Stav: {state_path}")
    print("=" * 66)


# ─────────────────────────────────────────────────────────────────────────────
#  Fáze l3 — trasa kamionu po doplánování všech dep
# ─────────────────────────────────────────────────────────────────────────────

def _l3_unplanned_alert(l3_block: dict, locs: list[dict], merged: Path,
                        state_dir: Path, date_str: str, reason: str,
                        out_dir: Path | None = None) -> None:
    """L3 trasa nevznikne — objednávky nejsou v ŽÁDNÉM plánu. Vypíše přesně
    co komu vrátit, zapíše to strojově (l3_unplanned_{DATE}.json) a
    uklidí prázdnou výstupní složku (driver_assignment by ji jinak vzal
    jako zónu L3). Končí exit ≠ 0."""
    by_depot: dict[str, list[dict]] = {}
    for o in l3_block.get("orders", []):
        by_depot.setdefault(o["depot"], []).append(o)
    lines = ["", "=" * 66,
             f"[ALERT] L3 trasa NEVYŠLA ({reason}) — vyřazené objednávky "
             f"nejsou v ŽÁDNÉM plánu!",
             f"        Objednávky: {merged.as_posix()}",
             f"        Celkem {len(locs)} lokací / "
             f"{sum(l['kg'] for l in locs):,.0f} kg",
             "        Co komu vrátit (depo: objednávky):"]
    for depot in sorted(by_depot):
        kg = sum(o["kg"] for o in by_depot[depot])
        lines.append(f"          {depot}: {len(by_depot[depot])} obj / {kg:,.0f} kg — "
                     + ", ".join(o["order_number"] for o in by_depot[depot]))
    lines += ["        Ruční zásah: přeplánovat dotčená depa bez vyřazení "
              "(smazat depo ze state.planned + vrátit jeho auta do "
              "state.remaining, pak `plan_day.py real {DEPO}` — bez L3 bloku "
              "v decision, nebo s upraveným výběrem), nebo objednávky "
              "rozvézt jinak.",
              "        Strojově: " + (state_dir / f"l3_unplanned_{date_str}.json").as_posix(),
              "=" * 66]
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"l3_unplanned_{date_str}.json").write_text(
        json.dumps({"date": date_str, "reason": reason,
                    "orders_by_depot": by_depot}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    if out_dir is not None and out_dir.exists() and not any(out_dir.iterdir()):
        out_dir.rmdir()
    raise SystemExit("\n".join(lines))


def main_l3(args: argparse.Namespace) -> None:
    depots, date_str = resolve_depots_and_date(
        [d.upper() for d in args.depots], root=REAL_ROOT)
    decision = load_decision(date_str)
    l3_block = decision.get("l3")
    if not l3_block:
        raise SystemExit(f"[CHYBA] decision_{date_str}.json nemá blok l3 — "
                         f"predikce kamion nevybrala, není co plánovat.")
    run_startup_tests_once(args.skip_tests)
    env = {**os.environ, "SKIP_STARTUP_TESTS": "1"}

    # L3 se staví až z REÁLNÝCH vyřazených objednávek — tedy po depech
    state_dir = real_state_dir(date_str, args.label)
    state_path = state_dir / "state.json"
    if not state_path.exists():
        raise SystemExit("[CHYBA] Chybí stav večera — L3 trasa se staví až "
                         "PO doplánování všech dep (plan_day.py real).")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    day_depots = decision.get("depots") or depots
    missing = [d for d in day_depots if d not in state["planned"]]
    if missing:
        raise SystemExit(f"[CHYBA] Depa {', '.join(missing)} ještě nemají "
                         f"plán — L3 se spouští po posledním depu.")

    by_depot = l3.orders_by_depot(l3_block)
    files = []
    for depot in sorted(by_depot):
        p = REAL_ROOT / "prepared" / depot / f"l3_orders_{depot}_{date_str}.csv"
        if not p.exists():
            raise SystemExit(f"[CHYBA] Chybí {p} — prepare depa {depot} "
                             f"neběžel s vyřazením L3 objednávek?")
        files.append(p)
    merged = REAL_ROOT / "prepared" / "L3" / f"orders_L3_{date_str}.csv"
    n_orders = l3.merge_l3_orders(files, merged)

    fleet_path = find_vehicle_types_file()
    fleet_rows = fb.load_fleet_rows(fleet_path)
    trucks_count = {t: int(n) for t, n in l3_block["trucks"].items()}
    trucks = l3_truck_units(fleet_rows, trucks_count)

    suffix = f"_{args.label}" if args.label else ""
    out_dir = REAL_ROOT / "results" / "L3" / f"{date_str}{suffix}"

    print("=" * 66)
    print(f"PLAN_DAY L3 — {date_str} | {n_orders} objednávek / "
          f"{l3_block['selected_kg']:,.0f} kg | kamiony "
          + ", ".join(f"{t}×{n}" for t, n in sorted(trucks_count.items())))
    print(f"Okna {l3.L3_CONFIG['window_from']}–{l3.L3_CONFIG['window_to']} "
          f"(okna lokací pro L3 neplatí) | režim řidiče EU ZAPNUT")
    print("=" * 66)

    # ── Kontrola sjízdnosti PŘED solverem (stejný model jako odpolední
    #    výběr, ale s REÁLNÝMI objednávkami a všechny povinné). Odpoví za
    #    ~20 s, ne po 5–10 minutách solveru; když nevyjde, zkusí přidat
    #    kamion ze zbytku flotily; když ani to ne, vypíše, co komu vrátit.
    with open(merged, encoding="utf-8") as f:
        merged_rows = list(csv.DictReader(f))
    locs = l3.aggregate_locations({"L3": merged_rows})
    mats = l3_hgv_matrix(locs, args.osm_source)
    if mats is None:
        print("  [!] Bez hgv matice — kontrola sjízdnosti přeskočena, "
              "rozhodne až solver")
    else:
        rules = l3.driver_rules()
        chk = l3.check_l3_feasible(locs, mats[0], mats[1], trucks, driver=rules)
        if chk["feasible"]:
            print("  Kontrola sjízdnosti: OK")
            print(l3.format_routes(chk["routes"], rules["max_drive_h"]))
        else:
            print("  [!] Kontrola sjízdnosti: NEVYJDE s kamiony "
                  f"{fmt_mix(trucks_count)} ({len(locs)} lokací; limity: "
                  f"denní jízda {rules['max_drive_h']:g} h, pauzy, okno "
                  f"{l3.L3_CONFIG['window_from']}–{l3.L3_CONFIG['window_to']}"
                  + (f", max {rules['max_stops']} zastávek/trasa"
                     if rules.get("max_stops") else "") + ")")
            # D) zkusit přidat kamion, který večer nikdo nepoužil
            spare = l3_truck_units(fleet_rows, state.get("remaining", {}))
            added = []
            for extra in spare:
                trial = trucks + added + [extra]
                chk2 = l3.check_l3_feasible(locs, mats[0], mats[1], trial,
                                            driver=rules)
                added.append(extra)
                if chk2["feasible"]:
                    chk = chk2
                    break
            if chk["feasible"]:
                for extra in added:
                    t = extra["type_code"]
                    trucks_count[t] = trucks_count.get(t, 0) + 1
                    state["remaining"][t] = max(0, state["remaining"].get(t, 0) - 1)
                trucks = trucks + added
                save_real_state(state_path, state)
                print(f"  → přidán kamion ze zbytku flotily: "
                      f"{fmt_mix({e['type_code']: 1 for e in added})} "
                      f"(odečteno ze stavu) — teď sjízdné:")
                print(l3.format_routes(chk["routes"], rules["max_drive_h"]))
            else:
                _l3_unplanned_alert(l3_block, locs, merged, state_dir, date_str,
                                    reason=("žádná kombinace kamionů nedá "
                                            "sjízdnou trasu"), out_dir=out_dir)

    overrides = {r["type_code"].strip(): 0 for r in fleet_rows}
    overrides.update(trucks_count)
    fleet_file = fb.write_fleet_file(fleet_rows, state_dir / "fleet_L3.csv",
                                     overrides)

    cmd = [PY, "vrp_solver_lines_v6.py",
           "--orders-file", merged.as_posix(),
           "--output-dir", out_dir.as_posix(),
           "--budget-min", _fmt_num(args.budget),
           "--vehicle-types-file", fleet_file.as_posix(),
           "--capacity-multiplier", "1.0",
           "--driver-breaks"]
    if args.run_log_path:
        cmd += ["--run-log-path", args.run_log_path]
    if args.osm_source:
        cmd += ["--osm-source", args.osm_source]
    print(f"  $ {' '.join(cmd[1:])}")
    rc = subprocess.run(cmd, env=env).returncode
    if rc != 0:
        _l3_unplanned_alert(l3_block, locs, merged, state_dir, date_str,
                            reason="solver trasu nenašel", out_dir=out_dir)

    lines = lines_from_run(out_dir)
    kg = sum(l["total_kg"] for l in lines)
    print(f"\n✓ L3 trasa: {len(lines)} linek, {kg:,.0f} kg → {out_dir}")
    print("  (ESO export a stops v output složce; řidiče přiřadí "
          "driver_assignment — zóna L3 se přibere sama)")
    if not args.no_visualize:
        run_cmd([PY, "visualize_routes.py", out_dir.as_posix()]
                + (["--osm-source", args.osm_source] if args.osm_source else []),
                env, "mapa L3")


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
    l3p = sub.add_parser(
        "l3", help="Trasa kamionu (porušení L3) — spouští se až PO "
                   "doplánování všech dep dne (po posledním real)")
    l3p.add_argument("depots", nargs="*",
                     help="Jen pro odvození data z aktivních riro; "
                          "normálně prázdné")
    l3p.add_argument("--budget", type=float,
                     default=l3.L3_CONFIG["budget_min"],
                     help=f"Solver budget v minutách "
                          f"(default {l3.L3_CONFIG['budget_min']:g})")
    l3p.add_argument("--osm-source", choices=["current", "stable"],
                     default="current", help="Routing instance")
    l3p.add_argument("--no-visualize", action="store_true",
                     help="Nevytvářet mapu")
    l3p.add_argument("--skip-tests", action="store_true",
                     help="Přeskočit startup testy")
    l3p.add_argument("--label", default="",
                     help="Přípona výstupů a stavu (testovací běhy)")
    l3p.add_argument("--run-log-path", default="",
                     help="Vlastní run log (testy); default = ostrý log")
    args = parser.parse_args()

    if not Path("vrp_solver_lines_v6.py").exists():
        sys.exit("[CHYBA] Spusť z kořene repa.")
    if args.phase == "predict":
        main_predict(args)
    elif args.phase == "real":
        main_real(args)
    elif args.phase == "l3":
        main_l3(args)


if __name__ == "__main__":
    main()
