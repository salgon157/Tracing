"""
driver_assignment.py — celodenní přiřazení řidičů k naplánovaným linkám
=======================================================================

SAMOSTATNÝ krok PO naplánování všech dep dne (do plan_day se nezapojuje;
spouštění vyřeší vrstva nad námi):

  python driver_assignment.py 2026-08-19
  python driver_assignment.py 2026-08-19 --label b5      # testovací běhy
  python driver_assignment.py 2026-08-19 --depots CB MO  # vědomá podmnožina
  python driver_assignment.py 2026-08-19 --force         # přes neshody registru

Vstupy (od 19. 8. 2026 — nový export z ESO):
  data/ridici/aktivni/vehicles-active-*.csv   registr AUTO+ŘIDIČ (právě jeden
                                              soubor; 1 řádek = 1 auto s jeho
                                              řidičem, vč. max_kg a type_code;
                                              PII — složka gitignored)
  data/static/vehicle_types-YYYYMMDD.csv      vozový park dne — kontrola, že
                                              type_code + max_kg registru sedí
                                              (tentýž den, táž DB), počty aut;
                                              bez type_code v registru se kód
                                              odvodí z (type_name, max_kg)
  data/historie_ridici/*.csv                  historie ŘIDIČ × ADRESA (počet
                                              závozů; právě jeden soubor; PII)
  data/results/{DEPO}/{DATUM}/                lines_summary.csv + lines_stops.csv
                                              všech dep dne (+ zóna L3)
  data/prepared/{DEPO}/orders_{DEPO}_{DATUM}.csv  (volitelně) order -> id adresy
                                              (eso_col7 = id_subj_adr historie)

Výstupy:
  data/results/driver_assignment/{DATUM}/driver_plan_{DATUM}.csv  (celý den)
  data/results/{DEPO}/{DATUM}/driver_plan_{DEPO}_{DATUM}.csv      (per depo)
  data/results/driver_assignment/{DATUM}/summary.json

Model: JEDNA přiřazovací úloha za celý den — všechny linky všech dep ×
řidiči. Globální optimum najde maďarský algoritmus; CB si tak „nevyžere"
řidiče, kteří se víc hodí na HK linky.

HARD (zakázaná buňka): den v týdnu nesedí (dny_pouzitelnosti) / auto není
k dispozici (dostupnost_od ≤ den závozu ≤ dostupnost_do) / auto není
správného TYPE. Řidič jede max jednu linku denně. Dvojlinka (2 linky téhož
vozidla) = jedna jednotka.

TIER (nad soft skóre): NAŠE auta (km_plan_mes = km_plan_rok = 0 — nemají
co plnit) jedou AŽ KDYŽ na linky daného typu nestačí SMLUVNÍ auta.
Nikdy se ale nepřekročí typ — na linku malého auta nejede kamion.

SOFT (skóre 0–1 × váha z CONFIG, viz jednotlivé funkce):
  plneni_planu — kdo zaostává za poměrnou částí plánu km; roční plán váží
                 víc než měsíční (plan_year_share), oba se berou v potaz;
                 bez dat = neutrální
  dojezd       — dlouhé linky vzdáleným řidičům (pořadové párování)
  kvalita_tightness — Rychlý na linky s napjatými okny; tightness na
                 konci linky váží víc než na začátku
  familiarity  — POŘADÍ řidiče mezi všemi aktivními podle počtu závozů na
                 adresu (kdo tam jezdí nejvíc = 1, kdo nikdy = dole; shodné
                 počty sdílejí pořadí), zprůměrováno přes zastávky linky.
                 Adresa, kam nejezdil nikdo, je pro všechny neutrální (0,5).
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path

from fleet_budget import DEPOT_ORDER

# ── Konfigurace (váhy a parametry — ladí se tady, ne v kódu) ─────────────────
CONFIG = {
    "weights": {
        "plneni_planu":      3.0,   # priorita: smluvní km se musí plnit
        "dojezd":            1.0,
        "kvalita_tightness": 1.0,
        "familiarity":       1.0,
    },
    # Plnění plánu: kombinace ročního a měsíčního skluzu; rok váží víc.
    "plan_year_share": 0.65,
    # Naše auta (plán 0/0) až po smluvních — jako TIER nad soft skóre
    # (žádné soft skóre nepřebije smluvní auto), ne jako tvrdý zákaz.
    "own_fleet_last": True,
    # Zastávka je "tight", když rezerva do KONCE původního okna je <= tolik
    # minut (příjezd po konci okna — v toleranci +25 — je tight vždy).
    "tight_slack_min": 15,
    # Pozice na lince: zastávka na konci váží (1 + koef)x víc než na začátku.
    # 0.3 drží vlastnost: 5 tight na konci (6.5) > 5 na začátku (5),
    # ale < 7 na začátku (7).
    "tight_pos_coef": 0.3,
    # Kvalita řidiče -> "rychlost" 0..1; skóre = 1 - |tightness - rychlost|
    "quality_speed": {"Rychlý": 1.0, "Standart": 0.5, "Pomalý": 0.0},
    "ridici_dir":       "data/ridici/aktivni",
    "registry_pattern": "vehicles-active*.csv",
    "history_dir":      "data/historie_ridici",
    "history_pattern":  "*.csv",
    "results_root":     "data/results",
    "prepared_root":    "data/prepared",
}

BIG_COST = 1e9          # zakázaná buňka (hard constraint)

DAY_NAMES = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"]   # weekday() 0..6

REGISTRY_REQUIRED = [
    "id", "vehicle_code", "vehicle_name", "vehicle_comp", "driver",
    "driver_name", "vehicle_type", "vehicle_profile", "dny_pouzitelnosti",
    "dostupnost_od", "dostupnost_do", "km_aktual_mes", "km_aktual_rok",
    "km_plan_mes", "km_plan_rok", "driver_quality", "driver_km_to_depot",
    "valid_for_date",
]
# TYPE kód auta přímo z exportu (táž DB generuje i vehicle_types → stejné
# číslování téhož dne). Když chybí, odvodí se z (vehicle_type, nosnost).
TYPE_CODE_COLS = ("type_code", "vehicle_type_code")
# Nosnost auta — bez ní nejde poznat TYPE_01 (1 200 kg) od TYPE_02 (1 350 kg).
CAPACITY_COLS = ("max_kg", "nosnost", "nosnost_kg", "vehicle_max_kg")

HISTORY_REQUIRED = ["driver_code", "id_subj_adr", "adress_note", "visit_count"]


# ═════════════════════════════════════════════════════════════════════════════
#  Vozový park dne: (type_name, max_kg) -> TYPE kód + počty
# ═════════════════════════════════════════════════════════════════════════════

def load_type_map(vehicle_types_file: Path | str | None = None) -> dict:
    """
    {(type_name, max_kg int): {"type_code", "available_count", "profile"}}
    z vehicle_types-YYYYMMDD.csv (bez path: jediný soubor v data/static —
    tentýž, se kterým plánoval solver). Řídí se OBSAHEM souboru, žádná
    natvrdo psaná tabulka — přečíslování typů (TYPE_07 -> TYPE_06 19. 8.)
    tak nic nerozbije.
    """
    if vehicle_types_file is None:
        from vrp_solver_lines_v6 import find_vehicle_types_file
        vehicle_types_file = find_vehicle_types_file()
    p = Path(vehicle_types_file)
    out: dict = {}
    with open(p, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        need = {"type_code", "type_name", "max_kg", "available_count"}
        if not need.issubset(set(reader.fieldnames or [])):
            raise SystemExit(f"[CHYBA] {p} nemá sloupce {sorted(need)}")
        for row in reader:
            code = str(row.get("type_code", "")).strip()
            if not code or code.startswith("#"):
                continue
            key = (str(row["type_name"]).strip(), int(float(row["max_kg"])))
            if key in out:
                raise SystemExit(f"[CHYBA] {p}: (typ, nosnost) {key} dvakrát — "
                                 f"nejde jednoznačně mapovat auta na TYPE.")
            out[key] = {"type_code": code,
                        "available_count": int(float(row["available_count"])),
                        "profile": str(row.get("profiles", "")).strip(),
                        "valid_for_date": str(row.get("valid_for_date", "")).strip()}
    if not out:
        raise SystemExit(f"[CHYBA] {p} neobsahuje žádný typ vozidla.")
    return out


def type_map_by_code(type_map: dict) -> dict:
    """{type_code: {"type_name", "max_kg", "available_count", ...}}"""
    return {v["type_code"]: {"type_name": k[0], "max_kg": k[1], **v}
            for k, v in type_map.items()}


def map_type(typ: str, nosnost, type_map: dict) -> str:
    """(vehicle_type, max_kg) -> TYPE kód podle vozového parku dne."""
    key = (str(typ).strip(), int(float(nosnost)))
    if key not in type_map:
        raise ValueError(
            f"[CHYBA] Auto s (typ, nosnost) {key} není ve vozovém parku dne. "
            f"Známé: {sorted(type_map)}. Buď je registr z jiného dne než "
            f"vehicle_types, nebo ESO poslalo jinou nosnost.")
    return type_map[key]["type_code"]


# ═════════════════════════════════════════════════════════════════════════════
#  Registr aut+řidičů (vehicles-active-*.csv z ESO)
# ═════════════════════════════════════════════════════════════════════════════

def parse_days(spec: str) -> set[int]:
    """
    'dny_pouzitelnosti' -> množina dní (Po=0 … Ne=6).

    Lomítko dělí týdenní a víkendovou část, obě se sjednocují:
      'Po-Pá/So-Ne'      -> {0..6}
      'Po,Pá'            -> {0, 4}
      'Po,St,Pá/So-Ne'   -> {0, 2, 4, 5, 6}
      'Po/So-Ne'         -> {0, 5, 6}
    Neznámý zápis = chyba (tichá dezinterpretace by řidiče nasadila
    v den, kdy nejezdí).
    """
    days: set[int] = set()
    for part in str(spec).split("/"):
        for token in part.split(","):
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                a, b = (t.strip() for t in token.split("-", 1))
                if a not in DAY_NAMES or b not in DAY_NAMES:
                    raise ValueError(f"[CHYBA] Neznámý rozsah dní {token!r} v {spec!r}")
                days.update(range(DAY_NAMES.index(a), DAY_NAMES.index(b) + 1))
            else:
                if token not in DAY_NAMES:
                    raise ValueError(f"[CHYBA] Neznámý den {token!r} v {spec!r}")
                days.add(DAY_NAMES.index(token))
    if not days:
        raise ValueError(f"[CHYBA] Prázdné dny použitelnosti: {spec!r}")
    return days


def find_registry_file(ridici_dir: Path | str | None = None) -> Path:
    """Právě jeden vehicles-active*.csv v data/ridici/aktivni — víc/míň je
    chyba (stejná filozofie jako riro aktivni/: program mezi soubory
    nevybírá). Starý .xlsx registr = jasná hláška, ne tichý fallback."""
    d = Path(ridici_dir if ridici_dir is not None else CONFIG["ridici_dir"])
    files = sorted(d.glob(CONFIG["registry_pattern"])) if d.exists() else []
    if len(files) != 1:
        legacy = sorted(d.glob("*.xlsx")) if d.exists() else []
        hint = ("\n        (Nalezen .xlsx — starý formát 'Auta - Řidiči - Eso.xlsx' "
                "se od 19. 8. 2026 nepoužívá; nahraj export vehicles-active-"
                "YYYYMMDD.csv z DB.)" if legacy else "")
        raise SystemExit(
            f"[CHYBA] V {d.as_posix()}/ musí být právě jeden "
            f"{CONFIG['registry_pattern']} (registr auto+řidič z ESO); "
            f"nalezeno {len(files)}.{hint}")
    return files[0]


def _num(v) -> float | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _date(v) -> date | None:
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"[CHYBA] Nečitelné datum v registru: {s!r}")


def load_registry(path: Path, type_map: dict, strict_types: bool = True) -> list[dict]:
    """
    Řádky registru = AUTO + JEHO ŘIDIČ (1:1). Vrací per řádek:
      row_id, vehicle_code, vehicle_name, dopravce, driver (kód = klíč do
      historie), driver_name, vehicle_type, profile, max_kg, type_code,
      days, avail_from, avail_to, dojezd_km, kvalita, plan_rok, plan_mes,
      aktual_rok, aktual_mes, own_fleet (plán 0/0), valid_for_date.

    TYPE kód: sloupec type_code z exportu (autoritativní — táž DB čísluje
    i vehicle_types), zkontrolovaný proti vozovému parku dne (kód existuje,
    nosnost i typ sedí). Bez sloupce se odvodí z (vehicle_type, max_kg).
    Neshoda = registr a vehicle_types nejsou z téhož dne: strict → tvrdá
    chyba, jinak (--force) varování a kód z registru se použije tak, jak je.
    """
    with open(path, encoding="utf-8-sig") as f:
        sample = f.read(4096)
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=delim)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in REGISTRY_REQUIRED if c not in header]
        if missing:
            raise SystemExit(f"[CHYBA] Registru {path.name} chybí sloupce: "
                             f"{missing} — jiný formát exportu z ESO?")
        cap_col = next((c for c in CAPACITY_COLS if c in header), None)
        code_col = next((c for c in TYPE_CODE_COLS if c in header), None)
        if cap_col is None and code_col is None:
            raise SystemExit(
                f"[CHYBA] Registr {path.name} nenese ani TYPE kód "
                f"({list(TYPE_CODE_COLS)}) ani NOSNOST auta "
                f"({list(CAPACITY_COLS)}). Bez toho nejde rozlišit TYPE_01 "
                f"(1 200 kg) od TYPE_02 (1 350 kg) — export z ESO musí "
                f"sloupec doplnit.")
        rows = list(reader)

    by_code = type_map_by_code(type_map)
    out, seen_ids, problems, type_issues = [], set(), [], []
    for n, row in enumerate(rows, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        rid = row.get("id", "")
        if not rid:
            continue
        if rid in seen_ids:
            problems.append(f"řádek {n}: id {rid} dvakrát")
            continue
        seen_ids.add(rid)
        try:
            driver = row["driver"]
            if not driver:
                raise ValueError("prázdný kód řidiče (driver)")
            cap = _num(row.get(cap_col)) if cap_col else None
            code = row.get(code_col, "") if code_col else ""
            if code:
                # export nese kód -> ověřit proti vozovému parku dne
                vt = by_code.get(code)
                if vt is None:
                    type_issues.append(f"id {rid}: type_code {code} není ve "
                                       f"vehicle_types (známé {sorted(by_code)})")
                elif cap is not None and int(cap) != vt["max_kg"]:
                    type_issues.append(f"id {rid}: {code} má v registru "
                                       f"{int(cap)} kg, ve vehicle_types "
                                       f"{vt['max_kg']} kg")
                elif row["vehicle_type"] and vt["type_name"] != row["vehicle_type"]:
                    type_issues.append(f"id {rid}: {code} je v registru "
                                       f"'{row['vehicle_type']}', ve vehicle_types "
                                       f"'{vt['type_name']}'")
                if cap is None and vt is not None:
                    cap = vt["max_kg"]
            else:
                if cap is None:
                    raise ValueError(f"prázdná nosnost ({cap_col}) a žádný type_code")
                code = map_type(row["vehicle_type"], cap, type_map)
            plan_rok, plan_mes = _num(row["km_plan_rok"]), _num(row["km_plan_mes"])
            own = (plan_rok is not None and plan_mes is not None
                   and plan_rok == 0 and plan_mes == 0)
            out.append({
                "row_id":       rid,
                "vehicle_code": row["vehicle_code"],
                "vehicle_name": row["vehicle_name"],
                "dopravce":     row["vehicle_comp"],
                "driver":       driver,
                "driver_name":  row["driver_name"] or driver,
                "vehicle_type": row["vehicle_type"],
                "profile":      row["vehicle_profile"],
                "max_kg":       int(cap) if cap is not None else None,
                "type_code":    code,
                "days":         parse_days(row["dny_pouzitelnosti"]),
                "avail_from":   _date(row["dostupnost_od"]),
                "avail_to":     _date(row["dostupnost_do"]),
                "dojezd_km":    _num(row["driver_km_to_depot"]) or 0.0,
                "kvalita":      row["driver_quality"] or "Standart",
                "plan_rok":     plan_rok,
                "plan_mes":     plan_mes,
                "aktual_rok":   _num(row["km_aktual_rok"]),
                "aktual_mes":   _num(row["km_aktual_mes"]),
                "own_fleet":    own,
                "valid_for_date": row["valid_for_date"],
            })
        except (ValueError, KeyError) as e:
            problems.append(f"řádek {n} (id {rid}): {e}")
    if problems:
        raise SystemExit("[CHYBA] Registr " + path.name + " má vadné řádky:\n  "
                         + "\n  ".join(problems))
    if type_issues:
        msg = (f"[CHYBA] TYPE kódy registru {path.name} nesedí na vozový park dne "
               f"(registr a vehicle_types musí být z téhož dne — kódy se mezi dny "
               f"přečíslovávají):\n  " + "\n  ".join(type_issues[:12])
               + (f"\n  … a dalších {len(type_issues) - 12}" if len(type_issues) > 12 else ""))
        if strict_types:
            raise SystemExit(msg + "\n  Nahraj vehicle_types téhož dne, nebo vědomě --force.")
        print(msg + "\n  --force: pokračuji s kódy z registru.")
    if not out:
        raise SystemExit(f"[CHYBA] Registr {path} nemá žádné řádky.")
    return out


def is_available(row: dict, day: date) -> bool:
    """dostupnost_od ≤ den závozu ≤ dostupnost_do (prázdné do = bez konce)."""
    if row.get("avail_from") and day < row["avail_from"]:
        return False
    if row.get("avail_to") and day > row["avail_to"]:
        return False
    return True


def usable_on(row: dict, day: date) -> bool:
    return day.weekday() in row["days"] and is_available(row, day)


def fleet_mismatches(registry: list[dict], type_map: dict, day: date) -> list[dict]:
    """
    Kontrola: počet aut v registru použitelných v den závozu per TYPE
    == available_count ve vehicle_types (s tím plánoval solver). Neshoda
    v obou směrech je vada exportu — míň aut = linky bez řidiče, víc aut =
    solver plánoval s menší flotilou, než máme.
    """
    have: dict[str, int] = {}
    for r in registry:
        if usable_on(r, day):
            have[r["type_code"]] = have.get(r["type_code"], 0) + 1
    planned = {v["type_code"]: v["available_count"] for v in type_map.values()}
    out = []
    for code in sorted(set(have) | set(planned)):
        h, p = have.get(code, 0), planned.get(code, 0)
        if h != p:
            out.append({"type_code": code, "planned": p, "registry": h})
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Historie řidič × adresa (počet závozů)
# ═════════════════════════════════════════════════════════════════════════════

def find_history_file(history_dir: Path | str | None = None) -> Path | None:
    """Právě jeden csv v data/historie_ridici; žádný = familiarity BEZ DAT
    (varování), víc = chyba (která je pravdivá?)."""
    d = Path(history_dir if history_dir is not None else CONFIG["history_dir"])
    files = sorted(d.glob(CONFIG["history_pattern"])) if d.exists() else []
    if not files:
        return None
    if len(files) > 1:
        raise SystemExit(f"[CHYBA] V {d.as_posix()}/ je víc souborů historie "
                         f"({len(files)}) — má tam být právě jeden pravdivý.")
    return files[0]


def load_history(path: Path) -> dict:
    """
    {"id:<id_subj_adr>": {driver_code: visits}, "note:<adress_note lower>":
    {...}} — obě adresní identity, protože lines_stops nese jen location_code
    (= adress_note) a id adresy (eso_col7 = id_subj_adr) se dohledává
    z prepared souboru, když existuje. Plus "_stats".
    """
    with open(path, encoding="utf-8-sig") as f:
        sample = f.read(2048)
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    fam: dict[str, dict[str, int]] = {}
    drivers, addrs, rows = set(), set(), 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=delim)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in HISTORY_REQUIRED if c not in header]
        if missing:
            raise SystemExit(f"[CHYBA] Historii {path.name} chybí sloupce {missing}")
        for row in reader:
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            drv, aid, note = row["driver_code"], row["id_subj_adr"], row["adress_note"].lower()
            cnt = _num(row["visit_count"])
            if not drv or cnt is None or (not aid and not note):
                continue
            rows += 1
            drivers.add(drv)
            for key in ((f"id:{aid}",) if aid else ()) + ((f"note:{note}",) if note else ()):
                addrs.add(key)
                d = fam.setdefault(key, {})
                d[drv] = d.get(drv, 0) + int(cnt)
    fam["_stats"] = {"rows": rows, "drivers": len(drivers),
                     "addresses": sum(1 for k in addrs if k.startswith("id:")) or len(addrs),
                     "file": path.name}
    return fam


def load_familiarity(history_dir: Path | str | None = None,
                     history_file: Path | str | None = None) -> dict | None:
    p = Path(history_file) if history_file else find_history_file(history_dir)
    return load_history(p) if p else None


def familiarity_ranks(fam: dict, keys: set[str], drivers: list[str]) -> dict:
    """
    Pro každou adresu (klíč) POŘADÍ každého aktivního řidiče podle počtu
    závozů, 0..1 (percentile_ranks — shodné počty sdílejí pořadí, kdo nikdy
    nejel je dole; když tam nejel nikdo, všichni 0,5).
    Příklad 6 řidičů 5×,4×,4×,0,0,0 -> 1.0, 0.7, 0.7, 0.2, 0.2, 0.2
    (= body 6, 4.5, 4.5, 2, 2, 2 v měřítku 1..6).
    """
    out: dict[str, dict[str, float]] = {}
    for key in keys:
        visits = fam.get(key, {})
        vals = [float(visits.get(d, 0)) for d in drivers]
        ranks = percentile_ranks(vals)
        out[key] = dict(zip(drivers, ranks))
    return out


def load_order_addresses(prepared_root: Path | str, depot: str, day: str) -> dict[str, str]:
    """{order_number -> eso_col7 (= id_subj_adr)} z prepared souboru dne;
    prázdné, když soubor není (pak se familiarity párují přes location_code)."""
    p = Path(prepared_root) / depot / f"orders_{depot}_{day}.csv"
    if not p.exists():
        return {}
    out = {}
    with open(p, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            on, aid = str(row.get("order_number", "")).strip(), str(row.get("eso_col7", "")).strip()
            if on and aid:
                out[on] = aid
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Linky dne (výsledky solveru všech dep)
# ═════════════════════════════════════════════════════════════════════════════

def _time_to_min(hhmm: str) -> int | None:
    try:
        h, m = hhmm.strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def load_depot_lines(results_dir: Path, depot: str,
                     order_addr: dict[str, str] | None = None) -> list[dict]:
    """Linky depa vč. zastávek. Dvojlinky (společné vehicle_id) sloučené
    do jedné jednotky — jeden řidič jede obě jízdy. Každá zastávka nese
    klíč adresy pro familiarity: 'id:<eso_col7>' když je znám, jinak
    'note:<location_code>'."""
    summary = results_dir / "lines_summary.csv"
    stops_f = results_dir / "lines_stops.csv"
    for f in (summary, stops_f):
        if not f.exists():
            raise SystemExit(f"[CHYBA] Chybí {f} — depo {depot} nemá "
                             f"kompletní výsledky.")
    order_addr = order_addr or {}

    stops_by_line: dict[str, list[dict]] = {}
    with open(stops_f, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            oid = str(row.get("order_id", "")).strip()
            if not oid:
                continue        # sklad (start/návrat)
            win = str(row.get("window", ""))
            end = _time_to_min(win.split("–")[-1]) if "–" in win else None
            arr = _time_to_min(str(row.get("arrival", "")))
            loc = str(row.get("location_code", "")).strip().lower()
            key = f"id:{order_addr[oid]}" if oid in order_addr else f"note:{loc}"
            stops_by_line.setdefault(row["line_id"], []).append({
                "location_code": loc, "addr_key": key,
                "arrival_min":   arr, "window_end_min": end,
            })

    by_vehicle: dict[str, dict] = {}
    with open(summary, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            vehicle_id = str(row.get("vehicle_id", "")).strip()
            if not vehicle_id:
                continue        # CELKEM
            line_id = str(row.get("line_id", "")).strip()
            unit = by_vehicle.setdefault(vehicle_id, {
                "depot": depot,
                "vehicle_id": vehicle_id,
                "type_code": vehicle_id.rsplit("_", 1)[0],
                "line_ids": [], "km": 0.0, "tightness_raw": 0.0,
                "stops_total": 0, "stop_keys": [],
            })
            unit["line_ids"].append(line_id)
            unit["km"] += float(row.get("total_km", 0) or 0)
            stops = stops_by_line.get(line_id, [])
            unit["tightness_raw"] += line_tightness(stops)
            unit["stops_total"] += len(stops)
            unit["stop_keys"].extend(s["addr_key"] for s in stops)
    return list(by_vehicle.values())


def line_tightness(stops: list[dict],
                   slack_min: int | None = None,
                   pos_coef: float | None = None) -> float:
    """
    T = Σ tightᵢ × (1 + pos_coef × posᵢ),  posᵢ ∈ [0, 1] pozice na lince.

    Tight = rezerva do konce PŮVODNÍHO okna <= slack_min (příjezd po konci
    okna je tight vždy). Konec linky váží víc než začátek (zpoždění se
    kumuluje), ale pozice nesmí přebít počet — proto koeficient, ne násobek.
    """
    slack_min = CONFIG["tight_slack_min"] if slack_min is None else slack_min
    pos_coef = CONFIG["tight_pos_coef"] if pos_coef is None else pos_coef
    n = len(stops)
    total = 0.0
    for i, s in enumerate(stops):
        if s["arrival_min"] is None or s["window_end_min"] is None:
            continue
        if s["window_end_min"] - s["arrival_min"] <= slack_min:
            pos = i / (n - 1) if n > 1 else 1.0
            total += 1.0 + pos_coef * pos
    return total


# ═════════════════════════════════════════════════════════════════════════════
#  Skóre (každé 0..1, vyšší = lepší pár)
# ═════════════════════════════════════════════════════════════════════════════

def percentile_ranks(values: list[float]) -> list[float]:
    """Pořadová normalizace do [0,1]; shodné hodnoty = shodný rank
    (deterministicky). Jedna hodnota -> 0.5."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    return [(sum(1 for v in values if v < x)
             + (sum(1 for v in values if v == x) - 1) / 2) / (n - 1)
            for x in values]


