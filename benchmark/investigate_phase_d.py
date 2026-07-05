"""
investigate_phase_d.py — diagnostika a test opravy Phase D (LNS)
================================================================

NEPŘEPISUJE PRODUKČNÍ KÓD — monkey-patch za runtime.

Dva módy:
  --mode broken   Produkční parametry (baseline, co vidíme v benchmarcích)
  --mode fix      Opravené parametry:
                    time_per_resolve        20  →  60 s
                    lns_accept_worse_max_pct 0.015 → 0.09
                    lns_accept_worse_prob    0.08  → 0.15

Obě varianty použijí plně instrumentovanou kopii _lns_iteration a phase_d_lns
(loguje exit-kód každé iterace: A/B/C/D/OK).

Použití:
    cd vrp_benchmark
    PYTHONIOENCODING=utf-8 python benchmark/investigate_phase_d.py \\
        --mode broken --orders data/prepared/MO/orders_MO_2026-04-10.csv --budget-sec 300
    PYTHONIOENCODING=utf-8 python benchmark/investigate_phase_d.py \\
        --mode fix    --orders data/prepared/MO/orders_MO_2026-04-10.csv --budget-sec 900
"""
from __future__ import annotations
import sys, argparse, json, time, multiprocessing, random as _random_mod
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections import defaultdict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import numpy as np
import vrp_solver_lines_v6 as S
from closures_utils import apply_closures_to_matrix

# ─── Trace store ────────────────────────────────────────────────────────────
TRACE: list[dict] = []
_exit_counts: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "OK": 0}


# ════════════════════════════════════════════════════════════════════════════
#  INSTRUMENTOVANÁ KOPIE _lns_iteration  (identická logika + exit logging)
# ════════════════════════════════════════════════════════════════════════════

