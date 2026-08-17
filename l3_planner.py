"""
l3_planner.py — výběr rampových objednávek pro kamion (porušení L3)
===================================================================

Čistá logika pod plan_day (žádné subprocess, žádný solver):

L3 = kamion 18t jede „předem" a sebere VELKÉ RAMPOVÉ objednávky napříč
všemi depy, aby se zbytek dne vešel do malých aut. Vybírá se odpoledne
z PREDIKČNÍCH dat, ale jen ze SKUTEČNÝCH objednávek (predicted == 0 —
čísla existují a večer se nemění). Večer prepare ta čísla vyřadí
z plánů dep (--exclude-orders-file) a po posledním depu se z reálných
vyřazených objednávek spočítá finální trasa (plan_day l3).

Pravidla výběru (z diskuse 14. 8. 2026, doplněno 16. 8. 2026):
  - jen lokace s rampou (ramp == 1); bez rampy kamion vyložit neumí
  - co nejméně objednávek s co nejvíc kg
  - cíl kg = missing_kg + max(10 % missing_kg, 500 kg)
  - když rampové lokace tolik nedají, vezme se co je — zbytek řeší L1+L2
  - okna L3 lokací neplatí: pevně 04:00–20:00
  - trasa kamionu musí být SJÍZDNÁ: denní limit jízdy řidiče (EU 9 h),
    pauzy, okno dne, nosnost — a to celá smyčka, ne „okruh kolem skladu".
    3 × 1 000 kg v rozích 100km čtverce je lepší náklad než 20 × 70 kg
    v okruhu 40 km, pokud se ta smyčka vejde do dne.

Dva výběry:
  select_locations_vrp  — HLAVNÍ. Prize-collecting VRP v OR-Tools nad
                          reálnou hgv maticí (ORS): každá lokace je
                          volitelná s penále kg × λ, tvrdé podmínky jsou
                          nosnost, denní jízda, pauzy, okno; Σ kg ≤ cíl.
                          Solver sám dělá obchod „100 km zajížďky = 2 800 Kč
                          = stojí za to jen pro ≥ 2 800/λ kg".
  select_locations      — záložní greedy (kg × blízkost, bez času), jen
                          když matice není k dispozici. Nezná sjízdnost —
                          16. 8. 2026 vybral 611 km / 13 h jízdy na jeden
                          kamion.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

# ── Parametry (konfigurovatelné) ─────────────────────────────────────────────
L3_CONFIG = {
    "window_from":        "04:00",   # okna L3 lokací neplatí — pevný rozsah
    "window_to":          "20:00",
    "target_buffer_pct":  0.10,      # cíl = missing_kg + max(pct, min_kg)
    "target_buffer_min_kg": 500.0,
    # Záložní greedy: score = kg / (1 + km × koef).
    # 0.02 → lokace 50 km od klastru má poloviční score než stejná vedle.
    "proximity_koef":     0.02,
    "budget_min":         10.0,      # solver budget večerní L3 trasy
    # ── VRP výběr ────────────────────────────────────────────────────────
    # Hodnota 1 kg sebraného kamionem (Kč) = penále za VYNECHÁNÍ lokace.
    # Referenční bod: malá linka ≈ 2 650 Kč / ~1 000 kg ≈ 2,7 Kč/kg; L3 má
    # ale bránit porušením (L1+L2), ne jen šetřit — proto ~2× tolik.
    # Vyšší λ = kamion jede pro kg dál. Když se nedosáhne missing_kg,
    # zkusí se λ × escalation postupně (a vezme se nejlepší výsledek).
    "kg_value_kc":        6.0,
    "kg_value_escalation": (1, 3, 10),
    "select_time_limit_sec": 15,     # per pokus (× počet eskalací)
    "cap_at_target":      True,      # Σ naložených kg ≤ cíl (ne víc, než třeba)
    # Cena jedné zastávky kamionu (Kč) — vykládka na rampě není zadarmo a
    # pravidlo zní „co nejméně objednávek s co nejvíc kg": drobná lokace se
    # vezme jen když její kg × λ pokryjí zastávku i zajížďku.
    "stop_cost_kc":       150.0,
}


def l3_target_kg(missing_kg: float) -> float:
    """Kolik kg má kamion sebrat: přestřel + max(10 % přestřelu, 500 kg)."""
    return missing_kg + max(L3_CONFIG["target_buffer_pct"] * missing_kg,
                            L3_CONFIG["target_buffer_min_kg"])


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Vzdušná vzdálenost — na výběr lokací stačí (trasu řeší solver s OSRM)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# ═════════════════════════════════════════════════════════════════════════════
#  Kandidáti z prepared souborů
# ═════════════════════════════════════════════════════════════════════════════

def load_l3_candidates(prepared_root: Path | str, depots: list[str],
                       date_str: str) -> list[dict]:
    """
    Rampové SKUTEČNÉ objednávky (ramp==1, predicted==0) z prepared
    souborů, agregované per lokace:
    [{location_code, depot, kg, service_sec, lat, lon, customer_name,
      orders: [{order_number, kg}]}]
    """
    rows_by_depot: dict[str, list[dict]] = {}
    for depot in depots:
        path = Path(prepared_root) / depot / f"orders_{depot}_{date_str}.csv"
        if not path.exists():
            raise FileNotFoundError(f"[CHYBA] Chybí {path} — L3 výběr "
                                    f"potřebuje prepared soubor depa {depot}.")
        with open(path, encoding="utf-8") as f:
            rows_by_depot[depot] = [
                row for row in csv.DictReader(f)
                if row.get("ramp", "0") == "1"
                and row.get("predicted", "0") == "0"]   # dopredikovaná — NIKDY
    return aggregate_locations(rows_by_depot)


def aggregate_locations(rows_by_depot: dict[str, list[dict]]) -> list[dict]:
    """Řádky prepared formátu → lokace (kg, servis, GPS, objednávky).
    Společné pro odpolední výběr i večerní kontrolu sjízdnosti."""
    by_loc: dict[tuple[str, str], dict] = {}
    for depot, rows in rows_by_depot.items():
        for row in rows:
            key = (depot, row["location_code"])
            loc = by_loc.setdefault(key, {
                "location_code": row["location_code"],
                "depot": depot,
                "customer_name": row.get("customer_name", ""),
                "kg": 0.0,
                "service_sec": 0,
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "orders": [],
            })
            kg = float(row["weight_kg"])
            loc["kg"] += kg
            loc["service_sec"] += int(float(row.get("service_sec") or 0))
            loc["orders"].append({"order_number": row["order_number"],
                                  "kg": kg})
    return sorted(by_loc.values(),
                  key=lambda l: (-l["kg"], l["depot"], l["location_code"]))


# ═════════════════════════════════════════════════════════════════════════════
#  Výběr lokací — hybrid kg × blízkost, binování do kamionů
# ═════════════════════════════════════════════════════════════════════════════

def select_locations(candidates: list[dict], target_kg: float,
                     truck_caps_kg: list[float],
                     proximity_koef: float | None = None) -> dict:
    """
    Greedy výběr: seed = nejtěžší lokace; pak opakovaně lokace s nejlepším
    `kg / (1 + km_k_nejbližší_vybrané × koef)`, dokud nedosáhneme cíle
    nebo se nic nevejde do kamionů. Deterministicky (remíza: kg desc,
    depo, lokace).

    Binování: first-fit-decreasing do kamionů (kapacity v kg, L0 = 100 %).
    Lokace se NEdělí mezi kamiony (jedna vykládka = jeden kamion).

    Vrací {selected, selected_kg, target_kg, bins: [[loc, ...] per kamion],
           truck_caps_kg, exhausted: bool}.
    """
    koef = (L3_CONFIG["proximity_koef"] if proximity_koef is None
            else proximity_koef)
    caps = sorted(truck_caps_kg, reverse=True)
    if not candidates or not caps:
        return {"selected": [], "selected_kg": 0.0, "target_kg": target_kg,
                "bins": [[] for _ in caps], "truck_caps_kg": caps,
                "exhausted": not candidates}

    bins: list[list[dict]] = [[] for _ in caps]
    bin_kg = [0.0] * len(caps)

    def fits(loc: dict) -> int | None:
        """First-fit-decreasing: index kamionu, kam se lokace vejde."""
        for i in range(len(caps)):
            if bin_kg[i] + loc["kg"] <= caps[i]:
                return i
        return None

    selected: list[dict] = []
    remaining = list(candidates)
    selected_kg = 0.0

    while remaining and selected_kg < target_kg:
        if not selected:
            scored = [(loc["kg"], loc) for loc in remaining]
        else:
            def dist(loc):
                return min(haversine_km(loc["lat"], loc["lon"],
                                        s["lat"], s["lon"])
                           for s in selected)
            scored = [(loc["kg"] / (1.0 + dist(loc) * koef), loc)
                      for loc in remaining]
        # deterministicky: score desc, pak kg desc, pak depo + lokace
        scored.sort(key=lambda x: (-x[0], -x[1]["kg"], x[1]["depot"],
                                   x[1]["location_code"]))
        placed = False
        for _, loc in scored:
            b = fits(loc)
            if b is None:
                continue
            bins[b].append(loc)
            bin_kg[b] += loc["kg"]
            selected.append(loc)
            selected_kg += loc["kg"]
            remaining.remove(loc)
            placed = True
            break
        if not placed:
            break       # nic dalšího se do kamionů nevejde

    return {"selected": selected, "selected_kg": round(selected_kg, 1),
            "target_kg": round(target_kg, 1), "bins": bins,
            "truck_caps_kg": caps,
            "exhausted": selected_kg < target_kg,
            "method": "greedy", "routes": [], "dropped": []}


# ═════════════════════════════════════════════════════════════════════════════
#  Výběr lokací — VRP s volitelnými zastávkami (hlavní cesta)
# ═════════════════════════════════════════════════════════════════════════════

def _hhmm(minutes: int) -> str:
    minutes = int(minutes)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _time_to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def driver_rules() -> dict:
    """Parametry režimu řidiče EU — JEDINÝ zdroj je CONFIG solveru
    (večerní trasa je počítá stejně). Import až tady, aby l3_planner
    zůstal lehký pro testy bez OR-Tools modelu."""
    from vrp_solver_lines_v6 import CONFIG as SOLVER_CONFIG
    return {
        "break_after_h": float(SOLVER_CONFIG["driver_break_after_h"]),
        "break_min":     int(SOLVER_CONFIG["driver_break_min"]),
        "max_drive_h":   float(SOLVER_CONFIG["driver_max_drive_h"]),
        # strop zastávek na trasu platí i pro kamion — večerní solver ho
        # vynucuje, výběr ho tedy musí znát (16. 8. 2026: 25 lokací na
        # 1 kamion prošlo kontrolou a solver s max 20 spadl)
        "max_stops":     int(SOLVER_CONFIG.get("max_stops_per_route") or 0),
        # max čekání na zastávce (slack Time dimenze) — jinak by výběr
        # dovolil prostoj, který večerní solver zakáže
        "slack_min":     int(SOLVER_CONFIG.get("time_slack_max_min", 60)),
    }


def select_locations_vrp(candidates: list[dict], dist_km, dur_min,
                         trucks: list[dict], target_kg: float,
                         missing_kg: float, *,
                         kg_value_kc: float | None = None,
                         time_limit_sec: int | None = None,
                         mandatory: bool = False,
                         driver: dict | None = None,
                         escalation: tuple | None = None,
                         stop_cost_kc: float | None = None) -> dict:
    """
    Prize-collecting VRP: které rampové lokace má kamion (kamiony) sebrat,
    aby to bylo SJÍZDNÉ a vyplatilo se.

    Vstup:
      candidates  lokace [{location_code, depot, kg, service_sec, lat, lon,
                  orders}], v pořadí uzlů 1..n (uzel 0 = sklad)
      dist_km, dur_min   (n+1)×(n+1) matice (hgv profil), uzel 0 = sklad
      trucks      fyzické kamiony [{type_code, max_kg, cost_per_km,
                  start_cost}] — jeden záznam per kus
      target_kg   horní mez Σ naložených kg (cíl + rezerva); missing_kg =
                  kolik OPRAVDU chybí — pod to je výsledek „exhausted"
      mandatory   True = všechny lokace povinné (večerní kontrola
                  sjízdnosti reálných objednávek); bez penále a bez stropu

    Model (OR-Tools):
      cena = km × Kč/km + výjezd + zastávky × stop_cost + λ × nesebrané kg
      Capacity ≤ nosnost · Drive (jen jízda) ≤ driver.max_drive_h ·
      Stops ≤ driver.max_stops (= CONFIG max_stops_per_route solveru) ·
      Time: okna 04:00–20:00, pauzy jako solver (elapsed) · Σ kg ≤ target
    Když Σ kg < missing_kg, zkusí se λ × escalation a vezme se nejlepší.

    Vrací stejný tvar jako select_locations + routes (per kamion km /
    jízda / span / pauzy), dropped, kg_value_kc, method="vrp".
    """
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    import numpy as np

    n = len(candidates)
    K = len(trucks)
    if n == 0 or K == 0:
        return {"selected": [], "selected_kg": 0.0, "target_kg": round(target_kg, 1),
                "bins": [[] for _ in trucks],
                "truck_caps_kg": [float(t["max_kg"]) for t in trucks],
                "exhausted": True, "routes": [], "dropped": list(candidates),
                "kg_value_kc": kg_value_kc, "method": "vrp",
                "feasible": n == 0}

    rules = driver if driver is not None else driver_rules()
    lam = float(L3_CONFIG["kg_value_kc"] if kg_value_kc is None else kg_value_kc)
    tl = int(L3_CONFIG["select_time_limit_sec"] if time_limit_sec is None
             else time_limit_sec)
    steps = (1,) if mandatory else tuple(
        L3_CONFIG["kg_value_escalation"] if escalation is None else escalation)
    stop_cost = float(L3_CONFIG.get("stop_cost_kc", 0.0) if stop_cost_kc is None
                      else stop_cost_kc)

    dist = np.asarray(dist_km, dtype=float)
    dur = np.asarray(dur_min, dtype=float)
    assert dist.shape == (n + 1, n + 1) and dur.shape == (n + 1, n + 1), \
        "matice musí být (n+1)×(n+1), uzel 0 = sklad"

    SCALE = 100                                     # Kč → setiny
    dist_i = np.rint(dist).astype(int).tolist()
    dur_i = np.rint(dur).astype(int).tolist()
    service = [0] + [int(math.ceil(c.get("service_sec", 0) / 60)) for c in candidates]
    demand = [0] + [int(round(c["kg"])) for c in candidates]
    w_from, w_to = _time_to_min(L3_CONFIG["window_from"]), _time_to_min(L3_CONFIG["window_to"])
    max_drive = int(rules["max_drive_h"] * 60)
    max_stops = int(rules.get("max_stops") or 0)
    slack = int(rules.get("slack_min") or 24 * 60)
    break_after = int(rules["break_after_h"] * 60)
    break_min = int(rules["break_min"])
    horizon = 24 * 60

    def _solve(lam_now: float):
        manager = pywrapcp.RoutingIndexManager(n + 1, K, 0)
        routing = pywrapcp.RoutingModel(manager)
        stop_c = int(round(stop_cost * SCALE))
        for v, t in enumerate(trucks):
            cpk = float(t["cost_per_km"])
            cb = routing.RegisterTransitCallback(
                lambda fi, ti, cpk=cpk: int(round(
                    dist_i[manager.IndexToNode(fi)][manager.IndexToNode(ti)]
                    * cpk * SCALE))
                + (stop_c if manager.IndexToNode(ti) != 0 else 0))
            routing.SetArcCostEvaluatorOfVehicle(cb, v)
            routing.SetFixedCostOfVehicle(int(round(float(t.get("start_cost", 0)) * SCALE)), v)
        dem_cb = routing.RegisterUnaryTransitCallback(
            lambda fi: demand[manager.IndexToNode(fi)])
        routing.AddDimensionWithVehicleCapacity(
            dem_cb, 0, [int(t["max_kg"]) for t in trucks], True, "Capacity")
        time_cb = routing.RegisterTransitCallback(
            lambda fi, ti: dur_i[manager.IndexToNode(fi)][manager.IndexToNode(ti)]
            + service[manager.IndexToNode(fi)])
        routing.AddDimension(time_cb, slack, horizon, False, "Time")
        drive_cb = routing.RegisterTransitCallback(
            lambda fi, ti: dur_i[manager.IndexToNode(fi)][manager.IndexToNode(ti)])
        routing.AddDimension(drive_cb, 0, max_drive, True, "Drive")
        if max_stops:
            stop_cb = routing.RegisterUnaryTransitCallback(
                lambda fi: 0 if manager.IndexToNode(fi) == 0 else 1)
            routing.AddDimension(stop_cb, 0, max_stops, True, "Stops")
        time_dim = routing.GetDimensionOrDie("Time")
        for node in range(1, n + 1):
            time_dim.CumulVar(manager.NodeToIndex(node)).SetRange(w_from, w_to)
        for v in range(K):
            time_dim.CumulVar(routing.Start(v)).SetRange(0, horizon - 1)
            time_dim.CumulVar(routing.End(v)).SetRange(0, horizon - 1)
        # pauzy — stejný model jako solver (_add_driver_breaks)
        solver = routing.solver()
        transits = [0] * routing.Size()
        for idx in range(routing.Size()):
            if not (routing.IsStart(idx) or routing.IsEnd(idx)):
                transits[idx] = service[manager.IndexToNode(idx)]
        intervals_by_v = []
        max_breaks = max(1, horizon // break_after)
        for v in range(K):
            ivs = [solver.FixedDurationIntervalVar(0, horizon, break_min, True,
                                                   f"l3brk_{v}_{b}")
                   for b in range(max_breaks)]
            time_dim.SetBreakIntervalsOfVehicle(ivs, v, transits)
            time_dim.SetBreakDistanceDurationOfVehicle(break_after, break_min, v)
            intervals_by_v.append(ivs)
        # volitelnost lokací + strop Σ kg
        if not mandatory:
            for node in range(1, n + 1):
                routing.AddDisjunction([manager.NodeToIndex(node)],
                                       int(round(demand[node] * lam_now * SCALE)))
            if L3_CONFIG.get("cap_at_target", True) and target_kg > 0:
                cap_dim = routing.GetDimensionOrDie("Capacity")
                solver.Add(solver.Sum([cap_dim.CumulVar(routing.End(v))
                                       for v in range(K)]) <= int(target_kg))
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION)
        params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH)
        params.time_limit.seconds = max(1, tl)
        sol = routing.SolveWithParameters(params)
        if sol is None:
            return None
        drive_dim = routing.GetDimensionOrDie("Drive")
        routes, bins = [], [[] for _ in trucks]
        for v in range(K):
            idx = routing.Start(v)
            if routing.IsEnd(sol.Value(routing.NextVar(idx))):
                continue
            nodes, km = [], 0.0
            start_min = sol.Min(time_dim.CumulVar(idx))
            prev = 0
            while not routing.IsEnd(idx):
                node = manager.IndexToNode(idx)
                if node != 0:
                    nodes.append(node)
                nxt = sol.Value(routing.NextVar(idx))
                nnode = manager.IndexToNode(nxt)
                km += dist[prev][nnode]
                prev = nnode
                idx = nxt
            end_min = sol.Min(time_dim.CumulVar(idx))
            drive = sol.Min(drive_dim.CumulVar(idx))
            n_breaks = sum(1 for iv in intervals_by_v[v] if sol.PerformedValue(iv))
            locs = [candidates[i - 1] for i in nodes]
            bins[v] = locs
            routes.append({
                "truck_idx": v, "type_code": trucks[v]["type_code"],
                "locations": [c["location_code"] for c in locs],
                "kg": round(sum(c["kg"] for c in locs), 1),
                "km": round(km, 1), "drive_min": int(drive),
                "span_min": int(end_min - start_min),
                "start": _hhmm(start_min), "end": _hhmm(end_min),
                "breaks": n_breaks,
            })
        selected = [c for b in bins for c in b]
        cost_kc = sol.ObjectiveValue() / SCALE
        return {"routes": routes, "bins": bins, "selected": selected,
                "selected_kg": sum(c["kg"] for c in selected),
                "objective_kc": cost_kc}

    best, best_lam = None, lam
    for mult in steps:
        lam_now = lam * mult
        res = _solve(lam_now)
        if res is None:
            continue
        if best is None or res["selected_kg"] > best["selected_kg"] + 1e-6:
            best, best_lam = res, lam_now
        if res["selected_kg"] >= missing_kg - 1e-6:
            break

    if best is None:
        # ani povinná varianta nemá řešení / nic se nevešlo
        return {"selected": [], "selected_kg": 0.0, "target_kg": round(target_kg, 1),
                "bins": [[] for _ in trucks],
                "truck_caps_kg": [float(t["max_kg"]) for t in trucks],
                "exhausted": True, "routes": [], "dropped": list(candidates),
                "kg_value_kc": lam, "method": "vrp", "feasible": False}

    sel_codes = {(c["depot"], c["location_code"]) for c in best["selected"]}
    dropped = [c for c in candidates
               if (c["depot"], c["location_code"]) not in sel_codes]
    return {"selected": best["selected"],
            "selected_kg": round(best["selected_kg"], 1),
            "target_kg": round(target_kg, 1),
            "bins": best["bins"],
            "truck_caps_kg": [float(t["max_kg"]) for t in trucks],
            "exhausted": best["selected_kg"] < missing_kg - 1e-6,
            "routes": best["routes"], "dropped": dropped,
            "kg_value_kc": best_lam, "method": "vrp",
            "feasible": not dropped if mandatory else True,
            "objective_kc": round(best["objective_kc"], 1)}


def check_l3_feasible(locations: list[dict], dist_km, dur_min,
                      trucks: list[dict], time_limit_sec: int | None = None,
                      driver: dict | None = None) -> dict:
    """Večerní kontrola PŘED solverem: vejdou se VŠECHNY vyřazené lokace
    do kamionů při stejných pravidlech (jízda, pauzy, okno, nosnost)?
    Vrací výsledek select_locations_vrp v povinném režimu; feasible=False
    znamená „bez pomoci to nevyjde" — o 5 minut dřív než solver."""
    return select_locations_vrp(locations, dist_km, dur_min, trucks,
                                target_kg=0.0, missing_kg=0.0,
                                mandatory=True, time_limit_sec=time_limit_sec,
                                driver=driver)


