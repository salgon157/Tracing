"""
fleet_budget.py — rozpočet flotily, rezervace velkých aut a rozhodnutí o porušení
=================================================================================

Čistá logika pro `plan_day.py` (žádné subprocess, žádný solver):

  1. MALÁ vs. VELKÁ auta — malá = nosnost do SMALL_MAX_KG (TYPE_01, TYPE_02);
     malá se v predikci neomezují (jejich deficit je přesně to, co měříme),
     velká se rezervují a ubírají z budgetu.
  2. REZERVACE — z P1 („přání" dep, kde každé plánuje SAMOSTATNĚ s celým
     skladem) dostane každé depo rezervaci pro všechny velké typy. Přetečení
     se pozná samo: když je jeden kamion a chtějí ho tři depa, vzniknou tři
     přání na jeden kus. U přetečených typů se přání ořeže žebříčkem podle
     naloženosti linek (kg). Nerezervované kusy = volný pool.
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

# ── Zdražení výjezdu (#2): když chybí malá a střední auta stojí ───────────────
# Cenový model je mezi „1 velké" a „N malých" prakticky lhostejný (změřeno
# 10.8.2026: rozdíl 5 Kč ze 73 tis.), takže solver při neomezených malých
# střední auta nenasadí. Dočasné zdražení výjezdu VŠEM typům ho donutí
# konsolidovat do větších aut — deficit malých klesne bez porušení.
MEDIUM_KG_RANGE            = (1351, 3999)  # "střední" (dnes TYPE_03/04/07)
START_COST_TRIGGER_MISSING = 3     # zdražuj až když chybí VÍC než tolik malých
MEDIUM_USAGE_TRIGGER       = 0.5   # ...a střední jedou pod 50 % dostupných
START_COST_BASE_DELTA      = 200   # chybí 4 -> +200 Kč
START_COST_STEP            = 100   # každé další chybějící malé +100 Kč
START_COST_DELTA_MAX       = 500   # strop navýšení

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


def is_medium(row: dict) -> bool:
    return MEDIUM_KG_RANGE[0] <= float(row["max_kg"]) <= MEDIUM_KG_RANGE[1]


def small_type_codes(rows: list[dict]) -> set[str]:
    return {r["type_code"].strip() for r in rows if is_small(r)}


def medium_type_codes(rows: list[dict]) -> set[str]:
    return {r["type_code"].strip() for r in rows if is_medium(r)}


def available_by_type(rows: list[dict]) -> dict[str, int]:
    return {r["type_code"].strip(): int(float(r["available_count"] or 0))
            for r in rows}


def write_fleet_file(rows: list[dict], path: Path | str,
                     overrides: dict[str, int],
                     start_cost_delta: int = 0) -> Path:
    """
    Zapíše kopii vozového parku s přepsaným `available_count` podle overrides.
    `start_cost_delta` > 0 navíc zdraží výjezd VŠEM typům (viz
    start_cost_escalation) — solver si novou cenu přečte sám, nemění se.
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
            if start_cost_delta:
                out["start_cost_kc"] = str(
                    int(float(out["start_cost_kc"]) + start_cost_delta))
            writer.writerow(out)
    return path


# Pozn.: P1 žádné override nepotřebuje — jede přímo na ostrém vozovém parku.
# Každé depo plánuje samostatně s celým skladem, takže přetečení se projeví
# samo (jeden kamion, tři depa → tři přání na jeden kus). Nafukovat počty by
# jen dovolilo přát si auta, která neexistují, a rozešlo by to prohledávaný
# prostor P1 proti P2 (= umělý rozdíl mezi fázemi).


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
                "zone":       str(row.get("zone", "")).strip(),
                "line_id":    str(row.get("line_id", "")).strip(),
                "vehicle_id": vehicle_id,
                "type_code":  vehicle_id.rsplit("_", 1)[0],
                "total_kg":   float(row.get("total_kg", 0) or 0),
                "double_run": bool(str(row.get("double_run", "")).strip()),
            })
    return lines


def count_by_type(lines: list[dict]) -> dict[str, int]:
    return dict(Counter(l["type_code"] for l in lines))


def vehicles_used_by_type(lines: list[dict]) -> dict[str, int]:
    """
    Spotřeba FYZICKÝCH vozidel per typ = počet unikátních vehicle_id.

    Dvojlinka jede pod vehicle_id fyzického auta, takže dvě linky téhož
    auta = jedno spotřebované vozidlo. Počítání po linkách (count_by_type)
    by při dvojlinkách auto odečetlo dvakrát.
    """
    ids_by_type: dict[str, set] = {}
    for line in lines:
        ids_by_type.setdefault(line["type_code"], set()).add(line["vehicle_id"])
    return {t: len(ids) for t, ids in ids_by_type.items()}


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
#  Zdražení výjezdu (#2) — trigger a delta z P1
# ═════════════════════════════════════════════════════════════════════════════

