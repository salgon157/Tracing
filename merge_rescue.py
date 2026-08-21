"""
merge_rescue.py — nouzový plán, když PRAHA nejde naplánovat (spouští se RUČNĚ)
==============================================================================

Kdy: večer PR skončila exit 3 (řešení neexistuje) i po eskalaci L1+L2
a ve zbytku flotily (state.json) je aspoň jedno volné NE-malé auto.

  python merge_rescue.py 2026-08-20
  python merge_rescue.py 2026-08-20 --yes          # bez ptaní (testy, ladění)
  python merge_rescue.py 2026-08-20 --budget 5

Princip: velké auto Praze přímo nepomůže (hgv nedojede do center — a PR
padla, i když velká auta v poolu měla). Umí ale UVOLNIT malá auta jinde:
dvě linky malých aut na CB/MO/HK se spojí do jednoho volného velkého/
středního auta → +2 malá auta pro PRAHU.

Postup (schválení ČLOVĚKEM před každým krokem):
  1. najdi páry linek sloučitelné podle pravidel (níže), seřaď od nejmenšího
     porušení oken (počet oken → minuty → čas trasy)
  2. ukaž nejlepší pár s přesným výpisem porušených oken → Povolit?
  3. po povolení přeplánuj PRAHU s uvolněnými auty (krátký budget)
  4. nevyšla? → nabídni další pár (dokud jsou volná velká auta a páry)
  5. vyšla? → vypiš CO ZMĚNIT (zatím ručně v ESO) + hotový PR plán

Pravidla sloučené linky (z diskuse 20. 8. 2026, změřeno na 4 dnech):
  - dvě linky malých aut TÉŽE zóny (ne dvojlinky), max 30 zastávek,
    kg ≤ nosnost velkého auta (bez kapacitního násobiče)
  - trasa se PŘEUSPOŘÁDÁ (jednoautový re-solve), ne A-pak-B
  - hgv profil (střední i velká auta jsou driving-hgv): dostupnost všech
    zastávek, hgv časy, pauza 45 min po 4,5 h (konzervativní posun),
    max 9 h jízdy, návrat do 23:30, čekání max 60 min na zastávku
  - okna: porušení max ±60 min na zastávku (0 porušení se zkouší samo —
    pár s nulou se prostě seřadí první; změřeno: skoro nikdy neexistuje)

NIC se nepřepisuje: výsledky dep zůstávají, PR plán jde do
data/results/PR/{DATE}_rescue/, report (co změnit v ESO) do
data/results/plan_day/{DATE}/merge_rescue_{DATE}.md (+ .json).

Exit kódy (jako solver): 0 = PR vyšla, 2 = vadná/chybějící data,
3 = nezvládneme ani se slučováním, 1 = technická chyba.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import fleet_budget as fb
import paths

CONFIG = {
    "max_stops": 30,          # strop zastávek sloučené linky (dohodnuto 20. 8.)
    "win_tol_min": 60,        # max porušení okna na zastávku (obě strany)
    "slack_min": 60,          # max čekání na zastávce (jako solver)
    "latest_return_min": int(23.5 * 60),
    "max_drive_min": 9 * 60,
    "break_after_min": int(4.5 * 60),
    "break_min": 45,
    "solve_limit_s": 1,       # OR-Tools limit na jeden pár
    "budget_min": 5.0,        # budget přeplánování PR na jedno kolo
    "results_root": paths.RESULTS_ROOT.as_posix(),
    "state_root":   (paths.RESULTS_ROOT / "plan_day").as_posix(),
    "prepared_root": paths.PREPARED_ROOT.as_posix(),
    "zones": ["CB", "MO", "HK"],
}

EXIT_OK, EXIT_ERROR, EXIT_DATA, EXIT_NOWAY = 0, 1, 2, 3
PY = sys.executable


# ═════════════════════════════════════════════════════════════════════════════
#  Volná NE-malá auta ze stavu dne
# ═════════════════════════════════════════════════════════════════════════════

def free_big_types(remaining: dict, fleet_rows: list[dict]) -> list[dict]:
    """Volné NE-malé typy (nosnost > SMALL_MAX_KG) se zbytkem > 0, od
    nejmenšího — menší adekvátní auto má přednost (větší zůstane v záloze)."""
    small = fb.small_type_codes(fleet_rows)
    by_code = {r["type_code"].strip(): r for r in fleet_rows}
    out = []
    for code, cnt in (remaining or {}).items():
        row = by_code.get(code)
        if row is None or code in small or int(cnt) <= 0:
            continue
        out.append({"type_code": code, "max_kg": float(row["max_kg"]),
                    "count": int(cnt)})
    return sorted(out, key=lambda x: x["max_kg"])


def pick_big_for(kg: float, free_big: list[dict]) -> dict | None:
    """Nejmenší volné velké auto, do kterého se pár vejde (bez násobiče)."""
    for b in free_big:
        if b["count"] > 0 and kg <= b["max_kg"]:
            return b
    return None


def apply_merge_to_counts(remaining: dict, big_type: str,
                          freed_types: list[str]) -> dict:
    """Nový zbytek flotily: velké auto odjíždí sloučenou linku (−1),
    obě malá auta z linek se vracejí do poolu (+1 každé)."""
    out = dict(remaining)
    out[big_type] = int(out.get(big_type, 0)) - 1
    for t in freed_types:
        out[t] = int(out.get(t, 0)) + 1
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Linky dne (výsledky CB/MO/HK) a jejich zastávky
# ═════════════════════════════════════════════════════════════════════════════

def _tmin(s) -> int | None:
    try:
        h, m = str(s).strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _fmt_t(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def load_zone_lines(results_dir: Path, zone: str, small_codes: set[str]) -> list[dict]:
    """Linky malých aut (ne dvojlinky) se zastávkami. Chybějící výsledky
    zóny nejsou chyba téhle funkce — řeší volající."""
    stops: dict[str, list[dict]] = {}
    with open(results_dir / "lines_stops.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not str(r.get("order_id", "")).strip():
                continue        # sklad
            w = str(r.get("window", ""))
            ws, we = (_tmin(w.split("–")[0]), _tmin(w.split("–")[1])) if "–" in w else (None, None)
            stops.setdefault(r["line_id"], []).append({
                "order_id": r["order_id"],
                "loc": str(r.get("location_code", "")).strip(),
                "lat": float(r["lat"]), "lon": float(r["lon"]),
                "service": int(float(r.get("service_min", 0) or 0)),
                "ws": ws if ws is not None else 0,
                "we": we if we is not None else 24 * 60,
            })
    lines = []
    with open(results_dir / "lines_summary.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            vid = str(r.get("vehicle_id", "")).strip()
            lid = str(r.get("line_id", "")).strip()
            if not vid or not lid or lid not in stops:
                continue
            vtype = vid.rsplit("_", 1)[0]
            if vtype not in small_codes:
                continue
            if str(r.get("double_run", "")).strip().lower() in ("1", "true", "ano"):
                continue
            lines.append({"zone": zone, "line_id": lid, "vehicle_id": vid,
                          "type_code": vtype,
                          "kg": float(r.get("total_kg", 0) or 0),
                          "km": float(r.get("total_km", 0) or 0),
                          "stops": stops[lid]})
    return lines


# ═════════════════════════════════════════════════════════════════════════════
#  Jednoautový re-solve páru (OR-Tools) — jádro měřené 20. 8.
# ═════════════════════════════════════════════════════════════════════════════

def solve_pair(dur, nodes: list[int], meta: list[dict]) -> dict | None:
    """
    Trasa sklad → zastávky obou linek → sklad. Okna soft (penalta za minutu
    mimo původní okno), tvrdý strop hledání široký — klasifikuje se podle
    skutečně dosaženého porušení. Pauzy 45 min po 4,5 h se přičítají
    posunem PO řešení (konzervativně). Vrací dict s trasou, porušeními a
    časy, nebo None (OR-Tools nic nenašel).
    `dur` je hgv matice minut, `nodes` indexy do ní (nodes[0] = sklad),
    `meta` per zastávka: ws, we, service (+ libovolné identifikační klíče).
    """
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    C = CONFIG
    n = len(nodes)
    nodes_meta = [{"service": 0, "ws": 0, "we": C["latest_return_min"]}] + list(meta)
    man = pywrapcp.RoutingIndexManager(n, 1, 0)
    rt = pywrapcp.RoutingModel(man)

    def transit(i, j):
        a, b = man.IndexToNode(i), man.IndexToNode(j)
        return int(dur[nodes[a]][nodes[b]]) + (nodes_meta[a]["service"] if a else 0)

    cb = rt.RegisterTransitCallback(transit)
    rt.SetArcCostEvaluatorOfAllVehicles(cb)
    rt.AddDimension(cb, C["slack_min"], C["latest_return_min"], False, "T")
    tdim = rt.GetDimensionOrDie("T")
    hard = 6 * 60                       # strop hledání; dohodnutý limit řeší klasifikace
    for a in range(1, n):
        idx = man.NodeToIndex(a)
        m = nodes_meta[a]
        tdim.CumulVar(idx).SetRange(max(0, m["ws"] - hard), m["we"] + hard)
        tdim.SetCumulVarSoftLowerBound(idx, m["ws"], 100)
        tdim.SetCumulVarSoftUpperBound(idx, m["we"], 100)
    tdim.CumulVar(rt.End(0)).SetRange(0, C["latest_return_min"])

    p = pywrapcp.DefaultRoutingSearchParameters()
    p.first_solution_strategy = (routing_enums_pb2.FirstSolutionStrategy
                                 .PARALLEL_CHEAPEST_INSERTION)
    p.local_search_metaheuristic = (routing_enums_pb2.LocalSearchMetaheuristic
                                    .GUIDED_LOCAL_SEARCH)
    p.time_limit.FromMilliseconds(int(CONFIG["solve_limit_s"] * 1000))
    sol = rt.SolveWithParameters(p)
    if sol is None:
        return None

    order, arr = [], []
    idx = rt.Start(0)
    while not rt.IsEnd(idx):
        a = man.IndexToNode(idx)
        order.append(a)
        arr.append(sol.Value(tdim.CumulVar(idx)))
        idx = sol.Value(rt.NextVar(idx))
    end_t = sol.Value(tdim.CumulVar(idx))

    drive = sum(int(dur[nodes[order[k]]][nodes[order[k + 1]]])
                for k in range(len(order) - 1))
    drive += int(dur[nodes[order[-1]]][nodes[0]])

    # pauzy: posun příjezdů o 45 min po každých 4,5 h od výjezdu
    dep = arr[0]
    shift, next_break = 0, dep + CONFIG["break_after_min"]
    visits = []
    for a_i, t in zip(order[1:], arr[1:]):
        t2 = t + shift
        while t2 >= next_break:
            shift += CONFIG["break_min"]
            t2 = t + shift
            next_break += CONFIG["break_after_min"]
        m = nodes_meta[a_i]
        d = (m["ws"] - t2) if t2 < m["ws"] else (t2 - m["we"] if t2 > m["we"] else 0)
        visits.append({**m, "arrival": t2, "viol": max(0, d),
                       "early": t2 < m["ws"]})
    end2 = end_t + shift

    viol = [v["viol"] for v in visits if v["viol"] > 0]
    return {
        "visits": visits, "dep": dep, "end": end2, "drive": drive,
        "viol_n": len(viol), "viol_min": sum(viol),
        "viol_max": max(viol) if viol else 0,
        "time_ok": end2 <= CONFIG["latest_return_min"] and drive <= CONFIG["max_drive_min"],
        "breaks": shift // CONFIG["break_min"],
    }


def find_pairs(lines: list[dict], durations: dict, free_big: list[dict],
               depot_index: int = 0) -> list[dict]:
    """
    Kandidátní páry (táž zóna, filtry, re-solve), seřazené: nejmíň
    porušených oken → nejmíň minut → nejkratší jízda. `durations` je
    {zone: matice}; linky musí mít `idx` (indexy zastávek do matice zóny).
    """
    unreach = 10 ** 5
    max_cap = max((b["max_kg"] for b in free_big), default=0)
    out = []
    by_zone: dict[str, list[dict]] = {}
    for l in lines:
        by_zone.setdefault(l["zone"], []).append(l)
    for zone, zl in by_zone.items():
        dur = durations[zone]
        for i in range(len(zl)):
            for j in range(i + 1, len(zl)):
                A, B = zl[i], zl[j]
                if len(A["stops"]) + len(B["stops"]) > CONFIG["max_stops"]:
                    continue
                kg = A["kg"] + B["kg"]
                if kg > max_cap:
                    continue
                nodes = [depot_index] + A["idx"] + B["idx"]
                if any(dur[a][b] >= unreach for a in nodes for b in nodes):
                    continue        # hgv se tam nedostane
                r = solve_pair(dur, nodes, A["stops"] + B["stops"])
                if r is None or not r["time_ok"]:
                    continue
                if r["viol_max"] > CONFIG["win_tol_min"]:
                    continue
                out.append({**r, "zone": zone, "A": A, "B": B, "kg": kg,
                            "stops_n": len(A["stops"]) + len(B["stops"])})
    return sorted(out, key=lambda p: (p["viol_n"], p["viol_min"], p["drive"]))


# ═════════════════════════════════════════════════════════════════════════════
#  Prezentace návrhu a schválení
# ═════════════════════════════════════════════════════════════════════════════

def format_proposal(p: dict, big: dict, n_freed_total: int) -> str:
    head = (f"Sloučení: {p['zone']} {p['A']['line_id']} + {p['B']['line_id']}"
            f" → {big['type_code']} ({big['max_kg']:.0f} kg)\n"
            f"  {p['stops_n']} zastávek, {p['kg']:.0f} kg, jízda "
            f"{p['drive'] // 60}:{p['drive'] % 60:02d} h"
            + (f", {p['breaks']}× pauza 45 min" if p["breaks"] else "")
            + f", výjezd {_fmt_t(p['dep'])}, návrat {_fmt_t(p['end'])}\n"
            f"  Uvolní: {p['A']['vehicle_id']} + {p['B']['vehicle_id']} "
            f"(celkem +{n_freed_total} malých aut pro PRAHU)")
    if p["viol_n"] == 0:
        return head + "\n  Okna: VŠECHNA DODRŽENA (0 porušení)"
    rows = [f"  Porušená okna: {p['viol_n']}, celkem {p['viol_min']} min "
            f"(nejvíc {p['viol_max']} min):"]
    for v in p["visits"]:
        if v["viol"] > 0:
            kdy = "před oknem" if v["early"] else "po okně"
            rows.append(f"    ! {v['loc']:<24} okno "
                        f"{_fmt_t(v['ws'])}–{_fmt_t(v['we'])}, příjezd "
                        f"{_fmt_t(v['arrival'])} ({kdy} o {v['viol']} min)")
    return head + "\n" + "\n".join(rows)


def format_route(p: dict) -> str:
    rows = [f"  {'poř.':<5}{'lokace':<26}{'okno':<14}{'příjezd':<9}poznámka"]
    for k, v in enumerate(p["visits"], 1):
        note = ""
        if v["viol"] > 0:
            note = f"+{v['viol']} min {'PŘED oknem' if v['early'] else 'PO okně'}"
        rows.append(f"  {k:<5}{v['loc']:<26}"
                    f"{_fmt_t(v['ws'])}–{_fmt_t(v['we']):<8} "
                    f"{_fmt_t(v['arrival']):<9}{note}")
    return "\n".join(rows)


def ask_user(prompt: str) -> str:
    """Odpověď a/d/n (povolit / další návrh / nepovolit=konec)."""
    while True:
        r = input(prompt).strip().lower()
        if r in ("a", "d", "n", ""):
            return r or "n"


# ═════════════════════════════════════════════════════════════════════════════
#  Smyčka záchrany (testovatelná — vstřikuje se ask i běh solveru)
# ═════════════════════════════════════════════════════════════════════════════

def rescue_loop(pairs: list[dict], free_big: list[dict], remaining: dict,
                run_pr, ask=ask_user, echo=print) -> dict:
    """
    Nabízí páry od nejlepšího; po každém povolení přeplánuje PR.
    `run_pr(remaining) -> (rc, info)` spustí solver s upraveným zbytkem
    flotily. Vrací {"status": ok|refused|noway, "merges": [...],
    "remaining": {...}, "pr": info|None}.
    """
    free = [dict(b) for b in free_big]
    used_lines: set[str] = set()
    merges: list[dict] = []
    counts = dict(remaining)
    queue = list(pairs)

    while True:
        pick = None
        for p in queue:
            key_a = f"{p['zone']}:{p['A']['line_id']}"
            key_b = f"{p['zone']}:{p['B']['line_id']}"
            if key_a in used_lines or key_b in used_lines:
                continue
            big = pick_big_for(p["kg"], free)
            if big is None:
                continue
            pick = (p, big)
            break
        if pick is None:
            if not merges:
                echo("\nŽádný pár linek nejde sloučit v dohodnutých mezích "
                     "(±60 min, 30 zastávek, hgv, návrat 23:30) — "
                     "NEZVLÁDNEME to ani se slučováním.")
                return {"status": "noway", "merges": [], "remaining": remaining,
                        "pr": None}
            echo("\nDalší pár už není (došla velká auta nebo kandidáti) "
                 "a PRAHA stále nevychází.")
            return {"status": "noway", "merges": merges, "remaining": counts,
                    "pr": None}

        p, big = pick
        n_after = len(merges) * 2 + 2
        echo("\n" + "─" * 64)
        echo(format_proposal(p, big, n_after))
        ans = ask("Povolit tahle porušení a zkusit doplánovat PRAHU? "
                  "[a=povolit / d=další návrh / n=konec] ")
        if ans == "n":
            echo("Nepovoleno — končím bez zásahu.")
            return {"status": "refused", "merges": merges, "remaining": counts,
                    "pr": None}
        if ans == "d":
            queue = [q for q in queue if q is not p]
            continue

        used_lines.add(f"{p['zone']}:{p['A']['line_id']}")
        used_lines.add(f"{p['zone']}:{p['B']['line_id']}")
        for b in free:
            if b["type_code"] == big["type_code"]:
                b["count"] -= 1
        counts = apply_merge_to_counts(
            counts, big["type_code"],
            [p["A"]["type_code"], p["B"]["type_code"]])
        merges.append({**p, "big": dict(big)})

        echo(f"\nPovoleno. Přeplánovávám PRAHU s uvolněnými auty "
             f"(+{len(merges) * 2} malých)...")
        rc, info = run_pr(counts)
        if rc == 0:
            return {"status": "ok", "merges": merges, "remaining": counts,
                    "pr": info}
        if rc == 3:
            echo("PRAHA stále nevychází — zkusím uvolnit další auta.")
            continue
        echo(f"[CHYBA] Přeplánování PR skončilo kódem {rc} "
             f"({'vadná data' if rc == 2 else 'technická chyba'}) — končím.")
        return {"status": "error", "rc": rc, "merges": merges,
                "remaining": counts, "pr": info}


# ═════════════════════════════════════════════════════════════════════════════
#  Report — co se musí (zatím ručně) změnit v ESO
# ═════════════════════════════════════════════════════════════════════════════

def build_report(date_str: str, result: dict, pr_dir: Path | None) -> str:
    lines = [f"# Nouzový plán (merge_rescue) — {date_str}",
             f"Vytvořeno {datetime.now():%Y-%m-%d %H:%M}. "
             f"NIC není zapsáno do plánů dep — změny se dělají ručně v ESO.", ""]
    for i, m in enumerate(result["merges"], 1):
        lines += [f"## Změna {i}: {m['zone']} — zrušit {m['A']['line_id']} "
                  f"a {m['B']['line_id']}, místo nich JEDNA linka na "
                  f"{m['big']['type_code']}",
                  f"- auta {m['A']['vehicle_id']} a {m['B']['vehicle_id']} "
                  f"se uvolňují pro PRAHU",
                  f"- {m['stops_n']} zastávek, {m['kg']:.0f} kg, výjezd "
                  f"{_fmt_t(m['dep'])}, návrat {_fmt_t(m['end'])}, jízda "
                  f"{m['drive'] // 60}:{m['drive'] % 60:02d} h"
                  + (f", {m['breaks']}× pauza 45 min" if m['breaks'] else ""),
                  f"- porušená okna: {m['viol_n']} "
                  f"(celkem {m['viol_min']} min, max {m['viol_max']} min)",
                  "", "Pořadí zastávek:", "```", format_route(m), "```", ""]
    if result["status"] == "ok" and pr_dir is not None:
        lines += ["## PRAHA — nový plán",
                  f"- hotový plán: `{pr_dir.as_posix()}` "
                  f"(lines_summary, lines_stops, eso_export)",
                  "- POZOR: přiřazení řidičů běželo/poběží nad původními "
                  "výsledky — sloučenou linku a nové PR linky je nutné "
                  "zohlednit ručně.", ""]
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
#  Main — sestavení dat, matice, spuštění smyčky
# ═════════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Nouzový plán: PR nevyšla → sloučit dvě linky malých aut "
                    "do volného velkého a uvolněná malá dát Praze.")
    ap.add_argument("date")
    ap.add_argument("--zones", nargs="*", default=None,
                    help=f"Zóny s liniemi ke slučování (default "
                         f"{' '.join(CONFIG['zones'])})")
    ap.add_argument("--label", default="", help="Přípona výsledkových složek")
    ap.add_argument("--budget", type=float, default=CONFIG["budget_min"],
                    help="Budget přeplánování PR na jedno kolo (min)")
    ap.add_argument("--yes", action="store_true",
                    help="Bez ptaní — všechno povolit (ladění)")
    ap.add_argument("--osm-source", default="current",
                    choices=["current", "stable"])
    ap.add_argument("--state-file", default="")
    ap.add_argument("--vehicle-types-file", default="")
    ap.add_argument("--force", action="store_true",
                    help="Pokračovat, i když je PR podle state naplánovaná")
    args = ap.parse_args()

    date_str = args.date
    suffix = f"_{args.label}" if args.label else ""
    root = Path(CONFIG["results_root"])
    state_dir = Path(CONFIG["state_root"]) / date_str
    state_path = Path(args.state_file) if args.state_file else state_dir / "state.json"
    zones = [z.upper() for z in args.zones] if args.zones else list(CONFIG["zones"])

    print("=" * 64)
    print(f"NOUZOVÝ PLÁN (merge_rescue) — {date_str}")
    print("=" * 64)

    if not state_path.exists():
        print(f"[CHYBA] Chybí {state_path} — večerní běh (plan_day real) "
              f"ještě neproběhl?")
        return EXIT_DATA
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if "PR" in state.get("planned", []) and not args.force:
        print("[CHYBA] Podle state.json je PRAHA naplánovaná — nouzový plán "
              "je pro večer, kdy PR skončila exit 3.\n        Vědomě přes: --force")
        return EXIT_DATA

    import vrp_solver_lines_v6 as S
    from osm_routing import apply_osm_source
    apply_osm_source(S.CONFIG, args.osm_source)

    fleet_path = (Path(args.vehicle_types_file) if args.vehicle_types_file
                  else Path(S.find_vehicle_types_file()))
    fleet_rows = fb.load_fleet_rows(fleet_path)
    small_codes = fb.small_type_codes(fleet_rows)
    remaining = state.get("remaining", {})
    big = free_big_types(remaining, fleet_rows)
    if not big:
        print(f"[CHYBA] Ve zbytku flotily není žádné volné NE-malé auto "
              f"(remaining: {remaining}) — není čím slučovat.")
        return EXIT_NOWAY
    print(f"Volná velká auta: "
          + ", ".join(f"{b['type_code']} ({b['max_kg']:.0f} kg) ×{b['count']}"
                      for b in big))

    pr_orders = Path(CONFIG["prepared_root"]) / "PR" / f"orders_PR_{date_str}.csv"
    if not pr_orders.exists():
        print(f"[CHYBA] Chybí {pr_orders} — bez objednávek PR není co plánovat.")
        return EXIT_DATA

    # linky + hgv matice per zóna
    lines, durations = [], {}
    for z in zones:
        d = root / z / f"{date_str}{suffix}"
        if not (d / "lines_summary.csv").exists():
            print(f"  [!] {z}: chybí výsledky ({d}) — vynechávám")
            continue
        zl = load_zone_lines(d, z, small_codes)
        if len(zl) < 2:
            continue
        pts = [(S.DEPOT["lat"], S.DEPOT["lon"])]
        index: dict = {}
        for l in zl:
            l["idx"] = []
            for st in l["stops"]:
                key = (round(st["lat"], 6), round(st["lon"], 6))
                if key not in index:
                    index[key] = len(pts)
                    pts.append(key)
                l["idx"].append(index[key])
        _, dur = S.get_matrix(pts, profile="driving-hgv")
        durations[z] = dur
        lines.extend(zl)
    if not lines:
        print("[CHYBA] Žádné linky malých aut ke slučování.")
        return EXIT_DATA
    print(f"Kandidátní linky: {len(lines)} (zóny "
          f"{', '.join(sorted(durations))}); hledám sloučitelné páry...")

    t0 = time.time()
    pairs = find_pairs(lines, durations, big)
    print(f"Nalezeno {len(pairs)} sloučitelných párů v mezích "
          f"(±{CONFIG['win_tol_min']} min, {CONFIG['max_stops']} zastávek, "
          f"hgv, návrat {_fmt_t(CONFIG['latest_return_min'])}) "
          f"za {time.time() - t0:.0f} s")

    flags = state.get("flags", {"capacity_multiplier": 1.0, "double_runs": False})
    pr_out = root / "PR" / f"{date_str}_rescue{suffix}"

    def run_pr(counts: dict) -> tuple[int, dict | None]:
        fleet_file = fb.write_fleet_file(
            fleet_rows, state_dir / "fleet_PR_rescue.csv", counts)
        cmd = [PY, "vrp_solver_lines_v6.py",
               "--orders-file", pr_orders.as_posix(),
               "--output-dir", pr_out.as_posix(),
               "--budget-min", f"{args.budget:g}",
               "--vehicle-types-file", str(fleet_file),
               "--capacity-multiplier", f"{flags.get('capacity_multiplier', 1.0):g}",
               "--osm-source", args.osm_source]
        if flags.get("double_runs"):
            cmd.append("--double-runs")
        rc = subprocess.run(cmd).returncode
        info = None
        status_f = pr_out / "run_status.json"
        if status_f.exists():
            info = json.loads(status_f.read_text(encoding="utf-8"))
        return rc, info

    ask = (lambda _prompt: "a") if args.yes else ask_user
    result = rescue_loop(pairs, big, remaining, run_pr, ask=ask)

    if result["status"] == "refused":
        return EXIT_NOWAY
    if result["status"] == "error":
        return result.get("rc", EXIT_ERROR)

    # report i při neúspěchu (ať je vidět, co se zkusilo)
    report = build_report(date_str, result,
                          pr_out if result["status"] == "ok" else None)
    report_path = state_dir / f"merge_rescue_{date_str}.md"
    report_path.write_text(report, encoding="utf-8")
    (state_dir / f"merge_rescue_{date_str}.json").write_text(json.dumps({
        "date": date_str, "status": result["status"],
        "merges": [{"zone": m["zone"], "lines": [m["A"]["line_id"], m["B"]["line_id"]],
                    "freed_vehicles": [m["A"]["vehicle_id"], m["B"]["vehicle_id"]],
                    "big_type": m["big"]["type_code"], "kg": m["kg"],
                    "stops": m["stops_n"], "viol_n": m["viol_n"],
                    "viol_min": m["viol_min"], "viol_max": m["viol_max"],
                    "dep": _fmt_t(m["dep"]), "end": _fmt_t(m["end"])}
                   for m in result["merges"]],
        "remaining_after": result["remaining"],
        "pr": ({k: result["pr"].get(k) for k in
                ("status", "lines_count", "total_cost_kc", "output_dir")}
               if result.get("pr") else None),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if result["status"] == "ok":
        pr = result["pr"] or {}
        print("\n" + "=" * 64)
        print(f"PRAHA VYCHÁZÍ — {pr.get('lines_count', '?')} linek, "
              f"{pr.get('total_cost_kc', 0):,.0f} Kč "
              f"(+{len(result['merges']) * 2} malých aut ze slučování)")
        print(f"PR plán:  {pr_out.as_posix()}")
        print(f"Report:   {report_path.as_posix()}  ← CO ZMĚNIT (ručně v ESO)")
        return EXIT_OK
    print(f"\nReport o pokusu: {report_path.as_posix()}")
    return EXIT_NOWAY


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nPřerušeno — nic nebylo zapsáno do plánů dep.")
        sys.exit(EXIT_ERROR)