def _instrumented_lns_iteration(state, distances_km, vehicle_time_by_id, destroy_size,
                                 n_workers, time_limit_sec, rng, temperature):
    """
    Plná kopie S._lns_iteration se záznamem každého exit-bodu.
    Produkční soubor nedotčen.
    """
    rec: dict = {
        "destroy_size":   destroy_size,
        "time_limit_sec": time_limit_sec,
        "exit":           None,
        "n_to_move":      0,
        "n_moves":        0,
        "n_worker_args":  0,
        "resolved":       0,
        "expected":       0,
        "delta":          None,
        "accepted":       False,
        "improved":       False,
        "worker_details": [],
    }

    # ── Move generation ───────────────────────────────────────────────────
    scored_candidates = S._identify_destroy_candidates(state, distances_km)
    to_move = [idx for _, idx in scored_candidates[:destroy_size]]
    rec["n_to_move"] = len(to_move)

    if not to_move:
        _exit_counts["A"] += 1
        rec["exit"] = "EXIT-A:to_move_empty"
        TRACE.append(rec)
        return False, False, state

    centroids   = S._cluster_centroids(state.clusters) if state.clusters else np.array([])
    k_neighbors = S.CONFIG["lns_neighbor_clusters"]
    moves       = []

    for order_idx in to_move:
        from_c = state.cluster_labels[order_idx]
        order  = state.orders[order_idx]
        neighbors = (S._neighbor_clusters(from_c, centroids, k_neighbors)
                     if len(centroids) > 1 else [])

        candidate_targets = []
        for to_c in neighbors:
            centroid  = centroids[to_c] if len(centroids) else None
            score     = S.estimate_cluster_insertion_score(
                order, state.clusters[to_c], centroid)
            max_v_cap = max([v["max_kg"] for v in state.vehicle_assignments[to_c]],
                            default=0)
            if order["weight_kg"] > max_v_cap:
                score += 1e6
            candidate_targets.append((score, to_c))

        candidate_targets.sort(key=lambda x: x[0])
        if not candidate_targets:
            continue
        top_k = min(2, len(candidate_targets))
        _, chosen_target = candidate_targets[rng.randint(0, top_k - 1)]
        if chosen_target != from_c and candidate_targets[0][0] < 1e6:
            moves.append((order_idx, from_c, chosen_target))

    rec["n_moves"] = len(moves)

    if not moves:
        _exit_counts["A"] += 1
        rec["exit"] = "EXIT-A:moves_empty"
        TRACE.append(rec)
        return False, False, state

    # ── Cluster rebuild ───────────────────────────────────────────────────
    affected_clusters = set()
    new_labels = list(state.cluster_labels)
    for order_idx, from_c, to_c in moves:
        affected_clusters.add(from_c)
        affected_clusters.add(to_c)
        new_labels[order_idx] = to_c

    n_clusters   = len(state.clusters)
    new_clusters = [[] for _ in range(n_clusters)]
    new_indices  = [[] for _ in range(n_clusters)]
    for order_idx, order in enumerate(state.orders):
        c = new_labels[order_idx]
        new_clusters[c].append(order)
        new_indices[c].append(order_idx)

    worker_args = []
    for c_idx in affected_clusters:
        if not new_clusters[c_idx]:
            continue
        c_vehicles      = state.vehicle_assignments[c_idx]
        cluster_v_times = [vehicle_time_by_id[v["id"]] for v in c_vehicles]
        sub_dist, sub_times = S.extract_submatrix(
            distances_km, cluster_v_times, new_indices[c_idx])
        worker_args.append({
            "seed_name":       "lns",
            "cluster_idx":     c_idx,
            "cluster_orders":  new_clusters[c_idx],
            "cluster_vehicles":c_vehicles,
            "sub_dist":        sub_dist.tolist(),
            "sub_times":       [st.tolist() for st in sub_times],
            "time_limit_sec":  time_limit_sec,
        })

    rec["n_worker_args"] = len(worker_args)
    rec["expected"]      = len(worker_args)

    if not worker_args:
        _exit_counts["B"] += 1
        rec["exit"] = "EXIT-B:worker_args_empty"
        TRACE.append(rec)
        return False, False, state

    # ── OR-Tools re-solve ─────────────────────────────────────────────────
    new_cluster_routes = list(state.cluster_routes)
    new_cluster_costs  = list(state.cluster_costs)
    resolved = set()

    with ProcessPoolExecutor(max_workers=min(n_workers, len(worker_args))) as executor:
        futures = {executor.submit(S._worker_solve_cluster, args): args["cluster_idx"]
                   for args in worker_args}
        for future in as_completed(futures):
            try:
                res = future.result()
                c_idx     = res["cluster_idx"]
                routes_ok = bool(res["routes"])
                warg = next(a for a in worker_args if a["cluster_idx"] == c_idx)
                rec["worker_details"].append({
                    "cluster_idx": c_idx,
                    "n_orders":    len(warg["cluster_orders"]),
                    "routes_ok":   routes_ok,
                    "cost":        res.get("cost", 0),
                })
                if routes_ok:
                    new_cluster_routes[c_idx] = res["routes"]
                    new_cluster_costs[c_idx]  = res["cost"]
                    resolved.add(c_idx)
                else:
                    print(f"    [DIAG] cluster {c_idx}: routes EMPTY")
            except Exception as e:
                print(f"    [DIAG] worker exception: {e}")
                rec["worker_details"].append({"cluster_idx": -1, "exception": str(e),
                                              "routes_ok": False})

    rec["resolved"] = len(resolved)
    expected_set = {a["cluster_idx"] for a in worker_args}

    if resolved != expected_set:
        _exit_counts["C"] += 1
        rec["exit"] = f"EXIT-C:resolved({len(resolved)}/{len(expected_set)})"
        TRACE.append(rec)
        return False, False, state

    # ── SA Acceptance ─────────────────────────────────────────────────────
    old_cost = state.total_cost
    new_cost = sum(new_cluster_costs)
    delta    = new_cost - old_cost
    improved = delta < 0
    rec["delta"] = round(delta, 1)

    accept    = False
    max_abs   = max(1.0, old_cost * S.CONFIG["lns_accept_worse_max_pct"]
                    * max(temperature, 0.25))
    rec["sa_max_abs"] = round(max_abs, 1)

    if improved:
        accept = True
    elif (delta <= max_abs
          and _random_mod.random() < S.CONFIG["lns_accept_worse_prob"]
          * max(temperature, 0.35)):
        accept = True

    rec["accepted"] = accept
    rec["improved"] = improved

    if not accept:
        _exit_counts["D"] += 1
        rec["exit"] = f"EXIT-D:delta={delta:+.0f}_max={max_abs:.0f}"
        TRACE.append(rec)
        return False, False, state

    _exit_counts["OK"] += 1
    rec["exit"] = "EXIT-OK"
    TRACE.append(rec)

    new_state = S.SolutionState(
        orders=state.orders,
        cluster_labels=new_labels,
        clusters=new_clusters,
        cluster_indices=new_indices,
        vehicle_assignments=state.vehicle_assignments,
        cluster_routes_list=new_cluster_routes,
        cluster_costs=new_cluster_costs,
    )
    return True, improved, new_state


# ════════════════════════════════════════════════════════════════════════════
#  KOPIE phase_d_lns s konfigurovatelným time_per_resolve
#  (čte S.CONFIG["lns_time_per_resolve"], fallback na 20)
# ════════════════════════════════════════════════════════════════════════════