def plan_deficit(row: dict, day_of_year: int, days_in_year: int = 365) -> float | None:
    """Relativní skluz vůči poměrné části ROČNÍHO plánu; None = bez dat
    (žádný plán / neznámé najeté km)."""
    if not row.get("plan_rok") or row.get("aktual_rok") is None:
        return None
    expected = row["plan_rok"] * day_of_year / days_in_year
    return (expected - row["aktual_rok"]) / row["plan_rok"]


def plan_deficit_month(row: dict, day_of_month: int, days_in_month: int) -> float | None:
    """Totéž vůči MĚSÍČNÍMU plánu; None = bez dat."""
    if not row.get("plan_mes") or row.get("aktual_mes") is None:
        return None
    expected = row["plan_mes"] * day_of_month / days_in_month
    return (expected - row["aktual_mes"]) / row["plan_mes"]


def _rank_map(rows: list[dict], values: list[float | None]) -> dict[int, float | None]:
    """id(row) -> pořadí mezi řádky S DATY (None zůstane None)."""
    with_data = [(r, v) for r, v in zip(rows, values) if v is not None]
    ranks = percentile_ranks([v for _, v in with_data])
    out = {id(r): None for r in rows}
    for (r, _), rk in zip(with_data, ranks):
        out[id(r)] = rk
    return out


