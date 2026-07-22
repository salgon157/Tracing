"""
Run benchmark variants for the combined all-depots VRP solver.

Typical usage:
  python benchmark_all_depots_solver_v6.py 5 --date 2026-04-29
  python benchmark_all_depots_solver_v6.py 30 --cluster-factors 0.75,1.0,1.25

Default matrix:
  3 cluster factors x 2 budget profiles = 6 solver runs
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path("data")
PREPARED_DIR = DATA_DIR / "prepared"
RESULTS_DIR = DATA_DIR / "results"
SOLVER_SCRIPT = SCRIPT_DIR / "vrp_solver_lines_all_depots_v6.py"
DEFAULT_DEPOTS = ("CB", "MO", "HK", "PR")

BUDGET_PROFILES = {
    "combined_lns": {
        "ratios": (0.35, 0.25, 0.40),
        "description": "combined solver default with cross-cluster LNS",
    },
    "normal_no_lns": {
        "ratios": (0.40, 0.00, 0.60),
        "description": "single-zone solver style without Phase D LNS",
    },
}

PRESETS = {
    "tomorrow_4h": {
        "budget_min": 60.0,
        "cluster_factors": (0.75, 0.90, 1.00, 1.10),
        "budget_profiles": ("normal_no_lns",),
        "description": "4 x 60 min around the current best no-LNS cluster range",
    },
    "clusters_8_9_10_90min": {
        "budget_min": 90.0,
        "cluster_factors": (0.80, 0.90, 1.00),
        "budget_profiles": ("normal_no_lns",),
        "description": "3 x 90 min no-LNS validation for 8/9/10 clusters",
    },
    "clusters_8_9_10_120min_more_seeds": {
        "budget_min": 120.0,
        "cluster_factors": (0.80, 0.90, 1.00),
        "budget_profiles": ("normal_no_lns",),
        "seed_restarts": 5,
        "description": "3 x 120 min no-LNS validation for 8/9/10 clusters with more seed restarts",
    },
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


def parse_factors(value: str) -> list[float]:
    try:
        factors = [float(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Cluster factors must be numbers.") from exc
    if not factors:
        raise argparse.ArgumentTypeError("At least one cluster factor is required.")
    if any(factor <= 0 for factor in factors):
        raise argparse.ArgumentTypeError("Cluster factors must be positive.")
    return factors


def parse_profile_names(value: str) -> list[str]:
    names = [part.strip() for part in value.split(",") if part.strip()]
    if not names:
        raise argparse.ArgumentTypeError("At least one budget profile is required.")
    unknown = [name for name in names if name not in BUDGET_PROFILES]
    if unknown:
        raise argparse.ArgumentTypeError(
            "Unknown budget profile(s): "
            + ", ".join(unknown)
            + ". Available: "
            + ", ".join(sorted(BUDGET_PROFILES))
        )
    return names


def _prepared_dates_for_depot(depot: str) -> set[str]:
    depot_dir = PREPARED_DIR / depot
    if not depot_dir.exists():
        return set()

    pattern = re.compile(rf"orders_{re.escape(depot)}_(\d{{4}}-\d{{2}}-\d{{2}})\.csv$")
    dates: set[str] = set()
    for path in depot_dir.glob(f"orders_{depot}_*.csv"):
        match = pattern.match(path.name)
        if match:
            dates.add(match.group(1))
    return dates


def latest_common_date(depots: list[str]) -> str:
    common_dates: set[str] | None = None
    for depot in depots:
        dates = _prepared_dates_for_depot(depot)
        common_dates = dates if common_dates is None else common_dates & dates

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
        if path.exists():
            files[depot] = path
        else:
            missing.append(path)
    if missing:
        raise SystemExit(
            "[ERROR] Missing prepared order files:\n"
            + "\n".join(f"  {path}" for path in missing)
        )
    return files


def count_csv_rows(path: Path) -> int:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return max(0, sum(1 for _ in handle) - 1)


def count_available_vehicles(vehicle_types_file: Path) -> int:
    total = 0
    with open(vehicle_types_file, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            type_code = str(row.get("type_code", "")).strip()
            if not type_code or type_code.startswith("#"):
                continue
            try:
                total += int(float(row.get("available_count", 0) or 0))
            except ValueError:
                continue
    if total <= 0:
        raise SystemExit(f"[ERROR] No available vehicles found in {vehicle_types_file}.")
    return total


def auto_clusters(n_orders: int, n_vehicles: int) -> int:
    return max(8, math.ceil(n_orders / 75), math.ceil(n_vehicles / 10))


def cluster_variant_count(base_clusters: int, factor: float, n_orders: int) -> int:
    return max(1, min(n_orders, math.ceil(base_clusters * factor)))


def safe_factor_label(factor: float) -> str:
    return f"c{factor:.2f}".replace(".", "p").replace("-", "m")


def safe_budget_label(minutes: float) -> str:
    if abs(minutes - round(minutes)) < 1e-9:
        return f"{int(round(minutes))}min"
    return f"{minutes:.2f}".rstrip("0").rstrip(".").replace(".", "p") + "min"


def ratio_arg(ratios: tuple[float, float, float]) -> str:
    return ",".join(f"{ratio:.4f}".rstrip("0").rstrip(".") for ratio in ratios)


def build_variants(
    cluster_factors: list[float],
    budget_profile_names: list[str],
    base_clusters: int,
    n_orders: int,
) -> list[dict]:
    variants = []
    for factor in cluster_factors:
        clusters = cluster_variant_count(base_clusters, factor, n_orders)
        cluster_label = safe_factor_label(factor)
        for profile_name in budget_profile_names:
            profile = BUDGET_PROFILES[profile_name]
            variants.append({
                "variant_id": f"{cluster_label}_{profile_name}",
                "cluster_factor": factor,
                "clusters": clusters,
                "budget_profile": profile_name,
                "budget_ratios": profile["ratios"],
                "budget_profile_description": profile["description"],
            })
    return variants


def make_session_dir(args: argparse.Namespace, date_str: str) -> Path:
    if args.session_dir:
        return Path(args.session_dir)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"session_{stamp}_{date_str}_{safe_budget_label(args.budget_min)}"
    return RESULTS_DIR / "ALL_BENCHMARK" / name


def load_zone_summary(output_dir: Path) -> dict:
    path = output_dir / "zone_summary.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def run_solver(command: list[str], log_path: Path) -> tuple[int, float]:
    start = time.time()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    with open(log_path, "w", encoding="utf-8", newline="") as log_file:
        log_file.write("$ " + " ".join(command) + "\n\n")
        process = subprocess.Popen(
            command,
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            log_file.write(line)
        return_code = process.wait()

    elapsed_min = (time.time() - start) / 60
    return return_code, elapsed_min


def command_for_variant(args: argparse.Namespace, variant: dict, output_dir: Path) -> list[str]:
    command = [
        sys.executable,
        str(SOLVER_SCRIPT),
        "--date",
        args.date,
        "--depots",
        ",".join(args.depots),
        "--vehicle-types-file",
        args.vehicle_types_file,
        "--output-dir",
        str(output_dir),
        "--budget-min",
        str(args.budget_min),
        "--clusters",
        str(variant["clusters"]),
        "--seed-restarts",
        str(args.seed_restarts),
        "--budget-ratios",
        ratio_arg(variant["budget_ratios"]),
    ]
    if args.workers is not None:
        command.extend(["--workers", str(args.workers)])
    if args.force_matrix:
        command.append("--force-matrix")
    # Benchmark měří VÝKONNOST ALGORITMU → zamrzlá mapa, aby byla měření
    # porovnatelná napříč časem. Proto se zdroj předává explicitně.
    command += ["--osm-source", args.osm_source]
    if args.dry_run:
        command.append("--dry-run")
    if args.run_startup_tests:
        command.append("--run-startup-tests")
    return command


def record_from_run(
    args: argparse.Namespace,
    variant: dict,
    output_dir: Path,
    return_code: int,
    wall_min: float,
    command: list[str],
) -> dict:
    summary = load_zone_summary(output_dir)
    return {
        "variant_id": variant["variant_id"],
        "status": "ok" if return_code == 0 else "failed",
        "return_code": return_code,
        "date": args.date,
        "budget_min": args.budget_min,
        "cluster_factor": variant["cluster_factor"],
        "clusters": variant["clusters"],
        "budget_profile": variant["budget_profile"],
        "budget_ratios": ratio_arg(variant["budget_ratios"]),
        "seed_restarts": args.seed_restarts,
        "workers": args.workers if args.workers is not None else "auto_cpu_minus_2",
        "lines_count": summary.get("lines_count", ""),
        "total_cost_kc": summary.get("total_cost_kc", ""),
        "total_km": summary.get("total_km", ""),
        "total_hours": summary.get("total_hours", ""),
        "solver_elapsed_min": summary.get("elapsed_min", ""),
        "wall_elapsed_min": round(wall_min, 2),
        "output_dir": str(output_dir),
        "command": " ".join(command),
    }


def write_records(session_dir: Path, records: list[dict]) -> None:
    csv_path = session_dir / "benchmark_runs.csv"
    jsonl_path = session_dir / "benchmark_runs.jsonl"

    fieldnames = [
        "variant_id",
        "status",
        "return_code",
        "date",
        "budget_min",
        "cluster_factor",
        "clusters",
        "budget_profile",
        "budget_ratios",
        "seed_restarts",
        "workers",
        "lines_count",
        "total_cost_kc",
        "total_km",
        "total_hours",
        "solver_elapsed_min",
        "wall_elapsed_min",
        "output_dir",
        "command",
    ]

    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)

    with open(jsonl_path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def print_plan(
    variants: list[dict],
    n_orders: int,
    n_vehicles: int,
    base_clusters: int,
    session_dir: Path,
    workers: str,
    pause_sec: float,
    seed_restarts: int,
) -> None:
    print("=" * 72)
    print("ALL DEPOTS BENCHMARK PLAN")
    print("=" * 72)
    print(f"orders:          {n_orders}")
    print(f"vehicles:        {n_vehicles}")
    print(f"base clusters:   {base_clusters} = max(8, ceil(orders/75), ceil(vehicles/10))")
    print(f"workers/run:     {workers}")
    print(f"seed_restarts:   {seed_restarts}")
    print(f"pause/run:       {pause_sec:g} sec")
    print(f"runs:            {len(variants)}")
    print(f"session_dir:     {session_dir}")
    print("-" * 72)
    for idx, variant in enumerate(variants, start=1):
        print(
            f"{idx:02d}. {variant['variant_id']:<24} "
            f"clusters={variant['clusters']:<3} "
            f"factor={variant['cluster_factor']:<5g} "
            f"profile={variant['budget_profile']:<14} "
            f"ratios={ratio_arg(variant['budget_ratios'])}"
        )
    print("=" * 72)


def print_ranking(records: list[dict]) -> None:
    successful = [
        record for record in records
        if record["status"] == "ok" and record.get("total_cost_kc") not in ("", None)
    ]
    if not successful:
        print("\nNo successful runs with zone_summary.json were found.")
        return

    successful.sort(key=lambda record: float(record["total_cost_kc"]))
    print("\nBest variants by total_cost_kc:")
    print("-" * 72)
    for rank, record in enumerate(successful, start=1):
        print(
            f"{rank:02d}. {record['variant_id']:<24} "
            f"{float(record['total_cost_kc']):>12,.0f} Kc | "
            f"{record['lines_count']} lines | "
            f"{record['total_km']} km | "
            f"{record['output_dir']}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run cluster/budget benchmarks for the combined all-depots solver."
    )
    parser.add_argument(
        "budget_min",
        type=float,
        nargs="?",
        default=None,
        help="Total budget in minutes for every solver run.",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default="",
        help="Named benchmark preset. Example: tomorrow_4h",
    )
    parser.add_argument(
        "--date",
        default="",
        help="Delivery date YYYY-MM-DD. If omitted, uses latest date common to selected depots.",
    )
    parser.add_argument(
        "--depots",
        type=parse_depots,
        default=list(DEFAULT_DEPOTS),
        help="Comma-separated depots. Default: CB,MO,HK,PR",
    )
    parser.add_argument(
        "--vehicle-types-file",
        default="data/static/vehicle_types.csv",
        help="Vehicle types CSV. Uses available_count for planning.",
    )
    parser.add_argument(
        "--cluster-factors",
        type=parse_factors,
        default=parse_factors("0.75,1.0,1.25"),
        help="Comma-separated factors applied to base auto clusters. Default: 0.75,1.0,1.25",
    )
    parser.add_argument(
        "--budget-profiles",
        type=parse_profile_names,
        default=parse_profile_names("combined_lns,normal_no_lns"),
        help="Comma-separated profile names. Default: combined_lns,normal_no_lns",
    )
    parser.add_argument(
        "--seed-restarts",
        type=int,
        default=3,
        help="Passed to combined solver --seed-restarts. Default: 3",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel worker processes passed to solver. Default: solver auto cpu_count() - 2.",
    )
    parser.add_argument(
        "--pause-sec",
        type=float,
        default=0.0,
        help="Pause between variants. Default: 0",
    )
    parser.add_argument(
        "--session-dir",
        default="",
        help="Directory for benchmark outputs. Default: data/results/ALL_BENCHMARK/session_*",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Print planned variants and write no solver outputs.",
    )
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated variant_id filter, e.g. c0p75_combined_lns,c1p00_normal_no_lns.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pass --dry-run to the solver for every variant.",
    )
    parser.add_argument(
        "--force-matrix",
        action="store_true",
        help="Pass --force-matrix to the solver.",
    )
    parser.add_argument(
        "--osm-source",
        action="store_true",
        choices=["stable", "current"], default="stable",
        help="Routing instance for the solver (default: stable = frozen map, "
             "so performance measurements stay comparable over time).",
    )
    parser.add_argument(
        "--run-startup-tests",
        action="store_true",
        help="Run solver startup pytest suite before every solver run.",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop the benchmark after the first failed variant.",
    )
    return parser.parse_args()


def apply_preset(args: argparse.Namespace) -> None:
    if not args.preset:
        if args.budget_min is None:
            raise SystemExit("[ERROR] budget_min is required unless --preset is used.")
        return

    preset = PRESETS[args.preset]
    if args.budget_min is None:
        args.budget_min = preset["budget_min"]
    args.cluster_factors = list(preset["cluster_factors"])
    args.budget_profiles = list(preset["budget_profiles"])
    if "seed_restarts" in preset:
        args.seed_restarts = int(preset["seed_restarts"])


def main() -> None:
    configure_stdio()
    os.chdir(SCRIPT_DIR)
    args = parse_args()
    apply_preset(args)
    args.date = args.date or latest_common_date(args.depots)

    order_files = resolve_order_files(args.depots, args.date)
    n_orders = sum(count_csv_rows(path) for path in order_files.values())
    n_vehicles = count_available_vehicles(Path(args.vehicle_types_file))
    base_clusters = auto_clusters(n_orders, n_vehicles)
    variants = build_variants(
        args.cluster_factors,
        args.budget_profiles,
        base_clusters,
        n_orders,
    )
    if args.only:
        wanted = {part.strip() for part in args.only.split(",") if part.strip()}
        variants = [variant for variant in variants if variant["variant_id"] in wanted]
        missing = wanted - {variant["variant_id"] for variant in variants}
        if missing:
            raise SystemExit(
                "[ERROR] Unknown --only variant_id(s): "
                + ", ".join(sorted(missing))
            )
        if not variants:
            raise SystemExit("[ERROR] --only filter left no variants to run.")

    session_dir = make_session_dir(args, args.date)
    worker_count = (
        str(args.workers)
        if args.workers is not None
        else f"auto_cpu_minus_2 ({max(1, (os.cpu_count() or 1) - 2)})"
    )
    print_plan(
        variants,
        n_orders,
        n_vehicles,
        base_clusters,
        session_dir,
        worker_count,
        args.pause_sec,
        args.seed_restarts,
    )
    if args.list_only:
        return

    session_dir.mkdir(parents=True, exist_ok=True)
    plan_path = session_dir / "benchmark_plan.json"
    with open(plan_path, "w", encoding="utf-8") as handle:
        json.dump({
            "preset": args.preset,
            "date": args.date,
            "budget_min": args.budget_min,
            "depots": args.depots,
            "workers": worker_count,
            "pause_sec": args.pause_sec,
            "orders_count": n_orders,
            "vehicles_count": n_vehicles,
            "base_clusters": base_clusters,
            "variants": variants,
        }, handle, ensure_ascii=False, indent=2)

    records: list[dict] = []
    for index, variant in enumerate(variants, start=1):
        output_dir = session_dir / variant["variant_id"]
        log_path = output_dir / "solver.log"
        command = command_for_variant(args, variant, output_dir)

        print("\n" + "#" * 72)
        print(f"[{index}/{len(variants)}] Running {variant['variant_id']}")
        print("#" * 72)
        return_code, wall_min = run_solver(command, log_path)

        record = record_from_run(args, variant, output_dir, return_code, wall_min, command)
        records.append(record)
        write_records(session_dir, records)

        if return_code != 0:
            print(f"\n[WARN] Variant failed: {variant['variant_id']} (return_code={return_code})")
            if args.stop_on_failure:
                break
        if index < len(variants) and args.pause_sec > 0:
            print(f"\nPausing {args.pause_sec:g} sec before next variant...")
            time.sleep(args.pause_sec)

    write_records(session_dir, records)
    print_ranking(records)
    print(f"\nBenchmark summary saved to: {session_dir / 'benchmark_runs.csv'}")


if __name__ == "__main__":
    main()
