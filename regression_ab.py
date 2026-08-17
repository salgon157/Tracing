"""
regression_ab.py — regresní A/B harness solveru: baseline commit vs pracovní kopie
=================================================================================

Odpovídá na otázku „je nový solver na STEJNÝCH datech stejně dobrý nebo
lepší?" — přísně, ne „šum, ok". Baseline = solver z pinnutého commitu
v git worktree (stejný CONFIG té doby, stejné importy), kandidát = pracovní
kopie. Oba běží nad TÝMIŽ prepared soubory, TÝMŽ vozovým parkem, TOUŽ
routing instancí a TÝMŽ budgetem, střídavě (ABAB), aby se šum stroje
rozložil na obě strany.

    python regression_ab.py --baseline-dir <worktree 4f0f879> \
        --dates 2026-08-07 2026-08-10 2026-08-13 2026-08-17 \
        --depots CB MO HK PR --reps 3 --budget 5 --extras

Metriky per běh: počet linek, vykázaná cena, SKUTEČNÁ cena (Σ km × přesná
sazba + Σ start_cost per typ — dopočítáno z lines_summary, ne z výpisu),
km, čas běhu, exit kód, hlavičky výstupů (snapshot).

Kritérium „stejně nebo líp" per depo-den (všechny musí platit):
  - linky:  medián(B) ≤ medián(A)
  - cena:   medián(B) ≤ medián(A) × (1 + tol_median)   [1 %]
            max(B)    ≤ max(A)    × (1 + tol_max)      [2 %]
            žádný běh B > min(A) × (1 + tol_worst)     [3 %]
  - exit:   B = 0 všude, kde A = 0; run_status.json u B existuje
  - výstupy: hlavičky lines_summary / lines_stops / eso_export shodné
  - čas:    elapsed(B) ≤ budget + 60 s (a pro kontrolní 30min běh
            elapsed ≥ 0,9 × budget — fáze C využívá budget)
Jediné porušení = verdikt FAIL pro ten depo-den; celkový verdikt PASS jen
když projdou všechny.

Výstup: <out>/results.jsonl (každý běh), <out>/report.md (tabulka +
verdikt), konzole. Baseline běží se SKIP_STARTUP_TESTS=1 a vlastním run
logem (nepíše do ostrého).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PY = sys.executable
DEPOTS_ALL = ["CB", "MO", "HK", "PR"]
TOL_MEDIAN, TOL_MAX, TOL_WORST = 0.01, 0.02, 0.03
TIME_SLACK_SEC = 60


# ─────────────────────────────────────────────────────────────────────────────
#  Metriky
# ─────────────────────────────────────────────────────────────────────────────

def load_fleet_costs(fleet_file: Path) -> dict[str, dict]:
    out = {}
    with open(fleet_file, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            code = (r.get("type_code") or "").strip()
            if not code or code.startswith("#"):
                continue
            out[code] = {"cost_per_km": float(r["cost_per_km"]),
                         "start_cost": float(r.get("start_cost_kc") or 0)}
    return out


def true_cost_from_summary(summary_csv: Path, fleet_costs: dict) -> tuple[float, int, float]:
    """(skutečná cena, linek, km) — Σ km × přesná sazba typu + Σ start_cost."""
    cost, lines, km = 0.0, 0, 0.0
    with open(summary_csv, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            vid = (r.get("vehicle_id") or "").strip()
            if not vid:
                continue                                  # souhrnný řádek
            t = vid.rsplit("_", 1)[0]
            fc = fleet_costs.get(t)
            rate = fc["cost_per_km"] if fc else float(r.get("cost_per_km") or 0)
            start = fc["start_cost"] if fc else 0.0
            tk = float(r.get("total_km") or 0)
            cost += tk * rate + start
            km += tk
            lines += 1
    return round(cost, 1), lines, round(km, 1)


def header_of(path: Path, delimiter: str = ",", encoding: str = "utf-8-sig") -> list[str]:
    if not path.exists():
        return []
    with open(path, encoding=encoding, newline="") as f:
        return next(csv.reader(f, delimiter=delimiter), [])


def output_signature(out_dir: Path, zone: str) -> dict:
    eso = next(out_dir.glob("eso_export_*.csv"), None)
    return {
        "lines_summary": header_of(out_dir / "lines_summary.csv"),
        "lines_stops": header_of(out_dir / "lines_stops.csv"),
        "eso_export": header_of(eso, ";", "cp1250") if eso else [],
        "files": sorted(p.name for p in out_dir.iterdir()
                        if p.suffix in (".csv", ".json", ".xlsx")
                        and p.name != "run_status.json"),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Jeden běh
# ─────────────────────────────────────────────────────────────────────────────

def run_solver(script_dir: Path, orders: Path, out_dir: Path, fleet_file: Path,
               budget: float, osm: str, run_log: Path, console_log: Path,
               extra_args: list[str], env: dict) -> dict:
    cmd = [PY, (script_dir / "vrp_solver_lines_v6.py").as_posix(),
           "--orders-file", orders.as_posix(),
           "--output-dir", out_dir.as_posix(),
           "--budget-min", f"{budget:g}",
           "--run-log-path", run_log.as_posix(),
           "--vehicle-types-file", fleet_file.as_posix(),
           "--osm-source", osm] + list(extra_args)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with open(console_log, "w", encoding="utf-8") as lf:
        rc = subprocess.run(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT,
                            cwd=os.getcwd()).returncode
    elapsed = time.time() - t0
    rec = {"rc": rc, "elapsed_sec": round(elapsed, 1), "cost_reported": None,
           "cost_true": None, "lines": None, "km": None,
           "status_file": (out_dir / "run_status.json").exists()}
    zs = out_dir / "zone_summary.json"
    if rc == 0 and zs.exists():
        z = json.loads(zs.read_text(encoding="utf-8"))
        rec["cost_reported"] = z.get("total_cost_kc")
        fc = load_fleet_costs(fleet_file)
        rec["cost_true"], rec["lines"], rec["km"] = true_cost_from_summary(
            out_dir / "lines_summary.csv", fc)
        rec["signature"] = output_signature(out_dir, "")
    return rec


# ─────────────────────────────────────────────────────────────────────────────
#  Vyhodnocení
# ─────────────────────────────────────────────────────────────────────────────

def evaluate(case: str, A: list[dict], B: list[dict], budget: float) -> dict:
    """Vrátí {verdict: PASS|FAIL, reasons: [...], stats}."""
    reasons = []
    okA = [r for r in A if r["rc"] == 0]
    okB = [r for r in B if r["rc"] == 0]
    if len(okA) != len(A):
        reasons.append(f"baseline selhal v {len(A) - len(okA)}/{len(A)} bězích "
                       f"(rc {[r['rc'] for r in A]})")
    if okA and len(okB) != len(B):
        reasons.append(f"kandidát selhal v {len(B) - len(okB)}/{len(B)} bězích "
                       f"(rc {[r['rc'] for r in B]}) — baseline prošel")
    if any(not r["status_file"] for r in B):
        reasons.append("kandidát: chybí run_status.json")
    stats = {}
    if okA and okB:
        cA = [r["cost_true"] for r in okA]; cB = [r["cost_true"] for r in okB]
        lA = [r["lines"] for r in okA];     lB = [r["lines"] for r in okB]
        medA, medB = statistics.median(cA), statistics.median(cB)
        stats = {"cost_med_A": medA, "cost_med_B": medB,
                 "cost_max_A": max(cA), "cost_max_B": max(cB),
                 "cost_min_A": min(cA), "cost_min_B": min(cB),
                 "lines_med_A": statistics.median(lA),
                 "lines_med_B": statistics.median(lB),
                 "elapsed_B": [r["elapsed_sec"] for r in okB],
                 "delta_med_pct": round((medB - medA) / medA * 100, 2) if medA else 0}
        if statistics.median(lB) > statistics.median(lA):
            reasons.append(f"linek: medián B {statistics.median(lB)} > A {statistics.median(lA)}")
        if medB > medA * (1 + TOL_MEDIAN):
            reasons.append(f"cena medián: B {medB:,.0f} > A {medA:,.0f} × 1.01")
        if max(cB) > max(cA) * (1 + TOL_MAX):
            reasons.append(f"cena max: B {max(cB):,.0f} > A {max(cA):,.0f} × 1.02")
        worst_allowed = min(cA) * (1 + TOL_WORST)
        bad = [c for c in cB if c > worst_allowed]
        if bad:
            reasons.append(f"cena: {len(bad)} běh(y) B > nejlepší A × 1.03 "
                           f"({max(bad):,.0f} > {worst_allowed:,.0f})")
        for r in okB:
            if r["elapsed_sec"] > budget * 60 + TIME_SLACK_SEC:
                reasons.append(f"čas: B {r['elapsed_sec']:.0f} s > budget "
                               f"{budget * 60:.0f} + {TIME_SLACK_SEC} s")
                break
        sigA = okA[0].get("signature", {}); sigB = okB[0].get("signature", {})
        for k in ("lines_summary", "lines_stops", "eso_export"):
            if sigA.get(k) != sigB.get(k):
                reasons.append(f"hlavička {k} se liší: A {sigA.get(k)} vs B {sigB.get(k)}")
    return {"case": case, "verdict": "PASS" if not reasons else "FAIL",
            "reasons": reasons, "stats": stats}


def fmt_row(case: str, ev: dict) -> str:
    st = ev["stats"]
    if not st:
        return f"| {case} | — | — | — | — | {ev['verdict']} | {'; '.join(ev['reasons'])} |"
    return (f"| {case} | {st['lines_med_A']:g} → {st['lines_med_B']:g} "
            f"| {st['cost_med_A']:,.0f} → {st['cost_med_B']:,.0f} ({st['delta_med_pct']:+.2f} %) "
            f"| {st['cost_max_A']:,.0f} → {st['cost_max_B']:,.0f} "
            f"| {'/'.join(f'{e:.0f}' for e in st['elapsed_B'])} s "
            f"| **{ev['verdict']}** | {'; '.join(ev['reasons']) or '—'} |")


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline-dir", required=True,
                    help="git worktree s baseline solverem (např. commit 4f0f879)")
    ap.add_argument("--dates", nargs="+", required=True)
    ap.add_argument("--depots", nargs="*", default=DEPOTS_ALL)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--budget", type=float, default=5.0)
    ap.add_argument("--osm-source", default="current", choices=["current", "stable"])
    ap.add_argument("--fleet-file", default="",
                    help="vozový park pro obě strany (default: aktivní v data/static)")
    ap.add_argument("--extras", action="store_true",
                    help="navíc PR s dvojlinkami (fleet_PR ze stavu 17. 8.) a L3 běh")
    ap.add_argument("--extras-date", default="2026-08-17")
    ap.add_argument("--out", default="")
    ap.add_argument("--only", default="", choices=["", "A", "B"],
                    help="jen jedna strana (ladění)")
    args = ap.parse_args()

    root = Path.cwd()
    baseline = Path(args.baseline_dir).resolve()
    candidate = root
    if not (baseline / "vrp_solver_lines_v6.py").exists():
        raise SystemExit(f"[CHYBA] {baseline} není worktree se solverem")

    if args.fleet_file:
        fleet_file = Path(args.fleet_file)
    else:
        sys.path.insert(0, str(root))
        from vrp_solver_lines_v6 import find_vehicle_types_file
        fleet_file = Path(find_vehicle_types_file())

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_root = Path(args.out) if args.out else root / "data" / "results" / "_regression" / stamp
    out_root.mkdir(parents=True, exist_ok=True)
    results_path = out_root / "results.jsonl"
    env = {**os.environ, "SKIP_STARTUP_TESTS": "1", "PYTHONIOENCODING": "utf-8"}

    print("=" * 72)
    print(f"REGRESNÍ A/B — baseline {baseline.name} vs kandidát (pracovní kopie)")
    print(f"dny {args.dates} | depa {args.depots} | reps {args.reps} | budget {args.budget:g} min")
    print(f"vozový park {fleet_file} | výstup {out_root}")
    print("=" * 72)

    # sada případů
    cases: list[dict] = []
    for d in args.dates:
        for depot in args.depots:
            orders = root / "data" / "prepared" / depot / f"orders_{depot}_{d}.csv"
            if not orders.exists():
                print(f"  [!] chybí {orders} — přeskakuji")
                continue
            cases.append({"case": f"{depot} {d}", "orders": orders,
                          "fleet": fleet_file, "extra": []})
    if args.extras:
        st = root / "data" / "results" / "plan_day" / args.extras_date
        pr_orders = root / "data" / "prepared" / "PR" / f"orders_PR_{args.extras_date}.csv"
        if (st / "fleet_PR.csv").exists() and pr_orders.exists():
            cases.append({"case": f"PR {args.extras_date} dvojlinky",
                          "orders": pr_orders, "fleet": st / "fleet_PR.csv",
                          "extra": ["--double-runs", "--capacity-multiplier", "1.03"]})
        l3_orders = root / "data" / "prepared" / "L3" / f"orders_L3_{args.extras_date}.csv"
        if (st / "fleet_L3.csv").exists() and l3_orders.exists():
            cases.append({"case": f"L3 {args.extras_date}", "orders": l3_orders,
                          "fleet": st / "fleet_L3.csv",
                          "extra": ["--driver-breaks", "--capacity-multiplier", "1.0"]})

    total = len(cases) * args.reps * (1 if args.only else 2)
    print(f"{len(cases)} případů × {args.reps} reps × {'1' if args.only else '2'} strany "
          f"= {total} běhů ≈ {total * (args.budget + 0.4) / 60:.1f} h")

    per_case: dict[str, dict[str, list]] = {c["case"]: {"A": [], "B": []} for c in cases}
    done = 0
    t_all = time.time()
    for c in cases:
        for rep in range(args.reps):
            order = ("A", "B") if rep % 2 == 0 else ("B", "A")
            for side in order:
                if args.only and side != args.only:
                    continue
                script_dir = baseline if side == "A" else candidate
                tag = c["case"].replace(" ", "_")
                out_dir = out_root / side / f"{tag}_r{rep + 1}"
                console = out_root / side / f"{tag}_r{rep + 1}.log"
                run_log = out_root / f"run_log_{side}.jsonl"
                done += 1
                print(f"[{done}/{total}] {side} {c['case']} r{rep + 1} ...", end=" ", flush=True)
                rec = run_solver(script_dir, c["orders"], out_dir, c["fleet"],
                                 args.budget, args.osm_source, run_log, console,
                                 c["extra"], env)
                rec.update({"case": c["case"], "side": side, "rep": rep + 1,
                            "out_dir": out_dir.as_posix()})
                per_case[c["case"]][side].append(rec)
                with open(results_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({k: v for k, v in rec.items() if k != "signature"},
                                       ensure_ascii=False) + "\n")
                print(f"rc={rec['rc']} linek={rec['lines']} cena={rec['cost_true']} "
                      f"({rec['elapsed_sec']:.0f} s)")

    # vyhodnocení
    evals = []
    for c in cases:
        A, B = per_case[c["case"]]["A"], per_case[c["case"]]["B"]
        if args.only:
            continue
        evals.append(evaluate(c["case"], A, B, args.budget))

    lines = ["# Regresní A/B — solver", "",
             f"baseline: `{baseline}` | kandidát: pracovní kopie | "
             f"budget {args.budget:g} min | reps {args.reps} | {datetime.now():%Y-%m-%d %H:%M}",
             f"celkem {done} běhů, {(time.time() - t_all) / 3600:.1f} h", "",
             "| případ | linky A→B (medián) | skutečná cena A→B (medián) | max A→B | čas B | verdikt | důvody |",
             "|---|---|---|---|---|---|---|"]
    for ev in evals:
        lines.append(fmt_row(ev["case"], ev))
    n_fail = sum(1 for e in evals if e["verdict"] == "FAIL")
    verdict = "PASS" if evals and n_fail == 0 else ("FAIL" if evals else "n/a")
    lines += ["", f"## Verdikt: **{verdict}**"
              + (f" — {n_fail}/{len(evals)} případů porušilo kritérium" if n_fail else
                 f" — {len(evals)}/{len(evals)} případů v pásmu"),
              "", "Kritérium: linky medián B ≤ A; cena medián ≤ +1 %, max ≤ +2 %, "
              "žádný běh > nejlepší A +3 %; exit 0 kde A 0; run_status.json; "
              "hlavičky výstupů shodné; čas ≤ budget + 60 s."]
    report = "\n".join(lines)
    (out_root / "report.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\n→ {out_root / 'report.md'}")


if __name__ == "__main__":
    main()
