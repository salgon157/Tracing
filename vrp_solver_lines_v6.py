"""
VRP Solver Lines v6 — RiRo depot pipeline + Hierarchická matheuristika
======================================================================
Prerekvizity: pip install ortools requests numpy pandas openpyxl scikit-learn

Denní workflow:
  python prepare_inputs_v6.py CB
  python vrp_solver_lines_v6.py --orders-file data/prepared/CB/orders_CB_YYYY-MM-DD.csv

Depot kódy: CB (České Budějovice), HK (Hradec Králové), MO (Morava),
            PR (Praha), OM (Ovoce a mléko — zatím bez RiRo dat/lokací).

Statické soubory:
  data/static/vehicle_types.csv → jeden řádek = jeden typ auta (kapacita, Kč/km,
                                  fixní náklad, available_count = sdílený pool)
  data/static/closures.json     → aktivní uzavírky (objízdky)

Pozn.: data/static/locations_*.csv už solver NEPOUŽÍVÁ — GPS i předpočítaný
čas zastávky chodí přímo v RiRo souboru z ESO9 (od 17. 7. 2026).

Poznámky:
- Depot kód je businessové omezení a respektuje se už ve vstupním kroku.
- Solver pracuje vždy nad jedním depem / zónou.
- Výstupem je line + vehicle type, ne konkrétní řidič.
"""
import csv
import re
import argparse
import json
import os
import sys
import subprocess
import requests
import numpy as np
import pandas as pd
import multiprocessing
import math
import time
import random
from collections import Counter
from datetime import datetime
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from sklearn.cluster import KMeans
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

from osm_routing import (add_osm_args, apply_osm_source,
                         resolve_osm_source, start_hint)

SOLVER_VERSION = "v6"   # verze solveru — zvedni ručně při větších změnách logiky

# ── Exit kódy solveru (čte je plan_day i server / UI) ────────────────────
#   0  OK — plán uložen
#   1  technická chyba (výjimka, routing instance neběží, IO)
#   2  vadná data — validace/závory (vstup je třeba opravit a spustit znovu);
#      NIKDY se na to nesmí eskalovat porušení
#   3  řešení neexistuje — žádný seed / záchrana nevyšla / dvojlinky se
#      nespárovaly; tady dává smysl eskalace (L0 → L1+L2)
EXIT_OK, EXIT_ERROR, EXIT_DATA, EXIT_INFEASIBLE = 0, 1, 2, 3


EXIT_STATUS_NAME = {EXIT_OK: "ok", EXIT_ERROR: "error",
                    EXIT_DATA: "data_error", EXIT_INFEASIBLE: "infeasible"}

# Kontext běhu pro run_status.json — plní main(), jakmile zná výstupní
# složku. Dokud ji nezná (chybí --orders-file…), status soubor nevzniká
# a platí jen exit kód.
RUN_CONTEXT: dict = {"output_dir": None, "zone": None, "delivery_date": None,
                     "started": None, "run_id": None}


class SolverAbort(SystemExit):
    """SystemExit se zprávou i číselným kódem. `str(e)` vrací zprávu
    (testy), interpret končí kódem `e.code`."""
    def __init__(self, message: str, code: int):
        super().__init__(code)
        self.message = message

    def __str__(self) -> str:
        return self.message


def _extract_order_numbers(message: str) -> list[str]:
    """Čísla objednávek z hlášky (formát ESO `O1234…`) — pro UI, ať nemusí
    parsovat text. Duplicitní se sloučí, pořadí zůstane."""
    seen, out = set(), []
    for m in re.findall(r"\bO\d{6,}\b", message or ""):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def write_run_status(status: str, exit_code: int, message: str = "",
                     orders: list[str] | None = None,
                     extra: dict | None = None) -> Path | None:
    """
    Strojově čitelný stav běhu → `run_status.json` ve výstupní složce.
    Píše se při KAŽDÉM konci (ok i chyba), aby server / UI nemusely
    parsovat konzoli:
      {status: ok|error|data_error|infeasible, exit_code, reason (1. řádek),
       message, orders: [dotčené objednávky], zone, delivery_date, run_id,
       elapsed_sec, finished_at, …extra (lines_count, total_cost_kc)}
    Vrací cestu, nebo None když výstupní složka není známá.
    """
    out_dir = RUN_CONTEXT.get("output_dir")
    if not out_dir:
        return None
    started = RUN_CONTEXT.get("started")
    first_line = next((ln.strip() for ln in (message or "").splitlines()
                       if ln.strip() and not set(ln.strip()) <= {"=", "-", "!"}), "")
    doc = {
        "status": status,
        "exit_code": int(exit_code),
        "reason": first_line[:200],
        "message": message or "",
        "orders": list(orders) if orders is not None else _extract_order_numbers(message),
        "zone": RUN_CONTEXT.get("zone"),
        "delivery_date": RUN_CONTEXT.get("delivery_date"),
        "run_id": RUN_CONTEXT.get("run_id"),
        "elapsed_sec": round(time.time() - started, 1) if started else None,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "solver_version": SOLVER_VERSION,
    }
    if extra:
        doc.update(extra)
    try:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "run_status.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return path
    except OSError as e:                                    # status je bonus
        print(f"  [!] run_status.json se nepodařilo zapsat: {e}", file=sys.stderr)
        return None


def abort(message: str, code: int = EXIT_ERROR,
          orders: list[str] | None = None) -> None:
    """Ukončí běh s hláškou (stderr), run_status.json a exit kódem.
    Jednotné místo — plan_day podle kódu rozhoduje, jestli má eskalovat
    (jen 3 = řešení neexistuje; 2 = vadná data se opravují, ne eskalují)."""
    print(message, file=sys.stderr, flush=True)
    write_run_status(EXIT_STATUS_NAME.get(code, "error"), code, message, orders)
    raise SolverAbort(message, code)

# Sentinel pro nedosažitelné páry v OSRM/ORS matici.
# 999_999 minut ≈ 16 666 hodin → OR-Tools to chápe jako prohibitivně drahou hranu.
# Používáme místo NaN/inf, které by po .astype(int) daly undefined behavior (INT_MIN).
UNREACHABLE_TIME_MIN = 999_999

# Práh pro hard-fail: kolik % matice smí být nedosažitelných, než to považujeme
# za rozbitá data. Je to hlídač KVALITY DAT, ne bezpečnostní pojistka —
# bezpečnost dělá sentinel UNREACHABLE_TIME_MIN (999 999 min) proti stropu
# délky trasy (1 410 min): takový úsek se do trasy nevejde, solver ho nepoužije.
# Matice jsou per-vozidlo, takže HGV-nedosažitelný pár blokuje jen kamion
# a adresu obslouží dodávka.
#
# Práh je PER PROFIL, protože každý profil má jinou realitu:
#
#   driving (dodávky) — 0,1 %, prakticky nulová tolerance.
#     Naměřeno 22. 7. 2026 na všech depech: CB 0,000 %, HK 0,000 %,
#     MO 0,000 %, PR 0,000 % (125k+ párů, ani jeden nedosažitelný).
#     Pozor co 0,1 % znamená v praxi: JEDEN úplně izolovaný bod udělá
#     0,94–1,36 % matice (podle velikosti depa), takže tenhle práh
#     neprojde ani jedna nedosažitelná adresa. To je ZÁMĚR — když
#     dodávka někam nedojede, nedojede tam nic (kamion je omezenější),
#     objednávka je neobsloužitelná a model by stejně skončil jako
#     infeasible, jen o pár minut později a s méně srozumitelnou chybou.
#     Tolerovaných ~21–45 párů kryje jen drobné anomálie typu jednosměrek.
#
#   driving-hgv (kamiony) — 5 %, legitimně nedojedou do center měst
#     (pěší zóny, zákazy vjezdu). Naměřeno CB 1,14 %, PR 2,04 %; dvě
#     adresy v centru Teplic samy dělají ~2 % matice. Tyhle adresy
#     obslouží dodávka, proto se kvůli nim nesmí zastavit celý běh.
UNREACHABLE_MATRIX_FAIL_PCT = 0.001                 # default (driving)
UNREACHABLE_MATRIX_FAIL_PCT_BY_PROFILE = {
    "driving-hgv": 0.05,
}
# Nastaví --force-matrix; helper pak vrací 100 % (nikdy nezfailuje).
FORCE_MATRIX = False


def unreachable_fail_pct(profile: str) -> float:
    """Práh pro daný routing profil (--force-matrix vypíná úplně)."""
    if FORCE_MATRIX:
        return 1.0
    return UNREACHABLE_MATRIX_FAIL_PCT_BY_PROFILE.get(
        profile, UNREACHABLE_MATRIX_FAIL_PCT)

# Když routing pro těžká vozidla (ORS / driving-hgv) selže, DEFAULT je hard-fail.
# Tiché "spadnutí" na osobní profil (driving) by naplánovalo kamiony po trasách,
# kam nesmí (mosty, úzké uličky, váhové/výškové zákazy) — bez viditelné chyby.
# True (přes --allow-profile-fallback) vědomě dovolí fallback na 'driving'.
ALLOW_PROFILE_FALLBACK = False

# ============================================================
#  SKLAD (výchozí bod všech tras) — uprav na svůj sklad
# ============================================================
DEPOT = {
    "name":  "Hlavní sklad",
    "lat":   49.5061806,   # <-- GPS souřadnice tvého skladu
    "lon":   15.5950131,
    "open":  "00:00",
    "close": "23:59",
}

# ============================================================
#  KONFIGURACE
# ============================================================
CONFIG = {
    # Cesty k souborům
    # orders_file: prázdný = musí být předán přes --orders-file.
    # Nemá smysl hardcodovat default, protože cesta zahrnuje depot+datum
    # (např. data/prepared/CB/orders_CB_2026-04-10.csv).
    # Hodnota se za běhu přepíše na args.orders_file (viz main()).
    "orders_file":                   "",
    # Prázdné = automaticky nejnovější vehicle_types-YYYYMMDD.csv
    # z data/static (find_vehicle_types_file). Přepsat lze --vehicle-types-file.
    "vehicle_types_file":            "",

    # Časový buffer na každý úsek: fixní + procentuální (v OSRM/ORS matrici)
    "time_buffer_fixed_min":         0,
    "time_buffer_pct":               0,

    # ── Plánovací buffery (solver only — data se nemění) ──────────
    # Rozšíření závozových oken zákazníků:
    #   tw_expand_before_min  … posun začátku okna doleva  (řidič může přijet dříve)
    #   tw_expand_after_min   … posun konce okna doprava   (řidič může přijet později)
    # Rychlostní faktor:
    #   travel_time_speed_factor … travel_time_solver = travel_time / faktor
    #   (1.0 = důvěřujeme mapě; dříve bylo 1.03 = 3 % rychleji — zrušeno,
    #    reálnou rezervu řešíme přes vehicle_capacity_multiplier níže)
    "tw_expand_before_min":          5,
    "tw_expand_after_min":           25,
    "travel_time_speed_factor":      1.0,

    # Kapacitní násobič vozidel:
    #   effective_max_kg = csv.max_kg * vehicle_capacity_multiplier
    #   DEFAULT 1.0 = L0, plánuje se na papírovou nosnost (od vlny 3 srpen
    #   2026 — porušení řídí plan_day podle decision). Porušení L1 = 1.03
    #   přes --capacity-multiplier 1.03 („přesně jako dřív" = tento flag).
    #   Ladí se za provozu — testy nekontrolují konkrétní hodnotu.
    "vehicle_capacity_multiplier":   1.0,

    # Pozn.: doba zastávky NENÍ v CONFIG — chodí předpočítaná z ESO9 v riro
    # (payload SEC) a prepare ji předává ve sloupci `service_sec`. Žádný vzorec.

    # Maximální počet zákaznických zastávek na jedné trase (sklad se nepočítá)
    # None nebo 0 = neomezeno. Platí i pro L3 kamion (výběr L3 ho zná).
    "max_stops_per_route":           20,

    # Max čekání na JEDNÉ zastávce (slack Time dimenze) v minutách — auto smí
    # stát nejvýš tolik, než pojede dál (čekání na okno další zastávky, pauza
    # řidiče 45 min se vejde). 120 = auto smí počkat až 2 h místo toho, aby
    # solver posílal na pozdější okno druhé auto (do 16. 8. 2026 bylo 60).
    # Výběr L3 čte stejnou hodnotu.
    "time_slack_max_min":            120,

    # Pozn.: fixní náklad za výjezd vozidla (mzda řidiče atd.) je per-type
    # ve sloupci `start_cost_kc` v vehicle_types.csv. Není v CONFIG.

    # NEJZAZŠÍ HODINA NÁVRATU do skladu (hodiny od půlnoci) — horní mez
    # kumulativního času v Time dimenzi. NENÍ to délka trasy (span): trasa
    # smí být libovolně dlouhá, jen musí být zpátky do této hodiny.
    # 23,5 = do 23:30. Dřív se jmenovalo max_route_duration_h a mátlo.
    "latest_return_h":               23.5,

    # Nakládka ve skladu před výjezdem (minuty). Pro export do ESO
    # (plán příjezd Depo = odjezd − nakládka) a pro dvojlinky (druhá jízda
    # smí vyjet nejdřív návrat první + tahle nakládka).
    "depot_loading_min":             40,

    # ── Dvojlinky (--double-runs, porušení L2) ────────────────────────────
    # Druhá jízda malého auta v týž den. Platí se jako PLNÝ druhý výjezd
    # (start_cost typu) — rozhodnutí uživatele, srpen 2026. Virtuální
    # vozidla dostanou nejdřívější výjezd double_run_earliest; po solve se
    # párují na fyzická auta (návrat + nakládka <= výjezd druhé jízdy),
    # nespárovatelná dvojlinka = fatální chyba.
    "double_run_earliest":           "10:00",
    "double_runs_max":               10,

    # OSRM adresy per profil (driving = osobní/dodávka, driving-hgv = nákladní)
    # Pro driving-hgv spusť druhý OSRM kontejner na portu 5001 s truck profilem.
    # Pokud profil chybí, solver automaticky použije fallback na "driving".
    "osrm_url":                      "http://localhost:5000",   # fallback
    "osrm_urls": {
        "driving":     "http://localhost:5000",   # OSRM
        "driving-hgv": "http://localhost:8080",   # ORS
    },
    "closure_route_profiles": {
        "driving":     "driving-hgv",
        "driving-hgv": "driving-hgv",
    },

    # ── Časový budget ──────────────────────────────────────────
    "total_time_budget_sec":         1800,   # 3600 = 60 minut celkem

    # Rozdělení budgetu po odečtení OSRM fáze (součet musí být 1.0)
    # Winner z benchmarku (config 06_2clusters, +1.7 % vs baseline průměr / 9 datasetů,
    # +2.1 % na cross-validačních dnech Apr 16+17).
    # Phase D (LNS) je prakticky mrtvá — investigate_phase_d.py prokázal 0 % efektivitu
    # i s opravenými SA parametry. Celý D budget přesunut do E (cluster intensification).
    "budget_phase_C_pct":            0.40,   # seed solve
    "budget_phase_D_pct":            0.00,   # cross-cluster LNS (deaktivováno — viz benchmark)
    "budget_phase_E_pct":            0.60,   # finální intenzifikace

    # Kolik nejlepších seedů z fáze C dotáhnout ve fázi E. 1 = jen vítěz
    # (chování do 11.8.2026). "auto" = kolik se vejde do JEDNÉ vlny workerů
    # (workers // clusters, max počet seedů) — wall clock se neprodlouží,
    # na slabém stroji samo spadne na 1.
    # Pozadí: pořadí seedů po fázi C je špatný odhad kvality po fázi E.
    # A/B na 8 depo-dnech (5 min budget): v 7 z 24 běhů (29 %) vyhrál po E
    # jiný seed, než vybrala fáze C — na PR 7.8. dokonce ten, co v C prohrál
    # o 3 050 Kč. Přínos je v chvostu (10.8. PR: −4 572 Kč, 7.8. CB: o auto
    # míň), medián je zhruba nula. Zaplaceno jádry, která jinak leží ladem.
    "seed_finalists":                "auto",

    # ── Clustering ─────────────────────────────────────────────
    # 2 clustery — winner z benchmarku (Phase 2, cross-validation na Apr 16+17).
    # Méně, větších clusterů dává solveru širší geografický výhled na cross-cluster
    # optimalizaci uvnitř seed solve (Phase C), a protože Phase D je vypnutá,
    # jemnější dělení už nemá co přinést.
    # Pozn.: MO dataset (~44 objednávek) může benefitovat z 1-2 clusterů;
    # CB/HK (100+) z 2-3. Zatím držíme 2 jako robustní default napříč depy.
    "num_clusters":                  2,

    # Počet paralelních workerů ("auto" = cpu_count() - 1)
    "parallel_workers":              "auto",

    # ── LNS parametry ──────────────────────────────────────────
    "lns_destroy_min":               5,
    "lns_destroy_max":               25,
    "lns_neighbor_clusters":         3,      # sousední clustery při repair
    "seed_unsolved_cluster_penalty_kc": 2_000_000,

    # Mírně ne-greedy acceptance (Simulated Annealing prvek)
    "lns_accept_worse_prob":         0.08,
    "lns_accept_worse_max_pct":      0.015,
    "lns_stagnation_limit":          10,

    # Reprodukovatelnost
    "random_seed":                   42,

    # ── Režim řidiče EU (jen s --driver-breaks; L3 kamionové trasy) ───
    # Zjednodušeně, na bezpečné straně:
    #   • pauza: v žádném úseku trasy delším než driver_break_after_h
    #     (měřeno UPLYNULÝM časem trasy — jízda + vykládka, ne jen jízda,
    #     tak to počítá OR-Tools SetBreakDistanceDuration) nesmí chybět
    #     driver_break_min pauza. Přísnější než EU (4,5 h jízdy), o pár
    #     desítek minut na dlouhé trase.
    #   • denní limit ČISTÉ JÍZDY driver_max_drive_h (EU 561/2006: 9 h,
    #     2× týdně 10 h — bereme 9). Tvrdá podmínka: trasa, kterou jeden
    #     řidič nesmí odjet, se nesmí naplánovat.
    # Běžné dodávkové linky (do 3,5 t) tachograf nemají — nemodeluje se.
    "driver_break_after_h":          4.5,
    "driver_break_min":              45,
    "driver_max_drive_h":            9.0,
}


# ============================================================
#  NAČTENÍ DAT
# ============================================================


# ── Vozový park: právě jeden soubor v data/static ────────────────────────────
# Aktivní vozový park je `vehicle_types-*.csv` (středníky, sloupec
# valid_for_date navíc) a ve složce smí být PRÁVĚ JEDEN. Který to je,
# neřeší tenhle program — postará se o to vrstva nad ním; my jen ověříme,
# že je jednoznačný. Co neplatí, patří do `data/static/vehicle_types_archiv/`.
# Starý čárkový formát se odmítá — tichý fallback by znamenal plánování
# na neaktuální flotile.
VEHICLE_TYPES_DIR     = Path("data/static")
VEHICLE_TYPES_PATTERN = "vehicle_types-*.csv"


def find_vehicle_types_file(static_dir: Path | str | None = None) -> Path:
    """
    Najde jediný soubor vozového parku v data/static.

    Víc souborů je vada, ne situace k řešení heuristikou: kdyby program
    sám vybíral (podle data v názvu, času úpravy…), plánoval by podle
    souboru, o kterém nikdo nerozhodl.
    """
    # Modulová konstanta se čte AŽ TADY, ne jako default parametru — jinak
    # by nešla přepsat (testy, jiný kořen dat).
    static_path = Path(static_dir if static_dir is not None else VEHICLE_TYPES_DIR)
    found = sorted(static_path.glob(VEHICLE_TYPES_PATTERN)) if static_path.exists() else []

    if not found:
        raise FileNotFoundError(
            f"[CHYBA] V {static_path} není žádný soubor vozového parku.\n"
            f"        Očekávám právě jeden {VEHICLE_TYPES_PATTERN} "
            f"(středníky, sloupec valid_for_date).\n"
            f"        Co už neplatí, patří do "
            f"{static_path / 'vehicle_types_archiv'}."
        )
    if len(found) > 1:
        names = "\n".join(f"          - {f.name}" for f in found)
        raise ValueError(
            f"[CHYBA] V {static_path} je {len(found)} souborů vozového parku:\n"
            f"{names}\n"
            f"        Nech tam PRÁVĚ JEDEN — ostatní přesuň do "
            f"{static_path / 'vehicle_types_archiv'}.\n"
            f"        Program schválně nevybírá sám: plánovat podle souboru,\n"
            f"        o kterém nikdo nerozhodl, je horší než se zastavit."
        )
    return found[0]


# Hodnota, která vypadá jako datum (17.04.2026, 17.4.2026, 2026-04-17, XII.00)
# — typický otisk Excelu, který desetinné číslo přeformátoval na datum.
_LOOKS_LIKE_DATE_RE = re.compile(
    r"^\s*(\d{1,4}[./-]\d{1,2}([./-]\d{1,4})?|[IVXLC]+\.\d+)\s*$"
)


