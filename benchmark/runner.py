"""
benchmark/runner.py — Persistent benchmark s obnovením sezení
=============================================================

Každé spuštění = "sezení". Výsledky se průběžně ukládají do
experiment_log.jsonl, takže příští sezení pokračuje přesně tam
kde předchozí skončilo.

Proč tento design:
  - Jeden běh = šum (1–3 % variance stochastického solveru).
    Rankujeme podle průměru ≥ 3 běhů, nikoliv podle jednoho čísla.
  - Vítěz 5 min ≠ vítěz 30 min. Budget se ukládá do logu,
    rankiny jsou tedy oddělené per-budget.
  - Testujeme na více depotech najednou: --datasets přijímá
    N souborů, skóre = průměr normalizovaných zlepšení vs baseline.

Použití:
  # 2-hodinové sezení, 30 min/run, 2 datasety
  python benchmark/runner.py \\
      --datasets data/prepared/CB/orders_CB_2026-04-10.csv \\
                 data/prepared/HK/orders_HK_2026-04-08.csv \\
      --budget 30 --session-time 120

  # Jen zobrazit výsledky bez spuštění solveru
  python benchmark/runner.py --report \\
      --datasets data/prepared/CB/orders_CB_2026-04-10.csv \\
                 data/prepared/HK/orders_HK_2026-04-08.csv \\
      --budget 30
"""
from __future__ import annotations

import argparse
import copy
import json
import multiprocessing
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev

# ── Projekt na sys.path ───────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ["SKIP_STARTUP_TESTS"] = "1"

import numpy as np

from osm_routing import add_osm_args, apply_osm_source, resolve_osm_source

try:
    import vrp_solver_lines_v6 as S
except ImportError:
    print("[CHYBA] vrp_solver_lines_v6.py nenalezen.")
    sys.exit(1)

from benchmark.configs import CONFIGS

# ── Cesty ─────────────────────────────────────────────────────────────────────
_BENCH_DIR  = Path(__file__).parent
RESULTS_DIR = _BENCH_DIR / "results"
LOG_FILE    = RESULTS_DIR / "experiment_log.jsonl"

# ── Výchozí hodnoty ───────────────────────────────────────────────────────────
DEFAULT_BUDGET_MIN  = 30
DEFAULT_SESSION_MIN = 120
DEFAULT_TARGET_RUNS = 3
BASELINE_CONFIG     = "01_baseline"


# ═════════════════════════════════════════════════════════════════════════════
#  Persistentní log
# ═════════════════════════════════════════════════════════════════════════════

def load_log(log_path: Path = LOG_FILE) -> list[dict]:
    """Načte všechny záznamy z experiment_log.jsonl (append-only soubor)."""
    if not log_path.exists():
        return []
    runs: list[dict] = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    runs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return runs


