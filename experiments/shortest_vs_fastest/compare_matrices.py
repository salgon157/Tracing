r"""
FÁZE 1 — porovnání matic: nejrychlejší vs. nejkratší trasa. BEZ SOLVERU.

Odpovídá na hlavní otázku levně a jednoznačně:
  "Kdybychom jeli STEJNÉ trasy, ale nejkratšími cestami, kolik km ušetříme
   a kolik času to stojí?"

Tím se izoluje efekt ROUTOVACÍHO ENGINE od chování solveru (ten by trasy
přeskládal a smíchal by nám dva efekty dohromady).

Použití (z experiments/shortest_vs_fastest/):
  python compare_matrices.py \
      --orders-file ..\..\data\prepared\CB\orders_CB_2026-07-17.csv \
      --plan-dir    ..\..\data\results\CB\2026-07-17_ceny

Nic nezapisuje mimo results/ v této složce. Produkce se nedotýká.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import requests

# Sklad (Štoky) — musí sedět s DEPOT ve vrp_solver_lines_v6.py, proto ho
# načteme přímo odtud (žádná duplicita konstanty).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from vrp_solver_lines_v6 import DEPOT  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"


def fetch_matrix(base_url: str, locations: list[tuple[float, float]]) -> tuple:
    """RAW distance+duration matice z OSRM. Bez bufferů a sanitizace —
    chceme čistá čísla z engine, ne to, co si solver upraví."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in locations)
    url = f"{base_url}/table/v1/driving/{coords}"
    r = requests.get(url, params={"annotations": "duration,distance"}, timeout=900)
    r.raise_for_status()
    data = r.json()
    dist_km = np.array(data["distances"], dtype=float) / 1000.0
    dur_min = np.array(data["durations"], dtype=float) / 60.0
    return dist_km, dur_min


def load_locations(orders_file: Path) -> tuple[list, list[dict]]:
    """locations[0] = sklad, dál objednávky v pořadí souboru (jako solver)."""
    orders = list(csv.DictReader(open(orders_file, encoding="utf-8")))
    locs = [(DEPOT["lat"], DEPOT["lon"])]
    locs += [(float(o["lat"]), float(o["lon"])) for o in orders]
    return locs, orders


def plan_legs(plan_dir: Path, orders: list[dict]) -> list[tuple[int, int]]:
    """Úseky (from_node, to_node) podle pořadí zastávek v existujícím plánu.
    Uzel objednávky = její index v orders + 1 (0 je sklad)."""
    stops_file = plan_dir / "lines_stops.csv"
    if not stops_file.exists():
        return []
    node_of = {o["order_number"]: i + 1 for i, o in enumerate(orders)}

    by_line: dict[str, list[tuple[int, int]]] = {}
    for s in csv.DictReader(open(stops_file, encoding="utf-8-sig")):
        oid = (s.get("order_id") or "").strip()
        if oid in ("", "—", "-") or oid not in node_of:
            continue
        seq = int(s.get("stop_seq") or 0)
        by_line.setdefault(s["line_id"], []).append((seq, node_of[oid]))

    legs: list[tuple[int, int]] = []
    for _line, seq_nodes in by_line.items():
        nodes = [n for _s, n in sorted(seq_nodes)]
        if not nodes:
            continue
        prev = 0                       # výjezd ze skladu
        for n in nodes:
            legs.append((prev, n))
            prev = n
        legs.append((prev, 0))         # návrat do skladu
    return legs