def plan_scores(rows: list[dict], day: date, year_share: float | None = None) -> tuple[dict, list[str]]:
    """
    id(row) -> skóre plnění 0..1: rok a měsíc zvlášť seřazené (kdo zaostává
    víc = výš), zkombinované year_share : (1 - year_share). Jen jedna část
    s daty -> jen ta; nic -> 0.5. Naše auta (plán 0/0) = 0.5 (o jejich
    pořadí rozhoduje TIER, ne plnění).
    """
    ys = CONFIG["plan_year_share"] if year_share is None else year_share
    contracted = [r for r in rows if not r.get("own_fleet")]
    doy, dim = day.timetuple().tm_yday, calendar.monthrange(day.year, day.month)[1]
    diy = 366 if calendar.isleap(day.year) else 365
    ry = _rank_map(contracted, [plan_deficit(r, doy, diy) for r in contracted])
    rm = _rank_map(contracted, [plan_deficit_month(r, day.day, dim) for r in contracted])
    scores, warns = {}, []
    n_year = sum(1 for v in ry.values() if v is not None)
    n_month = sum(1 for v in rm.values() if v is not None)
    for r in rows:
        if r.get("own_fleet"):
            scores[id(r)] = 0.5
            continue
        y, m = ry.get(id(r)), rm.get(id(r))
        if y is not None and m is not None:
            scores[id(r)] = ys * y + (1 - ys) * m
        elif y is not None:
            scores[id(r)] = y
        elif m is not None:
            scores[id(r)] = m
        else:
            scores[id(r)] = 0.5
    if contracted and n_year == 0 and n_month == 0:
        warns.append("[!] Plnění plánu BEZ DAT — registr nenese km_plan/km_aktual; "
                     "kritérium je neutrální (0.5).")
    elif contracted and (n_year < len(contracted) or n_month < len(contracted)):
        warns.append(f"[!] Plnění plánu: data rok {n_year}/{len(contracted)}, "
                     f"měsíc {n_month}/{len(contracted)} smluvních aut; ostatní neutrální.")
    return scores, warns