def append_log(record: dict, log_path: Path = LOG_FILE) -> None:
    """Připojí jeden záznam na konec logu (nikdy nepřepisuje)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ═════════════════════════════════════════════════════════════════════════════
#  Dataset helpers
# ═════════════════════════════════════════════════════════════════════════════

def build_dataset_key(orders_file: Path) -> str:
    """
    Extrahuje krátký klíč z názvu souboru.
    orders_CB_2026-04-10.csv  →  "CB_2026-04-10"
    Pokud pattern nesedí, vrátí stem souboru.
    """
    m = re.match(
        r"orders_([A-Za-z]+)_(\d{4}-\d{2}-\d{2})\.csv",
        orders_file.name,
        re.IGNORECASE,
    )
    if m:
        return f"{m.group(1).upper()}_{m.group(2)}"
    return orders_file.stem


# ═════════════════════════════════════════════════════════════════════════════
#  Plánování sezení
# ═════════════════════════════════════════════════════════════════════════════

def count_runs(
    log: list[dict],
    config_name: str,
    dataset_key: str,
    budget_min: int,
) -> int:
    """Počet úspěšně dokončených běhů pro danou kombinaci (config, dataset, budget)."""
    return sum(
        1 for r in log
        if r.get("config")     == config_name
        and r.get("dataset")   == dataset_key
        and r.get("budget_min") == budget_min
        and r.get("error") is None
        and r.get("total_cost_kc") is not None
    )


def plan_session(
    log: list[dict],
    configs: list[dict],
    dataset_keys: list[str],
    budget_min: int,
    session_min: int,
    target_runs: int,
) -> list[tuple[str, str]]:
    """
    Vrátí seřazený seznam (config_name, dataset_key) k provedení v tomto sezení.

    Strategie: round-robin — nejdřív první kolo pro všechny kombinace,
    pak druhé kolo atd. V rámci kola: outer loop = konfigurace, inner = datasety,
    takže se datasety střídají pro každou konfiguraci.

    Počet naplánovaných běhů je omezen na session_min // budget_min.
    Každá (config, dataset) kombinace je v sezení naplánována nanejvýš jednou
    za kolo, aby se předešlo duplicitám při max_runs > total_combos.
    """
    max_runs = max(1, session_min // budget_min)
    planned: list[tuple[str, str]] = []
    # Kolikrát jsme tuto kombinaci již naplánovali v tomto sezení
    session_extra: dict[tuple[str, str], int] = {}

    for round_n in range(1, target_runs + 1):
        if len(planned) >= max_runs:
            break
        for cfg in configs:
            if len(planned) >= max_runs:
                break
            for ds in dataset_keys:
                if len(planned) >= max_runs:
                    break
                key = (cfg["name"], ds)
                logged         = count_runs(log, cfg["name"], ds, budget_min)
                planned_so_far = session_extra.get(key, 0)
                if logged + planned_so_far < round_n:
                    planned.append(key)
                    session_extra[key] = planned_so_far + 1

    return planned


# ═════════════════════════════════════════════════════════════════════════════
#  Spuštění jedné konfigurace (jádro solveru)
# ═════════════════════════════════════════════════════════════════════════════

def run_config(
    orders_file: Path,
    vehicles_file: Path,
    config: dict,
    budget_sec: int,
) -> dict:
    """
    Spustí kompletní solver pipeline pro jednu konfiguraci.
    Vrátí dict s výsledky (nebo error).
    """
    result: dict = {
        "total_cost_kc": None,
        "lines_count":   None,
        "total_km":      None,
        "phase_C_sec":   None,
        "phase_D_sec":   None,
        "phase_E_sec":   None,
        "osrm_sec":      None,
        "elapsed_sec":   None,
        "effective_cfg": None,
        "error":         None,
    }

    original_config = copy.deepcopy(S.CONFIG)
    t0 = time.time()
    try:
        # Aplikuj overrides
        for k, v in config["overrides"].items():
            S.CONFIG[k] = v
        S.CONFIG["total_time_budget_sec"] = budget_sec

        # ── SANITY CHECK: zachyť co solver reálně uvidí po monkey-patchi ──
        # Slouží k ověření že overrides fungují (různé configs → různá čísla).
        # Global fields (speed_factor, capacity_mult) navíc — auditní stopa
        # kterou "érou" plánovacích pravidel běh patří.
        result["effective_cfg"] = {
            "C":             S.CONFIG.get("budget_phase_C_pct"),
            "D":             S.CONFIG.get("budget_phase_D_pct"),
            "E":             S.CONFIG.get("budget_phase_E_pct"),
            "clusters":      S.CONFIG.get("num_clusters"),
            "destroy_min":   S.CONFIG.get("lns_destroy_min"),
            "destroy_max":   S.CONFIG.get("lns_destroy_max"),
            "stagnation":    S.CONFIG.get("lns_stagnation_limit"),
            "speed_factor":  S.CONFIG.get("travel_time_speed_factor"),
            "capacity_mult": S.CONFIG.get("vehicle_capacity_multiplier"),
        }
        ec = result["effective_cfg"]
        print(
            f"  [CFG-CHECK] {config['name']}: "
            f"C={ec['C']} D={ec['D']} E={ec['E']} "
            f"clust={ec['clusters']} "
            f"destroy={ec['destroy_min']}-{ec['destroy_max']} "
            f"stagn={ec['stagnation']} "
            f"speed={ec['speed_factor']} cap×={ec['capacity_mult']}"
        )

        # Načti data
        orders   = S.load_orders_day(str(orders_file))
        block_id = orders[0].get("block_id", "").strip() if orders else ""
        vehicles = S.load_vehicle_types_db(str(vehicles_file), block_id=block_id)

        # OSRM matice
        t_osrm    = time.time()
        locations = (
            [(S.DEPOT["lat"], S.DEPOT["lon"])]
            + [(o["lat"], o["lon"]) for o in orders]
        )
        distinct_profiles = sorted(set(v["osrm_profile"] for v in vehicles))
        matrices_by_profile: dict = {}
        for prof in distinct_profiles:
            matrices_by_profile[prof] = S.get_matrix(locations, profile=prof)

        distances_km = matrices_by_profile.get(
            "driving", next(iter(matrices_by_profile.values()))
        )[0]

        # Uzavírky (volitelné — nesmí zastavit benchmark při chybě)
        try:
            from closures_utils import apply_closures_to_matrix  # type: ignore
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
        except Exception:
            pass

        # Per-vehicle časové matice
        vehicle_time_by_id: dict = {}
        for v in vehicles:
            _, dur_buffered = matrices_by_profile[v["osrm_profile"]]
            t_mat = dur_buffered * v["time_multiplier"]
            np.fill_diagonal(t_mat, 0)
            vehicle_time_by_id[v["id"]] = t_mat

        result["osrm_sec"] = round(time.time() - t_osrm, 1)
        remaining = budget_sec - (time.time() - t0)

        cfg        = S.CONFIG
        budget_C   = remaining * cfg["budget_phase_C_pct"]
        budget_D   = remaining * cfg["budget_phase_D_pct"]
        budget_E   = remaining * cfg["budget_phase_E_pct"]

        n_clusters = (
            S.auto_n_clusters(len(orders), len(vehicles))
            if cfg["num_clusters"] == "auto"
            else int(cfg["num_clusters"])
        )
        n_workers  = (
            max(1, multiprocessing.cpu_count() - 1)
            if cfg["parallel_workers"] == "auto"
            else int(cfg["parallel_workers"])
        )

        t_c = time.time()
        state = S.phase_c_best_seed(
            orders, vehicles, distances_km, vehicle_time_by_id,
            n_clusters, int(budget_C), n_workers,
        )
        result["phase_C_sec"] = round(time.time() - t_c, 1)

        t_d = time.time()
        state = S.phase_d_lns(state, distances_km, vehicle_time_by_id, budget_D, n_workers)
        result["phase_D_sec"] = round(time.time() - t_d, 1)

        t_e = time.time()
        state = S.phase_e_intensify(state, distances_km, vehicle_time_by_id, budget_E, n_workers)
        result["phase_E_sec"] = round(time.time() - t_e, 1)

        routes = state.all_routes()
        result["total_cost_kc"] = round(state.total_cost, 0)
        result["lines_count"]   = len(routes)
        result["total_km"]      = round(sum(r.get("total_km", 0) for r in routes), 1)

    except Exception as exc:
        result["error"] = str(exc)[:300]

    finally:
        result["elapsed_sec"] = round(time.time() - t0, 1)
        for k, v in original_config.items():
            S.CONFIG[k] = v

    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Výpočet rankingů ze záznamu logu
# ═════════════════════════════════════════════════════════════════════════════

def _collect_costs(
    log: list[dict],
    config_name: str,
    dataset_key: str,
    budget_min: int,
) -> list[float]:
    return [
        r["total_cost_kc"] for r in log
        if r.get("config")      == config_name
        and r.get("dataset")    == dataset_key
        and r.get("budget_min") == budget_min
        and r.get("error") is None
        and r.get("total_cost_kc") is not None
    ]


def compute_rankings(
    log: list[dict],
    configs: list[dict],
    dataset_keys: list[str],
    budget_min: int,
    target_runs: int,
) -> list[dict]:
    """
    Vypočítá rankingy ze záznamu logu.
    Vrátí seznam dictů, seřazených od nejlepšího.

    Skóre = průměrné procentuální zlepšení vs baseline přes všechny datasety.
    Kladné číslo = lepší než baseline (nižší cena).
    """
    # Průměrné náklady baseline per dataset (pro normalizaci)
    baseline_mean: dict[str, float | None] = {}
    for ds in dataset_keys:
        costs = _collect_costs(log, BASELINE_CONFIG, ds, budget_min)
        baseline_mean[ds] = mean(costs) if costs else None

    rows: list[dict] = []
    for cfg in configs:
        row: dict = {
            "name":              cfg["name"],
            "description":       cfg["description"],
            "datasets":          {},
            "normalized_scores": [],   # % zlepšení vs baseline per dataset
            "total_n":           0,
            "min_n":             None,
            "overall_score":     None,
        }
        ns: list[int] = []
        for ds in dataset_keys:
            costs = _collect_costs(log, cfg["name"], ds, budget_min)
            n     = len(costs)
            ns.append(n)
            if costs:
                ds_mean = mean(costs)
                ds_std  = stdev(costs) if n >= 2 else None
                row["datasets"][ds] = {"n": n, "mean": ds_mean, "std": ds_std}
                bm = baseline_mean.get(ds)
                if bm and bm > 0:
                    row["normalized_scores"].append((bm - ds_mean) / bm * 100)
            else:
                row["datasets"][ds] = {"n": 0, "mean": None, "std": None}

        row["total_n"] = sum(ns)
        row["min_n"]   = min(ns) if ns else 0
        if row["normalized_scores"]:
            row["overall_score"] = mean(row["normalized_scores"])

        rows.append(row)

    # Seřadit: nejlepší skóre první; bez skóre → řadit podle nejnižšího raw cost
    def sort_key(r: dict):
        if r["overall_score"] is not None:
            return (-r["overall_score"],)
        first_cost = next(
            (v["mean"] for v in r["datasets"].values() if v["mean"] is not None),
            float("inf"),
        )
        return (first_cost,)

    rows.sort(key=sort_key)
    return rows


# ═════════════════════════════════════════════════════════════════════════════
#  Zobrazení výsledků
# ═════════════════════════════════════════════════════════════════════════════

def print_report(
    log: list[dict],
    configs: list[dict],
    dataset_keys: list[str],
    budget_min: int,
    target_runs: int,
    session_min: int = DEFAULT_SESSION_MIN,
) -> None:
    """Vytiskne přehlednou tabulku rankingů."""
    rankings = compute_rankings(log, configs, dataset_keys, budget_min, target_runs)

    W = 75 + max(0, (len(dataset_keys) - 1) * 26)
    print(f"\n{'═' * W}")
    print(f"  VÝSLEDKY BENCHMARKU — budget: {budget_min} min/run")
    print(f"  Datasety: {', '.join(dataset_keys)}")
    print(f"{'═' * W}")

    # Záhlaví tabulky
    ds_headers = "  ".join(f"{ds:>22}" for ds in dataset_keys)
    print(f"\n  {'Konfigurace':28s}  {'N':>2}  {ds_headers}  {'Skóre':>12}")
    sep = "─" * 28 + "  ──  " + "  ".join(["─" * 22] * len(dataset_keys)) + "  " + "─" * 12
    print(f"  {sep}")

    # Najdi nejlepší konfiguraci pro ★
    best_row = next(
        (r for r in rankings if r["total_n"] > 0 and r["overall_score"] is not None),
        None,
    )

    for r in rankings:
        n_str = f"{r['min_n']}" if r["min_n"] is not None else "0"
        if r["min_n"] is not None and r["min_n"] < target_runs and r["total_n"] > 0:
            n_str += "⚠"

        # Dataset sloupce
        ds_cells: list[str] = []
        for ds in dataset_keys:
            d = r["datasets"].get(ds, {})
            if d.get("mean") is None:
                ds_cells.append(f"{'—':>22}")
            elif d.get("std") is not None:
                ds_cells.append(f"{d['mean']:>10,.0f} ±{d['std']:>6,.0f} Kč")
            else:
                ds_cells.append(f"{d['mean']:>10,.0f}          Kč")

        # Skóre sloupec
        score_str = ""
        if r["overall_score"] is not None:
            if r["name"] == BASELINE_CONFIG:
                score_str = "  0.0 % (ref)"
            else:
                marker = "★ " if r is best_row else "  "
                sign   = "+" if r["overall_score"] >= 0 else ""
                score_str = f"{marker}{sign}{r['overall_score']:.1f} %"
        elif r["total_n"] > 0:
            score_str = "  (bez ref.)"

        row_line = (
            f"  {r['name']:28s}  {n_str:>2}  "
            + "  ".join(ds_cells)
            + f"  {score_str:>12}"
        )
        print(row_line)

    # Progress souhrn
    total_cells     = len(configs) * len(dataset_keys)
    done_cells      = sum(
        1 for cfg in configs for ds in dataset_keys
        if count_runs(log, cfg["name"], ds, budget_min) >= target_runs
    )
    remaining_runs  = sum(
        max(0, target_runs - count_runs(log, cfg["name"], ds, budget_min))
        for cfg in configs
        for ds in dataset_keys
    )
    est_sessions    = -(-remaining_runs * budget_min // session_min)  # ceiling

    print()
    print(f"  {'─' * (W - 2)}")
    print(f"  Pokrytí: {done_cells}/{total_cells} konfigurací má ≥{target_runs} běhy")
    if remaining_runs:
        print(
            f"  Zbývá:   {remaining_runs} běhů × {budget_min} min "
            f"≈ {est_sessions} {'sezení' if est_sessions != 1 else 'sezení'} "
            f"(při --session-time {session_min})"
        )
    else:
        print(f"  ✓ Benchmark kompletní!")
    print(f"{'═' * W}\n")


# ═════════════════════════════════════════════════════════════════════════════
#  Orchestrátor sezení
# ═════════════════════════════════════════════════════════════════════════════

def _select_top_configs(
    log: list[dict],
    configs: list[dict],
    dataset_keys: list[str],
    top_n: int,
    ref_budget_min: int,
) -> list[dict]:
    """
    Vrátí top_n konfigurací podle rankingu při ref_budget_min.
    Pokud není dost dat, varuje a vrátí všechny konfigurace.
    """
    rankings = compute_rankings(log, configs, dataset_keys, ref_budget_min, target_runs=1)
    with_data = [r for r in rankings if r["total_n"] > 0]

    if len(with_data) < top_n:
        print(
            f"\n  [WARN] --top {top_n} požaduje data pro {top_n} konfigurací, "
            f"ale v logu je jen {len(with_data)} s alespoň 1 během při {ref_budget_min} min.\n"
            f"  Spusť nejdřív průzkumné sezení (--budget {ref_budget_min}) a pak opakuj.\n"
        )
        return configs

    top_names = {r["name"] for r in with_data[:top_n]}
    top_names.add(BASELINE_CONFIG)          # baseline vždy zahrnut jako reference
    selected  = [c for c in configs if c["name"] in top_names]
    finalists = [c["name"] for c in selected if c["name"] != BASELINE_CONFIG]
    print(
        f"\n  Finalisté (top {top_n} z {ref_budget_min}-min rankingu): "
        + ", ".join(finalists)
        + f"  +  {BASELINE_CONFIG} (reference)"
    )
    return selected


def run_session(
    orders_files:   list[Path],
    vehicles_file:  Path,
    budget_min:     int = DEFAULT_BUDGET_MIN,
    session_min:    int = DEFAULT_SESSION_MIN,
    target_runs:    int = DEFAULT_TARGET_RUNS,
    top_n:          int = 0,
    ref_budget_min: int = 5,
    explicit_configs: list[str] | None = None,
) -> None:
    log         = load_log()
    dataset_map = {build_dataset_key(f): f for f in orders_files}
    dataset_keys = list(dataset_map.keys())

    # Filtruj konfigurace
    if explicit_configs:
        # --configs: explicitní výběr dle jmen, zachová pořadí ze CONFIGS
        name_set = set(explicit_configs)
        active_configs = [c for c in CONFIGS if c["name"] in name_set]
        missing = name_set - {c["name"] for c in active_configs}
        if missing:
            print(f"\n  [WARN] Neznámé konfigurace v --configs: {', '.join(sorted(missing))}")
        print(
            f"\n  Explicitní výběr ({len(active_configs)} konfigurací): "
            + ", ".join(c["name"] for c in active_configs)
        )
    elif top_n > 0:
        active_configs = _select_top_configs(log, CONFIGS, dataset_keys, top_n, ref_budget_min)
    else:
        active_configs = CONFIGS

    plan      = plan_session(log, active_configs, dataset_keys, budget_min, session_min, target_runs)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Hlavička sezení ──────────────────────────────────────────────────────
    total_cells  = len(active_configs) * len(dataset_keys)
    done_before  = sum(
        1 for cfg in active_configs for ds in dataset_keys
        if count_runs(log, cfg["name"], ds, budget_min) >= target_runs
    )

    print(f"\n{'═' * 65}")
    print(f"  BENCHMARK SEZENÍ — {timestamp}")
    print(f"  Datasety:   {', '.join(dataset_keys)}")
    print(f"  Budget:     {budget_min} min/run")
    if top_n > 0:
        print(f"  Finalisté:  top {top_n} z {ref_budget_min}-min rankingu")
    print(f"  Sezení:     max {session_min} min  →  max {session_min // budget_min} běhů")
    print(f"  Cíl:        {target_runs}× každá konfigurace × každý dataset")
    print(f"  Stav před:  {done_before}/{total_cells} kombinací dokončeno")
    print(f"{'─' * 65}")

    if not plan:
        print(f"\n  ✓ Vše hotovo! Níže jsou aktuální výsledky.\n")
        print_report(log, active_configs, dataset_keys, budget_min, target_runs, session_min)
        return

    print(f"  Plán: {len(plan)} běhů v tomto sezení ({len(plan) * budget_min} min max)")
    print(f"{'═' * 65}")

    # ── Vytvoř složku pro ladění tohoto sezení ───────────────────────────────
    session_dir = RESULTS_DIR / f"session_{timestamp}"
    session_dir.mkdir(parents=True, exist_ok=True)

    config_map       = {c["name"]: c for c in CONFIGS}  # celá mapa, ať najdeme cfg objekt
    session_records: list[dict] = []

    # ── Proveď naplánované běhy ──────────────────────────────────────────────
    for i, (cfg_name, ds_key) in enumerate(plan, 1):
        cfg         = config_map[cfg_name]
        orders_file = dataset_map[ds_key]
        run_idx     = count_runs(log, cfg_name, ds_key, budget_min) + 1

        print(
            f"\n[{i}/{len(plan)}]  {cfg_name}  │  {ds_key}  │  "
            f"běh {run_idx}/{target_runs}"
        )
        print(f"  {cfg['description']}")

        result = run_config(orders_file, vehicles_file, cfg, budget_min * 60)

        record: dict = {
            "config":     cfg_name,
            "dataset":    ds_key,
            "budget_min": budget_min,
            "run_index":  run_idx,
            "timestamp":  datetime.now().isoformat(timespec="seconds"),
            **{k: result[k] for k in result},
        }
        append_log(record)
        log.append(record)
        session_records.append(record)

        if result["error"]:
            print(f"  ✗ CHYBA: {result['error'][:80]}")
        else:
            print(
                f"  ✓  {result['total_cost_kc']:>10,.0f} Kč  │  "
                f"{result['lines_count']} linek  │  "
                f"{result['total_km']:.1f} km  │  "
                f"C:{result['phase_C_sec']}s  D:{result['phase_D_sec']}s  "
                f"E:{result['phase_E_sec']}s  (celkem {result['elapsed_sec']}s)"
            )

    # ── Ulož ladící JSONL pro toto sezení ────────────────────────────────────
    session_jsonl = session_dir / "records.jsonl"
    with open(session_jsonl, "w", encoding="utf-8") as f:
        for rec in session_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ── Tiskni souhrnný report ───────────────────────────────────────────────
    print_report(log, active_configs, dataset_keys, budget_min, target_runs, session_min)
    print(f"  Globální log:  {LOG_FILE}")
    print(f"  Sezení detail: {session_jsonl}\n")


# ═════════════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark solveru — persistentní log, obnovení sezení",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Doporučený postup (dvě fáze):

  FÁZE 1 — průzkum, 5 min/run  (3 sezení × ~3h)
    Spusť 3× (různé dny / přes noc), scheduler pokračuje automaticky:
      python benchmark/runner.py --datasets CB.csv HK.csv MO.csv --budget 5 --session-time 180

    Průběžné výsledky:
      python benchmark/runner.py --report --budget 5

  FÁZE 2 — validace finalistů, 30 min/run  (3-4 sezení × 2h)
    Vezme top 4 z 5-min rankingu, ověří je s delším budgetem:
      python benchmark/runner.py --datasets CB.csv HK.csv MO.csv ^
          --budget 30 --session-time 120 --top 4 --ref-budget 5

    Finální výsledky:
      python benchmark/runner.py --report --budget 30
""",
    )
    parser.add_argument(
        "--datasets", nargs="+", metavar="CSV",
        help="Orders CSV soubory (jeden nebo více), např. data/prepared/CB/... HK/...",
    )
    parser.add_argument(
        "--budget", type=int, default=DEFAULT_BUDGET_MIN,
        metavar="MIN",
        help=f"Budget solveru v minutách per běh (default: {DEFAULT_BUDGET_MIN})",
    )
    parser.add_argument(
        "--session-time", type=int, default=DEFAULT_SESSION_MIN,
        dest="session_min", metavar="MIN",
        help=f"Max délka sezení v minutách (default: {DEFAULT_SESSION_MIN})",
    )
    parser.add_argument(
        "--target-runs", type=int, default=DEFAULT_TARGET_RUNS,
        dest="target_runs", metavar="N",
        help=f"Cílový počet běhů per (config × dataset) (default: {DEFAULT_TARGET_RUNS})",
    )
    parser.add_argument(
        "--vehicles-file", default="data/static/vehicle_types.csv",
        metavar="CSV",
        help="Cesta k vehicle_types.csv (default: data/static/vehicle_types.csv)",
    )
    parser.add_argument(
        "--top", type=int, default=0,
        metavar="N",
        help=(
            "Testuj jen top N konfigurací z --ref-budget rankingu. "
            "Použij ve fázi 2 pro 30-min validaci finalistů (default: 0 = všechny)"
        ),
    )
    parser.add_argument(
        "--ref-budget", type=int, default=5,
        dest="ref_budget_min", metavar="MIN",
        help="Budget (min) pro výběr top N konfigurací (default: 5)",
    )
    parser.add_argument(
        "--configs", default=None,
        metavar="NAMES",
        help=(
            "Explicitní výběr konfigurací oddělených čárkou, např. "
            "06_2clusters,07_4clusters,01_baseline. "
            "Má přednost před --top."
        ),
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Jen zobrazit výsledky z logu, nespouštět solver",
    )
    # Benchmark = měření výkonnosti algoritmu → zamrzlá mapa (stable),
    # aby byla měření porovnatelná napříč časem.
    add_osm_args(parser, default="stable")
    args = parser.parse_args()

    # ── Volba OSM routing instance (stable vs current) ────────────────────────
    # Mutuje S.CONFIG (importovaný modul vrp_solver_lines_v6) — runner pak při
    # každém běhu solveru použije správné URL bez dalšího zásahu.
    osm_source = resolve_osm_source(args)
    apply_osm_source(S.CONFIG, osm_source)
    if not args.report:
        # V report módu nesoláme nic, takže výpis URL by jen rušil
        print(f"[OSM] zdroj: {osm_source}"
              f"  | OSRM={S.CONFIG['osrm_urls']['driving']}"
              f"  | ORS={S.CONFIG['osrm_urls']['driving-hgv']}")

    # ── Report-only mód ───────────────────────────────────────────────────────
    if args.report:
        log = load_log()
        if args.datasets:
            orders_files = [Path(f) for f in args.datasets]
            dataset_keys = [build_dataset_key(f) for f in orders_files]
        else:
            # Automaticky seznam datasetů z logu pro daný budget
            dataset_keys = sorted(
                set(r["dataset"] for r in log if r.get("budget_min") == args.budget)
            )
        if not dataset_keys:
            print(f"[INFO] Žádná data v logu pro budget {args.budget} min.")
            sys.exit(0)
        report_configs = CONFIGS
        if args.configs:
            name_set = {s.strip() for s in args.configs.split(",") if s.strip()}
            report_configs = [c for c in CONFIGS if c["name"] in name_set]
        print_report(log, report_configs, dataset_keys, args.budget, args.target_runs, args.session_min)
        return

    # ── Běhový mód ────────────────────────────────────────────────────────────
    if not args.datasets:
        parser.error("--datasets je povinný (nebo použij --report bez --datasets pro přehled logu)")

    # Routing data se tu NEPŘESTAVUJÍ — instance musí běžet předem
    # (stable: docker start osrm-server ors-hgv; přestavba: refresh_osm.py).

    orders_files  = [Path(f) for f in args.datasets]
    vehicles_file = Path(args.vehicles_file)

    for f in orders_files:
        if not f.exists():
            print(f"[CHYBA] Orders soubor nenalezen: {f}")
            sys.exit(1)
    if not vehicles_file.exists():
        print(f"[CHYBA] Vehicles soubor nenalezen: {vehicles_file}")
        sys.exit(1)

    explicit_configs = (
        [s.strip() for s in args.configs.split(",") if s.strip()]
        if args.configs
        else None
    )

    run_session(
        orders_files     = orders_files,
        vehicles_file    = vehicles_file,
        budget_min       = args.budget,
        session_min      = args.session_min,
        target_runs      = args.target_runs,
        top_n            = args.top,
        ref_budget_min   = args.ref_budget_min,
        explicit_configs = explicit_configs,
    )


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