def _patched_phase_d_lns(state, distances_km, vehicle_time_by_id,
                          time_budget_sec, n_workers):
    """
    Kopie phase_d_lns — jediná změna:
      time_per_resolve = S.CONFIG.get("lns_time_per_resolve", 20)
    Produkční soubor nedotčen.
    """
    import random as _r
    rng             = _r.Random(S.CONFIG["random_seed"])
    destroy_min     = S.CONFIG["lns_destroy_min"]
    destroy_max     = S.CONFIG["lns_destroy_max"]
    destroy_size    = (destroy_min + destroy_max) // 2
    time_per_resolve = S.CONFIG.get("lns_time_per_resolve", 20)   # ← jediná změna

    t_start       = time.time()
    t_deadline    = t_start + time_budget_sec
    iteration     = 0
    improvements  = 0
    accepted_worse= 0
    best_cost     = state.total_cost
    best_state    = state
    stagnation    = 0

    print(f"  Pocatecni cena: {best_cost:,.0f} Kc")
    print(f"  LNS budget: {time_budget_sec/60:.0f} min | "
          f"time_per_resolve: {time_per_resolve}s | destroy_size start: {destroy_size}")

    while time.time() < t_deadline:
        iteration += 1
        now       = time.time()
        remaining = t_deadline - now
        if remaining < time_per_resolve * 2:
            break

        progress    = (now - t_start) / max(time_budget_sec, 1)
        temperature = max(0.15, 1.0 - progress)

        accepted, improved, candidate_state = S._lns_iteration(
            state, distances_km, vehicle_time_by_id,
            destroy_size=destroy_size,
            n_workers=n_workers,
            time_limit_sec=time_per_resolve,
            rng=rng,
            temperature=temperature,
        )

        if not accepted:
            stagnation   += 1
            destroy_size  = max(destroy_min, destroy_size - 1)
            if stagnation >= S.CONFIG["lns_stagnation_limit"]:
                destroy_size = rng.randint(destroy_min, destroy_max)
                stagnation   = 0
                print(f"  [LNS iter {iteration:3d}] stagnace, reset destroy={destroy_size}")
            continue

        old_cost = state.total_cost
        state    = candidate_state
        new_cost = state.total_cost

        if improved:
            improvements += 1
            stagnation    = 0
            destroy_size  = min(destroy_max, destroy_size + 2)
            if new_cost < best_cost:
                best_cost  = new_cost
                best_state = state
            print(f"  [LNS iter {iteration:3d}] + zlepseni -{old_cost - new_cost:,.0f} Kc"
                  f" -> {new_cost:,.0f} Kc  (destroy={destroy_size})")
        else:
            accepted_worse += 1
            stagnation     += 1
            destroy_size    = min(destroy_max, destroy_size + 1)
            print(f"  [LNS iter {iteration:3d}] ~ uphill "
                  f"{old_cost:,.0f} -> {new_cost:,.0f}  (temp={temperature:.2f})")

    elapsed = time.time() - t_start
    print(f"\n  LNS: {iteration} iter, {improvements} zlepseni, "
          f"{accepted_worse} uphill, {elapsed:.0f}s, best: {best_cost:,.0f} Kc")
    return best_state


# ────────────────────────────────────────────────────────────────────────────
#  OSRM + UZAVÍRKY
# ────────────────────────────────────────────────────────────────────────────

def prepare_matrices(orders, vehicles_expanded):
    locations = ([(S.DEPOT["lat"], S.DEPOT["lon"])]
                 + [(o["lat"], o["lon"]) for o in orders])

    distinct_profiles = sorted(set(v["osrm_profile"] for v in vehicles_expanded))
    matrices_by_profile: dict = {}
    for prof in distinct_profiles:
        matrices_by_profile[prof] = S.get_matrix(locations, profile=prof)

    for prof in list(matrices_by_profile.keys()):
        dist_p, dur_p = matrices_by_profile[prof]
        dur_p, dist_p = apply_closures_to_matrix(
            dur_p, dist_p, locations,
            matrix_profile=prof,
            osrm_url=S.CONFIG["osrm_url"],
            ors_url=S.CONFIG["osrm_urls"].get("driving-hgv", "http://localhost:8080"),
            closure_route_profile=S.CONFIG["closure_route_profiles"].get(prof),
            debug_label=prof,
        )
        matrices_by_profile[prof] = (dist_p, dur_p)

    distances_km = matrices_by_profile.get(
        "driving", next(iter(matrices_by_profile.values()))
    )[0]

    vehicle_time_by_id: dict = {}
    for v in vehicles_expanded:
        _, dur_buffered = matrices_by_profile[v["osrm_profile"]]
        t_mat = dur_buffered * v["time_multiplier"]
        np.fill_diagonal(t_mat, 0)
        vehicle_time_by_id[v["id"]] = t_mat

    return distances_km, vehicle_time_by_id