# ═════════════════════════════════════════════════════════════════════════════
#  Sestavení matice a přiřazení
# ═════════════════════════════════════════════════════════════════════════════

def build_assignment(units: list[dict], registry: list[dict], target_date: str,
                     familiarity: dict | None = None,
                     weights: dict | None = None,
                     own_fleet_last: bool | None = None) -> dict:
    """
    Celodenní matice jednotky × ŘIDIČI; buňka = nejlepší řidičovo auto
    správného typu. Vrací {"assigned": [...], "uncovered": [...],
    "warnings": [...], "weights": {...}}.

    TIER: smluvní auto má v buňce bonus větší než celý rozsah soft skóre,
    takže maďarský algoritmus napřed maximalizuje počet linek se smluvními
    auty a naše auta (plán 0/0) dostanou jen zbytek — ale jen ve svém typu.
    """
    W = dict(CONFIG["weights"]) if weights is None else dict(weights)
    tier_on = CONFIG["own_fleet_last"] if own_fleet_last is None else own_fleet_last
    day = datetime.strptime(target_date, "%Y-%m-%d").date()
    weekday = day.weekday()
    warnings: list[str] = []

    units = sorted(units, key=lambda u: (u["depot"], u["vehicle_id"]))
    drivers = sorted({r["driver"] for r in registry})
    rows_by_driver: dict[str, list[dict]] = {}
    for r in registry:
        rows_by_driver.setdefault(r["driver"], []).append(r)

    # Pořadové normalizace přes CELÝ den / celý registr
    km_rank = percentile_ranks([u["km"] for u in units])
    t_rank = percentile_ranks([u["tightness_raw"] for u in units])
    all_rows = [r for rs in rows_by_driver.values() for r in rs]
    dojezd_rank = dict(zip(map(id, all_rows),
                           percentile_ranks([r["dojezd_km"] for r in all_rows])))
    plan_score, plan_warns = plan_scores(all_rows, day)
    warnings.extend(plan_warns)

    if familiarity is None:
        warnings.append("[!] Familiarity BEZ DAT — chybí historie řidič×adresa "
                        f"({CONFIG['history_dir']}); kritérium je neutrální (0.5).")
        fam_rank: dict = {}
    else:
        keys = {k for u in units for k in u.get("stop_keys", [])}
        fam_rank = familiarity_ranks(familiarity, keys, drivers)
    speed = CONFIG["quality_speed"]
    tier_bonus = sum(W.values()) + 1.0 if tier_on else 0.0
    n_own = sum(1 for r in all_rows if r.get("own_fleet"))
    if n_own and tier_on:
        warnings.append(f"[i] Naše auta (plán 0/0): {n_own} — jedou až když na "
                        f"linky jejich typu nestačí smluvní.")

    def cell(u_idx: int, driver: str) -> tuple[float, dict] | None:
        """Nejlepší (skóre + tier, info) řidiče pro jednotku; None = hard zákaz."""
        u = units[u_idx]
        best = None
        for r in rows_by_driver[driver]:
            if (r["type_code"] != u["type_code"] or weekday not in r["days"]
                    or not is_available(r, day)):
                continue
            s_plan = plan_score[id(r)]
            s_doj = 1.0 - abs(km_rank[u_idx] - dojezd_rank[id(r)])
            s_qual = 1.0 - abs(t_rank[u_idx]
                               - speed.get(r["kvalita"], 0.5))
            keys = u.get("stop_keys", [])
            if familiarity is None or not keys:
                s_fam, known = 0.5, 0
            else:
                s_fam = sum(fam_rank[k].get(driver, 0.5) for k in keys) / len(keys)
                known = sum(1 for k in keys
                            if familiarity.get(k, {}).get(driver, 0) > 0)
            score = (W["plneni_planu"] * s_plan + W["dojezd"] * s_doj
                     + W["kvalita_tightness"] * s_qual
                     + W["familiarity"] * s_fam)
            total = score + (0.0 if r.get("own_fleet") else tier_bonus)
            entry = (total, {"row": r, "score": score,
                             "breakdown": {"plneni": round(s_plan, 3),
                                           "dojezd": round(s_doj, 3),
                                           "kvalita": round(s_qual, 3),
                                           "familiarity": round(s_fam, 3),
                                           "fam_known": known,
                                           "tier": "naše" if r.get("own_fleet") else "smluvní"}})
            if best is None or entry[0] > best[0]:
                best = entry
        return best

    cells: dict[tuple[int, int], tuple[float, dict]] = {}
    cost = [[BIG_COST] * len(drivers) for _ in units]
    for ui in range(len(units)):
        for di, drv in enumerate(drivers):
            c = cell(ui, drv)
            if c is not None:
                cells[(ui, di)] = c
                cost[ui][di] = -c[0]

    from scipy.optimize import linear_sum_assignment
    import numpy as np
    if units and drivers:
        row_ind, col_ind = linear_sum_assignment(np.array(cost))
    else:
        row_ind, col_ind = [], []

    assigned, uncovered = [], []
    matched = {ui: di for ui, di in zip(row_ind, col_ind)
               if (ui, di) in cells}
    for ui, u in enumerate(units):
        if ui not in matched:
            uncovered.append(u)
            continue
        di = matched[ui]
        _total, info = cells[(ui, di)]
        assigned.append({"unit": u, "driver": drivers[di],
                         "vehicle": info["row"], "score": round(info["score"], 3),
                         "breakdown": info["breakdown"]})
    if uncovered:
        warnings.append(f"[ALERT] {len(uncovered)} linek BEZ řidiče — po "
                        f"tvrdých filtrech (den, dostupnost, typ) nezbyl "
                        f"nikdo. Ruční zásah nutný.")
    n_own_used = sum(1 for a in assigned if a["vehicle"].get("own_fleet"))
    if n_own_used:
        warnings.append(f"[i] Nasazeno {n_own_used} našich aut (smluvní daného "
                        f"typu nestačila).")
    return {"assigned": assigned, "uncovered": uncovered,
            "warnings": warnings, "weights": W}


