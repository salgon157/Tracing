"""
fleet_budget.py — rozpočet flotily, rezervace velkých aut a rozhodnutí o porušení
=================================================================================

Čistá logika pro `plan_day.py` (žádné subprocess, žádný solver):

  1. MALÁ vs. VELKÁ auta — malá = nosnost do SMALL_MAX_KG (TYPE_01, TYPE_02);
     malá se v predikci neomezují (jejich deficit je přesně to, co měříme),
     velká se rezervují a ubírají z budgetu.
  2. REZERVACE — z P1 („přání" dep s neomezenými velkými) dostane každé depo
     rezervaci pro všechny velké typy. U typů, kde součet přání přeteče sklad,
     se přání ořeže žebříčkem podle naloženosti linek (kg). Nerezervované
     kusy = volný pool.
  3. CAPS — depo na řadě smí použít: vlastní rezervaci + volný zbytek po
     odečtení rezervací dep, která JEŠTĚ nebyla na řadě. Rezervace chrání
     jen budoucnost: jakmile depo doplánuje, jeho nevyužité kusy tečou
     do volného poolu samy.
  4. ROZHODNUTÍ — deficit malých aut (proti available − rezerva) se přepočte
     na kg přes X_NEED nejméně naložených malých linek a podle podílu na
     denních kg se určí porušení pro ostrý běh (L0 / L1+L2 / +L3 alert).
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# ── Parametry (konfigurovatelné, viz plán „predikcí řízené plánování") ────────
SMALL_MAX_KG          = 1350   # nosnost do tohoto limitu = "malé auto"
SMALL_FLEET_RESERVE   = 1      # bezpečnostní rezerva malých aut
L3_THRESHOLD_PCT      = 3.0    # nad tolik % denních kg nestačí L1+L2
UNLIMITED_LARGE_COUNT = 8      # "neomezeno" pro P1 — víc žádné depo nechce

# Pořadí uzávěrek — v tomhle pořadí se plánuje P2 i večerní ostrý běh
DEPOT_ORDER = ["CB", "MO", "HK", "PR"]


# ═════════════════════════════════════════════════════════════════════════════
#  Flotila: čtení, malá/velká, generování souborů
# ═════════════════════════════════════════════════════════════════════════════

def load_fleet_rows(path: Path | str) -> list[dict]:
    """Řádky vozového parku (středníkový formát) — per TYP, bez expanze."""
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        required = {"type_code", "max_kg", "available_count"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(
                f"[CHYBA] {path} nemá povinné sloupce {sorted(required)} — "
                f"očekávám středníkový vozový park.")
        for row in reader:
            code = str(row.get("type_code", "")).strip()
            if not code or code.startswith("#"):
                continue
            rows.append(row)
    if not rows:
        raise ValueError(f"[CHYBA] {path} neobsahuje žádné typy vozidel.")
    return rows


def is_small(row: dict) -> bool:
    return float(row["max_kg"]) <= SMALL_MAX_KG


def small_type_codes(rows: list[dict]) -> set[str]:
    return {r["type_code"].strip() for r in rows if is_small(r)}


def available_by_type(rows: list[dict]) -> dict[str, int]:
    return {r["type_code"].strip(): int(float(r["available_count"] or 0))
            for r in rows}


def write_fleet_file(rows: list[dict], path: Path | str,
                     overrides: dict[str, int]) -> Path:
    """
    Zapíše kopii vozového parku s přepsaným `available_count` podle overrides.
    Formát (středníky, sloupce) zůstává — soubor musí projít
    load_vehicle_types_db beze změny.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";",
                                lineterminator="\n")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            code = out["type_code"].strip()
            if code in overrides:
                out["available_count"] = str(int(overrides[code]))
            writer.writerow(out)
    return path