def _broken_vehicle_rows_report(path: Path, broken: list[dict]) -> str:
    """
    Hlášení pro řádky vozového parku s nečitelným číslem.

    Fatální schválně: tiché přeskočení by znamenalo plánovat s menší
    flotilou, než firma reálně má — a nikdo by si toho nevšiml.
    """
    lines = ["", "=" * 65,
             f"[CHYBA] VOZOVÝ PARK MÁ {len(broken)} VADNÝCH ŘÁDKŮ — nic se neplánuje",
             "=" * 65,
             f"Soubor: {path}", ""]
    excel_suspected = False
    for item in broken:
        lines.append(f"  řádek {item['line']:>3} | {item['type_code']}:")
        for col, value in item["values"].items():
            flag = ""
            if _LOOKS_LIKE_DATE_RE.match(str(value)):
                flag = "   <<< vypadá jako DATUM"
                excel_suspected = True
            lines.append(f"      {col:<16} = {value!r}{flag}")
        lines.append(f"      ({item['error']})")
    if excel_suspected:
        lines += ["",
                  "Hodnota ve tvaru data znamená, že soubor prošel Excelem —",
                  "ten české locale bere '17.4' jako 17. duben a při uložení",
                  "to zapíše natvrdo. Exportuj vozový park znovu z ESO9,",
                  "nebo ho oprav v textovém editoru (ne v Excelu)."]
    lines += ["",
              "Řádek se ZÁMĚRNĚ nepřeskakuje: chybějící typ vozidla by tiše",
              "zmenšil flotilu a plán by počítal s auty, která nemáme."]
    return "\n".join(lines)


