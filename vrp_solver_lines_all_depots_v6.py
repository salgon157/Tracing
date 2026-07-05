"""
Combined VRP Solver Lines v6 - one shared warehouse, all business areas.

This script is intentionally a separate entrypoint. It reuses the existing
single-block solver implementation, but loads CB/MO/HK/PR orders together and
uses the shared vehicle pool from available_count.

Typical usage:
  python vrp_solver_lines_all_depots_v6.py --date 2026-04-29 --budget-min 5
  python vrp_solver_lines_all_depots_v6.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import multiprocessing
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import vrp_solver_lines_v6 as solver
from osm_routing import add_osm_args, apply_osm_source


DEFAULT_DEPOTS = ("CB", "MO", "HK", "PR")
ZONE_LABEL = "ALL"
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path("data")
PREPARED_DIR = DATA_DIR / "prepared"
RESULTS_DIR = DATA_DIR / "results"

COMBINED_CONFIG_OVERRIDES = {
    "budget_phase_C_pct": 0.35,
    "budget_phase_D_pct": 0.25,
    "budget_phase_E_pct": 0.40,
    "lns_neighbor_clusters": 4,
}


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def parse_depots(value: str) -> list[str]:
    depots = [part.strip().upper() for part in value.split(",") if part.strip()]
    if not depots:
        raise argparse.ArgumentTypeError("At least one depot code is required.")
    return depots


def parse_budget_ratios(value: str) -> tuple[float, float, float]:
    try:
        parts = [float(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Budget ratios must be three numbers: C,D,E"
        ) from exc

    if len(parts) != 3:
        raise argparse.ArgumentTypeError("Budget ratios must have exactly three values: C,D,E")
    if any(part < 0 for part in parts):
        raise argparse.ArgumentTypeError("Budget ratios must be non-negative.")
    total = sum(parts)
    if total <= 0:
        raise argparse.ArgumentTypeError("At least one budget ratio must be positive.")
    if abs(total - 1.0) > 0.001:
        raise argparse.ArgumentTypeError("Budget ratios must sum to 1.0.")
    return parts[0], parts[1], parts[2]


def _prepared_dates_for_depot(depot: str) -> set[str]:
    depot_dir = PREPARED_DIR / depot
    if not depot_dir.exists():
        return set()

    dates: set[str] = set()
    pattern = re.compile(rf"orders_{re.escape(depot)}_(\d{{4}}-\d{{2}}-\d{{2}})\.csv$")
    for path in depot_dir.glob(f"orders_{depot}_*.csv"):
        match = pattern.match(path.name)
        if match:
            dates.add(match.group(1))
    return dates


def latest_common_date(depots: list[str]) -> str:
    common_dates: set[str] | None = None
    missing: list[str] = []

    for depot in depots:
        dates = _prepared_dates_for_depot(depot)
        if not dates:
            missing.append(depot)
        common_dates = dates if common_dates is None else common_dates & dates

    if missing:
        raise SystemExit(
            "[ERROR] No prepared orders found for depots: "
            + ", ".join(missing)
            + ". Run prepare_inputs_v6.py for each depot first."
        )
    if not common_dates:
        details = ", ".join(
            f"{depot}: {sorted(_prepared_dates_for_depot(depot))}" for depot in depots
        )
        raise SystemExit(
            "[ERROR] No common prepared date exists for all selected depots.\n"
            f"Available dates: {details}"
        )
    return max(common_dates)


def resolve_order_files(depots: list[str], date_str: str) -> dict[str, Path]:
    files: dict[str, Path] = {}
    missing: list[Path] = []
    for depot in depots:
        path = PREPARED_DIR / depot / f"orders_{depot}_{date_str}.csv"
        if not path.exists():
            missing.append(path)
        else:
            files[depot] = path
    if missing:
        raise SystemExit(
            "[ERROR] Missing prepared order files:\n"
            + "\n".join(f"  {path}" for path in missing)
        )
    return files


def load_combined_orders(order_files: dict[str, Path]) -> list[dict]:
    all_orders: list[dict] = []
    grouped_by_order_number: defaultdict[str, list[dict]] = defaultdict(list)

    for depot, path in order_files.items():
        orders = solver.load_orders_day(str(path))
        for order in orders:
            source_depot = (order.get("block_id") or depot).strip().upper()
            order["source_depot"] = source_depot
            order["block_id"] = source_depot
            order["source_orders_file"] = str(path)
            order["original_order_number"] = order.get("order_number", "")
            grouped_by_order_number[order["order_number"]].append(order)
            all_orders.append(order)

    duplicate_order_numbers = {
        order_no for order_no, orders in grouped_by_order_number.items()
        if len(orders) > 1
    }
    for order_no in duplicate_order_numbers:
        for order in grouped_by_order_number[order_no]:
            order["id"] = f"{order['source_depot']}:{order_no}"

    if duplicate_order_numbers:
        print(
            "  [WARN] Duplicate order_number values found across depots. "
            "Internal route IDs were prefixed with source depot."
        )

    return all_orders


def auto_n_clusters_combined(n_orders: int, n_vehicles: int) -> int:
    return max(8, math.ceil(n_orders / 75), math.ceil(n_vehicles / 10))


def budget_label(total_budget_sec: int) -> str:
    minutes = total_budget_sec / 60
    if abs(minutes - round(minutes)) < 1e-9:
        return f"budget{int(round(minutes))}min"
    safe = str(round(minutes, 2)).replace(".", "p")
    return f"budget{safe}min"


def build_output_dir(args: argparse.Namespace, date_str: str) -> Path:
    if args.output_dir:
        return Path(args.output_dir)
    return RESULTS_DIR / ZONE_LABEL / f"{date_str}_{budget_label(solver.CONFIG['total_time_budget_sec'])}"


def build_seed_labels(
    orders: list[dict],
    n_clusters: int,
    random_seed: int,
    seed_restarts: int,
) -> dict[str, list[int]]:
    seed_restarts = max(1, int(seed_restarts))
    labels: dict[str, list[int]] = {}

    for offset in range(seed_restarts):
        seed = random_seed + offset
        labels[f"kmeans_{offset + 1:02d}"] = solver.partition_kmeans(
            orders, n_clusters, seed
        )

    labels["sweep"] = solver.partition_sweep(orders, n_clusters)

    for offset in range(seed_restarts):
        seed = random_seed + 100 + offset
        labels[f"tw_midpoint_{offset + 1:02d}"] = solver.partition_tw_midpoint(
            orders, n_clusters, seed
        )

    return labels


def _phase_c_time_per_task(total_tasks: int, time_budget_sec: int, n_workers: int) -> int:
    if total_tasks <= 0:
        return 1
    waves = max(1, math.ceil(total_tasks / max(1, n_workers)))
    return max(1, int(time_budget_sec / waves))


def phase_c_best_multi_seed(
    orders: list[dict],
    vehicles_expanded: list[dict],
    distances_km: np.ndarray,
    vehicle_time_by_id: dict,
    n_clusters: int,
    time_budget_sec: int,
    n_workers: int,
    seed_restarts: int,
) -> solver.SolutionState:
    seed_labels = build_seed_labels(
        orders,
        n_clusters,
        int(solver.CONFIG["random_seed"]),
        seed_restarts,
    )

    all_worker_args: list[dict] = []
    seed_cluster_data: dict[str, dict] = {}

    for seed_name, labels in seed_labels.items():
        clusters, cluster_indices = solver.labels_to_clusters(orders, labels)
        vehicle_assignments = solver.assign_vehicles_to_clusters(
            clusters, vehicles_expanded
        )
        seed_cluster_data[seed_name] = {
            "clusters": clusters,
            "cluster_indices": cluster_indices,
            "vehicle_assignments": vehicle_assignments,
        }
        for c_idx, (c_orders, c_indices, c_vehicles) in enumerate(
            zip(clusters, cluster_indices, vehicle_assignments)
        ):
            cluster_v_times = [vehicle_time_by_id[v["id"]] for v in c_vehicles]
            sub_dist, sub_times = solver.extract_submatrix(
                distances_km, cluster_v_times, c_indices
            )
            all_worker_args.append({
                "seed_name": seed_name,
                "cluster_idx": c_idx,
                "cluster_orders": c_orders,
                "cluster_vehicles": c_vehicles,
                "sub_dist": sub_dist.tolist(),
                "sub_times": [st.tolist() for st in sub_times],
                "time_limit_sec": 1,  # filled below after task count is known
            })

    time_per_task = _phase_c_time_per_task(
        len(all_worker_args), max(1, int(time_budget_sec)), n_workers
    )
    for args in all_worker_args:
        args["time_limit_sec"] = time_per_task

    waves = math.ceil(len(all_worker_args) / max(1, n_workers))
    print(
        f"  {len(seed_labels)} seeds x {n_clusters} clusters = "
        f"{len(all_worker_args)} cluster-solve tasks"
    )
    print(
        f"  {n_workers} workers, {waves} waves, "
        f"{time_per_task} sec/cluster task"
    )

    results_by_seed = {seed_name: {} for seed_name in seed_labels}
    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(solver._worker_solve_cluster, args): args
            for args in all_worker_args
        }
        for future in as_completed(futures):
            args = futures[future]
            try:
                res = future.result()
                results_by_seed[res["seed_name"]][res["cluster_idx"]] = res
            except Exception as exc:
                print(
                    f"  [!] Seed={args['seed_name']} "
                    f"cluster={args['cluster_idx']} failed: {exc}"
                )

    best_seed_name = None
    best_penalized = float("inf")
    seed_penalty = solver.CONFIG["seed_unsolved_cluster_penalty_kc"]

    for seed_name, cluster_results in results_by_seed.items():
        expected = len(seed_cluster_data[seed_name]["clusters"])
        solved = sum(1 for res in cluster_results.values() if res.get("routes"))
        unsolved = expected - solved
        raw_total = sum(res.get("cost", 0) for res in cluster_results.values())
        penalized = raw_total + unsolved * seed_penalty
        print(
            f"  Seed '{seed_name}': {raw_total:,.0f} Kc raw | "
            f"{penalized:,.0f} Kc pen. | {solved}/{expected} clusters"
        )

        if solved == 0:
            continue
        if penalized < best_penalized:
            best_penalized = penalized
            best_seed_name = seed_name

    if best_seed_name is None:
        raise RuntimeError("No seed found a feasible cluster solution.")

    print(f"\n  Best seed: '{best_seed_name}' (pen. {best_penalized:,.0f} Kc)")

    labels = seed_labels[best_seed_name]
    best_data = seed_cluster_data[best_seed_name]
    clusters = best_data["clusters"]
    cluster_indices = best_data["cluster_indices"]
    vehicle_assignments = best_data["vehicle_assignments"]
    cluster_results = results_by_seed[best_seed_name]

    cluster_labels_arr = [0] * len(orders)
    cluster_routes_list: list[list] = []
    cluster_costs: list[float] = []
    for c_idx, order_indices in enumerate(cluster_indices):
        for order_idx in order_indices:
            cluster_labels_arr[order_idx] = c_idx
        res = cluster_results.get(c_idx, {})
        cluster_routes_list.append(res.get("routes", []))
        cluster_costs.append(
            res.get("cost", seed_penalty if not res.get("routes") else 0.0)
        )

    return solver.SolutionState(
        orders=orders,
        cluster_labels=cluster_labels_arr,
        clusters=clusters,
        cluster_indices=cluster_indices,
        vehicle_assignments=vehicle_assignments,
        cluster_routes_list=cluster_routes_list,
        cluster_costs=cluster_costs,
    )


def rebalance_vehicle_assignments(
    state: solver.SolutionState,
    vehicles_expanded: list[dict],
    distances_km: np.ndarray,
    vehicle_time_by_id: dict,
    time_budget_sec: int,
    n_workers: int,
) -> solver.SolutionState:
    if time_budget_sec <= 0 or not state.clusters:
        return state

    new_assignments = solver.assign_vehicles_to_clusters(
        state.clusters, vehicles_expanded
    )
    if new_assignments == state.vehicle_assignments:
        print("  Rebalance: assignment unchanged, skipping.")
        return state

    worker_args: list[dict] = []
    for c_idx, (c_orders, c_indices, c_vehicles) in enumerate(
        zip(state.clusters, state.cluster_indices, new_assignments)
    ):
        if not c_orders:
            continue
        cluster_v_times = [vehicle_time_by_id[v["id"]] for v in c_vehicles]
        sub_dist, sub_times = solver.extract_submatrix(
            distances_km, cluster_v_times, c_indices
        )
        worker_args.append({
            "seed_name": "rebalance",
            "cluster_idx": c_idx,
            "cluster_orders": c_orders,
            "cluster_vehicles": c_vehicles,
            "sub_dist": sub_dist.tolist(),
            "sub_times": [st.tolist() for st in sub_times],
            "time_limit_sec": 1,
        })

    time_per_task = _phase_c_time_per_task(
        len(worker_args), max(1, int(time_budget_sec)), n_workers
    )
    for args in worker_args:
        args["time_limit_sec"] = time_per_task

    print(
        f"  Rebalance: resolving {len(worker_args)} clusters, "
        f"{time_per_task} sec/cluster task"
    )

    new_routes = [None] * len(state.clusters)
    new_costs = [0.0] * len(state.clusters)
    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(solver._worker_solve_cluster, args): args
            for args in worker_args
        }
        for future in as_completed(futures):
            args = futures[future]
            try:
                res = future.result()
            except Exception as exc:
                print(f"  Rebalance cluster {args['cluster_idx']}: failed: {exc}")
                return state
            c_idx = res["cluster_idx"]
            if not res.get("routes"):
                print(f"  Rebalance cluster {c_idx}: infeasible, rejecting.")
                return state
            new_routes[c_idx] = res["routes"]
            new_costs[c_idx] = res["cost"]

    if any(routes is None for routes in new_routes):
        print("  Rebalance: incomplete result set, rejecting.")
        return state

    new_total = sum(new_costs)
    if new_total < state.total_cost:
        print(
            f"  Rebalance accepted: -{state.total_cost - new_total:,.0f} Kc "
            f"-> {new_total:,.0f} Kc"
        )
        return solver.SolutionState(
            orders=state.orders,
            cluster_labels=state.cluster_labels,
            clusters=state.clusters,
            cluster_indices=state.cluster_indices,
            vehicle_assignments=new_assignments,
            cluster_routes_list=new_routes,
            cluster_costs=new_costs,
        )

    print(
        f"  Rebalance rejected: {state.total_cost:,.0f} Kc "
        f"-> {new_total:,.0f} Kc"
    )
    return state


def enrich_routes_with_source_depot(routes: list[dict], orders: list[dict]) -> None:
    source_by_id = {order["id"]: order.get("source_depot", "") for order in orders}
    for route in routes:
        for stop in route.get("stops", []):
            order_id = stop.get("id", "")
            stop["source_depot"] = source_by_id.get(order_id, "")


def verify_all_orders_served(routes: list[dict], orders: list[dict]) -> None:
    expected = Counter(order["id"] for order in orders)
    served = Counter()
    for route in routes:
        for stop in route.get("stops", []):
            order_id = stop.get("id")
            if order_id:
                served[order_id] += 1

    missing = expected - served
    extra = served - expected
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {sum(missing.values())} orders")
        if extra:
            parts.append(f"extra/duplicate {sum(extra.values())} stops")
        raise RuntimeError(
            "Final solution does not match input orders: "
            + ", ".join(parts)
            + "."
        )


def source_depot_breakdown(orders: list[dict]) -> dict:
    breakdown: dict[str, dict] = {}
    for depot in sorted({order.get("source_depot", "") for order in orders}):
        depot_orders = [order for order in orders if order.get("source_depot") == depot]
        breakdown[depot] = {
            "orders_count": len(depot_orders),
            "orders_total_kg": round(sum(order["weight_kg"] for order in depot_orders), 1),
        }
    return breakdown


def save_excel_combined(routes: list[dict], total_cost_kc: float, filepath: Path) -> None:
    rows = []
    for line_no, route in enumerate(routes, start=1):
        for stop_seq, stop in enumerate(route["stops"]):
            rows.append({
                "Line": f"LINE_{line_no:02d}",
                "Vehicle ID": route["vehicle_id"],
                "Vehicle Type": route["vehicle_type"],
                "Type Code": route.get("type_code", ""),
                "Kc/km": route["cost_per_km"],
                "Stop Seq": stop_seq,
                "Source depot": stop.get("source_depot", ""),
                "Place": stop["stop"],
                "Order ID": stop.get("id", "-"),
                "Location code": stop.get("location_code", ""),
                "Arrival": stop["arrival"],
                "Leg km": stop.get("leg_km", ""),
                "Base service min": stop.get("base_service_min", ""),
                "Service min": stop.get("service_min", ""),
                "Departure": stop.get("departure", ""),
                "Kg": stop["kg"],
                "Window": stop.get("window", "-"),
                "Note": stop.get("note", ""),
            })
        rows.append({
            "Line": f"LINE_{line_no:02d}",
            "Vehicle Type": "SUMMARY",
            "Type Code": route.get("type_code", ""),
            "Kc/km": route["cost_per_km"],
            "Place": (
                f"Total: {route['total_km']} km | "
                f"{route['total_kg']:.0f} kg | "
                f"{route.get('duration_h', 0):.1f} h"
            ),
            "Arrival": f"{route['total_kc']:,.0f} Kc",
        })
        rows.append({})

    with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name="Lines")
    print(f"\nSaved: {filepath}")


def save_outputs_combined(
    routes: list[dict],
    total_cost_kc: float,
    output_dir: Path,
    elapsed_min: float,
    orders: list[dict],
    delivery_date: str,
    order_files: dict[str, Path],
    closures: list,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_rows = []
    stop_rows = []
    type_counter: dict[str, int] = {}

    for line_no, route in enumerate(routes, start=1):
        line_id = f"LINE_{line_no:02d}"
        type_name = route["vehicle_type"]
        type_counter[type_name] = type_counter.get(type_name, 0) + 1
        summary_rows.append({
            "zone": ZONE_LABEL,
            "line_id": line_id,
            "vehicle_id": route["vehicle_id"],
            "vehicle_type": type_name,
            "cost_per_km": route["cost_per_km"],
            "total_km": route["total_km"],
            "duration_h": route.get("duration_h", 0),
            "total_kg": route["total_kg"],
            "total_cost_kc": route["total_kc"],
        })

        for stop_seq, stop in enumerate(route["stops"]):
            stop_rows.append({
                "zone": ZONE_LABEL,
                "line_id": line_id,
                "vehicle_type": type_name,
                "stop_seq": stop_seq,
                "source_depot": stop.get("source_depot", ""),
                "place": stop["stop"],
                "order_id": stop.get("id", ""),
                "location_code": stop.get("location_code", ""),
                "arrival": stop["arrival"],
                "leg_km": stop.get("leg_km", ""),
                "base_service_min": stop.get("base_service_min", ""),
                "service_min": stop.get("service_min", ""),
                "departure": stop.get("departure", ""),
                "kg": stop["kg"],
                "window": stop.get("window", ""),
                "note": stop.get("note", ""),
                "lat": stop.get("lat", ""),
                "lon": stop.get("lon", ""),
            })

    summary_rows.append({
        "zone": "CELKEM",
        "line_id": f"{len(routes)} linek",
        "vehicle_id": "",
        "vehicle_type": "",
        "cost_per_km": "",
        "total_km": round(sum(route["total_km"] for route in routes), 1),
        "duration_h": round(sum(route.get("duration_h", 0) for route in routes), 2),
        "total_kg": round(sum(route["total_kg"] for route in routes), 1),
        "total_cost_kc": round(sum(route["total_kc"] for route in routes), 0),
    })

    pd.DataFrame(summary_rows).to_csv(output_dir / "lines_summary.csv", index=False)
    pd.DataFrame(stop_rows).to_csv(output_dir / "lines_stops.csv", index=False)
    save_excel_combined(routes, total_cost_kc, output_dir / "lines_plan.xlsx")

    total_km_all = round(sum(route["total_km"] for route in routes), 1)
    total_hours_all = round(sum(route.get("duration_h", 0) for route in routes), 1)
    zone_summary = {
        "zone": ZONE_LABEL,
        "delivery_date": delivery_date,
        "orders_files": {depot: str(path) for depot, path in order_files.items()},
        "source_depot_breakdown": source_depot_breakdown(orders),
        "lines_count": len(routes),
        "vehicle_type_mix": type_counter,
        "total_cost_kc": total_cost_kc,
        "total_km": total_km_all,
        "total_hours": total_hours_all,
        "elapsed_min": round(elapsed_min, 2),
        "config": {
            "total_time_budget_sec": solver.CONFIG["total_time_budget_sec"],
            "num_clusters": solver.CONFIG["num_clusters"],
            "budget_phase_C_pct": solver.CONFIG["budget_phase_C_pct"],
            "budget_phase_D_pct": solver.CONFIG["budget_phase_D_pct"],
            "budget_phase_E_pct": solver.CONFIG["budget_phase_E_pct"],
            "random_seed": solver.CONFIG["random_seed"],
        },
        "closures": [closure.get("id") for closure in closures],
    }
    with open(output_dir / "zone_summary.json", "w", encoding="utf-8") as handle:
        json.dump(zone_summary, handle, ensure_ascii=False, indent=2)

    print(f"Saved: {output_dir / 'lines_summary.csv'}")
    print(f"Saved: {output_dir / 'lines_stops.csv'}")
    print(f"Saved: {output_dir / 'zone_summary.json'}")


def print_combined_settings(
    args: argparse.Namespace,
    order_files: dict[str, Path],
    orders: list[dict],
    vehicles_expanded: list[dict],
    n_clusters: int,
    n_workers: int,
    output_dir: Path,
) -> None:
    depot_counts = source_depot_breakdown(orders)
    vehicle_profiles = Counter(vehicle.get("osrm_profile", "driving") for vehicle in vehicles_expanded)
    vehicle_types = Counter(vehicle.get("type_code", "UNKNOWN") for vehicle in vehicles_expanded)

    print("\n" + "=" * 65)
    print("COMBINED RUN SETTINGS")
    print("=" * 65)
    print(f"date:                         {args.date}")
    print(f"depots:                       {', '.join(order_files)}")
    print(f"orders_files:                 {';'.join(str(p) for p in order_files.values())}")
    print(f"vehicle_types_file:           {args.vehicle_types_file}")
    print(f"output_dir:                   {output_dir}")
    print(f"zone_label:                   {ZONE_LABEL}")
    print(f"orders_count:                 {len(orders)}")
    print(f"orders_total_kg:              {sum(o['weight_kg'] for o in orders):,.0f}")
    print(f"orders_by_source_depot:       {depot_counts}")
    print(f"vehicles_count:               {len(vehicles_expanded)}")
    print(f"vehicles_by_profile:          {dict(vehicle_profiles)}")
    print(f"vehicles_by_type_code:        {dict(vehicle_types)}")
    print(f"resolved_clusters:            {n_clusters}")
    print(f"resolved_workers:             {n_workers}")
    print(f"seed_restarts:                {args.seed_restarts}")
    print(f"total_time_budget_sec:        {solver.CONFIG['total_time_budget_sec']}")
    print(f"budget_phase_C_pct:           {solver.CONFIG['budget_phase_C_pct']}")
    print(f"budget_phase_D_pct:           {solver.CONFIG['budget_phase_D_pct']}")
    print(f"budget_phase_E_pct:           {solver.CONFIG['budget_phase_E_pct']}")
    print(f"random_seed:                  {solver.CONFIG['random_seed']}")
    print("=" * 65)


def prepare_run(args: argparse.Namespace) -> tuple[str, dict[str, Path], list[dict], list[dict], int, int, Path]:
    for key, value in COMBINED_CONFIG_OVERRIDES.items():
        solver.CONFIG[key] = value

    if args.budget_ratios:
        c_ratio, d_ratio, e_ratio = args.budget_ratios
        solver.CONFIG["budget_phase_C_pct"] = c_ratio
        solver.CONFIG["budget_phase_D_pct"] = d_ratio
        solver.CONFIG["budget_phase_E_pct"] = e_ratio

    if args.budget_min is not None:
        solver.CONFIG["total_time_budget_sec"] = int(args.budget_min * 60)

    if args.force_matrix:
        solver.UNREACHABLE_MATRIX_FAIL_PCT = 1.0

    date_str = args.date or latest_common_date(args.depots)
    args.date = date_str

    order_files = resolve_order_files(args.depots, date_str)
    orders = load_combined_orders(order_files)
    solver.CONFIG["orders_file"] = ";".join(str(path) for path in order_files.values())
    solver.CONFIG["vehicle_types_file"] = args.vehicle_types_file

    vehicles_expanded = solver.load_vehicle_types_db(args.vehicle_types_file, block_id="")

    if args.clusters == "auto":
        n_clusters = auto_n_clusters_combined(len(orders), len(vehicles_expanded))
    else:
        n_clusters = int(args.clusters)
    n_clusters = max(1, min(n_clusters, len(orders)))
    solver.CONFIG["num_clusters"] = n_clusters

    if args.workers is not None:
        n_workers = max(1, int(args.workers))
    elif solver.CONFIG["parallel_workers"] == "auto":
        n_workers = max(1, multiprocessing.cpu_count() - 2)
    else:
        n_workers = int(solver.CONFIG["parallel_workers"])
    solver.CONFIG["parallel_workers"] = n_workers

    output_dir = build_output_dir(args, date_str)
    return date_str, order_files, orders, vehicles_expanded, n_clusters, n_workers, output_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan CB/MO/HK/PR together as one shared-warehouse VRP."
    )
    parser.add_argument(
        "--date",
        default="",
        help="Delivery date YYYY-MM-DD. If omitted, uses latest date common to all depots.",
    )
    parser.add_argument(
        "--depots",
        type=parse_depots,
        default=list(DEFAULT_DEPOTS),
        help="Comma-separated source depots to combine. Default: CB,MO,HK,PR",
    )
    parser.add_argument(
        "--vehicle-types-file",
        default=solver.CONFIG["vehicle_types_file"],
        help="CSV with vehicle type definitions. Uses available_count in combined mode.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Output directory. Default: data/results/ALL/{DATE}_budget{N}min",
    )
    parser.add_argument(
        "--clusters",
        default="auto",
        help="Cluster count or 'auto'. Combined auto=max(8, ceil(orders/75), ceil(vehicles/10)).",
    )
    parser.add_argument(
        "--seed-restarts",
        type=int,
        default=3,
        help="Number of KMeans and TW-aware restarts. Total seeds = 2*N + 1 sweep.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel worker processes. Default auto uses cpu_count() - 2.",
    )
    parser.add_argument(
        "--force-matrix",
        action="store_true",
        help="Disable hard-fail for unreachable matrix pairs.",
    )
    parser.add_argument(
        "--budget-min",
        type=float,
        default=None,
        help="Override total solver time budget in minutes.",
    )
    parser.add_argument(
        "--budget-ratios",
        type=parse_budget_ratios,
        default=None,
        help="Override phase budget ratios as C,D,E. Example: 0.35,0.25,0.40",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load and validate combined inputs, then exit before routing/solving.",
    )
    parser.add_argument(
        "--run-startup-tests",
        action="store_true",
        help="Run the existing pytest startup suite before solving.",
    )
    add_osm_args(parser)
    return parser.parse_args()


def ensure_routing_ready(args: argparse.Namespace) -> None:
    osm_source = "current" if args.fresh_osm else "stable"
    apply_osm_source(solver.CONFIG, osm_source)
    print(
        f"[OSM] source: {osm_source}"
        f"{' (fresh)' if args.fresh_osm else ''}"
        f" | OSRM={solver.CONFIG['osrm_urls']['driving']}"
        f" | ORS={solver.CONFIG['osrm_urls']['driving-hgv']}"
    )

    if args.fresh_osm:
        from osrm_orchestrator import ensure_fresh_routing_ready

        ensure_fresh_routing_ready()
    else:
        osrm_ping_url = (
            f"{solver.CONFIG['osrm_url']}/route/v1/driving/"
            "14.4,50.0;14.5,50.1?overview=false"
        )
        try:
            requests.get(osrm_ping_url, timeout=2)
        except requests.exceptions.RequestException:
            raise SystemExit(
                f"\n[ERROR] Routing instance ({solver.CONFIG['osrm_url']}) is not responding.\n"
                "        Start Docker container: scripts/start_osrm_stable.bat"
            )

    solver.run_routing_tests(
        osrm_url=solver.CONFIG["osrm_urls"]["driving"],
        ors_url=solver.CONFIG["osrm_urls"]["driving-hgv"],
    )


def build_matrices(orders: list[dict], vehicles_expanded: list[dict]) -> tuple[np.ndarray, dict]:
    locations = (
        [(solver.DEPOT["lat"], solver.DEPOT["lon"])]
        + [(order["lat"], order["lon"]) for order in orders]
    )

    distinct_profiles = sorted({vehicle["osrm_profile"] for vehicle in vehicles_expanded})
    matrices_by_profile: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for profile in distinct_profiles:
        matrices_by_profile[profile] = solver.get_matrix(locations, profile=profile)

    from closures_utils import apply_closures_to_matrix

    for profile in list(matrices_by_profile):
        dist_p, dur_p = matrices_by_profile[profile]
        dur_p, dist_p = apply_closures_to_matrix(
            dur_p,
            dist_p,
            locations,
            matrix_profile=profile,
            osrm_url=solver.CONFIG["osrm_url"],
            ors_url=solver.CONFIG["osrm_urls"].get("driving-hgv", "http://localhost:8080"),
            closure_route_profile=solver.CONFIG["closure_route_profiles"].get(profile),
            debug_label=profile,
        )
        matrices_by_profile[profile] = (dist_p, dur_p)

    distances_km = matrices_by_profile.get(
        "driving", next(iter(matrices_by_profile.values()))
    )[0]

    vehicle_time_by_id: dict = {}
    for vehicle in vehicles_expanded:
        _, dur_buffered = matrices_by_profile[vehicle["osrm_profile"]]
        time_matrix = dur_buffered * vehicle["time_multiplier"]
        np.fill_diagonal(time_matrix, 0)
        vehicle_time_by_id[vehicle["id"]] = time_matrix

    return distances_km, vehicle_time_by_id


def main() -> None:
    configure_stdio()
    os.chdir(SCRIPT_DIR)
    args = parse_args()
    t_global_start = time.time()

    print("=" * 65)
    print("VRP Solver Lines v6 - combined CB/MO/HK/PR")
    print("=" * 65)

    (
        date_str,
        order_files,
        orders,
        vehicles_expanded,
        n_clusters,
        n_workers,
        output_dir,
    ) = prepare_run(args)

    print_combined_settings(
        args,
        order_files,
        orders,
        vehicles_expanded,
        n_clusters,
        n_workers,
        output_dir,
    )

    if args.dry_run:
        print("\n[DRY RUN] Inputs loaded successfully. No routing or output files written.")
        return

    if args.run_startup_tests:
        solver.run_startup_tests()
    ensure_routing_ready(args)

    print("\n" + "-" * 65)
    print("[A] Routing matrices")
    print("-" * 65)
    distances_km, vehicle_time_by_id = build_matrices(orders, vehicles_expanded)

    t_after_matrices = time.time()
    matrix_elapsed = t_after_matrices - t_global_start
    remaining = max(1, solver.CONFIG["total_time_budget_sec"] - matrix_elapsed)
    budget_c = remaining * solver.CONFIG["budget_phase_C_pct"]
    budget_d = remaining * solver.CONFIG["budget_phase_D_pct"]
    budget_e = remaining * solver.CONFIG["budget_phase_E_pct"]
    print(f"\nMatrices: {matrix_elapsed:.0f} sec | remaining {remaining / 60:.1f} min")
    print(
        f"Budgets -> C: {budget_c / 60:.1f} min | "
        f"D: {budget_d / 60:.1f} min | E: {budget_e / 60:.1f} min"
    )

    print("\n" + "-" * 65)
    print("[B+C] Multi-seed partition + parallel solve")
    print("-" * 65)
    state = phase_c_best_multi_seed(
        orders,
        vehicles_expanded,
        distances_km,
        vehicle_time_by_id,
        n_clusters,
        int(budget_c),
        n_workers,
        args.seed_restarts,
    )
    print(f"Phase C: {time.time() - t_after_matrices:.0f} sec | {state.total_cost:,.0f} Kc")

    print("\n" + "-" * 65)
    print("[D] Cross-cluster LNS")
    print("-" * 65)
    t_d = time.time()
    state = solver.phase_d_lns(state, distances_km, vehicle_time_by_id, budget_d, n_workers)
    print(f"Phase D: {time.time() - t_d:.0f} sec | {state.total_cost:,.0f} Kc")

    print("\n" + "-" * 65)
    print("[D2] Vehicle reassignment after LNS")
    print("-" * 65)
    rebalance_budget = min(180, max(1, int(budget_e * 0.20)))
    state = rebalance_vehicle_assignments(
        state,
        vehicles_expanded,
        distances_km,
        vehicle_time_by_id,
        rebalance_budget,
        n_workers,
    )
    budget_e = max(1, budget_e - rebalance_budget)

    print("\n" + "-" * 65)
    print("[E] Final intensification")
    print("-" * 65)
    t_e = time.time()
    state = solver.phase_e_intensify(
        state, distances_km, vehicle_time_by_id, budget_e, n_workers
    )
    print(f"Phase E: {time.time() - t_e:.0f} sec | {state.total_cost:,.0f} Kc")

    all_routes = state.all_routes()
    enrich_routes_with_source_depot(all_routes, orders)
    verify_all_orders_served(all_routes, orders)

    total_cost = state.total_cost
    elapsed_min = (time.time() - t_global_start) / 60
    print(f"\nTotal runtime: {elapsed_min:.1f} min")

    solver.print_results(all_routes, total_cost)

    from closures_utils import load_active_closures

    active_closures = load_active_closures()
    save_outputs_combined(
        all_routes,
        total_cost,
        output_dir,
        elapsed_min,
        orders,
        date_str,
        order_files,
        active_closures,
    )


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