# ────────────────────────────────────────────────────────────────────────────
#  MAIN
# ────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--orders", required=True)
    p.add_argument("--vehicle-types", default="data/static/vehicle_types.csv")
    p.add_argument("--mode", choices=["broken", "fix"], default="broken",
                   help="broken = produkční parametry | fix = opravené parametry")
    p.add_argument("--budget-sec",  type=int, default=None,
                   help="Phase D budget (default: 300 pro broken, 900 pro fix)")
    p.add_argument("--phase-c-sec", type=int, default=60)
    p.add_argument("--clusters",    type=int, default=3)
    p.add_argument("--out", default=None)
    return p.parse_args()


FIX_PARAMS = {
    "lns_time_per_resolve":      60,    # bylo hardcoded 20
    "lns_accept_worse_max_pct":  0.09,  # bylo 0.015
    "lns_accept_worse_prob":     0.15,  # bylo 0.08
}

BROKEN_PARAMS = {
    "lns_time_per_resolve":      20,
    "lns_accept_worse_max_pct":  0.015,
    "lns_accept_worse_prob":     0.08,
}


def main():
    args = parse_args()
    n_workers = max(1, multiprocessing.cpu_count() - 1)

    # Defaultní budget
    budget_sec = args.budget_sec or (900 if args.mode == "fix" else 300)
    out_path   = args.out or f"benchmark/results/phase_d_trace_{args.mode}.json"

    # ── Aplikuj parametry dle módu ─────────────────────────────────────────
    params = FIX_PARAMS if args.mode == "fix" else BROKEN_PARAMS
    for k, v in params.items():
        S.CONFIG[k] = v

    # ── Monkey-patch ───────────────────────────────────────────────────────
    S._lns_iteration = _instrumented_lns_iteration
    S.phase_d_lns    = _patched_phase_d_lns

    print("=" * 65)
    print(f" PHASE D INVESTIGATION -- mode: {args.mode.upper()}")
    print("=" * 65)
    print(f"  time_per_resolve:       {params['lns_time_per_resolve']}s")
    print(f"  accept_worse_max_pct:   {params['lns_accept_worse_max_pct']}")
    print(f"  accept_worse_prob:      {params['lns_accept_worse_prob']}")
    print(f"  Phase D budget:         {budget_sec}s ({budget_sec/60:.0f} min)")
    print(f"  Max iteraci odhad:      ~{budget_sec // (params['lns_time_per_resolve'] * 3 + 10)}")

    orders = S.load_orders_day(args.orders)
    block_id = orders[0].get("block_id", "").strip() if orders else ""
    vehicles_expanded = S.load_vehicle_types_db(args.vehicle_types, block_id=block_id)
    print(f"\nOrders: {len(orders)}  |  Vehicles: {len(vehicles_expanded)}  |  "
          f"Clusters: {args.clusters}  |  Workers: {n_workers}")

    print("\n[A] OSRM matrices + uzavirky...")
    t0 = time.time()
    distances_km, vehicle_time_by_id = prepare_matrices(orders, vehicles_expanded)
    print(f"    {time.time()-t0:.0f}s")

    print(f"\n[C] Seed solve ({args.phase_c_sec}s)...")
    t_c = time.time()
    state = S.phase_c_best_seed(
        orders, vehicles_expanded, distances_km, vehicle_time_by_id,
        args.clusters, args.phase_c_sec, n_workers,
    )
    seed_cost = state.total_cost
    print(f"Phase C: {time.time()-t_c:.0f}s | seed cost: {seed_cost:,.0f} Kc")

    print(f"\n[D] LNS {args.mode.upper()} ({budget_sec}s)...")
    t_d = time.time()
    final_state = S.phase_d_lns(
        state, distances_km, vehicle_time_by_id, budget_sec, n_workers,
    )
    d_elapsed = time.time() - t_d
    final_cost = final_state.total_cost
    print(f"Phase D: {d_elapsed:.0f}s | final: {final_cost:,.0f} Kc "
          f"| zmena: {final_cost - seed_cost:+,.0f} Kc")

    report(out_path, budget_sec, args.mode, seed_cost, final_cost)


# ────────────────────────────────────────────────────────────────────────────
#  REPORT
# ────────────────────────────────────────────────────────────────────────────