# ═════════════════════════════════════════════════════════════════════════════
#  Výstupy
# ═════════════════════════════════════════════════════════════════════════════

CSV_HEADER = ["depot", "line_id", "vehicle_id", "type_code", "km",
              "tightness", "driver", "driver_code", "vehicle_code",
              "vehicle_name", "dopravce", "tier", "kvalita", "dojezd_km",
              "plan_rok", "aktual_rok", "plan_mes", "aktual_mes",
              "score", "s_plneni", "s_dojezd", "s_kvalita", "s_familiarity",
              "fam_known_stops", "dvojlinka"]


def _fmt_num(v) -> str:
    return "" if v is None else (str(int(v)) if float(v).is_integer() else str(v))


def result_rows(result: dict) -> list[dict]:
    """Jeden řádek per LINKA (dvojlinka -> 2 řádky, týž řidič)."""
    rows = []
    for a in sorted(result["assigned"],
                    key=lambda x: (x["unit"]["depot"], x["unit"]["vehicle_id"])):
        u, v, b = a["unit"], a["vehicle"], a["breakdown"]
        for line_id in u["line_ids"]:
            rows.append({
                "depot": u["depot"], "line_id": line_id,
                "vehicle_id": u["vehicle_id"], "type_code": u["type_code"],
                "km": round(u["km"], 1),
                "tightness": round(u["tightness_raw"], 2),
                "driver": v.get("driver_name") or a["driver"],
                "driver_code": a["driver"],
                "vehicle_code": v.get("vehicle_code", ""),
                "vehicle_name": v.get("vehicle_name", ""),
                "dopravce": v.get("dopravce", ""),
                "tier": b.get("tier", ""),
                "kvalita": v.get("kvalita", ""), "dojezd_km": v.get("dojezd_km", ""),
                "plan_rok": _fmt_num(v.get("plan_rok")),
                "aktual_rok": _fmt_num(v.get("aktual_rok")),
                "plan_mes": _fmt_num(v.get("plan_mes")),
                "aktual_mes": _fmt_num(v.get("aktual_mes")),
                "score": a["score"], "s_plneni": b["plneni"],
                "s_dojezd": b["dojezd"], "s_kvalita": b["kvalita"],
                "s_familiarity": b["familiarity"],
                "fam_known_stops": f"{b.get('fam_known', 0)}/{u.get('stops_total', len(u.get('stop_keys', [])))}",
                "dvojlinka": "ano" if len(u["line_ids"]) > 1 else "",
            })
    for u in result["uncovered"]:
        for line_id in u["line_ids"]:
            rows.append({**{k: "" for k in CSV_HEADER},
                         "depot": u["depot"], "line_id": line_id,
                         "vehicle_id": u["vehicle_id"],
                         "type_code": u["type_code"], "km": round(u["km"], 1),
                         "tightness": round(u["tightness_raw"], 2),
                         "driver": "!!! NEPŘIŘAZENO !!!",
                         "dvojlinka": "ano" if len(u["line_ids"]) > 1 else ""})
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        w.writerows(rows)


