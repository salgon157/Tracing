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

Pravidla výběru (z diskuse 14. 8. 2026):
  - jen lokace s rampou (ramp == 1); bez rampy kamion vyložit neumí
  - co nejméně objednávek s co nejvíc kg; blízkost lokací se cení
  - cíl kg = missing_kg + max(10 % missing_kg, 500 kg)
  - když rampové lokace tolik nedají, vezme se co je — zbytek řeší L1+L2
  - okna L3 lokací neplatí: pevně 04:00–20:00
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
    # Penalizace vzdálenosti při výběru: score = kg / (1 + km × koef).
    # 0.02 → lokace 50 km od klastru má poloviční score než stejná vedle.
    "proximity_koef":     0.02,
    "budget_min":         10.0,      # solver budget večerní L3 trasy
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
    [{location_code, depot, kg, lat, lon, customer_name,
      orders: [{order_number, kg}]}]
    """
    by_loc: dict[tuple[str, str], dict] = {}
    for depot in depots:
        path = Path(prepared_root) / depot / f"orders_{depot}_{date_str}.csv"
        if not path.exists():
            raise FileNotFoundError(f"[CHYBA] Chybí {path} — L3 výběr "
                                    f"potřebuje prepared soubor depa {depot}.")
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("ramp", "0") != "1":
                    continue
                if row.get("predicted", "0") != "0":
                    continue        # dopredikovaná — kamion NIKDY
                key = (depot, row["location_code"])
                loc = by_loc.setdefault(key, {
                    "location_code": row["location_code"],
                    "depot": depot,
                    "customer_name": row.get("customer_name", ""),
                    "kg": 0.0,
                    "lat": float(row["lat"]),
                    "lon": float(row["lon"]),
                    "orders": [],
                })
                kg = float(row["weight_kg"])
                loc["kg"] += kg
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
            "exhausted": selected_kg < target_kg}


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
        "params": {k: L3_CONFIG[k] for k in
                   ("target_buffer_pct", "target_buffer_min_kg",
                    "proximity_koef", "window_from", "window_to")},
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
