"""
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!!  NEPOUŽÍVAT — tento soubor je archivní verze v7  !!!
!!!  Aktivní solver je: vrp_solver_lines_v6.py       !!!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

VRP Solver Lines v7 — RiRo block pipeline, přímý solve
=======================================================
Prerekvizity: pip install ortools requests numpy pandas openpyxl

Denní workflow:
  python prepare_inputs_v5.py riro-YYYYMMDD-POB.csv --block-id 211
  python vrp_solver_lines_v7.py --orders-file data/prepared/orders_block_211.csv

Statické soubory:
  data/static/vehicle_types.csv    → jeden řádek = jeden typ auta + available_count
  data/static/locations_lookup.csv → GPS souřadnice lokací

Změny oproti v6:
  - Odstraněn clustering (KMeans / Sweep / TW-midpoint)
  - Odstraněn cross-cluster LNS a SolutionState
  - Odstraněna závislost na scikit-learn
  - Pipeline: A (OSRM) → B (přímý OR-Tools solve) → C (výstup)
  - Celý časový budget jde do jednoho OR-Tools pass
  - Důvod: max 300 zastávek / blok — clustering by byl kompromis bez zisku
"""

import csv
import re
import argparse
import json
import requests
import numpy as np
import pandas as pd
import math
import time
from pathlib import Path
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

# ============================================================
#  SKLAD
# ============================================================
DEPOT = {
    "name":  "Hlavní sklad",
    "lat":   49.5061806,
    "lon":   15.5950131,
    "open":  "00:00",
    "close": "23:59",
}

# ============================================================
#  KONFIGURACE
# ============================================================
CONFIG = {
    "orders_file":                      "data/prepared/orders_block_211.csv",
    "vehicle_types_file":               "data/static/vehicle_types.csv",

    "time_buffer_fixed_min":            0,
    "time_buffer_pct":                  0,

    "service_time_base_min":            4,
    "service_time_per_150kg_step_min":  1,

    "start_cost_km_equiv":              30,
    "max_route_duration_h":             23.5,

    "osrm_url":                         "http://localhost:5000",   # fallback
    "osrm_urls": {
        "driving":     "http://localhost:5000",
        "driving-hgv": "http://localhost:5001",
    },

    # Celý budget jde do jednoho OR-Tools solve
    "total_time_budget_sec":            360,   # 60 minut

    "random_seed":                      42,
}


# ============================================================
#  NAČTENÍ DAT (beze změny od v6)
# ============================================================

def load_vehicle_types_db(path: str, block_id: str = "") -> list:
    """
    Načte vehicle_types.csv. Pokud existuje sloupec count_block_{block_id},
    použije ho; jinak fallback na available_count.
    """
    vehicles = []
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"[CHYBA] {path} nenalezen.")

    with open(p, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"type_code", "type_name", "max_kg", "cost_per_km", "available_count"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"[CHYBA] {path} nemá povinné sloupce: {sorted(required)}")

        block_col = f"count_block_{block_id}" if block_id else ""
        use_block_col = block_col and block_col in (reader.fieldnames or [])
        if use_block_col:
            print(f"  [vehicle] Používám per-block počty ze sloupce '{block_col}'")
        else:
            print(f"  [vehicle] Sloupec '{block_col}' nenalezen — fallback na available_count")

        for row in reader:
            type_code = str(row.get("type_code", "")).strip()
            if not type_code or type_code.startswith("#"):
                continue
            try:
                max_kg      = float(row["max_kg"])
                cost_per_km = float(row["cost_per_km"])
                if use_block_col:
                    count = int(float(row[block_col]))
                else:
                    count = int(float(row["available_count"]))
            except (ValueError, KeyError) as e:
                print(f"  [!] vehicle_types: přeskakuji řádek {row} — {e}")
                continue

            if count <= 0:
                continue

            time_multiplier = float(row.get("time_multiplier") or 1.0)
            osrm_profile    = str(row.get("osrm_profile") or "driving").strip() or "driving"
            start_cost      = cost_per_km * CONFIG["start_cost_km_equiv"]
            type_name       = str(row.get("type_name", type_code)).strip() or type_code

            for i in range(count):
                vehicles.append({
                    "id":              f"{type_code}_{i+1:02d}",
                    "type_code":       type_code,
                    "type":            type_name,
                    "driver":          "",
                    "max_kg":          max_kg,
                    "cost_per_km":     cost_per_km,
                    "start_cost":      start_cost,
                    "time_multiplier": time_multiplier,
                    "osrm_profile":    osrm_profile,
                })

    if not vehicles:
        raise ValueError(f"[CHYBA] {path} neobsahuje žádné dostupné typy vozidel.")
    return vehicles