def format_table(rows: list[dict]) -> str:
    out = []
    for depot in sorted({r["depot"] for r in rows}):
        out.append(f"\n{depot}:")
        out.append(f"  {'linka':<8} {'typ':<8} {'km':>7} {'tight':>6}  "
                   f"{'řidič':<24} {'auto':<6} {'tier':<8} {'kvalita':<9} "
                   f"{'skóre':>6} {'zná':>6}")
        for r in [x for x in rows if x["depot"] == depot]:
            out.append(f"  {r['line_id']:<8} {r['type_code']:<8} "
                       f"{r['km']:>7} {r['tightness']:>6}  "
                       f"{r['driver']:<24} {r['vehicle_code']:<6} "
                       f"{r['tier']:<8} {r['kvalita']:<9} {r['score']!s:>6} "
                       f"{r['fam_known_stops']:>6}"
                       + ("  [dvojlinka]" if r["dvojlinka"] else ""))
    return "\n".join(out)


# ═════════════════════════════════════════════════════════════════════════════
#  Main
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Celodenní přiřazení řidičů k naplánovaným linkám "
                    "(spouštět až po naplánování VŠECH dep dne).")
    ap.add_argument("date", help="Datum závozu (YYYY-MM-DD)")
    ap.add_argument("--depots", nargs="*", default=None,
                    help=f"Vědomá podmnožina dep (default: všechna "
                         f"{' '.join(DEPOT_ORDER)} — celodenní optimum "
                         f"potřebuje celý den)")
    ap.add_argument("--label", default="",
                    help="Přípona výsledkových složek (testovací běhy)")
    ap.add_argument("--results-root", default=CONFIG["results_root"])
    ap.add_argument("--ridici-file", default="",
                    help="Explicitní cesta k registru (default: jediný "
                         f"{CONFIG['registry_pattern']} v {CONFIG['ridici_dir']}/)")
    ap.add_argument("--history-file", default="",
                    help="Explicitní historie řidič×adresa (default: jediný csv "
                         f"v {CONFIG['history_dir']}/)")
    ap.add_argument("--vehicle-types-file", default="",
                    help="Vozový park dne (default: jediný v data/static)")
    ap.add_argument("--force", action="store_true",
                    help="Pokračovat i při neshodě registru s dnem/vozovým parkem")
    args = ap.parse_args()

    depots = [d.upper() for d in args.depots] if args.depots else list(DEPOT_ORDER)
    unknown = sorted(set(depots) - set(DEPOT_ORDER))
    if unknown:
        raise SystemExit(f"[CHYBA] Neznámá depa: {unknown}")
    suffix = f"_{args.label}" if args.label else ""
    root = Path(args.results_root)
    day = datetime.strptime(args.date, "%Y-%m-%d").date()

    type_map = load_type_map(args.vehicle_types_file or None)
    registry_path = (Path(args.ridici_file) if args.ridici_file
                     else find_registry_file())
    registry = load_registry(registry_path, type_map, strict_types=not args.force)

    print("=" * 66)
    print(f"PŘIŘAZENÍ ŘIDIČŮ — {args.date} ({DAY_NAMES[day.weekday()]})"
          + (f" | label {args.label}" if args.label else ""))
    print(f"Registr: {registry_path.name} — {len(registry)} aut+řidičů "
          f"({sum(1 for r in registry if r['own_fleet'])} našich, "
          f"{sum(1 for r in registry if usable_on(r, day))} použitelných "
          f"{args.date})")
    print("=" * 66)

    # ── kontroly registru vs den / vozový park (tvrdé, --force přebije) ──
    problems: list[str] = []
    vfd = {r["valid_for_date"] for r in registry}
    if vfd != {args.date}:
        problems.append(f"registr má valid_for_date {sorted(vfd)}, plánuje se {args.date}")
    vt_dates = {v["valid_for_date"] for v in type_map.values()} - {""}
    if vt_dates and vt_dates != {args.date}:
        print(f"  [!] vehicle_types má valid_for_date {sorted(vt_dates)}, "
              f"plánuje se {args.date}")
    mism = fleet_mismatches(registry, type_map, day)
    if mism:
        problems.append("počty aut per typ NESEDÍ (vehicle_types available_count "
                        "vs registr použitelný v den závozu): "
                        + ", ".join(f"{m['type_code']} plán {m['planned']} / "
                                    f"registr {m['registry']}" for m in mism))
    if problems:
        msg = "\n  - ".join(["[CHYBA] Registr aut+řidičů nesedí:"] + problems)
        if args.force:
            print(msg + "\n  --force: pokračuji.")
        else:
            raise SystemExit(msg + "\n  Oprav exporty (registr i vehicle_types "
                             "musí být z téhož dne), nebo vědomě --force.")

    missing = [d for d in depots
               if not (root / d / f"{args.date}{suffix}" / "lines_summary.csv").exists()]
    if missing:
        raise SystemExit(
            f"[CHYBA] Chybí výsledky dep: {', '.join(missing)} "
            f"(hledám {root.as_posix()}/{{DEPO}}/{args.date}{suffix}/).\n"
            f"        Celodenní optimum potřebuje všechna depa — nejdřív "
            f"doplánuj, nebo vědomě: --depots "
            + " ".join(d for d in depots if d not in missing))

    prepared_root = Path(CONFIG["prepared_root"])
    units = []
    for depot in depots:
        units.extend(load_depot_lines(root / depot / f"{args.date}{suffix}", depot,
                                      load_order_addresses(prepared_root, depot, args.date)))

    # L3 kamionová trasa (plan_day l3) — když existuje, řidiče dostane taky
    l3_dir = root / "L3" / f"{args.date}{suffix}"
    if (l3_dir / "lines_summary.csv").exists():
        units.extend(load_depot_lines(l3_dir, "L3",
                                      load_order_addresses(prepared_root, "L3", args.date)))
        depots = depots + ["L3"]
        print("[L3] Kamionové linky přibrány (zóna L3)")

    n_id = sum(1 for u in units for k in u["stop_keys"] if k.startswith("id:"))
    n_all = sum(len(u["stop_keys"]) for u in units)
    print(f"Linek: {sum(len(u['line_ids']) for u in units)} "
          f"({len(units)} jednotek po sloučení dvojlinek) "
          f"za depa {', '.join(depots)}; zastávek {n_all}, z toho podle id "
          f"adresy {n_id}, podle location_code {n_all - n_id}")

    familiarity = load_familiarity(history_file=args.history_file or None)
    if familiarity is not None:
        st = familiarity["_stats"]
        print(f"Historie: {st['file']} — {st['rows']} řádků, {st['drivers']} "
              f"řidičů, {st['addresses']} adres")
    result = build_assignment(units, registry, args.date,
                              familiarity=familiarity)

    rows = result_rows(result)
    print(format_table(rows))
    print()
    for w in result["warnings"]:
        print(w)

    out_dir = root / "driver_assignment" / f"{args.date}{suffix}"
    day_csv = out_dir / f"driver_plan_{args.date}.csv"
    write_csv(day_csv, rows)
    for depot in depots:
        depot_rows = [r for r in rows if r["depot"] == depot]
        if depot_rows:
            write_csv(root / depot / f"{args.date}{suffix}"
                      / f"driver_plan_{depot}_{args.date}.csv", depot_rows)
    (out_dir / "summary.json").write_text(json.dumps({
        "date": args.date, "label": args.label, "depots": depots,
        "registry_file": registry_path.name,
        "history_file": familiarity["_stats"]["file"] if familiarity else None,
        "vehicle_types_file": Path(args.vehicle_types_file).name if args.vehicle_types_file else None,
        "lines": sum(len(u["line_ids"]) for u in units),
        "units": len(units), "assigned": len(result["assigned"]),
        "own_fleet_used": sum(1 for a in result["assigned"] if a["vehicle"].get("own_fleet")),
        "uncovered": [{"depot": u["depot"], "lines": u["line_ids"],
                       "type": u["type_code"]} for u in result["uncovered"]],
        "fleet_mismatches": mism, "forced": bool(args.force),
        "weights": result["weights"], "warnings": result["warnings"],
        "params": {k: CONFIG[k] for k in
                   ("tight_slack_min", "tight_pos_coef", "quality_speed",
                    "plan_year_share", "own_fleet_last")},
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nVýstup: {day_csv.as_posix()} (+ driver_plan per depo)")
    if result["uncovered"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