def report(out_path: str, budget_sec: int, mode: str,
           seed_cost: float, final_cost: float) -> None:
    print("\n" + "=" * 65)
    print(f"  PHASE D REPORT -- {mode.upper()}")
    print("=" * 65)

    if not TRACE:
        print("Zadne iterace zachyceny.")
        return

    n = len(TRACE)
    improvements   = sum(1 for t in TRACE if t.get("improved"))
    accepted_worse = sum(1 for t in TRACE if t.get("accepted") and not t.get("improved"))
    rejected       = sum(1 for t in TRACE if not t.get("accepted"))

    print(f"\nIteraci: {n}  |  Zlepseni: {improvements}  |  "
          f"Uphill: {accepted_worse}  |  Odmitnuto: {rejected}")
    print(f"Seed cost: {seed_cost:,.0f} Kc  |  Final: {final_cost:,.0f} Kc  "
          f"|  Zmena: {final_cost - seed_cost:+,.0f} Kc")

    print("\nExit kody:")
    for ex, cnt in _exit_counts.items():
        bar = "#" * min(cnt, 40)
        print(f"  {ex:5s}: {cnt:3d}  {bar}")

    # Tabulka
    print(f"\n{'iter':>4}  {'size':>4}  {'tlim':>4}  {'moves':>5}  "
          f"{'wrkrs':>5}  {'rslvd':>5}  {'delta':>8}  {'sa_max':>7}  exit")
    print("-" * 70)
    for i, t in enumerate(TRACE):
        delta_s  = f"{t['delta']:+.0f}" if t.get("delta") is not None else "  --"
        samax_s  = f"{t.get('sa_max_abs', 0):.0f}" if t.get("sa_max_abs") else " --"
        print(f"{i+1:>4}  {t['destroy_size']:>4}  {t['time_limit_sec']:>4}  "
              f"{t['n_moves']:>5}  {t['n_worker_args']:>5}  "
              f"{t['resolved']:>5}  {delta_s:>8}  {samax_s:>7}  {t['exit']}")

    # Deltas distribuce
    deltas = [t["delta"] for t in TRACE if t.get("delta") is not None]
    if deltas:
        neg = sum(1 for d in deltas if d < 0)
        zer = sum(1 for d in deltas if d == 0)
        pos = sum(1 for d in deltas if d > 0)
        print(f"\nDelta distribuce: neg={neg}  zero={zer}  pos={pos}")
        if deltas:
            s = sorted(deltas)
            print(f"  min={s[0]:+.0f}  median={s[len(s)//2]:+.0f}  max={s[-1]:+.0f}")

    # Verdikt
    print("\n" + "-" * 65)
    print(" VERDIKT")
    print("-" * 65)
    if improvements > 0:
        saving = seed_cost - final_cost
        print(f"[LNS FUNGUJE] {improvements} zlepseni, uspora {saving:,.0f} Kc")
        print(f"  LNS je zlaty dul — oprava parametru ma smysl pro produkci.")
    elif accepted_worse > 0 and improvements == 0:
        print(f"[SA BEZI, ALE NEMA EFEKT] {accepted_worse} uphill prijato, 0 zlepseni.")
        print(f"  LNS prozkoumava prostor, ale nenachazi lepsi reseni.")
        print(f"  Mozna potrebuje delsi repair cas nebo vice iteraci.")
    elif _exit_counts["D"] == n:
        print(f"[SA ODMITA VZDY] Vsechny iterace EXIT-D.")
        if mode == "fix":
            print(f"  I s opravenymi parametry SA odmita — delty jsou stale prilis vysoke.")
            print(f"  LNS jako cross-cluster operator nema potencial pro tento dataset.")
        else:
            print(f"  Ocekavane — produkci parametry jsou prilis konzervativni.")
    elif _exit_counts["C"] > n // 2:
        print(f"[REPAIR SELHAVA] EXIT-C dominuje — OR-Tools nenachazi feasible reseni.")
        print(f"  time_per_resolve mozna stale prilis kratky, nebo TW nefeasibilni po presunu.")
    else:
        dom = max(_exit_counts, key=lambda k: _exit_counts[k])
        print(f"[SMISENY] Dominantni exit: {dom}, dalsi diagnostika potreba.")

    # Uloz JSON
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump({
            "mode":       mode,
            "budget_sec": budget_sec,
            "iterations": n,
            "seed_cost":  seed_cost,
            "final_cost": final_cost,
            "exit_counts": _exit_counts,
            "params":     FIX_PARAMS if mode == "fix" else BROKEN_PARAMS,
            "trace":      TRACE,
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nTrace: {out.resolve()}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