def start_cost_escalation(p1_lines_by_depot: dict[str, list[dict]],
                          fleet_rows: list[dict], *,
                          reserve: int = SMALL_FLEET_RESERVE) -> dict:
    """
    Z P1 rozhodne, jestli dočasně zdražit výjezd (platí pro P2 i večer).

    Trigger: chybí VÍC než START_COST_TRIGGER_MISSING malých aut
    (Σ malých linek P1 vs. available − rezerva) A ZÁROVEŇ střední auta
    jedou pod MEDIUM_USAGE_TRIGGER dostupných (2/3 použité → ne, 1/3 → ano).

    Delta: chybí 4 → +BASE (200), každé další +STEP (100), strop MAX (500):
    `min(MAX, BASE + STEP × (chybějící − 4))`. Pod triggerem se cena
    NIKDY nesahá (delta 0) — žádné zlevňování neexistuje.
    """
    small_codes = small_type_codes(fleet_rows)
    medium_codes = medium_type_codes(fleet_rows)
    avail = available_by_type(fleet_rows)

    small_available = sum(avail.get(t, 0) for t in small_codes)
    small_need = sum(1 for lines in p1_lines_by_depot.values()
                     for l in lines if l["type_code"] in small_codes)
    missing = small_need - (small_available - reserve)

    medium_available = sum(avail.get(t, 0) for t in medium_codes)
    medium_used = sum(
        n for lines in p1_lines_by_depot.values()
        for t, n in vehicles_used_by_type(lines).items() if t in medium_codes)
    # Bez středních aut nemá zdražení co nasadit → chovej se jako plné využití
    medium_usage = (medium_used / medium_available) if medium_available else 1.0

    triggered = (missing > START_COST_TRIGGER_MISSING
                 and medium_usage < MEDIUM_USAGE_TRIGGER)
    delta = 0
    if triggered:
        delta = min(START_COST_DELTA_MAX,
                    START_COST_BASE_DELTA
                    + START_COST_STEP * (missing - START_COST_TRIGGER_MISSING - 1))

    return {
        "delta": delta,
        "triggered": triggered,
        "missing_small": missing,
        "small_need": small_need,
        "small_available": small_available,
        "reserve": reserve,
        "medium_used": medium_used,
        "medium_available": medium_available,
        "medium_usage": round(medium_usage, 3),
    }


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


def caps_for_depot(depot: str, protected_depots: list[str],
                   budget: FleetBudget,
                   reservations: dict[str, dict[str, int]],
                   small_codes: set[str],
                   small_full: dict[str, int] | None) -> dict[str, int]:
    """
    Kolik čeho smí depo na řadě použít.

    Velká: fyzický zbytek − rezervace CHRÁNĚNÝCH dep. Chráněné je každé
    depo, které ten den JEŠTĚ neplánovalo (kdo je hotový, ví volající —
    v P2 je to zbytek sekvence, v ostrém běhu depa mimo state.planned).
    Díky tomu funguje i běh po jednotlivých depech mimo pořadí. Vlastní
    rezervace depa je uvnitř zbytku a nikdy se mu neodečítá.

    Malá: v PREDIKCI (P2) dostanou plný sklad `small_full` — neomezená,
    deficit se MĚŘÍ, nemaskuje. V OSTRÉM běhu `small_full=None`: malá jedou
    stejným vzorcem jako velká (rezervace pro ně neexistují, takže cap =
    prostě fyzický zbytek) — ubývají doopravdy.
    """
    caps: dict[str, int] = {}
    for type_code, remaining in budget.remaining.items():
        if small_full is not None and type_code in small_codes:
            caps[type_code] = small_full.get(type_code, remaining)
        else:
            protected = sum(reservations.get(d, {}).get(type_code, 0)
                            for d in protected_depots if d != depot)
            caps[type_code] = max(0, remaining - protected)
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


def escalate_flags(flags: dict) -> dict | None:
    """
    Další stupeň porušení, když depo na aktuálním nevyšlo.

    L0 → L1+L2 (103 % + dvojlinky). Z L1+L2 už není kam — L3 (kamiony/rampa)
    zatím není postavené → None = alert, člověk rozhodne.
    """
    if not flags.get("double_runs"):
        return {"capacity_multiplier": 1.03, "double_runs": True}
    return None