def load_orders_day(path: str) -> list:
    orders = []
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"[CHYBA] {path} nenalezen.\n"
            "Spusť nejdřív: python prepare_inputs_v5.py riro-YYYYMMDD-POB.csv"
        )

    with open(p, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = ["order_number", "location_code", "time_from", "time_to",
                    "weight_kg", "lat", "lon", "base_service_min"]
        for i, row in enumerate(reader, 1):
            missing = [c for c in required if c not in row or not row[c].strip()]
            if missing:
                print(f"  [!] Řádek {i}: chybí {missing}, přeskakuji")
                continue
            try:
                weight_kg        = float(row["weight_kg"])
                lat              = float(row["lat"])
                lon              = float(row["lon"])
                base_service_min = int(float(row["base_service_min"]))
            except ValueError as e:
                print(f"  [!] Řádek {i}: neplatná čísla — {e}, přeskakuji")
                continue

            orders.append({
                "order_number":    row["order_number"].strip(),
                "location_code":   row["location_code"].strip(),
                "customer_name":   row.get("customer_name", "").strip(),
                "block_id":        row.get("block_id", "").strip(),
                "time_from":       row["time_from"].strip(),
                "time_to":         row["time_to"].strip(),
                "payload_raw":     row.get("payload_raw", "").strip(),
                "weight_kg":       weight_kg,
                "lat":             lat,
                "lon":             lon,
                "city":            row.get("city", "").strip(),
                "note":            row.get("note", "").strip(),
                "base_service_min":base_service_min,
                # aliasy pro _extract_routes
                "id":              row["order_number"].strip(),
                "name":            row.get("customer_name", row["order_number"]).strip(),
            })

    if not orders:
        raise ValueError(f"[CHYBA] {path} neobsahuje žádné objednávky.")
    return orders


# ============================================================
#  POMOCNÉ FUNKCE
# ============================================================

def time_to_minutes(t: str) -> int:
    h, m = map(int, t.strip().split(":"))
    return h * 60 + m


def service_time_min(order: dict) -> int:
    """
    4 min základ + 1 min za každých započatých 150 kg.
    Zdroj pravidla: 1 sec / 2.5 kg, zaokrouhlení na minuty nahoru.
    """
    base      = int(order.get("base_service_min", CONFIG["service_time_base_min"]))
    weight_kg = float(order.get("weight_kg", 0.0) or 0.0)
    extra_min = math.ceil(weight_kg / 150.0) if weight_kg > 0 else 0
    return base + extra_min


# ============================================================
#  OSRM — CELÁ MATICE
# ============================================================

def get_matrix(locations: list, profile: str = "driving") -> tuple:
    base_url = CONFIG["osrm_urls"].get(profile, CONFIG["osrm_url"])
    coords   = ";".join(f"{lon},{lat}" for lat, lon in locations)
    url      = f"{base_url}/table/v1/{profile}/{coords}"
    params   = {"annotations": "duration,distance"}

    n = len(locations)
    print(f"  Počítám matici {n}×{n} přes OSRM (profil: {profile})...")
    t0 = time.time()
    try:
        r = requests.get(url, params=params, timeout=600)
        if r.status_code in (400, 404) and profile != "driving":
            print(f"  [WARN] OSRM profil '{profile}' není dostupný "
                  f"(HTTP {r.status_code}), fallback na 'driving'")
            return get_matrix(locations, profile="driving")
        r.raise_for_status()
    except requests.exceptions.ConnectionError:
        if profile != "driving":
            print(f"  [WARN] OSRM pro profil '{profile}' neodpovídá, "
                  f"fallback na 'driving'")
            return get_matrix(locations, profile="driving")
        raise SystemExit("\n[CHYBA] OSRM neběží. Spusť: docker start osrm-server")

    data = r.json()
    print(f"  Matice OK ({time.time() - t0:.0f} s).")

    durations_sec = np.array(data["durations"], dtype=float)
    distances_m   = np.array(data["distances"],  dtype=float)
    durations_min = durations_sec / 60.0
    distances_km  = distances_m   / 1000.0

    fixed = CONFIG["time_buffer_fixed_min"]
    pct   = CONFIG["time_buffer_pct"]
    durations_buffered = durations_min * (1 + pct) + fixed
    np.fill_diagonal(durations_buffered, 0)
    np.fill_diagonal(distances_km, 0)
    return distances_km, durations_buffered


# ============================================================
#  DATA MODEL
# ============================================================

def build_data_model(orders, vehicles_expanded, distances_km, durations_min_list):
    depot_open  = time_to_minutes(DEPOT["open"])
    depot_close = time_to_minutes(DEPOT["close"])
    COST_SCALE  = 100

    dist_int      = (np.array(distances_km) * 100).astype(int).tolist()
    time_int_list = [np.array(dm).astype(int).tolist() for dm in durations_min_list]

    tw = [(depot_open, depot_close)]
    for o in orders:
        tw.append((time_to_minutes(o["time_from"]), time_to_minutes(o["time_to"])))

    demands       = [0] + [int(o["weight_kg"]) for o in orders]
    service_times = [0] + [service_time_min(o) for o in orders]
    capacities    = [int(v["max_kg"])      for v in vehicles_expanded]
    costs_per_km  = [v["cost_per_km"]      for v in vehicles_expanded]
    start_costs   = [int(v["start_cost"] * COST_SCALE) for v in vehicles_expanded]
    max_dur_min   = int(CONFIG["max_route_duration_h"] * 60)

    return {
        "dist_int":      dist_int,
        "time_int_list": time_int_list,
        "time_windows":  tw,
        "demands":       demands,
        "service_times": service_times,
        "capacities":    capacities,
        "costs_per_km":  costs_per_km,
        "start_costs":   start_costs,
        "num_vehicles":  len(vehicles_expanded),
        "depot":         0,
        "max_dur_min":   max_dur_min,
        "cost_scale":    COST_SCALE,
    }


# ============================================================
#  SOLVER — přímý solve na celý blok
# ============================================================

def solve_direct(orders, vehicles_expanded, distances_km, durations_min_list,
                 time_limit_sec: int) -> tuple:
    """
    Spustí OR-Tools VRPTW přímo na všechny objednávky bloku bez předchozího
    clusterování. Pro ≤300 zastávek je to lepší než clustering — solver vidí
    celý prostor a může volně kombinovat.

    durations_min_list: list[np.ndarray] — jedna časová matice na vozidlo.
    Vrátí (routes, total_cost_kc) nebo ([], 0) při neúspěchu.
    """
    data = build_data_model(orders, vehicles_expanded, distances_km, durations_min_list)
    n    = len(data["demands"])

    manager = pywrapcp.RoutingIndexManager(n, data["num_vehicles"], data["depot"])
    routing = pywrapcp.RoutingModel(manager)

    # Cost callback — každé auto má vlastní sazbu
    for v_idx in range(data["num_vehicles"]):
        cb_idx = routing.RegisterTransitCallback(
            lambda fi, ti, vi=v_idx: (
                data["dist_int"][manager.IndexToNode(fi)][manager.IndexToNode(ti)]
                * int(data["costs_per_km"][vi])
            )
        )
        routing.SetArcCostEvaluatorOfVehicle(cb_idx, v_idx)
        routing.SetFixedCostOfVehicle(data["start_costs"][v_idx], v_idx)

    # Kapacita
    demand_cb_idx = routing.RegisterUnaryTransitCallback(
        lambda fi: data["demands"][manager.IndexToNode(fi)]
    )
    routing.AddDimensionWithVehicleCapacity(
        demand_cb_idx, 0, data["capacities"], True, "Capacity"
    )

    # Per-vehicle čas: každé vozidlo má vlastní matici (jiný OSRM profil + time_multiplier)
    time_cb_indices = []
    for v_idx in range(data["num_vehicles"]):
        cb_idx = routing.RegisterTransitCallback(
            lambda fi, ti, vi=v_idx: (
                data["time_int_list"][vi][manager.IndexToNode(fi)][manager.IndexToNode(ti)]
                + data["service_times"][manager.IndexToNode(fi)]
            )
        )
        time_cb_indices.append(cb_idx)
    routing.AddDimensionWithVehicleTransitAndCapacity(
        time_cb_indices, 60,
        [data["max_dur_min"]] * data["num_vehicles"],
        False, "Time"
    )
    time_dim = routing.GetDimensionOrDie("Time")

    # Time windows
    for node_idx in range(n):
        idx = manager.NodeToIndex(node_idx)
        tw  = data["time_windows"][node_idx]
        time_dim.CumulVar(idx).SetRange(tw[0], tw[1])

    # Nastavení solveru
    # PARALLEL_CHEAPEST_INSERTION dává lepší startovní bod pro VRPTW než PATH_CHEAPEST_ARC
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy    = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.seconds = time_limit_sec
    params.log_search         = False

    print(f"  Spouštím OR-Tools GLS (limit: {time_limit_sec} sec = "
          f"{time_limit_sec // 60} min {time_limit_sec % 60} sec)...")
    solution = routing.SolveWithParameters(params)

    if not solution:
        print("  [!] OR-Tools nenašel řešení. Zkontroluj TW a kapacity.")
        return [], 0

    return _extract_routes(manager, routing, solution, time_dim,
                           vehicles_expanded, orders, np.array(distances_km))


def _extract_routes(manager, routing, solution, time_dim,
                    vehicles_expanded, orders, distances_km):
    routes        = []
    total_cost_kc = 0

    for v_idx in range(len(vehicles_expanded)):
        v     = vehicles_expanded[v_idx]
        index = routing.Start(v_idx)
        if routing.IsEnd(solution.Value(routing.NextVar(index))):
            continue

        stops, route_km, prev_node = [], 0.0, None
        t_start_min = None
        while not routing.IsEnd(index):
            node  = manager.IndexToNode(index)
            t_var = solution.Min(time_dim.CumulVar(index))
            t_str = f"{t_var // 60:02d}:{t_var % 60:02d}"
            if t_start_min is None:
                t_start_min = t_var
            leg_km = 0.0 if prev_node is None else round(float(distances_km[prev_node][node]), 1)
            route_km += leg_km
            if node == 0:
                stops.append({"stop": DEPOT["name"], "arrival": t_str, "kg": 0,
                               "leg_km": 0.0, "lat": DEPOT["lat"], "lon": DEPOT["lon"]})
            else:
                o = orders[node - 1]
                stops.append({
                    "stop":    o["name"],
                    "id":      o["id"],
                    "arrival": t_str,
                    "kg":      o["weight_kg"],
                    "window":  f"{o['time_from']}–{o['time_to']}",
                    "city":    o.get("city", ""),
                    "note":    o.get("note", ""),
                    "leg_km":  leg_km,
                    "lat":     o["lat"],
                    "lon":     o["lon"],
                })
            prev_node = node
            index = solution.Value(routing.NextVar(index))

        node  = manager.IndexToNode(index)
        t_var = solution.Min(time_dim.CumulVar(index))
        t_end_min = t_var
        leg_km_return = round(float(distances_km[prev_node][0]), 1) if prev_node is not None else 0.0
        route_km += leg_km_return
        stops.append({"stop": DEPOT["name"] + " (návrat)",
                       "arrival": f"{t_var // 60:02d}:{t_var % 60:02d}", "kg": 0,
                       "leg_km": leg_km_return, "lat": DEPOT["lat"], "lon": DEPOT["lon"]})

        route_cost     = v["start_cost"] + route_km * v["cost_per_km"]
        total_cost_kc += route_cost
        total_kg       = sum(s["kg"] for s in stops)
        duration_h     = round((t_end_min - (t_start_min or 0)) / 60, 2)

        routes.append({
            "vehicle_id":   v["id"],
            "vehicle_type": v["type"],
            "type_code":    v.get("type_code", ""),
            "driver":       v.get("driver", ""),
            "cost_per_km":  v["cost_per_km"],
            "start_cost":   v["start_cost"],
            "stops":        stops,
            "total_km":     round(route_km, 1),
            "total_kc":     round(route_cost, 0),
            "total_kg":     total_kg,
            "duration_h":   duration_h,
        })

    return routes, round(total_cost_kc, 0)


# ============================================================
#  VÝSTUP (beze změny od v6)
# ============================================================

def print_results(routes, total_cost_kc):
    print("\n" + "=" * 65)
    print("VÝSLEDEK PLÁNOVÁNÍ TRAS")
    print("=" * 65)
    for r in routes:
        print(f"\n{r['vehicle_id']} ({r['vehicle_type']}, {r['cost_per_km']} Kč/km)")
        print(f"  Celkem: {r['total_km']} km | {r['total_kg']:.0f} kg "
              f"| {r['total_kc']:,.0f} Kč | {r.get('duration_h', 0):.1f} h")
        for i, s in enumerate(r["stops"]):
            prefix = "  ├" if i < len(r["stops"]) - 1 else "  └"
            win    = f"  [{s['window']}]" if "window" in s else ""
            kg_str = f"  {s['kg']:.0f} kg" if s["kg"] > 0 else ""
            city   = f"  {s['city']}" if s.get("city") else ""
            print(f"{prefix} {s['arrival']}  {s['stop']}{city}{kg_str}{win}")

    total_km    = sum(r["total_km"] for r in routes)
    total_hours = sum(r.get("duration_h", 0) for r in routes)
    print("\n" + "─" * 65)
    print(f"CELKOVÝ NÁKLAD DNE:  {total_cost_kc:,.0f} Kč")
    print(f"Navrženo lines:      {len(routes)}")
    print(f"Celkem km:           {total_km:,.1f} km")
    print(f"Celkem hodin:        {total_hours:.1f} h  (součet délek všech tras)")
    print("=" * 65)


def save_excel(routes, total_cost_kc, filepath="lines_plan.xlsx"):
    rows = []
    for line_no, r in enumerate(routes, start=1):
        for i, s in enumerate(r["stops"]):
            rows.append({
                "Line":        f"LINE_{line_no:02d}",
                "Vehicle ID":  r["vehicle_id"],
                "Vehicle Type":r["vehicle_type"],
                "Type Code":   r.get("type_code", ""),
                "Kč/km":       r["cost_per_km"],
                "Stop Seq":    i,
                "Place":       s["stop"],
                "Order ID":    s.get("id", "—"),
                "Arrival":     s["arrival"],
                "Leg km":      s.get("leg_km", ""),
                "Kg":          s["kg"],
                "Window":      s.get("window", "—"),
                "Note":        s.get("note", ""),
            })
        rows.append({
            "Line":        f"LINE_{line_no:02d}",
            "Vehicle Type":"SUMMARY",
            "Type Code":   r.get("type_code", ""),
            "Kč/km":       r["cost_per_km"],
            "Place":       f"Total: {r['total_km']} km | {r['total_kg']:.0f} kg | {r.get('duration_h',0):.1f} h",
            "Arrival":     f"{r['total_kc']:,.0f} Kč",
        })
        rows.append({})
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Lines")
    print(f"\nUloženo: {filepath}")


def save_outputs(routes, total_cost_kc, output_dir: Path, zone_label: str, elapsed_min: float):
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_rows = []
    stop_rows    = []
    type_counter = {}
    for line_no, r in enumerate(routes, start=1):
        line_id   = f"LINE_{line_no:02d}"
        type_name = r["vehicle_type"]
        type_counter[type_name] = type_counter.get(type_name, 0) + 1
        summary_rows.append({
            "zone":           zone_label,
            "line_id":        line_id,
            "vehicle_id":     r["vehicle_id"],
            "vehicle_type":   type_name,
            "cost_per_km":    r["cost_per_km"],
            "total_km":       r["total_km"],
            "duration_h":     r.get("duration_h", 0),
            "total_kg":       r["total_kg"],
            "total_cost_kc":  r["total_kc"],
        })
        for i, s in enumerate(r["stops"]):
            stop_rows.append({
                "zone":         zone_label,
                "line_id":      line_id,
                "vehicle_type": type_name,
                "stop_seq":     i,
                "place":        s["stop"],
                "order_id":     s.get("id", ""),
                "arrival":      s["arrival"],
                "leg_km":       s.get("leg_km", ""),
                "kg":           s["kg"],
                "window":       s.get("window", ""),
                "note":         s.get("note", ""),
                "lat":          s.get("lat", ""),
                "lon":          s.get("lon", ""),
            })

    pd.DataFrame(summary_rows).to_csv(output_dir / "lines_summary.csv", index=False)
    pd.DataFrame(stop_rows).to_csv(output_dir / "lines_stops.csv", index=False)
    save_excel(routes, total_cost_kc, filepath=output_dir / "lines_plan.xlsx")

    total_km_all    = round(sum(r["total_km"] for r in routes), 1)
    total_hours_all = round(sum(r.get("duration_h", 0) for r in routes), 1)
    zone_summary = {
        "zone":             zone_label,
        "lines_count":      len(routes),
        "vehicle_type_mix": type_counter,
        "total_cost_kc":    total_cost_kc,
        "total_km":         total_km_all,
        "total_hours":      total_hours_all,
        "elapsed_min":      round(elapsed_min, 2),
    }
    with open(output_dir / "zone_summary.json", "w", encoding="utf-8") as f:
        json.dump(zone_summary, f, ensure_ascii=False, indent=2)


# ============================================================
#  ARGS
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders-file",        default=CONFIG["orders_file"])
    parser.add_argument("--vehicle-types-file", default=CONFIG["vehicle_types_file"])
    parser.add_argument("--output-dir",         default="output")
    parser.add_argument("--zone-label",         default="")
    parser.add_argument("--time-limit",         type=int, default=None,
                        help="Přepíše CONFIG total_time_budget_sec (v sekundách)")
    return parser.parse_args()


# ============================================================
#  MAIN — 3 fáze místo 5
# ============================================================

def main():
    t_start = time.time()
    args    = parse_args()

    time_budget = args.time_limit if args.time_limit else CONFIG["total_time_budget_sec"]

    print("=" * 65)
    print("VRP Solver Lines v7 — přímý solve (bez clusterování)")
    print(f"Budget: {time_budget // 60} min {time_budget % 60} sec")
    print("=" * 65)

    # ── Načti data ─────────────────────────────────────────────
    print("\nNačítám data...")
    orders            = load_orders_day(args.orders_file)
    block_id          = orders[0].get("block_id", "").strip() if orders else ""
    vehicles_expanded = load_vehicle_types_db(args.vehicle_types_file, block_id=block_id)

    # Auto-detekce výstupní složky z názvu orders souboru
    orders_path = Path(args.orders_file)
    m_path = re.match(r'orders_([A-Z]+)_(\d{4}-\d{2}-\d{2})\.csv', orders_path.name)
    if m_path and args.output_dir == "output":
        depot_code_out, date_out = m_path.group(1), m_path.group(2)
        output_dir = Path(f"data/results/{depot_code_out}/{date_out}")
    else:
        output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    total_kg   = sum(o["weight_kg"] for o in orders)
    zone_label = args.zone_label.strip() or (orders[0].get("block_id", "") if orders else "")
    print(f"  Objednávky:  {len(orders):,}  ({total_kg:,.0f} kg)")
    print(f"  Vozidla:     {len(vehicles_expanded)} dostupných")
    print(f"  Zóna/block:  {zone_label}")

    # ── Phase A: OSRM ──────────────────────────────────────────
    print("\n" + "─" * 65)
    print("[A] OSRM matice")
    print("─" * 65)
    locations = ([(DEPOT["lat"], DEPOT["lon"])]
                 + [(o["lat"], o["lon"]) for o in orders])

    distinct_profiles = sorted(set(v["osrm_profile"] for v in vehicles_expanded))
    matrices_by_profile: dict = {}
    for prof in distinct_profiles:
        matrices_by_profile[prof] = get_matrix(locations, profile=prof)

    distances_km = matrices_by_profile.get(
        "driving", next(iter(matrices_by_profile.values()))
    )[0]

    # Aplikuj uzavírky na všechny profily
    from closures_utils import apply_closures_to_matrix
    for prof in list(matrices_by_profile.keys()):
        dist_p, dur_p = matrices_by_profile[prof]
        dur_p, dist_p = apply_closures_to_matrix(dur_p, dist_p, locations,
                                                  osrm_url=CONFIG["osrm_url"])
        matrices_by_profile[prof] = (dist_p, dur_p)
    distances_km = matrices_by_profile.get(
        "driving", next(iter(matrices_by_profile.values()))
    )[0]

    vehicle_time_matrices = []
    for v in vehicles_expanded:
        _, dur_buffered = matrices_by_profile[v["osrm_profile"]]
        t_mat = dur_buffered * v["time_multiplier"]
        np.fill_diagonal(t_mat, 0)
        vehicle_time_matrices.append(t_mat)

    t_after_osrm  = time.time()
    osrm_elapsed  = t_after_osrm - t_start
    solver_budget = max(30, int(time_budget - osrm_elapsed))
    print(f"OSRM: {osrm_elapsed:.0f} sec | solver dostane: "
          f"{solver_budget // 60} min {solver_budget % 60} sec")

    # ── Phase B: Přímý OR-Tools solve ──────────────────────────
    print("\n" + "─" * 65)
    print("[B] Přímý OR-Tools solve")
    print("─" * 65)
    t_b    = time.time()
    routes, total_cost = solve_direct(
        orders, vehicles_expanded, distances_km, vehicle_time_matrices,
        time_limit_sec=solver_budget,
    )
    print(f"Solve: {time.time() - t_b:.0f} sec | {total_cost:,.0f} Kč "
          f"| {len(routes)} lines")

    if not routes:
        print("\n[!] Solver nenašel žádné řešení. Možné příčiny:")
        print("    - Časová okna jsou vzájemně nekompatibilní")
        print("    - Celková kapacita vozidel nestačí pro celkový kg")
        print("    - OSRM vrátil nulové nebo extrémní hodnoty")
        return

    # ── Phase C: Výstup ────────────────────────────────────────
    elapsed_min = (time.time() - t_start) / 60
    print(f"\nCelková doba: {elapsed_min:.1f} min")
    print_results(routes, total_cost)
    save_outputs(routes, total_cost, output_dir, zone_label, elapsed_min)


if __name__ == "__main__":
    main()