def pair_stats(dist: np.ndarray, dur: np.ndarray) -> dict:
    """Statistika přes všechny mimodiagonální páry."""
    n = dist.shape[0]
    mask = ~np.eye(n, dtype=bool) & np.isfinite(dist) & np.isfinite(dur)
    return {
        "pairs": int(mask.sum()),
        "dist_km_sum": float(dist[mask].sum()),
        "dur_min_sum": float(dur[mask].sum()),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Fáze 1: matice nejrychlejší vs. nejkratší.")
    p.add_argument("--orders-file", required=True, help="prepared orders CSV")
    p.add_argument("--plan-dir", default=None,
                   help="složka existujícího plánu (lines_stops.csv) — porovná km "
                        "po STEJNÝCH trasách; bez ní jen statistika celé matice")
    p.add_argument("--fast-url", default="http://localhost:5000", help="nejrychlejší (stable)")
    p.add_argument("--short-url", default="http://localhost:5002", help="nejkratší (experiment)")
    args = p.parse_args()

    orders_file = Path(args.orders_file)
    locs, orders = load_locations(orders_file)
    print(f"Bodů: {len(locs)} (sklad + {len(orders)} objednávek)")

    print(f"\nStahuji matici NEJRYCHLEJŠÍ ({args.fast_url})...")
    fast_d, fast_t = fetch_matrix(args.fast_url, locs)
    print(f"Stahuji matici NEJKRATŠÍ  ({args.short_url})...")
    short_d, short_t = fetch_matrix(args.short_url, locs)

    if fast_d.shape != short_d.shape:
        sys.exit("[CHYBA] Matice mají různý rozměr — nesrovnatelné.")

    out: dict = {
        "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "orders_file": str(orders_file),
        "nodes": len(locs),
        "fast_url": args.fast_url,
        "short_url": args.short_url,
    }

    # ── A) celá matice (obecný indikátor) ────────────────────────────────
    fs, ss = pair_stats(fast_d, fast_t), pair_stats(short_d, short_t)
    d_pct = (ss["dist_km_sum"] / fs["dist_km_sum"] - 1) * 100 if fs["dist_km_sum"] else 0
    t_pct = (ss["dur_min_sum"] / fs["dur_min_sum"] - 1) * 100 if fs["dur_min_sum"] else 0
    out["all_pairs"] = {"fastest": fs, "shortest": ss,
                        "dist_delta_pct": d_pct, "dur_delta_pct": t_pct}

    print("\n" + "=" * 70)
    print("A) VŠECHNY PÁRY (obecný indikátor)")
    print("=" * 70)
    print(f"  km celkem:  nejrychlejší {fs['dist_km_sum']:>12,.0f}  →  "
          f"nejkratší {ss['dist_km_sum']:>12,.0f}   ({d_pct:+.2f} %)")
    print(f"  min celkem: nejrychlejší {fs['dur_min_sum']:>12,.0f}  →  "
          f"nejkratší {ss['dur_min_sum']:>12,.0f}   ({t_pct:+.2f} %)")

    # ── B) po STEJNÝCH trasách (to hlavní číslo) ─────────────────────────
    if args.plan_dir:
        legs = plan_legs(Path(args.plan_dir), orders)
        if not legs:
            print("\n[!] V --plan-dir nenalezeny použitelné trasy — sekci B přeskakuji.")
        else:
            fk = sum(fast_d[i, j] for i, j in legs)
            sk = sum(short_d[i, j] for i, j in legs)
            ft = sum(fast_t[i, j] for i, j in legs)
            st = sum(short_t[i, j] for i, j in legs)
            saved_km = fk - sk
            saved_pct = (saved_km / fk * 100) if fk else 0
            extra_h = (st - ft) / 60

            # Úspora v Kč sazbou dodávek (11 Kč/km) — km jsou 68 % ceny plánu
            out["same_routes"] = {
                "legs": len(legs),
                "fastest_km": fk, "shortest_km": sk,
                "saved_km": saved_km, "saved_pct": saved_pct,
                "fastest_h": ft / 60, "shortest_h": st / 60, "extra_h": extra_h,
                "saved_kc_at_11": saved_km * 11,
            }
            print("\n" + "=" * 70)
            print("B) STEJNÉ TRASY, JINÉ CESTY  ← hlavní číslo")
            print("=" * 70)
            print(f"  úseků: {len(legs)}")
            print(f"  km:    {fk:>9,.1f}  →  {sk:>9,.1f}   "
                  f"úspora {saved_km:>7,.1f} km ({saved_pct:+.2f} %)")
            print(f"  hod:   {ft/60:>9,.1f}  →  {st/60:>9,.1f}   "
                  f"daň    {extra_h:>7,.1f} h")
            print(f"\n  úspora při 11 Kč/km: {saved_km * 11:,.0f} Kč")
            print()
            if saved_pct < 1:
                print("  VERDIKT: < 1 % — nestojí to za přestavbu produkčního grafu.")
            elif saved_pct < 3:
                print("  VERDIKT: 1–3 % — hraniční, zvaž časovou daň výše.")
            else:
                print("  VERDIKT: > 3 % — má smysl jít do FÁZE 2 (plný běh solveru).")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_file = RESULTS_DIR / f"matrix_compare_{orders_file.stem}_{stamp}.json"
    out_file.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nzapsáno: {out_file}")


if __name__ == "__main__":
    main()