def p1_overrides(rows: list[dict]) -> dict[str, int]:
    """P1 = velká „neomezená": max(available, UNLIMITED_LARGE_COUNT).
    Malá beze změny — plný sklad je pro jedno depo de facto neomezený."""
    out = {}
    for row in rows:
        if not is_small(row):
            code = row["type_code"].strip()
            avail = int(float(row["available_count"] or 0))
            out[code] = max(avail, UNLIMITED_LARGE_COUNT)
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Výstupy solveru: linky z lines_summary
# ═════════════════════════════════════════════════════════════════════════════

def parse_lines_summary(path: Path | str) -> list[dict]:
    """
    Linky z lines_summary.csv: [{zone, line_id, type_code, total_kg}].
    Souhrnný řádek CELKEM (bez vehicle_id) se přeskakuje. type_code se bere
    z vehicle_id (TYPE_02_01 -> TYPE_02) — jediné místo, kde je kód, ne název.
    """
    lines = []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            vehicle_id = str(row.get("vehicle_id", "")).strip()
            if not vehicle_id:
                continue
            lines.append({
                "zone":      str(row.get("zone", "")).strip(),
                "line_id":   str(row.get("line_id", "")).strip(),
                "type_code": vehicle_id.rsplit("_", 1)[0],
                "total_kg":  float(row.get("total_kg", 0) or 0),
            })
    return lines


def count_by_type(lines: list[dict]) -> dict[str, int]:
    return dict(Counter(l["type_code"] for l in lines))


# ═════════════════════════════════════════════════════════════════════════════
#  Rezervace velkých aut z P1
# ═════════════════════════════════════════════════════════════════════════════

def allocate_reservations(lines_by_depot: dict[str, list[dict]],
                          large_available: dict[str, int]) -> dict:
    """
    Z P1 linek udělá rezervace velkých typů per depo.

    Typ, kde se přání vejdou do skladu, projde celý. Přetečený typ se ořeže
    žebříčkem: linky s tím typem přes všechna depa se seřadí podle kg
    (deterministicky: kg desc, pak depo, pak line_id) a kusy dostanou depa
    s nejnaloženějšími linkami.

    Vrací {"reservations": {depo: {typ: n}}, "wishes": {depo: {typ: n}},
           "free_pool": {typ: n}, "truncated": [{type, wanted, available}]}
    """
    reservations: dict[str, dict[str, int]] = {d: {} for d in lines_by_depot}
    wishes = {d: count_by_type([l for l in lines if l["type_code"] in large_available])
              for d, lines in lines_by_depot.items()}
    truncated = []

    for type_code, avail in large_available.items():
        candidates = [
            (l["total_kg"], depot, l["line_id"])
            for depot, lines in lines_by_depot.items()
            for l in lines if l["type_code"] == type_code
        ]
        if not candidates:
            continue
        if len(candidates) > avail:
            truncated.append({"type": type_code,
                              "wanted": len(candidates), "available": avail})
            candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
            candidates = candidates[:avail]
        for _, depot, _ in candidates:
            reservations[depot][type_code] = \
                reservations[depot].get(type_code, 0) + 1

    free_pool = {
        t: avail - sum(res.get(t, 0) for res in reservations.values())
        for t, avail in large_available.items()
    }
    return {"reservations": reservations, "wishes": wishes,
            "free_pool": free_pool, "truncated": truncated}


# ═════════════════════════════════════════════════════════════════════════════
#  Budget a caps
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class FleetBudget:
    """Zbývající kusy per typ. Sleduje jen to, co se mu dá — v predikci
    se odečítají jen velká auta, večer všechna."""
    remaining: dict[str, int]

    @classmethod
    def from_fleet(cls, rows: list[dict]) -> "FleetBudget":
        return cls(remaining=available_by_type(rows))

    def consume(self, used: dict[str, int], *, context: str = "") -> None:
        for type_code, count in used.items():
            if type_code not in self.remaining:
                raise ValueError(
                    f"[CHYBA] Budget nezná typ {type_code}{' (' + context + ')' if context else ''}.")
            self.remaining[type_code] -= count
            if self.remaining[type_code] < 0:
                raise ValueError(
                    f"[CHYBA] Budget typu {type_code} přetekl do mínusu "
                    f"({self.remaining[type_code]}){' po ' + context if context else ''} "
                    f"— plán použil víc aut, než bylo povoleno.")

    def save(self, path: Path | str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.remaining, ensure_ascii=False, indent=2),
            encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> "FleetBudget":
        return cls(remaining=json.loads(Path(path).read_text(encoding="utf-8")))