def load_vehicle_types_db(path: str | None = None, block_id: str = "") -> list:
    """
    Načte vozový park — každý řádek = jeden typ vozidla.

    Bez `path` si sám vezme nejnovější `vehicle_types-YYYYMMDD.csv`
    z data/static (viz find_vehicle_types_file). Vrátí list
    pseudo-vozidel expandovaných podle count_block_{block_id}
    (pokud sloupec existuje), jinak podle available_count.
    """
    vehicles = []
    p = Path(path) if path else find_vehicle_types_file()
    if not p.exists():
        raise FileNotFoundError(
            f"[CHYBA] {p} nenalezen.\n"
            f"        Vozový park patří do {VEHICLE_TYPES_DIR} pod názvem "
            f"vehicle_types-YYYYMMDD.csv."
        )

    with open(p, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        required = {"type_code", "type_name", "max_kg", "cost_per_km", "available_count"}
        fields = set(reader.fieldnames or [])
        if not required.issubset(fields):
            # Jediné pole s čárkami uvnitř = soubor je ve starém formátu.
            # Radši jasná chyba než plánovat s prázdnou flotilou.
            if len(reader.fieldnames or []) == 1 and "," in (reader.fieldnames or [""])[0]:
                raise ValueError(
                    f"[CHYBA] {p} je ve STARÉM formátu (oddělovač čárka).\n"
                    f"        Od 6. 8. 2026 je vozový park středníkový a má "
                    f"sloupec valid_for_date.\n"
                    f"        Exportuj soubor znovu jako "
                    f"vehicle_types-YYYYMMDD.csv."
                )
            raise ValueError(f"[CHYBA] {p} nemá povinné sloupce: {sorted(required)}")

        block_col = f"count_block_{block_id}" if block_id else ""
        use_block_col = block_col and block_col in (reader.fieldnames or [])
        if use_block_col:
            print(f"  [vehicle] Používám per-block počty ze sloupce '{block_col}'")
        elif block_col:
            print(f"  [vehicle] Sloupec '{block_col}' nenalezen — beru available_count")
        else:
            print("  [vehicle] Počty vozidel z available_count (sdílený pool)")

        broken: list[dict] = []
        for line_no, row in enumerate(reader, start=2):    # 1 = hlavička
            type_code = str(row.get("type_code", "")).strip()
            if not type_code or type_code.startswith("#"):
                continue

            count_col = block_col if use_block_col else "available_count"
            try:
                max_kg_raw = float(row["max_kg"])
                # Kapacitní násobič — solver počítá s mírně vyšší kapacitou
                # (slack při balení, vzdušné mezery). Config: vehicle_capacity_multiplier.
                max_kg = max_kg_raw * float(CONFIG.get("vehicle_capacity_multiplier", 1.0))
                cost_per_km = float(row["cost_per_km"])
                count = int(float(row[count_col]))
            except (ValueError, KeyError, TypeError) as e:
                # Vadné číslo v povinném sloupci NESMÍ řádek tiše vyhodit —
                # typ vozidla by zmizel z flotily a plánovalo by se s menším
                # parkem, aniž by si toho někdo všiml. Typická příčina:
                # soubor prošel Excelem, který 17.4 přepsal na 17.04.2026.
                broken.append({
                    "line": line_no,
                    "type_code": type_code,
                    "values": {col: row.get(col, "") for col in
                               ("max_kg", "cost_per_km", count_col)},
                    "error": str(e),
                })
                continue

            if count <= 0:      # legitimní: typ, který dnes není k dispozici
                continue

            time_multiplier = float(row.get("time_multiplier") or 1.0)
            osrm_profile    = str(row.get("osrm_profile") or "driving").strip() or "driving"
            # start_cost: absolutní Kč fixní náklad za výjezd vozidla (modeluje
            # mzdu řidiče / amortizaci / overhead). Per-type, ne per-vehicle.
            # Default 0 pokud sloupec chybí (backward compat).
            start_cost      = float(row.get("start_cost_kc") or 0)
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

    if broken:
        raise ValueError(_broken_vehicle_rows_report(p, broken))
    if not vehicles:
        raise ValueError(f"[CHYBA] {p} neobsahuje žádné dostupné typy vozidel.")
    return vehicles


# ============================================================
#  DVOJLINKY (--double-runs, porušení L2)
#
#  Auto smí naložit ve skladu 2× za den. Modeluje se virtuálními
#  „druhá jízda" vozidly: kopie malého auta s plným druhým fixem
#  (start_cost) a nejdřívějším výjezdem CONFIG double_run_earliest.
#  OR-Tools neumí vazbu „vyjeď až po návratu prvního" mezi vozidly,
#  proto se po solve páruje: druhá jízda dostane fyzické auto, které
#  se vrátilo aspoň depot_loading_min před jejím výjezdem. Když
#  párování neexistuje, běh SPADNE — žádné tiché překrytí směn.
# ============================================================

# Dvojlinku smí jet jen malé auto. Práh je nad 1350×1.03 (L1 násobič),
# ale pod 2000 — funguje pro L0 i L1 bez znalosti syrové nosnosti.
DOUBLE_RUN_SMALL_MAX_KG = 1400
# Poznávací znamení virtuálního vozidla v id: TYPE_02_2R01. Segment bez
# podtržítka, aby type_code šel dál číst přes rsplit("_", 1).
DOUBLE_RUN_ID_TAG = "_2R"


def is_double_run_vehicle(vehicle_id: str) -> bool:
    return DOUBLE_RUN_ID_TAG in str(vehicle_id)


def build_double_run_vehicles(vehicles_expanded: list) -> list:
    """
    Virtuální „druhá jízda" vozidla pro malé typy.

    Nejvýš double_runs_max kusů celkem a nejvýš tolik per typ, kolik je
    fyzických aut (jedno auto = max jedna dvojlinka). Fix je start_cost
    + 1 Kč: solver tak vždy preferuje fyzické auto a virtuální sáhne
    až když fyzická došla — druhá jízda bez první nemá smysl (a párování
    by ji stejně zabilo).
    """
    limit = int(CONFIG.get("double_runs_max", 0))
    earliest = time_to_minutes(CONFIG["double_run_earliest"])

    by_type: dict[str, list[dict]] = {}
    for v in vehicles_expanded:
        if v["max_kg"] <= DOUBLE_RUN_SMALL_MAX_KG:
            by_type.setdefault(v["type_code"], []).append(v)

    virtuals = []
    # největší typ první — deficit malých pokrývá především TYPE_02
    for type_code, physicals in sorted(by_type.items(),
                                       key=lambda kv: -len(kv[1])):
        for i in range(min(len(physicals), limit - len(virtuals))):
            template = physicals[0]
            virtuals.append({
                **template,
                "id": f"{type_code}{DOUBLE_RUN_ID_TAG}{i + 1:02d}",
                "start_cost": template["start_cost"] + 1,
                "earliest_start_min": earliest,
            })
        if len(virtuals) >= limit:
            break
    return virtuals


def _route_departure_min(route: dict) -> int:
    return time_to_minutes(route["stops"][0]["arrival"])


def _route_return_min(route: dict) -> int:
    return time_to_minutes(route["stops"][-1]["arrival"])


def pair_double_runs(routes: list, vehicles_expanded: list | None = None) -> list:
    """
    Přiřadí druhé jízdy fyzickým autům, nebo spadne.

    Pravidla: stejný typ auta, fyzické auto max jednu dvojlinku, návrat
    prvního + depot_loading_min <= výjezd druhé jízdy. Druhé jízdy se
    berou od nejdřívějšího výjezdu a dostávají fyzické auto s nejdřívějším
    vyhovujícím návratem (nechává pozdější návraty pozdějším dvojlinkám).
    Po spárování nese route fyzické vehicle_id + příznak double_run.

    Když se žádné vrátivší se auto nehodí, vezme se **nečinné fyzické auto
    téhož typu z celé flotily** (`vehicles_expanded`) — jelo by to jako svou
    první a jedinou jízdu (double_run=False). Dřív se hledalo jen mezi auty,
    která už jela: cluster A vyčerpal svá auta a použil dvojlinku, cluster B
    měl auta, která celý den stála — a párování spadlo (audit 2.4).
    Vrátivší se auto má přednost: nečinné auto se šetří dalším depům
    (budget flotily ubývá po fyzických kusech).
    """
    reload_min = int(CONFIG.get("depot_loading_min", 40))
    virtual = [r for r in routes if is_double_run_vehicle(r["vehicle_id"])]
    if not virtual:
        return routes

    physical_by_type: dict[str, list[dict]] = {}
    for r in routes:
        if not is_double_run_vehicle(r["vehicle_id"]):
            physical_by_type.setdefault(r["type_code"], []).append(r)

    # nečinná fyzická auta (ve flotile, ale bez trasy) per typ
    used_ids = {r["vehicle_id"] for r in routes
                if not is_double_run_vehicle(r["vehicle_id"])}
    idle_by_type: dict[str, list[dict]] = {}
    for v in (vehicles_expanded or []):
        if is_virtual_vehicle(v) or v["id"] in used_ids:
            continue
        idle_by_type.setdefault(v["type_code"], []).append(v)

    paired_ids: set[str] = set()
    failures = []
    for v_route in sorted(virtual, key=_route_departure_min):
        departure = _route_departure_min(v_route)
        candidates = sorted(
            (p for p in physical_by_type.get(v_route["type_code"], [])
             if p["vehicle_id"] not in paired_ids
             and _route_return_min(p) + reload_min <= departure),
            key=_route_return_min,
        )
        if candidates:
            host = candidates[0]
            paired_ids.add(host["vehicle_id"])
            v_route["vehicle_id"] = host["vehicle_id"]
            v_route["driver"] = host.get("driver", "")
            v_route["double_run"] = True
            continue

        idle = idle_by_type.get(v_route["type_code"], [])
        if idle:
            car = idle.pop(0)
            print(f"  [DVOJLINKY] {v_route['vehicle_id']} (výjezd "
                  f"{departure // 60:02d}:{departure % 60:02d}): žádný včasný "
                  f"návrat, jede jako první jízda NEČINNÉHO auta {car['id']}")
            v_route["vehicle_id"] = car["id"]
            v_route["driver"] = car.get("driver", "")
            v_route["double_run"] = False
            physical_by_type.setdefault(car["type_code"], []).append(v_route)
            continue

        same_type = physical_by_type.get(v_route["type_code"], [])
        failures.append(
            f"  - {v_route['vehicle_id']} (typ {v_route['type_code']}, "
            f"výjezd {departure // 60:02d}:{departure % 60:02d}): "
            f"žádné fyzické auto s návratem do "
            f"{(departure - reload_min) // 60:02d}:"
            f"{(departure - reload_min) % 60:02d}, nečinná auta typu: žádná\n"
            f"    návraty téhož typu: "
            + (", ".join(
                f"{p['vehicle_id']}"
                f"{' (obsazeno)' if p['vehicle_id'] in paired_ids else ''}"
                f" {_route_return_min(p) // 60:02d}:"
                f"{_route_return_min(p) % 60:02d}"
                for p in sorted(same_type, key=_route_return_min)) or "žádná"))

    if failures:
        abort(
            "\n" + "=" * 65 + "\n"
            "[CHYBA] DVOJLINKY NEJDOU SPÁROVAT — plán se neukládá\n"
            + "=" * 65 + "\n"
            + "\n".join(failures) + "\n\n"
            f"Druhá jízda potřebuje fyzické auto vrácené aspoň "
            f"{reload_min} min před svým výjezdem (nakládka).\n"
            "Zvaž pozdější CONFIG double_run_earliest, nebo je den pro "
            "dvojlinky nevhodný (dlouhé první trasy).", EXIT_INFEASIBLE)
    return routes


def load_orders_day(path: str) -> list:
    """
    Načte orders_day.csv (výstup z prepare_inputs.py).
    Vrátí list objednávek solver-ready.
    Pole 'id' a 'name' jsou aliasy pro kompatibilitu s algoritmem.
    """
    orders = []
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"[CHYBA] {path} nenalezen.\n"
            "Spusť nejdřív: python prepare_inputs.py riro-YYYYMMDD-POB.csv"
        )

    with open(p, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = ["order_number", "location_code", "time_from", "time_to",
                    "weight_kg", "lat", "lon", "service_sec"]
        # Fail-fast na hlavičce: bez service_sec by se skipnul KAŽDÝ řádek
        # a uživatel by dostal matoucí "neobsahuje žádné objednávky".
        if "service_sec" not in (reader.fieldnames or []):
            abort(
                f"[CHYBA] {path} je ze starého prepare — chybí sloupec 'service_sec'.\n"
                "        Solver podporuje jen data s předpočítaným časem zastávky "
                "z ESO9.\n"
                "        Vytvoř soubor znovu: python prepare_inputs_v6.py {DEPO}",
                EXIT_DATA)
        # Vadný řádek NIKDY nepřeskočit: objednávka by zmizela dřív, než ji
        # uvidí závory (validate_orders_servable, verify_plan_complete) —
        # plán by se uložil bez ní a nikdo by to nepoznal. Chyby se sbírají
        # a po dočtení souboru běh spadne se soupisem (exit 2 = vadná data).
        bad_rows: list[str] = []
        for i, row in enumerate(reader, 1):
            order_no = (row.get("order_number") or "").strip() or "?"
            missing = [c for c in required if c not in row or not (row[c] or "").strip()]
            if missing:
                bad_rows.append(f"  - řádek {i} ({order_no}): chybí {missing}")
                continue

            try:
                weight_kg = float(row["weight_kg"])
                lat       = float(row["lat"])
                lon       = float(row["lon"])
                service_sec = int(float(row["service_sec"]))
            except ValueError as e:
                bad_rows.append(f"  - řádek {i} ({order_no}): neplatná čísla — {e}")
                continue

            orders.append({
                # Primární pole z prepare_inputs
                "order_number":  row["order_number"].strip(),
                "location_code": row["location_code"].strip(),
                "customer_name": row.get("customer_name", "").strip(),
                "block_id":      row.get("block_id", "").strip(),
                "time_from":     row["time_from"].strip(),
                "time_to":       row["time_to"].strip(),
                "payload_raw":   row.get("payload_raw", "").strip(),
                "weight_kg":     weight_kg,
                "lat":           lat,
                "lon":           lon,
                "city":          row.get("city", "").strip(),
                "note":          row.get("note", "").strip(),
                "service_sec":   service_sec,
                # Rampa (0/1) — do výstupů a pro výběr L3 (kamion předem);
                # optimalizaci linek neřídí. Starší prepared soubory sloupec
                # nemají → 0.
                "ramp":          1 if row.get("ramp", "").strip() == "1" else 0,

                # Aliasy pro kompatibilitu s algoritmem (neměň)
                "id":            row["order_number"].strip(),
                "name":          row.get("customer_name", row["order_number"]).strip(),
            })

    if bad_rows:
        abort("\n".join([
            "", "=" * 65,
            f"[CHYBA] VADNÉ ŘÁDKY V {p.name} — plánování zastaveno",
            "=" * 65,
            f"{len(bad_rows)} z {len(bad_rows) + len(orders)} řádků nejde načíst:",
            *bad_rows,
            "",
            "Žádný plán se neuložil — plán bez těchto objednávek by znamenal "
            "nerozvezené zboží.",
            "Prepared soubor vzniká z prepare_inputs_v6.py; vygeneruj ho znovu "
            "(nebo oprav ručně upravený soubor).",
        ]), EXIT_DATA)

    if not orders:
        abort(f"[CHYBA] {path} neobsahuje žádné objednávky.", EXIT_DATA)

    return orders


# ============================================================
#  POMOCNÉ FUNKCE
# ============================================================

def time_to_minutes(t: str) -> int:
    h, m = map(int, t.strip().split(":"))
    return h * 60 + m


def service_time_min(order: dict) -> int:
    """
    Doba zastávky = `service_sec` z ESO9, zaokrouhlená nahoru na minuty.

    SEC je KOMPLETNÍ čas (už zahrnuje složku za váhu i manipulaci), takže se
    k němu nic nepřipočítává — je to jediný zdroj pravdy. Chybí-li, jde o data
    ze starého prepare a solver s nimi vědomě odmítá počítat.
    """
    sec = order.get("service_sec")
    try:
        sec_int = int(sec)
    except (TypeError, ValueError):
        sec_int = 0
    if sec_int <= 0:
        raise ValueError(
            f"[CHYBA] Objednávka {order.get('order_number', '?')} nemá platný "
            f"service_sec (={sec!r}). Solver podporuje jen data s předpočítaným "
            f"časem zastávky z ESO9 — vytvoř orders CSV znovu přes prepare_inputs_v6.py."
        )
    return math.ceil(sec_int / 60)


# ============================================================
#  POJISTKY — žádná objednávka se nesmí tiše ztratit
#
#  Vznik: 31. 7. 2026 poslalo ESO9 vadné SEC (až 96 742 s = 26,9 h).
#  Servis delší než strop trasy udělá objednávku neobsloužitelnou,
#  a protože jsou všechny objednávky povinné, OR-Tools prohlásí celý
#  cluster za neřešitelný — a jeho objednávky (49 z 91!) se tiše
#  ztratily z uloženého plánu. Tyhle závory to už nikdy nedovolí.
# ============================================================

def validate_orders_servable(orders: list,
                             vehicle_time_by_id: dict | None = None,
                             vehicles_expanded: list | None = None) -> None:
    """
    Fail-fast PŘED solvem: každá objednávka musí být vůbec obsloužitelná.
    Každá kontrola je levná (O(objednávky × vozidla)) a odpoví za sekundy —
    místo aby solver půl hodiny hledal řešení, které neexistuje, a pak
    hlásil „cluster neřešitelný" bez viníka.

    1. servis < nejzazší návrat (latest_return_h) — chytá vadné SEC z ESO9
       (prepare má vlastní limit SERVICE_SEC_MAX, tohle je druhá závora
       přímo v solveru, chytí i staré prepared soubory)
    2. je-li k dispozici matice: objednávka dosažitelná ze skladu tam i zpět
       alespoň v jedné vozidlové matici (sentinel UNREACHABLE_TIME_MIN)
    3. s --driver-breaks: cesta tam a zpět <= denní limit jízdy řidiče
    4. je-li k dispozici flotila: objednávka se vejde do NEJVĚTŠÍHO auta
       (nosnost už s capacity_multiplier)
    5. je-li k dispozici matice: okno je stihnutelné — nejrychlejší auto
       tam dojede do konce okna (+ povolené protažení) a po nejdřívějším
       možném začátku okna se stihne vrátit do nejzazšího návratu
    Vše končí exit 2 (vadná data) — na tohle se nesmí eskalovat porušení.
    """
    max_dur_min = int(CONFIG["latest_return_h"] * 60)
    bad_service = []
    for o in orders:
        svc = service_time_min(o)
        if svc >= max_dur_min:
            bad_service.append(
                f"  - {o['order_number']} {o.get('customer_name', '')}: "
                f"servis {svc} min ({int(o['service_sec']):,} s) "
                f">= nejzazší návrat {max_dur_min} min"
            )

    bad_reach = []
    if vehicle_time_by_id:
        mats = list(vehicle_time_by_id.values())
        for i, o in enumerate(orders, start=1):   # node 0 = sklad
            reachable = any(
                m[0][i] < UNREACHABLE_TIME_MIN and m[i][0] < UNREACHABLE_TIME_MIN
                for m in mats
            )
            if not reachable:
                bad_reach.append(
                    f"  - {o['order_number']} {o.get('customer_name', '')}: "
                    f"lat={o['lat']}, lon={o['lon']} — nedosažitelná ze skladu "
                    f"v žádném profilu"
                )

    # 3. režim řidiče EU: samotná cesta tam a zpět nesmí překročit denní
    #    limit jízdy — jinak objednávku neodveze žádný kamion s jedním řidičem
    bad_drive = []
    if vehicle_time_by_id and CONFIG.get("_driver_breaks_enabled"):
        max_drive = int(CONFIG["driver_max_drive_h"] * 60)
        mats = list(vehicle_time_by_id.values())
        for i, o in enumerate(orders, start=1):
            best = min(m[0][i] + m[i][0] for m in mats)
            if best > max_drive:
                bad_drive.append(
                    f"  - {o['order_number']} {o.get('customer_name', '')}: "
                    f"tam a zpět {best:.0f} min jízdy > denní limit "
                    f"{max_drive} min ({CONFIG['driver_max_drive_h']:g} h)"
                )

    # 4. váha: objednávka těžší než největší dostupné auto nemá kam
    bad_weight = []
    if vehicles_expanded:
        biggest = max(vehicles_expanded, key=lambda v: v["max_kg"])
        for o in orders:
            if float(o["weight_kg"]) > float(biggest["max_kg"]):
                bad_weight.append(
                    f"  - {o['order_number']} {o.get('customer_name', '')}: "
                    f"{o['weight_kg']:,.0f} kg > největší dostupné auto "
                    f"{biggest['id']} ({biggest['max_kg']:,.0f} kg vč. rezervy)"
                )

    # 5. okno: nejrychlejší auto to musí stihnout tam do konce okna a zpět
    #    do nejzazšího návratu — jinak je objednávka neobsloužitelná
    #    nezávisle na zbytku dne (jen matice + okno, žádný solver)
    bad_window = []
    if vehicle_time_by_id:
        mats = list(vehicle_time_by_id.values())
        tw_before = int(CONFIG.get("tw_expand_before_min", 0) or 0)
        tw_after  = int(CONFIG.get("tw_expand_after_min", 0) or 0)
        depot_open = time_to_minutes(DEPOT["open"])
        for i, o in enumerate(orders, start=1):
            go   = min(m[0][i] for m in mats)
            back = min(m[i][0] for m in mats)
            if go >= UNREACHABLE_TIME_MIN or back >= UNREACHABLE_TIME_MIN:
                continue                       # už hlásí bod 2
            tw_start = max(depot_open, time_to_minutes(o["time_from"]) - tw_before)
            tw_end   = time_to_minutes(o["time_to"]) + tw_after
            svc      = service_time_min(o)
            if depot_open + go > tw_end:
                bad_window.append(
                    f"  - {o['order_number']} {o.get('customer_name', '')}: "
                    f"okno {o['time_from']}–{o['time_to']}, ale ze skladu je to "
                    f"{go:.0f} min — do konce okna (+{tw_after} min) se nedá dojet"
                )
            elif max(tw_start, depot_open + go) + svc + back > max_dur_min:
                bad_window.append(
                    f"  - {o['order_number']} {o.get('customer_name', '')}: "
                    f"okno {o['time_from']}–{o['time_to']}, servis {svc} min, "
                    f"zpět {back:.0f} min — návrat po nejzazším čase "
                    f"({max_dur_min // 60:02d}:{max_dur_min % 60:02d})"
                )

    if bad_service or bad_reach or bad_drive or bad_weight or bad_window:
        msg = ["", "=" * 65,
               "[CHYBA] NEOBSLOUŽITELNÉ OBJEDNÁVKY — plánování zastaveno",
               "=" * 65]
        if bad_service:
            msg.append(f"\nServis delší než nejzazší návrat "
                       f"({CONFIG['latest_return_h']} h) — vadné SEC z ESO9:")
            msg.extend(bad_service)
        if bad_reach:
            msg.append("\nNedosažitelné ze skladu (zkontroluj GPS / routing instanci):")
            msg.extend(bad_reach)
        if bad_drive:
            msg.append("\nPřes denní limit jízdy řidiče (--driver-breaks):")
            msg.extend(bad_drive)
        if bad_weight:
            msg.append("\nTěžší než největší dostupné auto (chybí kamion / "
                       "střední auto ve flotile, nebo vadná váha):")
            msg.extend(bad_weight)
        if bad_window:
            msg.append("\nNestihnutelné okno (vzdálenost vs. okno / nejzazší návrat):")
            msg.extend(bad_window)
        msg.append("\nŽádný plán se neuložil — plán bez těchto objednávek by "
                   "znamenal nerozvezené zboží. Oprav data a spusť znovu.")
        abort("\n".join(msg), EXIT_DATA)


def verify_plan_complete(orders: list, routes: list) -> None:
    """
    Finální invariant PŘED uložením: každá objednávka ze vstupu je v plánu
    právě jednou. Cokoli jiného = bug solveru nebo vadná data → neuložit NIC.
    """
    planned = Counter(s["id"] for r in routes
                      for s in r.get("stops", []) if "id" in s)
    input_ids = [o["id"] for o in orders]
    input_set = set(input_ids)
    missing = [oid for oid in input_ids if oid not in planned]
    dupes   = [oid for oid, cnt in planned.items() if cnt > 1]
    extra   = [oid for oid in planned if oid not in input_set]
    if not missing and not dupes and not extra:
        return

    by_id = {o["id"]: o for o in orders}
    msg = ["", "=" * 65,
           "[CHYBA] PLÁN NENÍ KOMPLETNÍ — výstup se NEUKLÁDÁ",
           "=" * 65,
           f"Vstup: {len(input_ids)} objednávek | v plánu: {len(planned)}"]
    if missing:
        msg.append(f"\nChybí v plánu ({len(missing)}):")
        for oid in missing:
            o = by_id[oid]
            msg.append(f"  - {oid} {o.get('customer_name', '')} "
                       f"({o.get('city', '')}, {o['weight_kg']:.0f} kg)")
    if dupes:
        msg.append(f"\nDuplicitně naplánované ({len(dupes)}): " + ", ".join(dupes))
    if extra:
        msg.append(f"\nV plánu navíc, nejsou ve vstupu ({len(extra)}): "
                   + ", ".join(extra))
    msg.append("\nPlán s tiše vynechanými objednávkami = nerozvezené zboží. "
               "Tohle je chyba solveru nebo dat — nahlas ji.")
    abort("\n".join(msg), EXIT_ERROR, EXIT_DATA)


def auto_n_clusters(n_orders: int, n_vehicles: int) -> int:
    # Block-level solve: uvnitř business blocku nechceme zbytečně jemné dělení
    if n_orders <= 100:
        return 2
    if n_orders <= 300:
        return 3
    return 4



def cluster_profile(cluster: list) -> dict:
    """Lehký profil clusteru pro vehicle allocation."""
    if not cluster:
        return {"kg": 0.0, "tightness": 0.0, "radial_km": 0.0,
                "stops": 0, "demand_score": 0.0}

    kg = float(sum(o["weight_kg"] for o in cluster))
    widths = [max(1, time_to_minutes(o["time_to"]) - time_to_minutes(o["time_from"]))
              for o in cluster]
    tightness = float(np.mean([1.0 / w for w in widths]))

    depot_lat, depot_lon = DEPOT["lat"], DEPOT["lon"]
    radial = []
    for o in cluster:
        dx = (o["lon"] - depot_lon) * 71.0
        dy = (o["lat"] - depot_lat) * 111.0
        radial.append((dx * dx + dy * dy) ** 0.5)
    radial_km = float(np.mean(radial)) if radial else 0.0

    demand_score = (kg * 1.0 + len(cluster) * 220.0
                    + radial_km * 140.0 + tightness * 180000.0)
    return {"kg": kg, "tightness": tightness, "radial_km": radial_km,
            "stops": len(cluster), "demand_score": demand_score}


def expected_vehicle_need(cluster: list, vehicles: list) -> float:
    if not cluster:
        return 0.0
    profile  = cluster_profile(cluster)
    avg_cap  = max(1.0, float(np.mean([v["max_kg"] for v in vehicles])))
    return max(1.0, profile["kg"] / avg_cap
               + profile["stops"] / 14.0
               + profile["tightness"] * 150.0)


def estimate_cluster_insertion_score(order: dict, target_cluster: list,
                                      centroid: np.ndarray | None) -> float:
    tw_width   = max(1, time_to_minutes(order["time_to"]) - time_to_minutes(order["time_from"]))
    tw_penalty = 120.0 / tw_width
    geo_penalty = 0.0
    if centroid is not None:
        dx = (order["lon"] - centroid[1]) * 71.0
        dy = (order["lat"] - centroid[0]) * 111.0
        geo_penalty = (dx * dx + dy * dy) ** 0.5
    if not target_cluster:
        compatibility = 0.0
    else:
        widths = [max(1, time_to_minutes(o["time_to"]) - time_to_minutes(o["time_from"]))
                  for o in target_cluster]
        avg_width = float(np.mean(widths))
        compatibility = abs(avg_width - tw_width) / max(avg_width, tw_width, 1)
    return geo_penalty * 1.0 + tw_penalty * 25.0 + compatibility * 35.0


# ============================================================
#  MATICE — OSRM (driving) nebo ORS (driving-hgv)
# ============================================================

# Profily které používají ORS API místo OSRM
_ORS_PROFILES = {"driving-hgv"}


def _sanitize_matrix(
    durations: np.ndarray,
    distances: np.ndarray,
    locations: list,
    profile: str,
) -> tuple:
    """
    Detekuje NaN/inf v OSRM/ORS matici, hlásí konkrétní problematické páry
    a nahrazuje je sentinelem UNREACHABLE_TIME_MIN.

    Hard-failuje pokud je rozbitých víc než práh pro daný profil
    (viz unreachable_fail_pct) — hlídač kvality dat, ne bezpečnostní pojistka.
    """
    # Kombinovaná maska: rozbité je to, co je NaN/inf v durations NEBO distances.
    # Ignoruj diagonálu (přepíše se na 0 v _parse_matrix_result).
    bad_mask = ~np.isfinite(durations) | ~np.isfinite(distances)
    np.fill_diagonal(bad_mask, False)
    bad_count = int(bad_mask.sum())

    if bad_count == 0:
        return durations, distances

    total_off_diag = durations.size - durations.shape[0]   # n² - n
    bad_pct        = bad_count / total_off_diag if total_off_diag else 0.0

    print(f"  [WARN] Matrix ({profile}): {bad_count} nedosažitelných párů "
          f"({bad_pct*100:.2f} % off-diagonal entries)")

    # Ukázat první 5 problematických dvojic (lat,lon → lat,lon)
    bad_pairs = np.argwhere(bad_mask)
    for i, j in bad_pairs[:5]:
        lat_a, lon_a = locations[i]
        lat_b, lon_b = locations[j]
        print(f"         [{i:>3}] ({lat_a:.4f},{lon_a:.4f}) → "
              f"[{j:>3}] ({lat_b:.4f},{lon_b:.4f})  "
              f"duration={durations[i,j]}, distance={distances[i,j]}")
    if len(bad_pairs) > 5:
        print(f"         ... a dalších {len(bad_pairs) - 5} párů")

    limit = unreachable_fail_pct(profile)
    if bad_pct > limit:
        abort(
            f"\n[CHYBA] OSRM/ORS matrix má {bad_count} nedosažitelných párů "
            f"({bad_pct*100:.2f} % > limit {limit*100:.1f} % pro profil '{profile}').\n"
            f"Zkontroluj GPS souřadnice — pravděpodobně jsou body mimo silniční "
            f"síť nebo na izolovaném ostrově grafu.\n"
            f"Pokud jsou data v pořádku a jde o legitimní omezení vozidla, "
            f"zvaž úpravu prahu v UNREACHABLE_MATRIX_FAIL_PCT_BY_PROFILE."
        )

    # Pod prahem: nahraď sentinelem OBĚ matice na stejných pozicích.
    # Pár je "rozbitý" pokud je rozbitý v kterékoliv matici → obě hodnoty
    # nastavíme konzistentně, aby downstream kód (cost callback, time callback,
    # LNS scoring) viděl pár identicky jako "prohibitivně drahý".
    durations = np.where(bad_mask, UNREACHABLE_TIME_MIN, durations)
    distances = np.where(bad_mask, UNREACHABLE_TIME_MIN, distances)
    return durations, distances


def _parse_matrix_result(data: dict, profile: str, locations: list) -> tuple:
    """Převede JSON odpověď (OSRM nebo ORS) na numpy matice a aplikuje buffer."""
    durations_sec = np.array(data["durations"], dtype=float)
    distances_m   = np.array(data["distances"],  dtype=float)
    durations_min = durations_sec / 60.0
    distances_km  = distances_m   / 1000.0

    # Sanitizace PŘED aplikací bufferu — aby NaN×(1+pct) nešířilo problém dál
    durations_min, distances_km = _sanitize_matrix(
        durations_min, distances_km, locations, profile
    )

    fixed = CONFIG["time_buffer_fixed_min"]
    pct   = CONFIG["time_buffer_pct"]
    durations_buffered = durations_min * (1 + pct) + fixed
    np.fill_diagonal(durations_buffered, 0)
    np.fill_diagonal(distances_km, 0)
    return distances_km, durations_buffered


def _profile_fallback_or_fail(locations: list, profile: str, reason: str) -> tuple:
    """
    Rozhodne co dělat když routing pro NE-driving profil (typicky driving-hgv)
    selže. DEFAULT = hard-fail (SystemExit), aby se kamiony nikdy tiše
    nenaplánovaly po osobních trasách. S --allow-profile-fallback vědomě
    spadne na 'driving' a jen varuje.
    """
    if ALLOW_PROFILE_FALLBACK:
        print(f"  [WARN] Profil '{profile}': {reason} → fallback na 'driving' "
              f"(--allow-profile-fallback aktivní).")
        return get_matrix(locations, profile="driving")
    abort(
        f"\n[CHYBA] Routing pro profil '{profile}' selhal: {reason}\n"
        f"        Těžká vozidla (ORS / driving-hgv) NEJSOU dostupná. Plánování by je\n"
        f"        jinak tiše počítalo jako osobní auta → špatné trasy pro kamiony\n"
        f"        (mosty, úzké uličky, váhové/výškové zákazy).\n"
        f"        • Zkontroluj ORS kontejner (ors-current / ors-stable) a jeho logy.\n"
        f"        • Vědomě dovolit fallback na osobní profil: --allow-profile-fallback"
    , EXIT_ERROR)


def get_matrix(locations: list, profile: str = "driving") -> tuple:
    """
    Stáhne distance+time matici pro daný profil.
      driving     → OSRM (port 5000), GET /table/v1/driving/...
      driving-hgv → ORS  (port 8080), POST /ors/v2/matrix/driving-hgv
    Když NE-driving profil selže: hard-fail (viz _profile_fallback_or_fail),
    nebo fallback na 'driving' pokud je aktivní --allow-profile-fallback.
    Vrátí (distances_km, durations_buffered) — obě numpy matice.
    """
    n = len(locations)

    if profile in _ORS_PROFILES:
        base_url = CONFIG["osrm_urls"].get(profile, "http://localhost:8080")
        url      = f"{base_url}/ors/v2/matrix/{profile}"
        payload  = {
            "locations": [[lon, lat] for lat, lon in locations],
            "metrics":   ["duration", "distance"],
        }
        print(f"  Počítám matici {n}×{n} přes ORS (profil: {profile})...")
        t0 = time.time()
        try:
            r = requests.post(url, json=payload, timeout=600)
            if r.status_code >= 400:
                return _profile_fallback_or_fail(
                    locations, profile,
                    f"HTTP {r.status_code}: {r.text[:200]}")
        except requests.exceptions.RequestException as e:
            return _profile_fallback_or_fail(
                locations, profile, f"{base_url} neodpovídá ({type(e).__name__})")
        print(f"  Matice OK ({time.time() - t0:.0f} s).")
        return _parse_matrix_result(r.json(), profile, locations)

    else:
        base_url = CONFIG["osrm_urls"].get(profile, CONFIG["osrm_url"])
        coords   = ";".join(f"{lon},{lat}" for lat, lon in locations)
        url      = f"{base_url}/table/v1/{profile}/{coords}"
        params   = {"annotations": "duration,distance"}
        print(f"  Počítám matici {n}×{n} přes OSRM (profil: {profile})...")
        t0 = time.time()
        try:
            r = requests.get(url, params=params, timeout=600)
            if r.status_code >= 400 and profile != "driving":
                return _profile_fallback_or_fail(
                    locations, profile, f"HTTP {r.status_code}")
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            if profile != "driving":
                return _profile_fallback_or_fail(
                    locations, profile, f"{base_url} neodpovídá ({type(e).__name__})")
            abort("\n[CHYBA] OSRM neběží. Spusť: docker start osrm-server", EXIT_ERROR)
        print(f"  Matice OK ({time.time() - t0:.0f} s).")
        return _parse_matrix_result(r.json(), profile, locations)


# ============================================================
#  SEED PARTICE — 3 různé způsoby dělení
# ============================================================

def partition_kmeans(orders: list, n_clusters: int, seed: int) -> list:
    if n_clusters >= len(orders):
        return list(range(len(orders)))
    coords = np.array([[o["lat"], o["lon"]] for o in orders])
    km = KMeans(n_clusters=n_clusters, random_state=seed, n_init=10)
    return km.fit_predict(coords).tolist()


def partition_sweep(orders: list, n_clusters: int) -> list:
    """Sweep: seřadí zastávky dle úhlu od depa jako ručička hodin."""
    angles = [math.atan2(o["lat"] - DEPOT["lat"], o["lon"] - DEPOT["lon"])
              for o in orders]
    order_by_angle = sorted(range(len(orders)), key=lambda i: angles[i])
    labels = [0] * len(orders)
    cluster_size = math.ceil(len(orders) / n_clusters)
    for rank, idx in enumerate(order_by_angle):
        labels[idx] = min(rank // cluster_size, n_clusters - 1)
    return labels


def partition_tw_midpoint(orders: list, n_clusters: int, seed: int) -> list:
    """TW-aware clustering: kombinuje GPS + střed časového okna jako feature."""
    if n_clusters >= len(orders):
        return list(range(len(orders)))
    depot_open  = time_to_minutes(DEPOT["open"])
    depot_close = time_to_minutes(DEPOT["close"])
    day_len = max(depot_close - depot_open, 1)

    feats = []
    for o in orders:
        tw_mid      = (time_to_minutes(o["time_from"]) + time_to_minutes(o["time_to"])) / 2
        tw_norm     = (tw_mid - depot_open) / day_len
        lat_norm    = (o["lat"] - 48.5) / 3.0    # ČR: 48.5–51.5
        lon_norm    = (o["lon"] - 12.0) / 6.0    # ČR: 12–18
        feats.append([lat_norm * 0.6, lon_norm * 0.6, tw_norm * 0.4])

    km = KMeans(n_clusters=n_clusters, random_state=seed, n_init=10)
    return km.fit_predict(np.array(feats)).tolist()


def labels_to_clusters(orders: list, labels: list) -> tuple:
    n_clusters = max(labels) + 1
    clusters = [[] for _ in range(n_clusters)]
    indices  = [[] for _ in range(n_clusters)]
    for i, label in enumerate(labels):
        clusters[label].append(orders[i])
        indices[label].append(i)
    valid    = [(c, ix) for c, ix in zip(clusters, indices) if c]
    clusters = [v[0] for v in valid]
    indices  = [v[1] for v in valid]
    return clusters, indices


def is_virtual_vehicle(v: dict) -> bool:
    """Virtuální „druhá jízda" (dvojlinka) — smí vyjet až od earliest_start_min.
    Poznává se podle ID i podle pole, aby přežila i ručně sestavené flotily."""
    return bool(v.get("earliest_start_min")) or is_double_run_vehicle(v.get("id", ""))


def _cluster_latest_end_min(order: dict) -> int:
    """Nejzazší minuta, kdy jde objednávku obsloužit (okno + povolené
    protažení). Stejný výpočet jako v build_data_model."""
    return (time_to_minutes(order["time_to"])
            + int(CONFIG.get("tw_expand_after_min", 0) or 0))


def _spread_virtual_vehicles(clusters: list, assignments: list,
                             virtual: list) -> None:
    """
    Rozdělí dvojlinky (virtuální jízdy) mezi clustery POMĚRNĚ — podle
    počtu objednávek, které cluster vůbec může obsloužit po jejich
    nejdřívějším výjezdu. Cluster, kde po té hodině není co vozit,
    nedostane žádnou.

    Proč: 16. 8. 2026 (PR, poslední depo, 19 fyzických + 10 virtuálních
    aut) skončilo všech 10 virtuálních jízd v jednom clusteru, protože se
    auta rozdávala jako souvislý úsek seznamu seřazeného podle nosnosti.
    Cluster pak měl 4 fyzická auta na 41 ranních objednávek → neřešitelný
    ve všech třech seedech → depo bez plánu. Nemá to nic společného
    s budgetem solveru: řešení fyzicky neexistovalo.
    """
    if not virtual:
        return
    n_clusters = len(clusters)
    starts = [int(v.get("earliest_start_min") or 0) for v in virtual]
    earliest = min(starts) if starts else 0

    weights = [sum(1 for o in c if _cluster_latest_end_min(o) >= earliest)
               for c in clusters]
    if sum(weights) == 0:
        # nikde není odpolední práce → dvojlinky jsou k ničemu, ale
        # nesmí zůstat na hromadě; rozdej je podle velikosti clusteru
        weights = [len(c) for c in clusters]
    total = sum(weights) or 1

    # největší zbytek (Hamiltonova metoda) — součet sedí přesně
    quotas = [w / total * len(virtual) for w in weights]
    counts = [int(q) for q in quotas]
    for i in sorted(range(n_clusters), key=lambda i: quotas[i] - counts[i],
                    reverse=True)[: len(virtual) - sum(counts)]:
        counts[i] += 1

    # Cluster bez jediného fyzického auta virtuální nedostane — neměl by
    # by kdo jezdit ráno; jeho podíl přejde clusteru s nejvíc prací.
    for i in range(n_clusters):
        if counts[i] and not any(not is_virtual_vehicle(v)
                                 for v in assignments[i]):
            j = max((k for k in range(n_clusters) if k != i),
                    key=lambda k: weights[k], default=None)
            if j is not None:
                counts[j] += counts[i]
            counts[i] = 0

    # Rozdávat střídavě podle typu (větší typ první), aby cluster
    # nedostal jen jeden druh druhé jízdy, když je jich víc typů.
    pool = sorted(virtual, key=lambda v: (-v["max_kg"], v["id"]))
    for i in sorted(range(n_clusters), key=lambda i: weights[i], reverse=True):
        take, pool = pool[:counts[i]], pool[counts[i]:]
        assignments[i].extend(take)


def _repair_heaviest_order(clusters: list, assignments: list) -> list[str]:
    """
    Tvrdá podmínka VRP, kterou skóre podle součtů nevidí: každá objednávka
    se musí vejít do aspoň jednoho auta svého clusteru. Když ne, prohodí
    se nejmenší dostačující auto z jiného clusteru za nejmenší auto
    clusteru, kterému chybí — počty se nemění. Vrací seznam hlášek
    (prázdný = nic k opravě), aby si to fáze C mohla vypsat.
    """
    notes: list[str] = []
    n_clusters = len(clusters)

    def biggest(vs):
        return max((v["max_kg"] for v in vs), default=0.0)

    order = sorted(range(n_clusters),
                   key=lambda i: max((o["weight_kg"] for o in clusters[i]),
                                     default=0.0), reverse=True)
    for i in order:
        if not clusters[i] or not assignments[i]:
            continue
        heaviest = max(clusters[i], key=lambda o: o["weight_kg"])
        need = heaviest["weight_kg"]
        if biggest(assignments[i]) >= need:
            continue
        # kandidát: nejmenší auto jinde, které stačí a jehož odchodem
        # dárce nepřijde o auto na SVOJI nejtěžší objednávku
        best = None
        for j in range(n_clusters):
            if j == i:
                continue
            donor_need = max((o["weight_kg"] for o in clusters[j]), default=0.0)
            for v in assignments[j]:
                if is_virtual_vehicle(v) or v["max_kg"] < need:
                    continue
                rest = [w for w in assignments[j] if w is not v]
                if rest and biggest(rest) >= donor_need:
                    if best is None or v["max_kg"] < best[1]["max_kg"]:
                        best = (j, v)
        if best is None:
            notes.append(
                f"[!] Cluster {i}: objednávka {heaviest['order_number']} "
                f"({need:,.0f} kg) se nevejde do žádného auta clusteru "
                f"(největší {biggest(assignments[i]):,.0f} kg) a jinde není "
                f"volné dostačující auto — cluster bude neřešitelný.")
            continue
        j, v_in = best
        v_out = min((w for w in assignments[i] if not is_virtual_vehicle(w)),
                    key=lambda w: w["max_kg"], default=None)
        if v_out is None:
            v_out = min(assignments[i], key=lambda w: w["max_kg"])
        assignments[j].remove(v_in)
        assignments[i].remove(v_out)
        assignments[i].append(v_in)
        assignments[j].append(v_out)
        notes.append(
            f"[i] Cluster {i}: {v_in['id']} ({v_in['max_kg']:,.0f} kg) "
            f"přesunuto z clusteru {j} kvůli objednávce "
            f"{heaviest['order_number']} ({need:,.0f} kg); zpět jde {v_out['id']}.")
    return notes


def assign_vehicles_to_clusters(clusters: list, vehicles_expanded: list) -> list:
    """
    Přidělí vozidla clusterům dle kombinovaného demand score
    (kg + počet stop + TW tlak + vzdálenost od depa).

    Tři kroky, v tomto pořadí:
      1) FYZICKÁ auta podle demand score (souvislé úseky seznamu
         seřazeného podle nosnosti + lokální vyrovnání kapacit),
      2) tvrdá podmínka: nejtěžší objednávka každého clusteru se vejde
         do některého z jeho aut (jinak výměna s jiným clusterem),
      3) VIRTUÁLNÍ dvojlinky poměrně podle odpolední práce — nikdy jako
         souvislý blok do jednoho clusteru (viz _spread_virtual_vehicles).
    """
    n_clusters = len(clusters)
    if n_clusters == 0:
        return []

    physical = [v for v in vehicles_expanded if not is_virtual_vehicle(v)]
    virtual  = [v for v in vehicles_expanded if is_virtual_vehicle(v)]
    if not physical:
        # degenerovaný vstup (jen virtuální) — chovej se jako dřív
        physical, virtual = list(vehicles_expanded), []

    assignments = _assign_physical_by_demand(clusters, physical)
    for note in _repair_heaviest_order(clusters, assignments):
        print(f"      {note}")
    _spread_virtual_vehicles(clusters, assignments, virtual)
    return assignments


def _assign_physical_by_demand(clusters: list, vehicles_expanded: list) -> list:
    """Původní alokace podle demand score — jen pro fyzická auta."""
    n_clusters = len(clusters)
    n_vehicles = len(vehicles_expanded)

    profiles      = [cluster_profile(c) for c in clusters]
    demand_scores = [p["demand_score"] for p in profiles]
    total_score   = sum(demand_scores) or 1.0

    # Těžší auta prioritně pro těžší clustery
    vehicles_sorted = sorted(vehicles_expanded,
                              key=lambda v: (-v["max_kg"], v["cost_per_km"]))

    raw_counts = [max(1, round((score / total_score) * n_vehicles))
                  for score in demand_scores]

    while sum(raw_counts) > n_vehicles:
        removable = [i for i in range(n_clusters) if raw_counts[i] > 1]
        if not removable:
            break
        idx = max(removable,
                  key=lambda i: raw_counts[i] / max(
                      expected_vehicle_need(clusters[i], vehicles_expanded), 1.0))
        raw_counts[idx] -= 1

    while sum(raw_counts) < n_vehicles:
        idx = max(range(n_clusters),
                  key=lambda i: expected_vehicle_need(clusters[i], vehicles_expanded)
                                / max(raw_counts[i], 1))
        raw_counts[idx] += 1

    assignments = [[] for _ in range(n_clusters)]
    cluster_order = sorted(range(n_clusters),
                           key=lambda i: profiles[i]["demand_score"], reverse=True)
    vehicle_ptr = 0
    for c_idx in cluster_order:
        count = raw_counts[c_idx]
        assignments[c_idx] = vehicles_sorted[vehicle_ptr:vehicle_ptr + count]
        vehicle_ptr += count

    # Lokální repair: prohoď nevyrovnané kapacity
    def avg_cap(vs):
        return float(np.mean([v["max_kg"] for v in vs])) if vs else 0.0

    for _ in range(n_clusters * 2):
        needy = max(range(n_clusters),
                    key=lambda i: (profiles[i]["kg"]
                                   / max(avg_cap(assignments[i])
                                         * max(len(assignments[i]), 1), 1.0)))
        donor = min(range(n_clusters),
                    key=lambda i: (profiles[i]["kg"]
                                   / max(avg_cap(assignments[i])
                                         * max(len(assignments[i]), 1), 1.0)))
        if needy == donor or not assignments[donor] or not assignments[needy]:
            continue
        needy_best = max(assignments[needy], key=lambda v: v["cost_per_km"])
        donor_best = max(assignments[donor], key=lambda v: v["max_kg"])
        if donor_best["max_kg"] > needy_best["max_kg"]:
            assignments[donor].remove(donor_best)
            assignments[needy].remove(needy_best)
            assignments[donor].append(needy_best)
            assignments[needy].append(donor_best)

    return assignments


def extract_submatrix(full_dist: np.ndarray, cluster_vehicle_times: list,
                       cluster_order_indices: list) -> tuple:
    """
    Extrahuje sub-matici vzdáleností a list per-vehicle sub-matic časů
    pro daný cluster (depot = index 0, zastávky = cluster_order_indices + 1).

    cluster_vehicle_times: list[np.ndarray] — jedna matice na vozidlo v clusteru.
    Vrátí (sub_dist, sub_times) kde sub_times je list[np.ndarray].
    """
    full_indices = [0] + [i + 1 for i in cluster_order_indices]
    n = len(full_indices)

    sub_dist = np.zeros((n, n))
    for i, fi in enumerate(full_indices):
        for j, fj in enumerate(full_indices):
            sub_dist[i][j] = full_dist[fi][fj]

    sub_times = []
    for full_time in cluster_vehicle_times:
        st = np.zeros((n, n))
        for i, fi in enumerate(full_indices):
            for j, fj in enumerate(full_indices):
                st[i][j] = full_time[fi][fj]
        sub_times.append(st)

    return sub_dist, sub_times


# ============================================================
#  DATA MODEL + SOLVER (algoritmus beze změny od v2)
# ============================================================

def build_data_model(orders, vehicles_expanded, distances_km, durations_min_list):
    """
    durations_min_list: list[np.ndarray] — jedna časová matice na vozidlo.
    """
    depot_open  = time_to_minutes(DEPOT["open"])
    depot_close = time_to_minutes(DEPOT["close"])
    COST_SCALE  = 100

    # Defense-in-depth: symetrické s time matrix níže — chráníme .astype(int)
    # proti NaN/inf které by daly undefined behavior (INT_MIN).
    dist_arr = np.array(distances_km, dtype=float) * 100
    if not np.all(np.isfinite(dist_arr)):
        bad = int(np.sum(~np.isfinite(dist_arr)))
        print(f"  [WARN] build_data_model: {bad} NaN/inf v distance matrix, "
              f"nahrazuji sentinelem ({UNREACHABLE_TIME_MIN})")
        dist_arr = np.nan_to_num(
            dist_arr,
            nan=UNREACHABLE_TIME_MIN,
            posinf=UNREACHABLE_TIME_MIN,
            neginf=UNREACHABLE_TIME_MIN,
        )
    dist_int = dist_arr.astype(int).tolist()

    # Speed factor: solver vidí kratší cestovní časy (auta jedou ~3 % rychleji)
    speed_factor = float(CONFIG.get("travel_time_speed_factor", 1.0))
    # Defense-in-depth: matice by měla být čistá po _sanitize_matrix, ale kdyby
    # se NaN/inf dostaly sem přes aritmetiku, zabráníme undefined behavior v .astype(int).
    time_int_list = []
    for dm in durations_min_list:
        arr = np.array(dm, dtype=float) / speed_factor
        if not np.all(np.isfinite(arr)):
            bad = int(np.sum(~np.isfinite(arr)))
            print(f"  [WARN] build_data_model: {bad} NaN/inf v time matrix, "
                  f"nahrazuji sentinelem ({UNREACHABLE_TIME_MIN})")
            arr = np.nan_to_num(
                arr,
                nan=UNREACHABLE_TIME_MIN,
                posinf=UNREACHABLE_TIME_MIN,
                neginf=UNREACHABLE_TIME_MIN,
            )
        time_int_list.append(arr.astype(int).tolist())

    # Časová okna: rozšíření jen pro solver (data zůstávají beze změny)
    tw_before = int(CONFIG.get("tw_expand_before_min", 0))
    tw_after  = int(CONFIG.get("tw_expand_after_min",  0))
    tw = [(depot_open, depot_close)]
    for o in orders:
        start = max(0, time_to_minutes(o["time_from"]) - tw_before)
        end   = time_to_minutes(o["time_to"]) + tw_after
        tw.append((start, end))

    demands       = [0] + [int(o["weight_kg"]) for o in orders]
    service_times = [0] + [service_time_min(o) for o in orders]
    capacities    = [int(v["max_kg"])      for v in vehicles_expanded]
    costs_per_km  = [v["cost_per_km"]      for v in vehicles_expanded]
    start_costs   = [int(v["start_cost"] * COST_SCALE) for v in vehicles_expanded]
    # Dvojlinky: virtuální „druhá jízda" vozidla smí vyjet až od této minuty
    earliest_start = [int(v.get("earliest_start_min") or 0)
                      for v in vehicles_expanded]
    max_dur_min   = int(CONFIG["latest_return_h"] * 60)

    max_stops = int(CONFIG["max_stops_per_route"]) if CONFIG.get("max_stops_per_route") else None

    return {
        "dist_int":            dist_int,
        "time_int_list":       time_int_list,
        "time_windows":        tw,
        "demands":             demands,
        "service_times":       service_times,
        "capacities":          capacities,
        "costs_per_km":        costs_per_km,
        "start_costs":         start_costs,
        "earliest_start":      earliest_start,
        "num_vehicles":        len(vehicles_expanded),
        "depot":               0,
        "max_dur_min":         max_dur_min,
        "cost_scale":          COST_SCALE,
        "max_stops_per_route": max_stops,
    }


def _add_driver_breaks(routing, manager, time_dim, data) -> None:
    """
    Povinné pauzy řidiče (EU zjednodušeně): v žádném úseku trasy delším
    než driver_break_after_h nesmí chybět driver_break_min pauza.
    Kandidátní pauzy jsou volitelné intervaly kdekoli v dni —
    SetBreakDistanceDuration je aktivuje jen když je trasa potřebuje
    (krátká trasa = žádná pauza, žádný trest).

    node_visit_transits jsou indexované ROUTING INDEXEM (routing.Size()),
    ne uzlem — start/end indexy vozidel mají 0. Dřív se předával seznam
    v prostoru uzlů (kratší o 2×vozidla) → nedefinované chování v OR-Tools.
    """
    solver      = routing.solver()
    drive_limit = int(CONFIG["driver_break_after_h"] * 60)
    break_min   = int(CONFIG["driver_break_min"])
    horizon     = 24 * 60
    max_breaks  = max(1, horizon // drive_limit)
    transits    = [0] * routing.Size()
    for idx in range(routing.Size()):
        if routing.IsStart(idx) or routing.IsEnd(idx):
            continue
        transits[idx] = int(data["service_times"][manager.IndexToNode(idx)])
    for v_idx in range(data["num_vehicles"]):
        intervals = [
            solver.FixedDurationIntervalVar(
                0, horizon, break_min, True, f"break_v{v_idx}_{b}")
            for b in range(max_breaks)]
        time_dim.SetBreakIntervalsOfVehicle(intervals, v_idx, transits)
        time_dim.SetBreakDistanceDurationOfVehicle(drive_limit, break_min,
                                                   v_idx)


def _add_drive_limit(routing, manager, data) -> None:
    """
    Denní limit ČISTÉ JÍZDY per vozidlo (EU 561/2006 — driver_max_drive_h).
    Samostatná dimenze „Drive": transit = jen jízda (bez servisu),
    kapacita = limit v minutách. Tvrdá podmínka — trasa přes limit
    neexistuje. Zapíná se spolu s pauzami (--driver-breaks).
    """
    max_drive = int(CONFIG["driver_max_drive_h"] * 60)
    drive_cbs = []
    for v_idx in range(data["num_vehicles"]):
        cb_idx = routing.RegisterTransitCallback(
            lambda fi, ti, vi=v_idx:
                data["time_int_list"][vi][manager.IndexToNode(fi)]
                                         [manager.IndexToNode(ti)]
        )
        drive_cbs.append(cb_idx)
    routing.AddDimensionWithVehicleTransitAndCapacity(
        drive_cbs, 0, [max_drive] * data["num_vehicles"], True, "Drive")


def solve_cluster(orders, vehicles_expanded, distances_km, durations_min_list,
                  time_limit_sec: int,
                  strategy=routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION):
    data = build_data_model(orders, vehicles_expanded, distances_km, durations_min_list)
    n    = len(data["demands"])

    manager = pywrapcp.RoutingIndexManager(n, data["num_vehicles"], data["depot"])
    routing = pywrapcp.RoutingModel(manager)

    for v_idx in range(data["num_vehicles"]):
        cb_idx = routing.RegisterTransitCallback(
            lambda fi, ti, vi=v_idx: (
                data["dist_int"][manager.IndexToNode(fi)][manager.IndexToNode(ti)]
                * int(data["costs_per_km"][vi])
            )
        )
        routing.SetArcCostEvaluatorOfVehicle(cb_idx, v_idx)
        routing.SetFixedCostOfVehicle(data["start_costs"][v_idx], v_idx)

    demand_cb_idx = routing.RegisterUnaryTransitCallback(
        lambda fi: data["demands"][manager.IndexToNode(fi)]
    )
    routing.AddDimensionWithVehicleCapacity(demand_cb_idx, 0, data["capacities"],
                                             True, "Capacity")

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
        time_cb_indices, int(CONFIG.get("time_slack_max_min", 60)),
        [data["max_dur_min"]] * data["num_vehicles"],
        False, "Time"
    )

    # Limit počtu zastávek per trasa (sklad se nepočítá — callback vrací 0 pro depot)
    max_stops = data.get("max_stops_per_route")
    if max_stops:
        stop_cb_idx = routing.RegisterUnaryTransitCallback(
            lambda fi: 0 if manager.IndexToNode(fi) == data["depot"] else 1
        )
        routing.AddDimensionWithVehicleCapacity(
            stop_cb_idx, 0,
            [max_stops] * data["num_vehicles"],
            True, "Stops"
        )
    time_dim = routing.GetDimensionOrDie("Time")

    for node_idx in range(n):
        idx = manager.NodeToIndex(node_idx)
        tw  = data["time_windows"][node_idx]
        time_dim.CumulVar(idx).SetRange(tw[0], tw[1])

    # Dvojlinky: druhá jízda nesmí vyjet dřív než double_run_earliest
    for v_idx in range(data["num_vehicles"]):
        est = data.get("earliest_start", [0] * data["num_vehicles"])[v_idx]
        if est:
            time_dim.CumulVar(routing.Start(v_idx)).SetMin(est)

    # Režim řidiče EU (jen --driver-breaks; L3 kamionové trasy):
    # pauzy + denní limit čisté jízdy
    if CONFIG.get("_driver_breaks_enabled"):
        _add_driver_breaks(routing, manager, time_dim, data)
        _add_drive_limit(routing, manager, data)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy    = strategy
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.seconds = time_limit_sec
    params.log_search         = False

    solution = routing.SolveWithParameters(params)
    if not solution:
        return [], 0

    routes, total_cost = _extract_routes(manager, routing, solution, time_dim,
                                          vehicles_expanded, orders,
                                          np.array(distances_km),
                                          time_matrices=durations_min_list)
    return routes, total_cost


def _extract_routes(manager, routing, solution, time_dim,
                    vehicles_expanded, orders, distances_km,
                    time_matrices=None):
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
            # leg_km = km ujetý od předchozí zastávky k této
            leg_km = 0.0 if prev_node is None else round(float(distances_km[prev_node][node]), 1)
            route_km += leg_km
            if node == 0:
                # Skutečný čas výjezdu = příjezd první zastávky − jízdní čas ze skladu
                dep_t = t_var  # záloha: OR-Tools lower bound
                next_idx = solution.Value(routing.NextVar(index))
                if not routing.IsEnd(next_idx) and time_matrices is not None:
                    next_node = manager.IndexToNode(next_idx)
                    next_t    = solution.Min(time_dim.CumulVar(next_idx))
                    travel    = int(time_matrices[v_idx][0][next_node])
                    dep_t     = max(0, next_t - travel)
                dep_str = f"{dep_t // 60:02d}:{dep_t % 60:02d}"
                if t_start_min is None:
                    t_start_min = dep_t
                stops.append({"stop": DEPOT["name"], "arrival": dep_str, "kg": 0,
                               "leg_km": 0.0, "lat": DEPOT["lat"], "lon": DEPOT["lon"]})
            else:
                o = orders[node - 1]
                svc = service_time_min(o)
                t_dep = t_var + svc
                dep_str = f"{t_dep // 60:02d}:{t_dep % 60:02d}"
                stops.append({
                    "stop":             o["name"],
                    "id":               o["id"],
                    "location_code":    o.get("location_code", ""),
                    "arrival":          t_str,
                    "kg":               o["weight_kg"],
                    "window":           f"{o['time_from']}–{o['time_to']}",
                    "city":             o.get("city", ""),
                    "note":             o.get("note", ""),
                    "ramp":             o.get("ramp", 0),
                    "leg_km":           leg_km,
                    "service_min":      svc,
                    "departure":        dep_str,
                    "lat":              o["lat"],
                    "lon":              o["lon"],
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
#  WORKER PRO PARALELNÍ SOLVE
# ============================================================

def _worker_solve_cluster(args: dict) -> dict:
    # Windows spawn: worker je ČERSTVÝ import modulu s defaultním CONFIG.
    # Snapshot z hlavního procesu vrací runtime overridy (TW okna z CLI,
    # pauzy řidiče…) — bez něj by je fáze C/E tiše ignorovaly.
    CONFIG.update(args.get("config", {}))
    cluster_orders   = args["cluster_orders"]
    cluster_vehicles = args["cluster_vehicles"]
    sub_dist         = np.array(args["sub_dist"])
    sub_times        = [np.array(st) for st in args["sub_times"]]
    time_limit       = args["time_limit_sec"]
    strategy         = args.get("strategy")

    if strategy is None:
        routes, cost = solve_cluster(
            cluster_orders, cluster_vehicles, sub_dist, sub_times, time_limit)
    else:
        routes, cost = solve_cluster(
            cluster_orders, cluster_vehicles, sub_dist, sub_times, time_limit,
            strategy=strategy)
    return {
        "seed_name":   args["seed_name"],
        "cluster_idx": args["cluster_idx"],
        "routes":      routes,
        "cost":        cost,
        "strategy":    strategy,
    }


# ── Záchranný re-solve (audit 2.11): boxovaný budgetem, paralelní ────────────
RESCUE_MIN_SEC = 20        # kratší pokus nemá smysl (jen stavba modelu)
RESCUE_STRATEGIES = (
    routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION,
    routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC,
)


def rescue_time_for(time_per_cluster: int, remaining_sec: float | None) -> int:
    """
    Kolik sekund smí dostat záchranný re-solve nevyřešeného clusteru:
    3× čas původního pokusu, ale NIKDY víc, než kolik zbývá do konce
    celkového budgetu (běh musí držet slovo — na serveru na něj čekají
    další depa). `remaining_sec=None` = bez stropu (jen 3× tpc).
    Vrací 0, když už nezbývá nic → záchrana se přeskočí a běh končí exit 3
    (ledaže volající přidá --rescue-extra-min).
    """
    base = 3 * int(time_per_cluster)
    if remaining_sec is None:
        return max(0, base)
    return max(0, min(base, int(remaining_sec)))


def _rescue_unsolved_parallel(unsolved: list[int], seed_name: str, clusters,
                              c_indices, vehicle_asgn, distances_km,
                              vehicle_time_by_id, rescue_time: int,
                              n_workers: int) -> dict[int, dict]:
    """
    Všechny nevyřešené clustery najednou (paralelně stejným executorem jako
    fáze C), každý dvěma strategiemi VEDLE SEBE — wall clock = rescue_time,
    ne 2 × počet clusterů × rescue_time jako dřív. Vrací {cluster_idx:
    nejlevnější nalezený výsledek}.
    """
    tasks = []
    for ci in unsolved:
        c_orders, c_ix, c_vehicles = clusters[ci], c_indices[ci], vehicle_asgn[ci]
        cluster_v_times = [vehicle_time_by_id[v["id"]] for v in c_vehicles]
        sub_dist, sub_times = extract_submatrix(distances_km, cluster_v_times, c_ix)
        for strat in RESCUE_STRATEGIES:
            tasks.append({
                "seed_name":        seed_name,
                "cluster_idx":      ci,
                "cluster_orders":   c_orders,
                "cluster_vehicles": c_vehicles,
                "sub_dist":         sub_dist.tolist(),
                "sub_times":        [st.tolist() for st in sub_times],
                "time_limit_sec":   int(rescue_time),
                "strategy":         int(strat),
                "config":           dict(CONFIG),
            })
    found: dict[int, dict] = {}
    with ProcessPoolExecutor(max_workers=max(1, n_workers)) as executor:
        futures = {executor.submit(_worker_solve_cluster, a): a for a in tasks}
        for fut in as_completed(futures):
            a = futures[fut]
            try:
                res = fut.result()
            except Exception as e:                    # noqa: BLE001
                print(f"  [!] Záchrana cluster={a['cluster_idx']} "
                      f"strategie={a['strategy']}: {e}")
                continue
            if not res.get("routes"):
                continue
            ci = res["cluster_idx"]
            if ci not in found or res["cost"] < found[ci]["cost"]:
                found[ci] = res
    return found


# ============================================================
#  SOLUTION STATE
# ============================================================

class SolutionState:
    def __init__(self, orders, cluster_labels, clusters, cluster_indices,
                 vehicle_assignments, cluster_routes_list, cluster_costs):
        self.orders              = orders
        self.cluster_labels      = list(cluster_labels)
        self.clusters            = clusters
        self.cluster_indices     = cluster_indices
        self.vehicle_assignments = vehicle_assignments
        self.cluster_routes      = cluster_routes_list
        self.cluster_costs       = cluster_costs

    @property
    def total_cost(self):
        return sum(self.cluster_costs)

    def all_routes(self):
        routes = []
        for r_list in self.cluster_routes:
            routes.extend(r_list)
        routes.sort(key=lambda r: r["vehicle_id"])
        return routes


# ============================================================
#  PHASE C — výběr nejlepšího seedu
# ============================================================

def _unsolvable_cluster_report(seed_name: str, cluster_idx: int,
                               c_orders: list, c_vehicles: list) -> str:
    """Diagnostika pro fatální selhání clusteru — ať je hned vidět PROČ."""
    max_dur_min = int(CONFIG["latest_return_h"] * 60)
    total_kg  = sum(o["weight_kg"] for o in c_orders)
    total_cap = sum(v["max_kg"] for v in c_vehicles)
    physical  = [v for v in c_vehicles if not is_virtual_vehicle(v)]
    virtual   = [v for v in c_vehicles if is_virtual_vehicle(v)]
    worst_svc = sorted(c_orders, key=lambda o: o.get("service_sec", 0),
                       reverse=True)[:5]
    msg = ["", "=" * 65,
           f"[CHYBA] Cluster {cluster_idx} seedu '{seed_name}' je NEŘEŠITELNÝ "
           f"i po záchranném re-solve",
           "=" * 65,
           f"Objednávek: {len(c_orders)} ({total_kg:,.0f} kg) | "
           f"vozidel: {len(c_vehicles)} (kapacita {total_cap:,.0f} kg = součet "
           f"nosností, NE náklad)"]

    # ── Konkrétní viníci, které skóre podle součtů nevidí ────────────────
    findings: list[str] = []
    if c_orders and c_vehicles:
        heaviest = max(c_orders, key=lambda o: o["weight_kg"])
        biggest  = max(c_vehicles, key=lambda v: v["max_kg"])
        if heaviest["weight_kg"] > biggest["max_kg"]:
            findings.append(
                f"!!! Objednávka {heaviest['order_number']} "
                f"({heaviest['weight_kg']:,.0f} kg) je těžší než největší "
                f"auto clusteru {biggest['id']} ({biggest['max_kg']:,.0f} kg).")
    if virtual:
        earliest = min(int(v.get("earliest_start_min") or 0) for v in virtual)
        early_orders = [o for o in c_orders
                        if _cluster_latest_end_min(o) < earliest]
        line = (f"Fyzických aut {len(physical)}, dvojlinek {len(virtual)} "
                f"(smí vyjet až od {earliest // 60:02d}:{earliest % 60:02d}); "
                f"objednávek, které musí být hotové DŘÍV: {len(early_orders)}.")
        if len(physical) == 0 or (
                early_orders and len(early_orders) / max(len(physical), 1) > 12):
            line = "!!! " + line + " Na ranní okna je málo fyzických aut."
        findings.append(line)
    if findings:
        msg.append("Nálezy:")
        msg.extend(f"  {f}" for f in findings)

    msg.append(f"Nejzazší návrat: {max_dur_min // 60:02d}:{max_dur_min % 60:02d} | nejdelší servisy:")
    for o in worst_svc:
        svc_min = math.ceil(int(o.get("service_sec", 0)) / 60)
        msg.append(f"  - {o['order_number']} {o.get('customer_name', '')}: "
                   f"servis {svc_min} min, okno {o['time_from']}–{o['time_to']}, "
                   f"{o['weight_kg']:.0f} kg")
    msg.append("\nNejčastější příčiny: vadné SEC z ESO9 (servis > strop trasy), "
               "nesplnitelná časová okna, objednávka těžší než největší auto, "
               "málo FYZICKÝCH aut na ranní okna (dvojlinky ráno nejedou).")
    msg.append("Plán se NEUKLÁDÁ — jinak by objednávky clusteru tiše zmizely "
               "a zboží by se nerozvezlo.")
    return "\n".join(msg)


def resolve_seed_finalists(value, n_workers: int, n_clusters: int,
                           n_seeds: int = 3) -> int:
    """
    Kolik nejlepších seedů z fáze C dotáhnout ve fázi E.

    "auto" = tolik, kolik jich fáze E zvládne v JEDNÉ vlně workerů
    (workers // clusters) — wall clock se neprodlouží a slabý stroj
    samo spadne na 1 = dosavadní chování. Explicitní číslo se respektuje
    vždy; když se nevejde do jedné vlny, fáze E rozdělí čas na úlohu
    (phase_e_time_per_task) a wall clock drží taky.
    """
    if value == "auto":
        per_wave = n_workers // max(n_clusters, 1)
        return max(1, min(n_seeds, per_wave))
    n = int(value)
    if n < 1:
        raise ValueError(f"[CHYBA] seed_finalists musí být >= 1, je {value!r}.")
    return min(n, n_seeds)


def rank_seeds(results_by_seed: dict, expected_clusters: dict,
               penalty_kc: float) -> list[dict]:
    """
    Seřadí seedy fáze C podle penalizované ceny (nevyřešený cluster =
    +penalty_kc). Vrací [{seed, raw, penalized, solved, expected, complete}],
    nejlepší první. Seed bez jediného vyřešeného clusteru vypadne. Remíza
    se řeší jménem seedu — deterministicky.
    """
    ranked = []
    for seed_name, cluster_results in results_by_seed.items():
        expected  = expected_clusters[seed_name]
        solved    = sum(1 for r in cluster_results.values() if r.get("routes"))
        if solved == 0:
            continue
        raw_total = sum(r.get("cost", 0) for r in cluster_results.values())
        unsolved  = expected - solved
        ranked.append({
            "seed":      seed_name,
            "raw":       raw_total,
            "penalized": raw_total + unsolved * penalty_kc,
            "solved":    solved,
            "expected":  expected,
            "complete":  unsolved == 0,
        })
    ranked.sort(key=lambda r: (r["penalized"], r["seed"]))
    return ranked


def _state_from_cluster_results(orders, scd: dict, cluster_res: dict,
                                seed_penalty: float) -> SolutionState:
    """Sestaví SolutionState jednoho seedu z výsledků jeho clusterů."""
    c_indices = scd["cluster_indices"]
    cluster_labels_arr  = [0] * len(orders)
    cluster_routes_list = []
    cluster_costs       = []
    for c_idx, c_ix in enumerate(c_indices):
        for order_idx in c_ix:
            cluster_labels_arr[order_idx] = c_idx
        res = cluster_res.get(c_idx, {})
        cluster_routes_list.append(res.get("routes", []))
        cluster_costs.append(
            res.get("cost", seed_penalty if not res.get("routes") else 0.0)
        )
    return SolutionState(
        orders=orders,
        cluster_labels=cluster_labels_arr,
        clusters=scd["clusters"],
        cluster_indices=c_indices,
        vehicle_assignments=scd["vehicle_assignments"],
        cluster_routes_list=cluster_routes_list,
        cluster_costs=cluster_costs,
    )


def phase_c_best_seed(orders, vehicles_expanded, distances_km, vehicle_time_by_id,
                       n_clusters, time_budget_sec, n_workers,
                       n_finalists: int = 1,
                       deadline: float | None = None,
                       rescue_extra_sec: int = 0) -> list[tuple[str, SolutionState]]:
    """Vrací finalisty pro fázi E: [(seed_name, state)], vítěz první.
    S n_finalists=1 přesně dosavadní chování (jen vítěz + rescue).
    `deadline` = absolutní čas (time.time()), kdy končí CELKOVÝ budget běhu —
    záchranný re-solve se do něj musí vejít; `rescue_extra_sec` = vědomé
    druhé kolo záchrany nad budget (--rescue-extra-min)."""
    seed = CONFIG["random_seed"]
    seeds_labels = {
        "kmeans":      partition_kmeans(orders, n_clusters, seed),
        "sweep":       partition_sweep(orders, n_clusters),
        "tw_midpoint": partition_tw_midpoint(orders, n_clusters, seed),
    }

    all_worker_args  = []
    seed_cluster_data = {}

    for seed_name, labels in seeds_labels.items():
        clusters, cluster_indices = labels_to_clusters(orders, labels)
        vehicle_assignments = assign_vehicles_to_clusters(clusters, vehicles_expanded)
        seed_cluster_data[seed_name] = {
            "clusters":           clusters,
            "cluster_indices":    cluster_indices,
            "vehicle_assignments":vehicle_assignments,
        }
        for c_idx, (c_orders, c_indices, c_vehicles) in enumerate(
                zip(clusters, cluster_indices, vehicle_assignments)):
            cluster_v_times = [vehicle_time_by_id[v["id"]] for v in c_vehicles]
            sub_dist, sub_times = extract_submatrix(distances_km, cluster_v_times, c_indices)
            all_worker_args.append({
                "seed_name":       seed_name,
                "cluster_idx":     c_idx,
                "cluster_orders":  c_orders,
                "cluster_vehicles":c_vehicles,
                "sub_dist":        sub_dist.tolist(),
                "sub_times":       [st.tolist() for st in sub_times],
                "config":          dict(CONFIG),
            })

    # Čas na jednu úlohu podle počtu VLN (úlohy / workery), stejně jako
    # fáze E — ne podle počtu clusterů. Úloh je seedy × clustery (3× víc
    # než clusterů); dřív se dělilo jen clustery, takže na stroji s dost
    # jádry fáze C skončila za polovinu svého budgetu (30min běhy trvaly
    # 24 min, audit 2.3). Na 4 jádrech (3 workery, 6 úloh) = 2 vlny →
    # každá úloha půl budgetu, wall clock ~budget; na 7+ jádrech 1 vlna.
    time_per_cluster = phase_c_time_per_task(time_budget_sec,
                                             len(all_worker_args), n_workers)
    for a in all_worker_args:
        a["time_limit_sec"] = time_per_cluster
    waves = math.ceil(len(all_worker_args) / max(n_workers, 1))
    print(f"  {len(all_worker_args)} cluster-solve úloh paralelně "
          f"({n_workers} workerů, {time_per_cluster} sec/úloha"
          + (f", {waves} vlny" if waves > 1 else "") + ")...")

    results_by_seed = {sn: {} for sn in seeds_labels}
    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_worker_solve_cluster, args): args
                   for args in all_worker_args}
        for future in as_completed(futures):
            try:
                res = future.result()
                results_by_seed[res["seed_name"]][res["cluster_idx"]] = res
            except Exception as e:
                args = futures[future]
                print(f"  [!] Chyba seed={args['seed_name']} "
                      f"cluster={args['cluster_idx']}: {e}")

    seed_penalty      = CONFIG["seed_unsolved_cluster_penalty_kc"]
    expected_clusters = {sn: len(seed_cluster_data[sn]["clusters"])
                         for sn in seeds_labels}

    for seed_name, cluster_results in results_by_seed.items():
        expected  = expected_clusters[seed_name]
        solved    = sum(1 for r in cluster_results.values() if r.get("routes"))
        raw_total = sum(r.get("cost", 0) for r in cluster_results.values())
        penalized = raw_total + (expected - solved) * seed_penalty
        print(f"  Seed '{seed_name}': {raw_total:,.0f} Kč raw | "
              f"{penalized:,.0f} Kč pen. | {solved}/{expected} clusterů")

    ranked = rank_seeds(results_by_seed, expected_clusters, seed_penalty)
    if not ranked:
        abort("\n[CHYBA] Žádný seed nenašel řešení ani pro jeden rozklad — "
              "s touto flotilou a okny plán neexistuje (řešení neexistuje, "
              "ne vadná data: validace prošla).", EXIT_INFEASIBLE)

    best_seed_name = ranked[0]["seed"]
    print(f"\n  ✓ Nejlepší seed: '{best_seed_name}' "
          f"(pen. {ranked[0]['penalized']:,.0f} Kč)")

    scd           = seed_cluster_data[best_seed_name]
    clusters      = scd["clusters"]
    c_indices     = scd["cluster_indices"]
    vehicle_asgn  = scd["vehicle_assignments"]
    cluster_res   = results_by_seed[best_seed_name]

    # ── Pojistka: žádný cluster nejlepšího seedu nesmí zůstat nevyřešený ──
    # Dřív se objednávky nevyřešeného clusteru TIŠE ztratily z plánu
    # (31. 7. 2026: 49 z 91 objednávek PR). Teď: záchranný re-solve s delším
    # časem a náhradní strategií; když ani ten neuspěje, běh spadne s
    # diagnostikou — poloviční plán se nikdy neuloží.
    unsolved_cidx = [ci for ci in range(len(clusters))
                     if not cluster_res.get(ci, {}).get("routes")]
    if unsolved_cidx:
        remaining = (deadline - time.time()) if deadline is not None else None
        rounds: list[tuple[str, int]] = []
        first = rescue_time_for(time_per_cluster, remaining)
        if first >= RESCUE_MIN_SEC:
            rounds.append(("v budgetu", first))
        elif remaining is not None:
            print(f"  [!] Na záchranný re-solve nezbývá čas v budgetu "
                  f"({max(0, remaining):.0f} s) — přeskakuji"
                  + ("" if rescue_extra_sec else
                     "; vědomé prodloužení: --rescue-extra-min N"))
        if rescue_extra_sec > 0:
            rounds.append(("nad budget, --rescue-extra-min", int(rescue_extra_sec)))
        for label, rescue_time in rounds:
            todo = [ci for ci in unsolved_cidx
                    if not cluster_res.get(ci, {}).get("routes")]
            if not todo:
                break
            print(f"  [!] Nevyřešené clustery {todo} ("
                  + ", ".join(f"{len(clusters[ci])} obj." for ci in todo)
                  + f") — záchranný re-solve {rescue_time} s ({label}), "
                  f"{len(todo) * len(RESCUE_STRATEGIES)} úloh paralelně...")
            found = _rescue_unsolved_parallel(
                todo, best_seed_name, clusters, c_indices, vehicle_asgn,
                distances_km, vehicle_time_by_id, rescue_time, n_workers)
            for ci, res in found.items():
                print(f"      ✓ cluster {ci} zachráněn: {len(res['routes'])} tras, "
                      f"{res['cost']:,.0f} Kč")
                cluster_res[ci] = {"seed_name": best_seed_name, "cluster_idx": ci,
                                   "routes": res["routes"], "cost": res["cost"]}
        still = [ci for ci in unsolved_cidx
                 if not cluster_res.get(ci, {}).get("routes")]
        if still:
            ci = still[0]
            report = _unsolvable_cluster_report(
                best_seed_name, ci, clusters[ci], vehicle_asgn[ci])
            if not rescue_extra_sec:
                report += ("\nVědomě zkusit déle (nad budget): "
                           "--rescue-extra-min N")
            abort(report, EXIT_INFEASIBLE)

    finalists = [(best_seed_name,
                  _state_from_cluster_results(orders, scd, cluster_res,
                                              seed_penalty))]

    # Další finalisté (2..N): jen KOMPLETNÍ seedy — záchranný re-solve
    # náleží pouze vítězi (pojistka se nemění), děravý ne-vítěz do fáze E
    # nesmí, jinak by mohl vyhrát plán bez části objednávek.
    for entry in ranked[1:]:
        if len(finalists) >= n_finalists:
            break
        if not entry["complete"]:
            print(f"  [finalisté] Seed '{entry['seed']}' přeskočen — "
                  f"{entry['expected'] - entry['solved']} nevyřešených clusterů.")
            continue
        finalists.append((entry["seed"], _state_from_cluster_results(
            orders, seed_cluster_data[entry["seed"]],
            results_by_seed[entry["seed"]], seed_penalty)))
    if n_finalists > 1:
        print("  Finalisté pro fázi E: "
              + ", ".join(f"'{n}'" for n, _ in finalists))
    return finalists


# ============================================================
#  PHASE D — Cross-cluster LNS (destroy & repair)
# ============================================================

def _cluster_centroids(clusters: list) -> np.ndarray:
    centroids = []
    for c in clusters:
        centroids.append([np.mean([o["lat"] for o in c]),
                          np.mean([o["lon"] for o in c])])
    return np.array(centroids)


def _neighbor_clusters(cluster_idx: int, centroids: np.ndarray, k: int) -> list:
    my_pos = centroids[cluster_idx]
    dists  = [(np.linalg.norm(my_pos - centroids[i]), i)
               for i in range(len(centroids)) if i != cluster_idx]
    dists.sort()
    return [i for _, i in dists[:k]]


def _identify_destroy_candidates(state: SolutionState,
                                  distances_km: np.ndarray) -> list:
    """
    Score každé zastávky kombinuje:
    1. Hraniční poloha (nejbližší sousedka v jiném clusteru)
    2. Těsné TW (<90 min)
    3. Příslušnost k drahé trase
    """
    scored     = []
    all_costs  = []
    for c_routes in state.cluster_routes:
        for r in c_routes:
            n_del = sum(1 for s in r["stops"] if s["kg"] > 0)
            if n_del > 0:
                all_costs.append(r["total_kc"] / n_del)
    avg_cost_per_stop = np.mean(all_costs) if all_costs else 1

    for order_idx, order in enumerate(state.orders):
        c_idx           = state.cluster_labels[order_idx]
        full_matrix_idx = order_idx + 1

        # Kritérium 1: hraničnost
        row = distances_km[full_matrix_idx]
        nearest_score = 0.0
        for other_idx in range(len(state.orders)):
            if other_idx == order_idx:
                continue
            if state.cluster_labels[other_idx] != c_idx:
                dist = row[other_idx + 1]
                if dist > 0:
                    nearest_score = max(nearest_score, 1.0 / dist)

        # Kritérium 2: tight TW
        tw_width  = time_to_minutes(order["time_to"]) - time_to_minutes(order["time_from"])
        tw_score  = max(0.0, (90 - tw_width) / 90.0)

        # Kritérium 3: drahá trasa
        route_cost_score = 0.0
        for r in state.cluster_routes[c_idx]:
            ids_in_route = [s.get("id") for s in r["stops"] if s.get("id")]
            if order["id"] in ids_in_route:
                n_stops = max(sum(1 for s in r["stops"] if s["kg"] > 0), 1)
                route_cost_score = min(1.0, (r["total_kc"] / n_stops)
                                       / avg_cost_per_stop - 1.0)
                break

        score = nearest_score * 0.5 + tw_score * 0.3 + route_cost_score * 0.2
        scored.append((score, order_idx))

    scored.sort(reverse=True)
    return scored


def _lns_iteration(state, distances_km, vehicle_time_by_id, destroy_size,
                   n_workers, time_limit_sec, rng, temperature):
    """Jedna LNS iterace s mírně ne-greedy acceptance (SA prvek)."""
    scored_candidates = _identify_destroy_candidates(state, distances_km)
    to_move = [idx for _, idx in scored_candidates[:destroy_size]]
    if not to_move:
        return False, False, state

    centroids   = _cluster_centroids(state.clusters) if state.clusters else np.array([])
    k_neighbors = CONFIG["lns_neighbor_clusters"]
    moves       = []

    for order_idx in to_move:
        from_c = state.cluster_labels[order_idx]
        order  = state.orders[order_idx]
        neighbors = (_neighbor_clusters(from_c, centroids, k_neighbors)
                     if len(centroids) > 1 else [])

        candidate_targets = []
        for to_c in neighbors:
            centroid  = centroids[to_c] if len(centroids) else None
            score     = estimate_cluster_insertion_score(
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

    if not moves:
        return False, False, state

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
        sub_dist, sub_times = extract_submatrix(
            distances_km, cluster_v_times, new_indices[c_idx])
        worker_args.append({
            "seed_name":       "lns",
            "cluster_idx":     c_idx,
            "cluster_orders":  new_clusters[c_idx],
            "cluster_vehicles":c_vehicles,
            "sub_dist":        sub_dist.tolist(),
            "sub_times":       [st.tolist() for st in sub_times],
            "time_limit_sec":  time_limit_sec,
            "config":          dict(CONFIG),
        })

    if not worker_args:
        return False, False, state

    new_cluster_routes = list(state.cluster_routes)
    new_cluster_costs  = list(state.cluster_costs)
    resolved = set()

    with ProcessPoolExecutor(max_workers=min(n_workers, len(worker_args))) as executor:
        futures = {executor.submit(_worker_solve_cluster, args): args["cluster_idx"]
                   for args in worker_args}
        for future in as_completed(futures):
            try:
                res = future.result()
                c_idx = res["cluster_idx"]
                if res["routes"]:
                    new_cluster_routes[c_idx] = res["routes"]
                    new_cluster_costs[c_idx]  = res["cost"]
                    resolved.add(c_idx)
            except Exception as e:
                print(f"  [LNS] Chyba re-solve: {e}")

    if resolved != {args["cluster_idx"] for args in worker_args}:
        return False, False, state

    old_cost = state.total_cost
    new_cost = sum(new_cluster_costs)
    delta    = new_cost - old_cost
    improved = delta < 0

    accept = False
    if improved:
        accept = True
    else:
        max_abs = max(1.0, old_cost * CONFIG["lns_accept_worse_max_pct"]
                      * max(temperature, 0.25))
        if (delta <= max_abs
                and rng.random() < CONFIG["lns_accept_worse_prob"]
                * max(temperature, 0.35)):
            accept = True

    if not accept:
        return False, False, state

    new_state = SolutionState(
        orders=state.orders,
        cluster_labels=new_labels,
        clusters=new_clusters,
        cluster_indices=new_indices,
        vehicle_assignments=state.vehicle_assignments,
        cluster_routes_list=new_cluster_routes,
        cluster_costs=new_cluster_costs,
    )
    return True, improved, new_state


def phase_d_lns(state, distances_km, vehicle_time_by_id, time_budget_sec, n_workers):
    rng           = random.Random(CONFIG["random_seed"])
    destroy_min   = CONFIG["lns_destroy_min"]
    destroy_max   = CONFIG["lns_destroy_max"]
    destroy_size  = (destroy_min + destroy_max) // 2
    time_per_resolve = 20

    t_start       = time.time()
    t_deadline    = t_start + time_budget_sec
    iteration     = 0
    improvements  = 0
    accepted_worse= 0
    best_cost     = state.total_cost
    best_state    = state
    stagnation    = 0

    print(f"  Počáteční cena: {best_cost:,.0f} Kč")
    print(f"  LNS budget: {time_budget_sec/60:.0f} min, destroy_size start: {destroy_size}")

    while time.time() < t_deadline:
        iteration += 1
        now       = time.time()
        remaining = t_deadline - now
        if remaining < time_per_resolve * 2:
            break

        progress    = (now - t_start) / max(time_budget_sec, 1)
        temperature = max(0.15, 1.0 - progress)

        accepted, improved, candidate_state = _lns_iteration(
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
            if stagnation >= CONFIG["lns_stagnation_limit"]:
                destroy_size = rng.randint(destroy_min, destroy_max)
                stagnation   = 0
                print(f"  [LNS iter {iteration:3d}] ─ stagnace, "
                      f"reset destroy={destroy_size}")
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
            print(f"  [LNS iter {iteration:3d}] ✓ −{old_cost - new_cost:,.0f} Kč "
                  f"→ {new_cost:,.0f} Kč  (destroy={destroy_size})")
        else:
            accepted_worse += 1
            stagnation     += 1
            destroy_size    = min(destroy_max, destroy_size + 1)
            print(f"  [LNS iter {iteration:3d}] ~ uphill "
                  f"{old_cost:,.0f} → {new_cost:,.0f}  (temp={temperature:.2f})")

    elapsed = time.time() - t_start
    print(f"\n  LNS: {iteration} iterací, {improvements} zlepšení, "
          f"{accepted_worse} uphill, {elapsed:.0f} sec, best: {best_cost:,.0f} Kč")
    return best_state


# ============================================================
#  PHASE E — Finální intenzifikace
# ============================================================

def phase_e_time_per_task(time_budget_sec: float, n_tasks: int,
                          n_workers: int) -> int:
    """Čas na jednu solve úlohu fáze E: budget dělený počtem VLN
    (úlohy / workery, zaokrouhleno nahoru). Víc úloh než workerů →
    kratší čas na úlohu, wall clock fáze zůstává ~budget."""
    waves = math.ceil(max(n_tasks, 1) / max(n_workers, 1))
    return max(15, int(time_budget_sec / waves))


def phase_c_time_per_task(time_budget_sec: float, n_tasks: int,
                          n_workers: int) -> int:
    """Totéž pro fázi C (seedy × clustery), minimum 20 s na úlohu —
    jeden vzorec pro obě fáze, ať se budget využije celý bez ohledu na
    počet jader (4 / 7 / 20)."""
    waves = math.ceil(max(n_tasks, 1) / max(n_workers, 1))
    return max(20, int(time_budget_sec / waves))


def phase_e_intensify(finalists, distances_km, vehicle_time_by_id,
                      time_budget_sec, n_workers):
    """
    Finální intenzifikace. `finalists` = [(seed_name, SolutionState)] z fáze C,
    vítěz první. Každý finalista se dotáhne per-cluster re-solvem a vrátí se
    NEJLEVNĚJŠÍ výsledný stav — prohraný seed fáze C tak dostane šanci
    otočit těsný souboj (rozestupy seedů ~1,5–3 % jsou menší než šum GLS).
    S jedním finalistou přesně dosavadní chování.
    """
    if isinstance(finalists, SolutionState):      # zpětná kompatibilita
        finalists = [("best", finalists)]
    n_fin = len(finalists)

    # Úlohy = neprázdné clustery všech finalistů
    task_specs = []
    for f_idx, (_, st) in enumerate(finalists):
        for c_idx, (c_orders, c_indices, c_vehicles) in enumerate(
                zip(st.clusters, st.cluster_indices, st.vehicle_assignments)):
            if not c_orders:
                continue
            task_specs.append((f_idx, c_idx, c_orders, c_indices, c_vehicles))
    if not task_specs:
        return finalists[0][1]

    time_per_task = phase_e_time_per_task(time_budget_sec, len(task_specs),
                                          n_workers)
    if n_fin == 1:
        print(f"  {len(task_specs)} clusterů × {time_per_task} sec, "
              f"{n_workers} workerů")
    else:
        waves = math.ceil(len(task_specs) / max(n_workers, 1))
        print(f"  {n_fin} finalisté ({', '.join(n for n, _ in finalists)}) → "
              f"{len(task_specs)} úloh × {time_per_task} sec, "
              f"{n_workers} workerů"
              + (f", {waves} vlny" if waves > 1 else ""))

    worker_args = []
    for f_idx, c_idx, c_orders, c_indices, c_vehicles in task_specs:
        cluster_v_times = [vehicle_time_by_id[v["id"]] for v in c_vehicles]
        sub_dist, sub_times = extract_submatrix(distances_km, cluster_v_times, c_indices)
        worker_args.append({
            "seed_name":       f"F{f_idx}",     # finalista se veze v seed_name
            "cluster_idx":     c_idx,
            "cluster_orders":  c_orders,
            "cluster_vehicles":c_vehicles,
            "sub_dist":        sub_dist.tolist(),
            "sub_times":       [st.tolist() for st in sub_times],
            "time_limit_sec":  time_per_task,
            "config":          dict(CONFIG),
        })

    new_routes = [list(st.cluster_routes) for _, st in finalists]
    new_costs  = [list(st.cluster_costs)  for _, st in finalists]

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_worker_solve_cluster, args): args["cluster_idx"]
                   for args in worker_args}
        for future in as_completed(futures):
            try:
                res   = future.result()
                f_idx = int(res["seed_name"][1:])
                c_idx = res["cluster_idx"]
                tag   = f"[{finalists[f_idx][0]}] " if n_fin > 1 else ""
                old   = new_costs[f_idx][c_idx]
                if res["routes"] and res["cost"] < old:
                    new_routes[f_idx][c_idx] = res["routes"]
                    new_costs[f_idx][c_idx]  = res["cost"]
                    print(f"  [E] {tag}Cluster {c_idx+1:02d}: "
                          f"−{old - res['cost']:,.0f} Kč → {res['cost']:,.0f} Kč")
                else:
                    print(f"  [E] {tag}Cluster {c_idx+1:02d}: žádné zlepšení")
            except Exception as e:
                print(f"  [E] Chyba: {e}")

    totals = [sum(costs) for costs in new_costs]
    best_f = min(range(n_fin), key=lambda i: (totals[i], i))

    CONFIG["_finalists_summary"] = [
        {"seed": name, "cost_after_c": round(sum(st.cluster_costs), 1),
         "cost_after_e": round(totals[i], 1), "winner": i == best_f}
        for i, (name, st) in enumerate(finalists)]
    if n_fin > 1:
        print("\n  Výsledky finalistů (po C → po E):")
        for i, (name, st) in enumerate(finalists):
            mark = "  ← vítěz" if i == best_f else ""
            print(f"    {name:<12} {sum(st.cluster_costs):>12,.0f} → "
                  f"{totals[i]:>12,.0f} Kč{mark}")

    _, st = finalists[best_f]
    return SolutionState(
        orders=st.orders,
        cluster_labels=st.cluster_labels,
        clusters=st.clusters,
        cluster_indices=st.cluster_indices,
        vehicle_assignments=st.vehicle_assignments,
        cluster_routes_list=new_routes[best_f],
        cluster_costs=new_costs[best_f],
    )


# ============================================================
#  VÝSTUP
# ============================================================

def print_results(routes, total_cost_kc):
    print("\n" + "=" * 65)
    print("VÝSLEDEK PLÁNOVÁNÍ TRAS")
    print("=" * 65)
    for r in routes:
        driver = f" | {r['driver']}" if r.get("driver") else ""
        print(f"\n{r['vehicle_id']} ({r['vehicle_type']}{driver}, "
              f"{r['cost_per_km']} Kč/km)")
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
                "Order ID":      s.get("id", "—"),
                "Location code": s.get("location_code", ""),
                "Arrival":       s["arrival"],
                "Leg km":        s.get("leg_km", ""),
                "Servis min":    s.get("service_min", ""),
                "Departure":     s.get("departure", ""),
                "Kg":            s["kg"],
                "Window":      s.get("window", "—"),
                "Note":        s.get("note", ""),
                "Rampa":       s.get("ramp", 0),
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


# ============================================================
#  RUN LOG — porovnání runů
# ============================================================

RUN_LOG_PATH = Path("data/results/run_log.jsonl")

ORDERS_FILE_RE = re.compile(r"orders_([A-Z]+)_(\d{4}-\d{2}-\d{2})\.csv$")


def orders_file_meta(filename: str) -> tuple[str, str]:
    """Vytáhne (depot, date) z názvu orders_{DEPOT}_{YYYY-MM-DD}.csv, jinak ('', '')."""
    m = ORDERS_FILE_RE.match(filename)
    return (m.group(1), m.group(2)) if m else ("", "")

def _git_commit() -> str | None:
    """Vrátí krátký git hash nebo None pokud git není dostupný."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
            cwd=Path(__file__).parent,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _build_run_record(
    routes: list,
    total_cost_kc: float,
    output_dir: Path,
    zone_label: str,
    delivery_date: str,
    elapsed_min: float,
    orders: list,
    closures: list,
) -> dict:
    """Sestaví kompletní záznam o jednom runu."""
    total_km    = round(sum(r["total_km"] for r in routes), 1)
    total_hours = round(sum(r.get("duration_h", 0) for r in routes), 2)
    lines_count = len(routes)
    type_counter: dict = {}
    for r in routes:
        t = r["vehicle_type"]
        type_counter[t] = type_counter.get(t, 0) + 1

    record = {
        "run_id":         datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "solver_version": SOLVER_VERSION,
        "git_commit":     _git_commit(),

        "input": {
            "orders_file":      CONFIG["orders_file"],
            "zone":             zone_label,
            "delivery_date":    delivery_date,
            "orders_count":     len(orders),
            "orders_total_kg":  round(sum(o["weight_kg"] for o in orders), 1),
        },

        "config": {
            "total_time_budget_sec":        CONFIG["total_time_budget_sec"],
            "num_clusters":                 CONFIG["num_clusters"],
            "parallel_workers":             CONFIG["parallel_workers"],
            "random_seed":                  CONFIG["random_seed"],
            "tw_expand_before_min":         CONFIG.get("tw_expand_before_min", 0),
            "tw_expand_after_min":          CONFIG.get("tw_expand_after_min", 0),
            "travel_time_speed_factor":     CONFIG.get("travel_time_speed_factor", 1.0),
            "time_buffer_fixed_min":        CONFIG["time_buffer_fixed_min"],
            "time_buffer_pct":              CONFIG["time_buffer_pct"],
            "latest_return_h":         CONFIG["latest_return_h"],
            "budget_phase_C_pct":           CONFIG["budget_phase_C_pct"],
            "budget_phase_D_pct":           CONFIG["budget_phase_D_pct"],
            "budget_phase_E_pct":           CONFIG["budget_phase_E_pct"],
            "vehicle_capacity_multiplier":  CONFIG.get("vehicle_capacity_multiplier", 1.0),
            "double_runs":                  bool(CONFIG.get("_double_runs_enabled", False)),
            "seed_finalists":               CONFIG.get("_seed_finalists_resolved", 1),
            "driver_breaks":                bool(CONFIG.get("_driver_breaks_enabled", False)),
            "driver_break_after_h":         CONFIG["driver_break_after_h"],
            "driver_break_min":             CONFIG["driver_break_min"],
            "driver_max_drive_h":           CONFIG["driver_max_drive_h"],
        },

        "closures": [c["id"] for c in closures],

        "results": {
            "lines_count":      lines_count,
            "total_cost_kc":    total_cost_kc,
            "total_km":         total_km,
            "total_hours":      total_hours,
            "avg_km_per_line":  round(total_km / lines_count, 1) if lines_count else 0,
            "avg_kg_per_line":  round(sum(o["weight_kg"] for o in orders) / lines_count, 1) if lines_count else 0,
            "vehicle_type_mix": type_counter,
            "elapsed_min":      round(elapsed_min, 2),
            "output_dir":       str(output_dir),
        },
    }
    # Souboj finalistů fáze E — jen když se opravdu konal (>1), ať se
    # z run logu dá vyčíst, jak často prohraný seed fáze C otočil.
    fin_summary = CONFIG.get("_finalists_summary")
    if fin_summary and len(fin_summary) > 1:
        record["results"]["finalists"] = fin_summary
    return record


def _load_previous_run(zone: str, delivery_date: str,
                       log_path: Path = RUN_LOG_PATH) -> dict | None:
    """Najde poslední run se stejnou zónou a datem doručení."""
    if not log_path.exists():
        return None
    last = None
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("input", {}).get("zone") == zone \
                   and rec.get("input", {}).get("delivery_date") == delivery_date:
                    last = rec
            except json.JSONDecodeError:
                continue
    return last


def append_run_log(record: dict, log_path: Path = RUN_LOG_PATH) -> None:
    """Přidá záznam na konec run_log.jsonl."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def print_run_diff(current: dict, previous: dict) -> None:
    """Vypíše srovnání aktuálního runu s předchozím stejné zóny+data."""
    cr = current["results"]
    pr = previous["results"]

    def fmt_delta(cur, prev, unit="", higher_is_worse=True):
        delta = cur - prev
        if abs(delta) < 0.01:
            return f"{cur}{unit}  (beze změny)"
        sign  = "+" if delta > 0 else ""
        arrow = ("↑ horší" if delta > 0 else "↓ lepší") if higher_is_worse else \
                ("↑ lepší" if delta > 0 else "↓ horší")
        return f"{cur}{unit}  ({sign}{delta:.1f}  {arrow})"

    print("\n" + "=" * 65)
    print(f"SROVNÁNÍ S PŘEDCHOZÍM RUNEM  [{previous['run_id']}]")
    print("=" * 65)
    print(f"  {'Metrika':<28} {'Předchozí':>12}   {'Aktuální'}")
    print("  " + "-" * 61)
    print(f"  {'Celková cena (Kč)':<28} {pr['total_cost_kc']:>12,.0f}   "
          f"{fmt_delta(cr['total_cost_kc'], pr['total_cost_kc'], ' Kč')}")
    print(f"  {'Počet linek':<28} {pr['lines_count']:>12}   "
          f"{fmt_delta(cr['lines_count'], pr['lines_count'])}")
    print(f"  {'Celkem km':<28} {pr['total_km']:>12.1f}   "
          f"{fmt_delta(cr['total_km'], pr['total_km'], ' km')}")
    print(f"  {'Celkem hodin':<28} {pr['total_hours']:>12.2f}   "
          f"{fmt_delta(cr['total_hours'], pr['total_hours'], ' h')}")
    print(f"  {'Avg km/linka':<28} {pr['avg_km_per_line']:>12.1f}   "
          f"{fmt_delta(cr['avg_km_per_line'], pr['avg_km_per_line'], ' km')}")
    print(f"  {'Čas výpočtu':<28} {pr['elapsed_min']:>12.1f}   "
          f"{cr['elapsed_min']:.1f} min  (informativně)")

    # Config diff — ukaž pouze změněné klíče
    cc = current.get("config", {})
    pc = previous.get("config", {})
    changed = {k: (pc.get(k), cc.get(k)) for k in set(cc) | set(pc) if cc.get(k) != pc.get(k)}
    if changed:
        print("\n  Změny v configu:")
        for k, (old, new) in sorted(changed.items()):
            print(f"    {k:<34} {str(old):>10}  →  {new}")
    else:
        print("\n  Config beze změny oproti předchozímu runu.")

    # Uzavírky
    cc_ids = set(current.get("closures", []))
    pc_ids = set(previous.get("closures", []))
    if cc_ids != pc_ids:
        added   = cc_ids - pc_ids
        removed = pc_ids - cc_ids
        if added:   print(f"\n  Nové uzavírky:    {', '.join(sorted(added))}")
        if removed: print(f"  Odebrané uzavírky: {', '.join(sorted(removed))}")

    print("=" * 65)


# Hlavička přesně podle vzoru z ESO (včetně jejich překlepů „objendávky"
# a „odjedz" — parsují sloupce podle názvů, tak je neopravovat).
ESO_EXPORT_HEADER = [
    "č. objendávky", "adresa", "depo",
    "číslo linky", "pořadí zastávky", "počet zastávek na lince",
    "plán příjezd lokace", "plán odjezd lokace",
    "plán příjezd Depo", "plán odjedz depo", "čas konec linky",
    "typ vozidla", "nosnost vozu",
]


def _load_raw_max_kg_by_type(vehicle_types_file: str | None = None) -> dict:
    """type_code -> max_kg PŘESNĚ jak je v CSV (bez capacity multiplieru) —
    ESO chce papírovou nosnost vozu, ne interní plánovací rezervu."""
    mapping: dict[str, float] = {}
    try:
        path = Path(vehicle_types_file) if vehicle_types_file else find_vehicle_types_file()
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f, delimiter=";"):
                mapping[row["type_code"].strip()] = float(row["max_kg"])
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        pass
    return mapping


def save_eso_export(routes, output_dir: Path, orders: list, zone_label: str,
                    delivery_date: str = "",
                    vehicle_types_file: str | None = None) -> Path:
    """
    Export plánu pro import do ESO: jeden řádek na zastávku (bez skladu),
    středníky, cp1250, časy v SEKUNDÁCH od půlnoci.

    Depo časy: „plán odjedz depo" = výjezd na trasu, „plán příjezd Depo" =
    výjezd − nakládka (CONFIG depot_loading_min, teď 40 min), „čas konec
    linky" = návrat do skladu. Nakládka je jen v exportu, plánování tras
    neovlivňuje.
    """
    loading_sec = int(CONFIG.get("depot_loading_min", 40)) * 60
    max_kg_by_type = _load_raw_max_kg_by_type(
        vehicle_types_file or CONFIG["vehicle_types_file"] or None)
    depot_by_order = {o["id"]: o.get("block_id", "") for o in (orders or [])}

    rows = []
    for line_no, r in enumerate(routes, start=1):
        stops = [s for s in r["stops"] if s.get("id")]
        departure_sec = time_to_minutes(r["stops"][0]["arrival"]) * 60
        loading_start = max(0, departure_sec - loading_sec)
        line_end_sec  = time_to_minutes(r["stops"][-1]["arrival"]) * 60
        # "TYPE_02" -> 2; když kód nemá číslo, nech prázdné ať to ESO neshodí
        type_digits = re.sub(r"\D", "", r.get("type_code", ""))
        type_num    = int(type_digits) if type_digits else ""
        max_kg      = max_kg_by_type.get(r.get("type_code", ""), "")
        for seq, s in enumerate(stops, start=1):
            rows.append([
                s["id"],
                s.get("location_code", ""),
                depot_by_order.get(s["id"], zone_label),
                line_no,
                seq,
                len(stops),
                time_to_minutes(s["arrival"]) * 60,
                time_to_minutes(s["departure"]) * 60,
                loading_start,
                departure_sec,
                line_end_sec,
                type_num,
                max_kg,
            ])

    suffix = f"_{zone_label}" if zone_label else ""
    suffix += f"_{delivery_date}" if delivery_date else ""
    filepath = output_dir / f"eso_export{suffix}.csv"
    with open(filepath, "w", encoding="cp1250", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(ESO_EXPORT_HEADER)
        writer.writerows(rows)
    return filepath


def save_outputs(routes, total_cost_kc, output_dir: Path, zone_label: str, elapsed_min: float,
                 orders: list | None = None, delivery_date: str = "", closures: list | None = None,
                 run_log_path: Path = RUN_LOG_PATH):
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_rows = []
    stop_rows = []
    type_counter = {}
    for line_no, r in enumerate(routes, start=1):
        line_id = f"LINE_{line_no:02d}"
        type_name = r["vehicle_type"]
        type_counter[type_name] = type_counter.get(type_name, 0) + 1
        summary_rows.append({
            "zone": zone_label,
            "line_id": line_id,
            "vehicle_id": r["vehicle_id"],
            "vehicle_type": type_name,
            "cost_per_km": r["cost_per_km"],
            "total_km": r["total_km"],
            "duration_h": r.get("duration_h", 0),
            "total_kg": r["total_kg"],
            "total_cost_kc": r["total_kc"],
            "double_run": "2. jízda" if r.get("double_run") else "",
        })
        for i, s in enumerate(r["stops"]):
            stop_rows.append({
                "zone": zone_label,
                "line_id": line_id,
                "vehicle_type": type_name,
                "stop_seq": i,
                "place": s["stop"],
                "order_id": s.get("id", ""),
                "location_code": s.get("location_code", ""),
                "arrival": s["arrival"],
                "leg_km": s.get("leg_km", ""),
                "service_min": s.get("service_min", ""),
                "departure": s.get("departure", ""),
                "kg": s["kg"],
                "window": s.get("window", ""),
                "note": s.get("note", ""),
                "ramp": s.get("ramp", 0),
                "lat": s.get("lat", ""),
                "lon": s.get("lon", ""),
            })

    summary_rows.append({
        "zone":          "CELKEM",
        "line_id":       f"{len(routes)} linek",
        "vehicle_id":    "",
        "vehicle_type":  "",
        "cost_per_km":   "",
        "total_km":      round(sum(r["total_km"] for r in routes), 1),
        "duration_h":    round(sum(r.get("duration_h", 0) for r in routes), 2),
        "total_kg":      round(sum(r["total_kg"] for r in routes), 1),
        "total_cost_kc": round(sum(r["total_kc"] for r in routes), 0),
    })
    pd.DataFrame(summary_rows).to_csv(output_dir / "lines_summary.csv", index=False)
    pd.DataFrame(stop_rows).to_csv(output_dir / "lines_stops.csv", index=False)
    save_excel(routes, total_cost_kc, filepath=output_dir / "lines_plan.xlsx")
    eso_path = save_eso_export(routes, output_dir, orders or [], zone_label,
                               delivery_date=delivery_date)
    print(f"  [ESO export] {eso_path}")

    total_km_all    = round(sum(r["total_km"] for r in routes), 1)
    total_hours_all = round(sum(r.get("duration_h", 0) for r in routes), 1)
    zone_summary = {
        "zone": zone_label,
        "lines_count": len(routes),
        "vehicle_type_mix": type_counter,
        "total_cost_kc": total_cost_kc,
        "total_km": total_km_all,
        "total_hours": total_hours_all,
        "elapsed_min": round(elapsed_min, 2),
    }
    with open(output_dir / "zone_summary.json", "w", encoding="utf-8") as f:
        json.dump(zone_summary, f, ensure_ascii=False, indent=2)

    # ── Run log ───────────────────────────────────────────────
    _orders   = orders   or []
    _closures = closures or []
    _date     = delivery_date or ""

    previous = _load_previous_run(zone_label, _date, log_path=run_log_path)
    record   = _build_run_record(
        routes, total_cost_kc, output_dir, zone_label,
        _date, elapsed_min, _orders, _closures,
    )
    append_run_log(record, log_path=run_log_path)
    RUN_CONTEXT["run_id"] = record["run_id"]
    print(f"\n  [run log] uloženo → {run_log_path}  (run_id: {record['run_id']})")

    if previous:
        # Jen informativní výpis nad už ULOŽENÝMI výstupy — chyba tady
        # (starý formát záznamu, chybějící klíč) nesmí shodit běh, který
        # je hotový a v pořádku.
        try:
            print_run_diff(record, previous)
        except Exception as e:                        # noqa: BLE001
            print(f"  [!] Porovnání s minulým během se nepovedlo ({type(e).__name__}: "
                  f"{e}) — na plán to nemá vliv, výstupy jsou uložené.")


# ============================================================
#  MAIN
# ============================================================
# ============================================================
#  MAIN
# ============================================================
def print_run_settings(args, orders, vehicles_expanded, block_id, zone_label, n_clusters, n_workers, output_dir=None):
    total_kg = sum(o["weight_kg"] for o in orders)
    profiles = {}
    type_counts = {}

    for v in vehicles_expanded:
        prof = v.get("osrm_profile", "driving")
        profiles[prof] = profiles.get(prof, 0) + 1

        t = v.get("type_code", "UNKNOWN")
        type_counts[t] = type_counts.get(t, 0) + 1

    print("\n" + "=" * 65)
    print("DEBUG CONFIG RUNU")
    print("=" * 65)

    print(f"orders_file:                 {args.orders_file}")
    print(f"vehicle_types_file:          {args.vehicle_types_file}")
    print(f"output_dir:                  {output_dir or args.output_dir}")
    print(f"block_id:                    {block_id}")
    print(f"zone_label:                  {zone_label}")

    print(f"orders_count:                {len(orders)}")
    print(f"orders_total_kg:             {total_kg:,.0f}")
    print(f"vehicles_count:              {len(vehicles_expanded)}")
    print(f"vehicles_by_profile:         {profiles}")
    print(f"vehicles_by_type_code:       {type_counts}")

    print(f"resolved_clusters:           {n_clusters}")
    print(f"resolved_workers:            {n_workers}")

    print("\n[CONFIG]")
    print(f"total_time_budget_sec:       {CONFIG['total_time_budget_sec']}")
    print(f"budget_phase_C_pct:          {CONFIG['budget_phase_C_pct']}")
    print(f"budget_phase_D_pct:          {CONFIG['budget_phase_D_pct']}")
    print(f"budget_phase_E_pct:          {CONFIG['budget_phase_E_pct']}")
    print(f"seed_finalists:              {CONFIG.get('seed_finalists', 1)} "
          f"(resolved: {CONFIG.get('_seed_finalists_resolved', '?')})")

    print(f"num_clusters_raw:            {CONFIG['num_clusters']}")
    print(f"parallel_workers_raw:        {CONFIG['parallel_workers']}")
    print(f"random_seed:                 {CONFIG['random_seed']}")

    print(f"time_buffer_fixed_min:       {CONFIG['time_buffer_fixed_min']}")
    print(f"time_buffer_pct:             {CONFIG['time_buffer_pct']}")
    print(f"latest_return_h:        {CONFIG['latest_return_h']}")

    print(f"lns_destroy_min:             {CONFIG['lns_destroy_min']}")
    print(f"lns_destroy_max:             {CONFIG['lns_destroy_max']}")
    print(f"lns_neighbor_clusters:       {CONFIG['lns_neighbor_clusters']}")
    print(f"lns_accept_worse_prob:       {CONFIG['lns_accept_worse_prob']}")
    print(f"lns_accept_worse_max_pct:    {CONFIG['lns_accept_worse_max_pct']}")
    print(f"lns_stagnation_limit:        {CONFIG['lns_stagnation_limit']}")
    print(f"seed_unsolved_penalty_kc:    {CONFIG['seed_unsolved_cluster_penalty_kc']}")

    print("\n[OSRM]")
    print(f"default_osrm_url:            {CONFIG['osrm_url']}")
    for k, v in CONFIG["osrm_urls"].items():
        print(f"osrm_urls[{k}]:              {v}")

    print("=" * 65)


def print_effective_budgets(osrm_elapsed, remaining, budget_C, budget_D, budget_E, n_clusters):
    time_per_cluster_C = max(20, int(budget_C) // max(n_clusters, 1))
    time_per_cluster_E = max(15, int(budget_E / max(1, n_clusters)))
    time_per_resolve_D = 20

    print("\n" + "=" * 65)
    print("DEBUG ODVOZENÉ PARAMETRY")
    print("=" * 65)
    print(f"osrm_elapsed_sec:            {osrm_elapsed:.1f}")
    print(f"remaining_budget_sec:        {remaining:.1f}")
    print(f"budget_C_sec:                {budget_C:.1f}")
    print(f"budget_D_sec:                {budget_D:.1f}")
    print(f"budget_E_sec:                {budget_E:.1f}")
    print(f"phase_C_time_per_cluster:    {time_per_cluster_C} sec")
    print(f"phase_D_time_per_resolve:    {time_per_resolve_D} sec")
    print(f"phase_E_time_per_cluster:    {time_per_cluster_E} sec")
    print("=" * 65)

def apply_buffer_overrides(args, config: dict | None = None) -> list[str]:
    """
    Přepíše plánovací buffery v CONFIG podle CLI. Vrátí popis změn pro výpis
    (prázdný seznam = běží se na defaultech z CONFIG).

    Granulární přepínače mají přednost před --no-buffers, aby šlo udělat
    např. „tvrdý režim, ale nech +10 min na konci okna".

    MUSÍ se volat před load_vehicle_types_db() — ta z CONFIG čte násobič
    nosnosti při expanzi vozového parku.
    """
    cfg = CONFIG if config is None else config
    changes: list[str] = []

    def _set(key: str, value, label: str, fmt: str = "{}") -> None:
        old = cfg.get(key)
        if old == value:
            return
        cfg[key] = value
        changes.append(f"{label}: {fmt.format(old)} → {fmt.format(value)}")

    if getattr(args, "no_buffers", False):
        _set("vehicle_capacity_multiplier", 1.0, "nosnost vozidel", "{:.0%}")
        _set("tw_expand_before_min", 0, "okno před", "{} min")
        _set("tw_expand_after_min", 0, "okno po", "{} min")

    if getattr(args, "capacity_multiplier", None) is not None:
        _set("vehicle_capacity_multiplier", float(args.capacity_multiplier),
             "nosnost vozidel", "{:.0%}")
    if getattr(args, "tw_expand_before", None) is not None:
        _set("tw_expand_before_min", int(args.tw_expand_before),
             "okno před", "{} min")
    if getattr(args, "tw_expand_after", None) is not None:
        _set("tw_expand_after_min", int(args.tw_expand_after),
             "okno po", "{} min")

    return changes


def parse_args():
    # Defaulty do nápovědy se čtou z CONFIG, ne přepisují ručně — jinak
    # nápověda tvrdí něco jiného, než co běh doopravdy udělá.
    # Pozor: literál procenta musí být '%%' (argparse text prohání % formátem).
    _cap  = CONFIG.get("vehicle_capacity_multiplier", 1.0)
    _budg = CONFIG["total_time_budget_sec"]
    _fin  = CONFIG.get("seed_finalists", 1)
    _tw_b = CONFIG.get("tw_expand_before_min", 0)
    _tw_a = CONFIG.get("tw_expand_after_min", 0)
    # desetinná čárka — zbytek textů v souboru ji používá taky
    _unr_drv = f"{UNREACHABLE_MATRIX_FAIL_PCT * 100:g}".replace(".", ",")
    _unr_hgv = f"{UNREACHABLE_MATRIX_FAIL_PCT_BY_PROFILE.get('driving-hgv', UNREACHABLE_MATRIX_FAIL_PCT) * 100:g}".replace(".", ",")

    parser = argparse.ArgumentParser()
    parser.add_argument("--orders-file", default=CONFIG["orders_file"],
                        help="Solver-ready orders CSV pro jeden block")
    parser.add_argument("--vehicle-types-file", default=CONFIG["vehicle_types_file"],
                        help="CSV s vozovým parkem (středníky). Bez zadání se "
                             "vezme JEDINÝ soubor v data/static/ — víc souborů "
                             "je chyba, program mezi nimi nevybírá.")
    parser.add_argument("--output-dir", default="output",
                        help="Složka pro výstupy")
    parser.add_argument("--zone-label", default="",
                        help="Popisek zóny/bloku do výstupů; když chybí, bere se ze souboru")
    parser.add_argument("--force-matrix", action="store_true",
                        help="NOUZOVÝ přepínač: vypne limit nedosažitelných párů pro VŠECHNY "
                             f"profily. Běžně NENÍ potřeba — limity jsou per profil "
                             f"(driving {_unr_drv} %%, driving-hgv {_unr_hgv} %%) "
                             "a pokrývají i Prahu. "
                             "Použij jen když víš, že data jsou v pořádku a limit "
                             "přesto brání běhu. Nedosažitelné páry dostanou sentinel "
                             "a solver je nepřiřadí tak jako tak.")
    parser.add_argument("--budget-min", type=float, default=None,
                        help="Override celkového časového budgetu solveru (v minutách). "
                             f"Default z CONFIG: {_budg / 60:g} min. "
                             "Užitečné pro rychlé porovnávací běhy: --budget-min 5")
    parser.add_argument("--run-log-path", default=str(RUN_LOG_PATH),
                        help="Cesta k run_log.jsonl (default: data/results/run_log.jsonl). "
                             "Predikční běhy: data/prediction/results/run_log.jsonl — "
                             "ostrá historie zůstane čistá.")
    parser.add_argument("--allow-profile-fallback", action="store_true",
                        help="Když routing pro těžká vozidla (driving-hgv/ORS) selže, "
                             "dovol tichý fallback na osobní profil 'driving'. "
                             "DEFAULT je hard-fail — kamiony by jinak jely po trasách "
                             "pro osobáky (mosty, úzké uličky).")

    parser.add_argument("--double-runs", action="store_true",
                        help="Povol DVOJLINKY (porušení L2): malé auto smí "
                             "naložit ve skladu 2× za den. Druhá jízda platí "
                             "plný druhý výjezd a smí vyjet od CONFIG "
                             "double_run_earliest; po solve se páruje na "
                             "fyzická auta (návrat + nakládka), jinak běh "
                             "spadne. Zapíná plan_day podle decision.")

    parser.add_argument("--rescue-extra-min", type=float, default=0.0,
                        help="Když se cluster nevyřeší ani záchranou v budgetu, "
                             "zkusit ještě N minut NAD budget (default 0 = ne; "
                             "běh drží slovo o délce). Na serveru raději "
                             "nechat 0 a nechat rozhodnout člověka.")
    parser.add_argument("--driver-breaks", action="store_true",
                        help="Režim řidiče EU (zjednodušeně): v žádném úseku "
                             f"trasy delším než {CONFIG['driver_break_after_h']:g} h "
                             f"nesmí chybět {CONFIG['driver_break_min']} min pauza "
                             f"a čistá jízda za den max {CONFIG['driver_max_drive_h']:g} h "
                             "(tvrdý strop). Používají L3 kamionové trasy; běžné "
                             "dodávkové linky tachograf nemají.")

    parser.add_argument("--seed-finalists", default=None,
                        choices=["auto", "1", "2", "3"],
                        help="Kolik nejlepších seedů fáze C dotáhnout ve fázi E "
                             f"(default z CONFIG: {_fin}). 'auto' = kolik se "
                             "vejde do jedné vlny workerů (workery // clustery, "
                             "max 3) — na slabém stroji samo spadne na 1. "
                             "'1' = jen vítěz fáze C, chování před 11.8.2026. "
                             "Víc finalistů = stejný wall clock, víc jader, "
                             "menší loterie při těsném souboji seedů.")

    # ── Plánovací buffery: override z CLI (default = hodnoty v CONFIG) ──
    parser.add_argument("--no-buffers", action="store_true",
                        help="TVRDÝ režim bez rezerv: nosnost 100 %% a závozová "
                             "okna přesně jak je poslalo ESO9 (bez posunu). "
                             "Zkratka za --capacity-multiplier 1.0 "
                             "--tw-expand-before 0 --tw-expand-after 0. "
                             f"Aktuální defaulty: nosnost {_cap * 100:g} %%, "
                             f"okna -{_tw_b}/+{_tw_a} min.")
    parser.add_argument("--capacity-multiplier", type=float, default=None,
                        help="Násobič nosnosti vozidel (default z CONFIG: "
                             f"{_cap:.2f} = {_cap * 100:g} %%). 1.0 = plánuj "
                             "přesně na papírovou nosnost, 1.03 = porušení L1.")
    parser.add_argument("--tw-expand-before", type=int, default=None,
                        help="O kolik minut smí řidič přijet PŘED začátek okna "
                             f"(default z CONFIG: {_tw_b}). 0 = žádný posun.")
    parser.add_argument("--tw-expand-after", type=int, default=None,
                        help="O kolik minut smí řidič přijet PO konci okna "
                             f"(default z CONFIG: {_tw_a}). 0 = žádný posun.")
    add_osm_args(parser)
    return parser.parse_args()


# ============================================================
#  STARTUP TESTY
# ============================================================

def run_startup_tests():
    """
    Spustí pytest test suite před startem solveru.
    Pokud jakýkoliv test selže, solver se nespustí.
    Lze přeskočit nastavením env proměnné SKIP_STARTUP_TESTS=1.
    """
    import subprocess
    import os
    from pathlib import Path as _Path

    import sys as _sys
    if os.environ.get("SKIP_STARTUP_TESTS", "").strip() == "1":
        return

    tests_dir = _Path(__file__).parent / "tests"
    if not tests_dir.exists():
        print("[WARN] tests/ složka nenalezena — přeskakuji startup testy.")
        return

    print("\n[TEST] Spouštím startup testy...")
    result = subprocess.run(
        [
            _sys.executable, "-m", "pytest",
            str(tests_dir),
            "--ignore", str(tests_dir / "test_ors_hgv_integration.py"),
            "-x", "-q", "--tb=short", "--no-header",
        ],
        capture_output=False,
    )
    if result.returncode != 0:
        print("\n[ABORT] Startup testy selhaly — solver se nespustí.")
        print("        Oprav chybu výše nebo spusť: pytest tests/ -v")
        _sys.exit(1)
    print()


def run_routing_tests(osrm_url: str, ors_url: str) -> None:
    """
    Spustí integrační testy ORS vs OSRM proti aktuálně běžící routing instanci.
    Volá se po orchestrátoru / preflight pingu — Docker je v tu chvíli nahoře.

    Parametry osrm_url / ors_url určují která instance se testuje:
      stable:  http://localhost:5000 / http://localhost:8080
      current: http://localhost:5001 / http://localhost:8081
    """
    import subprocess
    import os
    from pathlib import Path as _Path

    import sys as _sys
    if os.environ.get("SKIP_STARTUP_TESTS", "").strip() == "1":
        return

    tests_dir = _Path(__file__).parent / "tests"
    integration_test = tests_dir / "test_ors_hgv_integration.py"
    if not integration_test.exists():
        return

    print(f"[TEST] Routing testy — OSRM={osrm_url}, ORS={ors_url}...")
    env = os.environ.copy()
    env["OSRM_TEST_URL"] = osrm_url
    env["ORS_TEST_URL"]  = ors_url
    result = subprocess.run(
        [
            _sys.executable, "-m", "pytest",
            str(integration_test),
            "-x", "-q", "--tb=short", "--no-header",
        ],
        env=env,
        capture_output=False,
    )
    if result.returncode != 0:
        print("\n[ABORT] Routing testy selhaly — zkontroluj ORS/OSRM konfiguraci.")
        print("        Detail: pytest tests/test_ors_hgv_integration.py -v")
        _sys.exit(1)
    print()


def main():
    global FORCE_MATRIX          # nastavuje --force-matrix níže
    run_startup_tests()
    t_global_start = time.time()

    print("=" * 65)
    print("VRP Solver Lines v6 — RiRo block pipeline")
    print("=" * 65)

    # ── Načti data ────────────────────────────────────────────
    print("\nNačítám data...")
    args = parse_args()

    # Validace: --orders-file je povinný (CONFIG default je prázdný)
    if not args.orders_file:
        abort(
            "\n[CHYBA] Chybí --orders-file.\n"
            "Příklad: python vrp_solver_lines_v6.py "
            "--orders-file data/prepared/CB/orders_CB_2026-04-10.csv",
            EXIT_DATA)
    if not Path(args.orders_file).exists():
        abort(
            f"\n[CHYBA] Orders soubor neexistuje: {args.orders_file}\n"
            f"Nejdříve spusť: python prepare_inputs_v6.py <DEPOT_CODE>",
            EXIT_DATA)

    # Auto-detekce výstupní složky z názvu orders souboru — HNED, ať má
    # každý konec běhu (i pád při načítání dat) run_status.json.
    # Pattern: orders_{DEPOT}_{YYYY-MM-DD}.csv → data/results/{DEPOT}/{YYYY-MM-DD}/
    # delivery_date se z názvu bere VŽDY (i s explicitním --output-dir) — jinak
    # by běhy s vlastní output složkou (predikce, porovnávací běhy) měly
    # v run logu prázdné datum a nešly párovat.
    orders_path = Path(args.orders_file)
    depot_code_out, date_out = orders_file_meta(orders_path.name)
    if depot_code_out and args.output_dir == "output":
        output_dir = Path(f"data/results/{depot_code_out}/{date_out}")
    else:
        output_dir = Path(args.output_dir)
    delivery_date = date_out
    output_dir.mkdir(parents=True, exist_ok=True)
    RUN_CONTEXT.update({"output_dir": output_dir, "delivery_date": delivery_date,
                        "started": t_global_start, "zone": depot_code_out or None})
    # Synchronizace CONFIG s reálně použitým souborem, aby to downstream
    # kód (zone_summary.json, logging) zaznamenal správně, ne starý default.
    CONFIG["orders_file"] = args.orders_file

    # ── --force-matrix: vypnout hard-fail při nedosažitelných párech ───────
    # Nastaví flag, který čte unreachable_fail_pct() při sanitizaci matice.
    if args.force_matrix:
        FORCE_MATRIX = True
        print("[FORCE] Limit nedosažitelných párů v matici vypnut (--force-matrix). "
              "Páry s NaN durations dostanou sentinel UNREACHABLE_TIME_MIN, "
              "solver je nepřiřadí.")

    # ── Plánovací buffery: CLI override PŘED načtením vozidel ─────────────
    # (load_vehicle_types_db čte vehicle_capacity_multiplier z CONFIG)
    _buffer_changes = apply_buffer_overrides(args)
    if _buffer_changes:
        print("[BUFFERY] Override z CLI:")
        for ch in _buffer_changes:
            print(f"          {ch}")

    # ── --budget-min: override total time budget ──────────────────────────
    if args.budget_min is not None:
        CONFIG["total_time_budget_sec"] = int(args.budget_min * 60)
        print(f"[BUDGET] Override: {args.budget_min:g} min "
              f"({CONFIG['total_time_budget_sec']} s)")

    # ── --driver-breaks: povinné pauzy řidiče (L3 kamionové trasy) ────────
    if args.driver_breaks:
        CONFIG["_driver_breaks_enabled"] = True
        print(f"[ŘIDIČ EU] Pauzy: {CONFIG['driver_break_min']} min v každém "
              f"úseku do {CONFIG['driver_break_after_h']:g} h | denní limit "
              f"jízdy {CONFIG['driver_max_drive_h']:g} h (tvrdý strop)")

    # ── --seed-finalists: kolik seedů fáze C dotáhnout ve fázi E ──────────
    if args.seed_finalists is not None:
        CONFIG["seed_finalists"] = (args.seed_finalists
                                    if args.seed_finalists == "auto"
                                    else int(args.seed_finalists))
        print(f"[FINALISTÉ] Override: --seed-finalists {args.seed_finalists}")

    # ── --allow-profile-fallback: vypnout hard-fail při výpadku HGV routingu ─
    if args.allow_profile_fallback:
        global ALLOW_PROFILE_FALLBACK
        ALLOW_PROFILE_FALLBACK = True
        print("[FALLBACK] Tichý fallback driving-hgv → driving POVOLEN. "
              "Kamiony můžou dostat osobní trasy pokud ORS selže.")

    # Snapshot total_budget AŽ TADY (po override), aby fáze C/D/E používaly
    # správnou hodnotu. Zároveň vytisknout banner s reálným budgetem.
    total_budget = CONFIG["total_time_budget_sec"]
    print(f"Budget: {total_budget // 60} min | Clusterů: {CONFIG['num_clusters']}")

    # ── Volba OSM routing instance (current = default, stable = na vyžádání) ──
    osm_source = resolve_osm_source(args)
    apply_osm_source(CONFIG, osm_source)
    print(f"[OSM] zdroj: {osm_source}"
          f"  | OSRM={CONFIG['osrm_urls']['driving']}"
          f"  | ORS={CONFIG['osrm_urls']['driving-hgv']}")

    # ── Preflight: jen ověř, že instance odpovídá ────────────────────────
    # Běh routing data ZÁMĚRNĚ nestahuje ani nepřestavuje — jinak by se
    # 30–60minutový rebuild spustil uprostřed plánování před uzávěrkou.
    # Přestavba je samostatný krok: python refresh_osm.py (typicky týdně).
    _osrm_ping_url = (
        f"{CONFIG['osrm_url']}/route/v1/driving/14.4,50.0;14.5,50.1?overview=false"
    )
    try:
        requests.get(_osrm_ping_url, timeout=2)
    except requests.exceptions.RequestException:
        abort(
            f"\n[CHYBA] Routing instance '{osm_source}' ({CONFIG['osrm_url']}) "
            f"neodpovídá.\n"
            f"        Nastartuj ji:  {start_hint(osm_source)}"
        , EXIT_ERROR)

    # Routing instance je nahoře — spusť integrační testy ORS vs OSRM.
    run_routing_tests(
        osrm_url=CONFIG["osrm_urls"]["driving"],
        ors_url=CONFIG["osrm_urls"]["driving-hgv"],
    )

    orders            = load_orders_day(args.orders_file)
    block_id          = orders[0].get("block_id", "").strip() if orders else ""
    # Bez --vehicle-types-file se vezme nejnovější datovaný soubor; ať je
    # v logu i v run recordu vidět, se kterým vozovým parkem se plánovalo.
    vehicle_types_path = (Path(args.vehicle_types_file) if args.vehicle_types_file
                          else find_vehicle_types_file())
    CONFIG["vehicle_types_file"] = str(vehicle_types_path)
    print(f"  Vozový park: {vehicle_types_path}")
    vehicles_expanded = load_vehicle_types_db(str(vehicle_types_path), block_id=block_id)

    # Pojistka č. 1: neobsloužitelná objednávka (vadné SEC) = stop hned,
    # ne tichá ztráta celého clusteru o pár minut později.
    validate_orders_servable(orders, vehicles_expanded=vehicles_expanded)

    # ── Dvojlinky (L2): virtuální „druhá jízda" vozidla ──────────────────
    CONFIG["_double_runs_enabled"] = bool(args.double_runs)   # do run logu
    if args.double_runs:
        virtuals = build_double_run_vehicles(vehicles_expanded)
        vehicles_expanded += virtuals
        print(f"  [DVOJLINKY] Povoleno: +{len(virtuals)} virtuálních jízd "
              f"(od {CONFIG['double_run_earliest']}, plný druhý fix, "
              f"nakládka {CONFIG['depot_loading_min']} min)")

    cfg_clusters = CONFIG["num_clusters"]
    n_clusters   = (auto_n_clusters(len(orders), len(vehicles_expanded))
                    if cfg_clusters == "auto" else int(cfg_clusters))
    # Nikdy víc clusterů než FYZICKÝCH vozidel — cluster bez vozidla shodí
    # OR-Tools nativně (žádná python výjimka), cluster jen s virtuálními
    # dvojlinkami nemá kdo obsloužit ráno. Týká se malých flotil
    # (L3: 1-2 kamiony; poslední depo dne s dvojlinkami).
    n_physical = sum(1 for v in vehicles_expanded if not is_virtual_vehicle(v))
    if n_clusters > n_physical:
        n_clusters = max(1, n_physical)
        print(f"  [!] Clusterů víc než fyzických vozidel — snižuji na {n_clusters}")

    cfg_workers = CONFIG["parallel_workers"]
    n_workers   = (max(1, multiprocessing.cpu_count() - 1)
                   if cfg_workers == "auto" else int(cfg_workers))

    # Kolik finalistů fáze C dotáhne fáze E (auto se řeší až tady,
    # protože potřebuje znát workery a clustery TOHOTO stroje)
    n_finalists = resolve_seed_finalists(CONFIG.get("seed_finalists", 1),
                                         n_workers, n_clusters)
    CONFIG["_seed_finalists_resolved"] = n_finalists

    total_kg = sum(o["weight_kg"] for o in orders)
    print(f"  Objednávky:  {len(orders):,}  ({total_kg:,.0f} kg celkem)")
    print(f"  Vozidla:     {len(vehicles_expanded)} dostupných")

    _tw_bef = CONFIG.get("tw_expand_before_min", 0)
    _tw_aft = CONFIG.get("tw_expand_after_min",  0)
    _spd    = CONFIG.get("travel_time_speed_factor", 1.0)
    _kg_mul = CONFIG.get("vehicle_capacity_multiplier", 1.0)
    print(f"  Buffery:     TW -{_tw_bef} min / +{_tw_aft} min  |  "
          f"speed ×{_spd:.3f}  (čas /{_spd:.3f})  |  kg ×{_kg_mul:.3f}")
    zone_label = args.zone_label.strip() or (orders[0].get("block_id", "") if orders else "")
    RUN_CONTEXT["zone"] = zone_label
    print(f"  Zóna/block:  {zone_label}")
    print(f"  Clustery:    {n_clusters}")
    print(f"  CPU workerů: {n_workers}")
    # Vypisuje se VŽDY — na slabším stroji "auto" tiše spadne na 1 a bez
    # téhle řádky by nikdo nepoznal, že fáze E jede jen na vítězi.
    print(f"  Finalisté E: {n_finalists} "
          + ("nejlepších seedů fáze C" if n_finalists > 1
             else "(jen vítěz fáze C)")
          + f"  [seed_finalists={CONFIG.get('seed_finalists', 1)}]")

    print_run_settings(
        args=args,
        orders=orders,
        vehicles_expanded=vehicles_expanded,
        block_id=block_id,
        zone_label=zone_label,
        n_clusters=n_clusters,
        n_workers=n_workers,
        output_dir=output_dir,
    )

    # ── Phase A: OSRM ────────────────────────────────────────
    print("\n" + "─" * 65)
    print("[A] OSRM matice")
    print("─" * 65)
    locations = ([(DEPOT["lat"], DEPOT["lon"])]
                 + [(o["lat"], o["lon"]) for o in orders])

    # Jeden OSRM dotaz per unikátní profil
    distinct_profiles = sorted(set(v["osrm_profile"] for v in vehicles_expanded))
    matrices_by_profile: dict = {}
    for prof in distinct_profiles:
        matrices_by_profile[prof] = get_matrix(locations, profile=prof)

    # Fyzická vzdálenost (pro scoring) vždy z driving profilu
    distances_km = matrices_by_profile.get(
        "driving", next(iter(matrices_by_profile.values()))
    )[0]

    # Aplikuj uzavírky na všechny profily
    from closures_utils import apply_closures_to_matrix
    for prof in list(matrices_by_profile.keys()):
        dist_p, dur_p = matrices_by_profile[prof]
        dur_p, dist_p = apply_closures_to_matrix(
            dur_p, dist_p, locations,
            matrix_profile=prof,
            osrm_url=CONFIG["osrm_url"],
            ors_url=CONFIG["osrm_urls"].get("driving-hgv", "http://localhost:8080"),
            closure_route_profile=CONFIG["closure_route_profiles"].get(prof),
            debug_label=prof,
        )
        matrices_by_profile[prof] = (dist_p, dur_p)
    # Obnov distances_km po aplikaci uzavírek
    distances_km = matrices_by_profile.get(
        "driving", next(iter(matrices_by_profile.values()))
    )[0]

    # Per-vehicle časová matice = profil × time_multiplier
    vehicle_time_by_id: dict = {}
    for v in vehicles_expanded:
        _, dur_buffered = matrices_by_profile[v["osrm_profile"]]
        t_mat = dur_buffered * v["time_multiplier"]
        np.fill_diagonal(t_mat, 0)
        vehicle_time_by_id[v["id"]] = t_mat

    # Pojistka č. 2: každá objednávka musí být dosažitelná ze skladu
    # alespoň v jedné vozidlové matici (kontrola sentinelů po sanitizaci).
    validate_orders_servable(orders, vehicle_time_by_id, vehicles_expanded)

    t_after_osrm = time.time()
    osrm_elapsed = t_after_osrm - t_global_start
    remaining    = total_budget - osrm_elapsed
    budget_C     = remaining * CONFIG["budget_phase_C_pct"]
    budget_D     = remaining * CONFIG["budget_phase_D_pct"]
    budget_E     = remaining * CONFIG["budget_phase_E_pct"]
    print(f"\nOSRM: {osrm_elapsed:.0f} sec | zbývá {remaining/60:.1f} min")
    print(f"Budgety → C: {budget_C/60:.1f} min | D: {budget_D/60:.1f} min "
          f"| E: {budget_E/60:.1f} min")
    

    # ── Phase B+C: Seed solve ─────────────────────────────────
    print("\n" + "─" * 65)
    print("[B+C] Seed partice + paralelní solve")
    print("─" * 65)
    deadline = t_global_start + total_budget          # konec CELKOVÉHO budgetu
    finalists = phase_c_best_seed(
        orders, vehicles_expanded, distances_km, vehicle_time_by_id,
        n_clusters, int(budget_C), n_workers, n_finalists=n_finalists,
        deadline=deadline,
        rescue_extra_sec=int(round(float(getattr(args, "rescue_extra_min", 0) or 0) * 60)),
    )
    state = finalists[0][1]
    print(f"Phase C: {time.time() - t_after_osrm:.0f} sec | {state.total_cost:,.0f} Kč")
    # Záchrana mohla ukousnout z času fáze E — E dostane nejvýš to, co zbývá
    # do deadline (jinak by běh přetekl budget). Když nezbývá nic, E se
    # zkrátí na minimum, ale plán už existuje.
    left = deadline - time.time()
    if left < budget_E - 5:                            # drobný posun nehlásit
        print(f"  [budget] Fáze E zkrácena z {budget_E:.0f} s na "
              f"{max(0, left):.0f} s (záchranný re-solve / přetečení C)")
    if left < budget_E:
        budget_E = max(0.0, left)

    # ── Phase D: LNS ─────────────────────────────────────────
    # (budget 0 = vypnutá; kdyby se zapnula, jede jen na vítězi fáze C)
    print("\n" + "─" * 65)
    print("[D] Cross-cluster LNS")
    print("─" * 65)
    t_d   = time.time()
    state = phase_d_lns(state, distances_km, vehicle_time_by_id, budget_D, n_workers)
    finalists[0] = (finalists[0][0], state)
    print(f"Phase D: {time.time() - t_d:.0f} sec | {state.total_cost:,.0f} Kč")

    # ── Phase E: Intenzifikace ────────────────────────────────
    print("\n" + "─" * 65)
    print("[E] Finální intenzifikace"
          + (f" — {len(finalists)} finalisté" if len(finalists) > 1 else ""))
    print("─" * 65)
    t_e   = time.time()
    state = phase_e_intensify(finalists, distances_km, vehicle_time_by_id,
                              budget_E, n_workers)
    print(f"Phase E: {time.time() - t_e:.0f} sec | {state.total_cost:,.0f} Kč")

    # ── Výstup ────────────────────────────────────────────────
    all_routes  = state.all_routes()
    total_cost  = state.total_cost
    elapsed_min = (time.time() - t_global_start) / 60
    print(f"\nCelková doba: {elapsed_min:.1f} min")

    print_results(all_routes, total_cost)

    # Dvojlinky: přiřadit druhé jízdy fyzickým autům (nebo spadnout) —
    # PŘED invariantem a uložením, ať výstupy nesou reálná vozidla.
    if args.double_runs:
        all_routes = pair_double_runs(all_routes, vehicles_expanded)

    # Pojistka č. 4 (poslední závora): vstup == naplánováno, jinak se
    # neuloží NIC. Pojistka č. 3 je záchranný re-solve ve phase C.
    verify_plan_complete(orders, all_routes)

    from closures_utils import load_active_closures
    active_closures = load_active_closures()

    save_outputs(
        all_routes, total_cost, output_dir, zone_label, elapsed_min,
        orders=orders,
        delivery_date=delivery_date,
        closures=active_closures,
        run_log_path=Path(args.run_log_path),
    )
    write_run_status("ok", EXIT_OK, "plán uložen", orders=[], extra={
        "lines_count": len(all_routes),
        "total_cost_kc": round(float(total_cost), 1),
        "total_kg": round(sum(r.get("total_kg", 0) for r in all_routes), 1),
        "output_dir": output_dir.as_posix(),
    })


if __name__ == "__main__":
    multiprocessing.freeze_support()   # nutné na Windows
    try:
        main()
    except SolverAbort:
        raise                                   # status už zapsaný v abort()
    except SystemExit as e:                     # cizí SystemExit (argparse…)
        code = e.code if isinstance(e.code, int) else EXIT_ERROR
        if code != EXIT_OK:
            write_run_status(EXIT_STATUS_NAME.get(code, "error"), code, str(e))
        raise
    except KeyboardInterrupt:
        write_run_status("error", EXIT_ERROR, "přerušeno uživatelem (Ctrl+C)")
        raise
    except Exception as e:                      # noqa: BLE001 — poslední záchrana
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        write_run_status("error", EXIT_ERROR,
                         f"[CHYBA] Neočekávaná výjimka: {type(e).__name__}: {e}\n{tb}")
        sys.exit(EXIT_ERROR)