def format_routes(routes: list[dict], max_drive_h: float | None = None) -> str:
    """Čitelný výpis odhadů per kamion do konzole / reportu."""
    if not routes:
        return "    (žádná trasa)"
    lines = []
    for r in routes:
        warn = ""
        if max_drive_h is not None and r["drive_min"] > max_drive_h * 60:
            warn = "  !!! přes denní limit jízdy"
        lines.append(
            f"    kamion {r['truck_idx'] + 1} {r['type_code']}: "
            f"{len(r['locations'])} lokací / {r['kg']:,.0f} kg | "
            f"{r['km']:,.0f} km, jízda {r['drive_min'] / 60:.1f} h, "
            f"{r['start']}–{r['end']} (span {r['span_min'] / 60:.1f} h), "
            f"pauzy {r['breaks']}{warn}")
    return "\n".join(lines)


def build_l3_decision_block(selection: dict, missing_kg: float,
                            trucks_by_type: dict[str, int]) -> dict:
    """Blok `l3` do decision — večer podle něj prepare vyřazuje a
    plan_day l3 staví trasu."""
    used_bins = [b for b in selection["bins"] if b]
    return {
        "missing_kg": round(missing_kg, 1),
        "target_kg": selection["target_kg"],
        "selected_kg": selection["selected_kg"],
        "exhausted": selection["exhausted"],
        "locations": [
            {"location_code": l["location_code"], "depot": l["depot"],
             "customer_name": l["customer_name"], "kg": round(l["kg"], 1)}
            for l in selection["selected"]],
        "orders": [
            {"order_number": o["order_number"], "depot": l["depot"],
             "location_code": l["location_code"], "kg": round(o["kg"], 1)}
            for l in selection["selected"] for o in l["orders"]],
        "trucks": trucks_by_type,
        "trucks_used": len(used_bins),
        "method": selection.get("method", "greedy"),
        # odhady per kamion z výběru (VRP): km / jízda / span / pauzy —
        # ať je už odpoledne vidět, že je trasa sjízdná
        "routes": selection.get("routes", []),
        "dropped_kg": round(sum(c["kg"] for c in selection.get("dropped", [])), 1),
        "kg_value_kc": selection.get("kg_value_kc"),
        "params": {k: L3_CONFIG[k] for k in
                   ("target_buffer_pct", "target_buffer_min_kg",
                    "proximity_koef", "window_from", "window_to",
                    "kg_value_kc", "cap_at_target", "stop_cost_kc")},
    }


def orders_by_depot(l3_block: dict) -> dict[str, list[str]]:
    """Čísla objednávek per depo — pro --exclude-orders-file."""
    out: dict[str, list[str]] = {}
    for o in l3_block.get("orders", []):
        out.setdefault(o["depot"], []).append(o["order_number"])
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Večer: sloučení l3_orders souborů pro solver
# ═════════════════════════════════════════════════════════════════════════════

def merge_l3_orders(l3_files: list[Path | str], out_path: Path | str) -> int:
    """
    Sloučí l3_orders_{DEPO} soubory do jednoho solver-ready CSV:
    block_id = "L3", okna přepsaná na pevný rozsah (okna L3 neplatí).
    Vrací počet objednávek.
    """
    rows, fieldnames = [], None
    for path in l3_files:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = fieldnames or reader.fieldnames
            for row in reader:
                row["block_id"] = "L3"
                row["time_from"] = L3_CONFIG["window_from"]
                row["time_to"] = L3_CONFIG["window_to"]
                rows.append(row)
    if not rows:
        raise ValueError("[CHYBA] Žádné L3 objednávky ke sloučení — "
                         "prepare žádné nevyřadil?")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