def caps_for_depot(depot: str, depot_order: list[str],
                   budget: FleetBudget,
                   reservations: dict[str, dict[str, int]],
                   small_codes: set[str],
                   small_full: dict[str, int]) -> dict[str, int]:
    """
    Kolik čeho smí depo na řadě použít.

    Velká: fyzický zbytek − rezervace dep, která JEŠTĚ nebyla na řadě.
    Vlastní rezervace je uvnitř zbytku (dřívější depa na ni nesměla sáhnout),
    a jakmile tohle depo doplánuje, přestává existovat — nevyužité kusy
    zůstanou ve zbytku pro další.

    Malá: v predikci vždy plný sklad (neomezená — deficit se MĚŘÍ, nemaskuje).
    """
    idx = depot_order.index(depot)
    future = depot_order[idx + 1:]
    caps: dict[str, int] = {}
    for type_code, remaining in budget.remaining.items():
        if type_code in small_codes:
            caps[type_code] = small_full.get(type_code, remaining)
        else:
            future_res = sum(reservations.get(f, {}).get(type_code, 0)
                             for f in future)
            caps[type_code] = max(0, remaining - future_res)
    return caps


# ═════════════════════════════════════════════════════════════════════════════
#  Rozhodnutí o porušení
# ═════════════════════════════════════════════════════════════════════════════

def decide_level(small_lines: list[dict], small_available: int, day_kg: float,
                 *, reserve: int = SMALL_FLEET_RESERVE,
                 threshold_pct: float = L3_THRESHOLD_PCT) -> dict:
    """
    Z P2 malých linek určí porušení pro ostrý běh.

    deficit = počet malých linek − (available − rezerva). Kladný deficit se
    přepočte na kg: X_NEED nejméně naložených malých linek = odhad toho, co
    nezavezeme. Do threshold_pct % denních kg stačí L1+L2 (103 % + dvojlinky),
    nad to navíc hlásíme potřebu L3 (kamiony/rampa — zatím jen alert).
    """
    need = len(small_lines)
    usable = small_available - reserve
    deficit = need - usable

    decision = {
        "small_need": need,
        "small_available": small_available,
        "reserve": reserve,
        "usable": usable,
        "deficit": max(0, deficit),
        "x_need": 0,
        "missing_kg": 0.0,
        "day_kg": round(day_kg, 1),
        "missing_pct": 0.0,
        "level": 0,
        "dvojlinky": False,
        "l3_needed": False,
    }
    if deficit <= 0:
        return decision

    least_loaded = sorted(small_lines, key=lambda l: (l["total_kg"],
                                                      l["zone"], l["line_id"]))
    x_need_lines = least_loaded[:deficit]
    missing_kg = sum(l["total_kg"] for l in x_need_lines)
    missing_pct = (missing_kg / day_kg * 100) if day_kg > 0 else 100.0

    decision.update({
        "x_need": deficit,
        "missing_kg": round(missing_kg, 1),
        "missing_pct": round(missing_pct, 2),
        "level": 1,
        "dvojlinky": True,
        "l3_needed": missing_pct > threshold_pct,
        "x_need_lines": [{"zone": l["zone"], "line_id": l["line_id"],
                          "total_kg": l["total_kg"]} for l in x_need_lines],
    })
    return decision


def solver_flags_for_level(decision: dict) -> dict:
    """Přepínače solveru pro večerní běh — ať je decision přímo vykonatelné."""
    if decision["level"] == 0:
        return {"capacity_multiplier": 1.0, "double_runs": False}
    return {"capacity_multiplier": 1.03, "double_runs": True}
