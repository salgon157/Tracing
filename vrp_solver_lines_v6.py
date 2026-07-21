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
  data/static/vehicle_types.csv    → jeden řádek = jeden typ auta + počty per-depot
                                     (count_block_CB / _HK / _MO / _PR / _OM)
  data/static/locations_lookup.csv → GPS souřadnice lokací

Poznámky:
- Depot kód je businessové omezení a respektuje se už ve vstupním kroku.
- Solver pracuje vždy nad jedním depem / zónou.
- Výstupem je line + vehicle type, ne konkrétní řidič.
"""
import csv
import re
import argparse
import json
import subprocess
import requests
import numpy as np
import pandas as pd
import multiprocessing
import math
import time
import random
from datetime import datetime
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from sklearn.cluster import KMeans
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

from osm_routing import add_osm_args, apply_osm_source

SOLVER_VERSION = "v6"   # verze solveru — zvedni ručně při větších změnách logiky

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
#   driving      — dodávky mají dojet skoro všude (naměřeno v Praze 0,00 %),
#                  jakýkoli nárůst = opravdu vadná data → přísně
#   driving-hgv  — kamiony legitimně nedojedou do center měst (pěší zóny,
#                  zákazy vjezdu). Naměřeno CB 1,14 %, PR 2,04 % — dvě adresy
#                  v centru Teplic samy dělají ~2 % matice → volněji
UNREACHABLE_MATRIX_FAIL_PCT = 0.015                 # default (driving)
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
    "vehicle_types_file":            "data/static/vehicle_types.csv",

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
    #   1.02 = počítáme s 2 % vyšší kapacitou (slack při balení, vzdušné mezery)
    "vehicle_capacity_multiplier":   1.02,

    # Pozn.: doba zastávky NENÍ v CONFIG — chodí předpočítaná z ESO9 v riro
    # (payload SEC) a prepare ji předává ve sloupci `service_sec`. Žádný vzorec.

    # Maximální počet zákaznických zastávek na jedné trase (sklad se nepočítá)
    # None nebo 0 = neomezeno
    "max_stops_per_route":           20,

    # Pozn.: fixní náklad za výjezd vozidla (mzda řidiče atd.) je per-type
    # ve sloupci `start_cost_kc` v vehicle_types.csv. Není v CONFIG.

    # Max délka jedné trasy
    "max_route_duration_h":          23.5,

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
}


# ============================================================
#  NAČTENÍ DAT
# ============================================================


def load_vehicle_types_db(path: str, block_id: str = "") -> list:
    """
    Načte vehicle_types.csv — každý řádek = jeden typ vozidla.
    Vrátí list pseudo-vozidel expandovaných podle count_block_{block_id}
    (pokud sloupec existuje), jinak podle available_count.
    """
    vehicles = []
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"[CHYBA] {path} nenalezen.\n"
            "Vytvoř soubor data/static/vehicle_types.csv."
        )

    with open(p, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"type_code", "type_name", "max_kg", "cost_per_km", "available_count"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"[CHYBA] {path} nemá povinné sloupce: {sorted(required)}")

        block_col = f"count_block_{block_id}" if block_id else ""
        use_block_col = block_col and block_col in (reader.fieldnames or [])
        if use_block_col:
            print(f"  [vehicle] Používám per-block počty ze sloupce '{block_col}'")
        else:
            print(f"  [vehicle] Sloupec '{block_col}' nenalezen — fallback na available_count")

        for row in reader:
            type_code = str(row.get("type_code", "")).strip()
            if not type_code or type_code.startswith("#"):
                continue

            try:
                max_kg_raw = float(row["max_kg"])
                # Kapacitní násobič — solver počítá s mírně vyšší kapacitou
                # (slack při balení, vzdušné mezery). Config: vehicle_capacity_multiplier.
                max_kg = max_kg_raw * float(CONFIG.get("vehicle_capacity_multiplier", 1.0))
                cost_per_km = float(row["cost_per_km"])
                if use_block_col:
                    count = int(float(row[block_col]))
                else:
                    count = int(float(row["available_count"]))
            except (ValueError, KeyError) as e:
                print(f"  [!] vehicle_types: přeskakuji řádek {row} — {e}")
                continue

            if count <= 0:
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

    if not vehicles:
        raise ValueError(f"[CHYBA] {path} neobsahuje žádné dostupné typy vozidel.")
    return vehicles


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
            raise ValueError(
                f"[CHYBA] {path} je ze starého prepare — chybí sloupec 'service_sec'.\n"
                "        Solver podporuje jen data s předpočítaným časem zastávky "
                "z ESO9.\n"
                "        Vytvoř soubor znovu: python prepare_inputs_v6.py {DEPO}"
            )
        for i, row in enumerate(reader, 1):
            # Kontrola povinných sloupců
            missing = [c for c in required if c not in row or not row[c].strip()]
            if missing:
                print(f"  [!] Řádek {i}: chybí {missing}, přeskakuji")
                continue

            try:
                weight_kg = float(row["weight_kg"])
                lat       = float(row["lat"])
                lon       = float(row["lon"])
                service_sec = int(float(row["service_sec"]))
            except ValueError as e:
                print(f"  [!] Řádek {i}: neplatná čísla — {e}, přeskakuji")
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

                # Aliasy pro kompatibilitu s algoritmem (neměň)
                "id":            row["order_number"].strip(),
                "name":          row.get("customer_name", row["order_number"]).strip(),
            })

    if not orders:
        raise ValueError(f"[CHYBA] {path} neobsahuje žádné objednávky.")

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
        raise SystemExit(
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
    raise SystemExit(
        f"\n[CHYBA] Routing pro profil '{profile}' selhal: {reason}\n"
        f"        Těžká vozidla (ORS / driving-hgv) NEJSOU dostupná. Plánování by je\n"
        f"        jinak tiše počítalo jako osobní auta → špatné trasy pro kamiony\n"
        f"        (mosty, úzké uličky, váhové/výškové zákazy).\n"
        f"        • Zkontroluj ORS kontejner (ors-current / ors-stable) a jeho logy.\n"
        f"        • Vědomě dovolit fallback na osobní profil: --allow-profile-fallback"
    )


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
            raise SystemExit("\n[CHYBA] OSRM neběží. Spusť: docker start osrm-server")
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


def assign_vehicles_to_clusters(clusters: list, vehicles_expanded: list) -> list:
    """
    Přidělí vozidla clusterům dle kombinovaného demand score
    (kg + počet stop + TW tlak + vzdálenost od depa).
    """
    n_clusters = len(clusters)
    n_vehicles = len(vehicles_expanded)
    if n_clusters == 0:
        return []

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
    max_dur_min   = int(CONFIG["max_route_duration_h"] * 60)

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
        "num_vehicles":        len(vehicles_expanded),
        "depot":               0,
        "max_dur_min":         max_dur_min,
        "cost_scale":          COST_SCALE,
        "max_stops_per_route": max_stops,
    }


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
        time_cb_indices, 60,
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
    cluster_orders   = args["cluster_orders"]
    cluster_vehicles = args["cluster_vehicles"]
    sub_dist         = np.array(args["sub_dist"])
    sub_times        = [np.array(st) for st in args["sub_times"]]
    time_limit       = args["time_limit_sec"]

    routes, cost = solve_cluster(
        cluster_orders, cluster_vehicles, sub_dist, sub_times, time_limit
    )
    return {
        "seed_name":   args["seed_name"],
        "cluster_idx": args["cluster_idx"],
        "routes":      routes,
        "cost":        cost,
    }


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

def phase_c_best_seed(orders, vehicles_expanded, distances_km, vehicle_time_by_id,
                       n_clusters, time_budget_sec, n_workers) -> SolutionState:
    seed = CONFIG["random_seed"]
    seeds_labels = {
        "kmeans":      partition_kmeans(orders, n_clusters, seed),
        "sweep":       partition_sweep(orders, n_clusters),
        "tw_midpoint": partition_tw_midpoint(orders, n_clusters, seed),
    }

    time_per_cluster = max(20, time_budget_sec // max(n_clusters, 1))
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
                "time_limit_sec":  time_per_cluster,
            })

    print(f"  {len(all_worker_args)} cluster-solve úloh paralelně "
          f"({n_workers} workerů, {time_per_cluster} sec/cluster)...")

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

    best_seed_name     = None
    best_penalized     = float("inf")
    seed_penalty       = CONFIG["seed_unsolved_cluster_penalty_kc"]

    for seed_name, cluster_results in results_by_seed.items():
        expected  = len(seed_cluster_data[seed_name]["clusters"])
        solved    = sum(1 for r in cluster_results.values() if r.get("routes"))
        unsolved  = expected - solved
        raw_total = sum(r.get("cost", 0) for r in cluster_results.values())
        penalized = raw_total + unsolved * seed_penalty

        print(f"  Seed '{seed_name}': {raw_total:,.0f} Kč raw | "
              f"{penalized:,.0f} Kč pen. | {solved}/{expected} clusterů")

        if solved == 0:
            continue
        if penalized < best_penalized:
            best_penalized = penalized
            best_seed_name = seed_name

    if best_seed_name is None:
        raise RuntimeError("Žádný seed nenašel řešení. Zkontroluj TW nebo kapacity.")

    print(f"\n  ✓ Nejlepší seed: '{best_seed_name}' (pen. {best_penalized:,.0f} Kč)")

    labels        = seeds_labels[best_seed_name]
    scd           = seed_cluster_data[best_seed_name]
    clusters      = scd["clusters"]
    c_indices     = scd["cluster_indices"]
    vehicle_asgn  = scd["vehicle_assignments"]
    cluster_res   = results_by_seed[best_seed_name]

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
        clusters=clusters,
        cluster_indices=c_indices,
        vehicle_assignments=vehicle_asgn,
        cluster_routes_list=cluster_routes_list,
        cluster_costs=cluster_costs,
    )


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

def phase_e_intensify(state, distances_km, vehicle_time_by_id, time_budget_sec, n_workers):
    n_clusters = len(state.clusters)
    if n_clusters == 0:
        return state

    time_per_cluster = max(15, int(time_budget_sec / math.ceil(n_clusters / n_workers)))
    print(f"  {n_clusters} clusterů × {time_per_cluster} sec, {n_workers} workerů")

    worker_args = []
    for c_idx, (c_orders, c_indices, c_vehicles) in enumerate(
            zip(state.clusters, state.cluster_indices, state.vehicle_assignments)):
        if not c_orders:
            continue
        cluster_v_times = [vehicle_time_by_id[v["id"]] for v in c_vehicles]
        sub_dist, sub_times = extract_submatrix(distances_km, cluster_v_times, c_indices)
        worker_args.append({
            "seed_name":       "intensify",
            "cluster_idx":     c_idx,
            "cluster_orders":  c_orders,
            "cluster_vehicles":c_vehicles,
            "sub_dist":        sub_dist.tolist(),
            "sub_times":       [st.tolist() for st in sub_times],
            "time_limit_sec":  time_per_cluster,
        })

    new_routes = list(state.cluster_routes)
    new_costs  = list(state.cluster_costs)

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_worker_solve_cluster, args): args["cluster_idx"]
                   for args in worker_args}
        for future in as_completed(futures):
            try:
                res   = future.result()
                c_idx = res["cluster_idx"]
                old   = new_costs[c_idx]
                if res["routes"] and res["cost"] < old:
                    new_routes[c_idx] = res["routes"]
                    new_costs[c_idx]  = res["cost"]
                    print(f"  [E] Cluster {c_idx+1:02d}: "
                          f"−{old - res['cost']:,.0f} Kč → {res['cost']:,.0f} Kč")
                else:
                    print(f"  [E] Cluster {c_idx+1:02d}: žádné zlepšení")
            except Exception as e:
                print(f"  [E] Chyba: {e}")

    return SolutionState(
        orders=state.orders,
        cluster_labels=state.cluster_labels,
        clusters=state.clusters,
        cluster_indices=state.cluster_indices,
        vehicle_assignments=state.vehicle_assignments,
        cluster_routes_list=new_routes,
        cluster_costs=new_costs,
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

    return {
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
            "max_route_duration_h":         CONFIG["max_route_duration_h"],
            "budget_phase_C_pct":           CONFIG["budget_phase_C_pct"],
            "budget_phase_D_pct":           CONFIG["budget_phase_D_pct"],
            "budget_phase_E_pct":           CONFIG["budget_phase_E_pct"],
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
    print(f"\n  [run log] uloženo → {run_log_path}  (run_id: {record['run_id']})")

    if previous:
        print_run_diff(record, previous)


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

    print(f"num_clusters_raw:            {CONFIG['num_clusters']}")
    print(f"parallel_workers_raw:        {CONFIG['parallel_workers']}")
    print(f"random_seed:                 {CONFIG['random_seed']}")

    print(f"time_buffer_fixed_min:       {CONFIG['time_buffer_fixed_min']}")
    print(f"time_buffer_pct:             {CONFIG['time_buffer_pct']}")
    print(f"max_route_duration_h:        {CONFIG['max_route_duration_h']}")

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

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders-file", default=CONFIG["orders_file"],
                        help="Solver-ready orders CSV pro jeden block")
    parser.add_argument("--vehicle-types-file", default=CONFIG["vehicle_types_file"],
                        help="CSV s typy vozidel")
    parser.add_argument("--output-dir", default="output",
                        help="Složka pro výstupy")
    parser.add_argument("--zone-label", default="",
                        help="Popisek zóny/bloku do výstupů; když chybí, bere se ze souboru")
    parser.add_argument("--force-matrix", action="store_true",
                        help="NOUZOVÝ přepínač: vypne limit nedosažitelných párů pro VŠECHNY "
                             "profily. Běžně NENÍ potřeba — limity jsou per profil "
                             "(driving 1,5 %%, driving-hgv 5 %%) a pokrývají i Prahu. "
                             "Použij jen když víš, že data jsou v pořádku a limit "
                             "přesto brání běhu. Nedosažitelné páry dostanou sentinel "
                             "a solver je nepřiřadí tak jako tak.")
    parser.add_argument("--budget-min", type=float, default=None,
                        help="Override celkového časového budgetu solveru (v minutách). "
                             "Default je v CONFIG['total_time_budget_sec'] (30 min). "
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
        raise SystemExit(
            "\n[CHYBA] Chybí --orders-file.\n"
            "Příklad: python vrp_solver_lines_v6.py "
            "--orders-file data/prepared/CB/orders_CB_2026-04-10.csv"
        )
    if not Path(args.orders_file).exists():
        raise SystemExit(
            f"\n[CHYBA] Orders soubor neexistuje: {args.orders_file}\n"
            f"Nejdříve spusť: python prepare_inputs_v6.py <DEPOT_CODE>"
        )
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

    # ── --budget-min: override total time budget ──────────────────────────
    if args.budget_min is not None:
        CONFIG["total_time_budget_sec"] = int(args.budget_min * 60)
        print(f"[BUDGET] Override: {args.budget_min:g} min "
              f"({CONFIG['total_time_budget_sec']} s)")

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

    # ── Volba OSM routing instance (stable vs current) ─────────────────────
    osm_source = "current" if args.fresh_osm else "stable"
    apply_osm_source(CONFIG, osm_source)
    print(f"[OSM] zdroj: {osm_source}"
          f"{' (fresh)' if args.fresh_osm else ''}"
          f"  | OSRM={CONFIG['osrm_urls']['driving']}"
          f"  | ORS={CONFIG['osrm_urls']['driving-hgv']}")

    # ── Preflight: zajisti že routing instance odpovídá ──────────────────
    # Pro --fresh-osm: orchestrator stáhne aktuální OSM data a nastartuje
    # Docker kontejnery (osrm-current, ors-current). Pro stable: jen ping.
    if args.fresh_osm:
        from osrm_orchestrator import ensure_fresh_routing_ready
        ensure_fresh_routing_ready()
    else:
        _osrm_ping_url = (
            f"{CONFIG['osrm_url']}/route/v1/driving/14.4,50.0;14.5,50.1?overview=false"
        )
        try:
            requests.get(_osrm_ping_url, timeout=2)
        except requests.exceptions.RequestException:
            raise SystemExit(
                f"\n[CHYBA] Routing instance ({CONFIG['osrm_url']}) neodpovídá.\n"
                f"        Spusť Docker kontejner: scripts/start_osrm_stable.bat"
            )

    # Routing instance je nahoře — spusť integrační testy ORS vs OSRM.
    run_routing_tests(
        osrm_url=CONFIG["osrm_urls"]["driving"],
        ors_url=CONFIG["osrm_urls"]["driving-hgv"],
    )

    orders            = load_orders_day(args.orders_file)
    block_id          = orders[0].get("block_id", "").strip() if orders else ""
    vehicles_expanded = load_vehicle_types_db(args.vehicle_types_file, block_id=block_id)

    # Auto-detekce výstupní složky z názvu orders souboru
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

    cfg_clusters = CONFIG["num_clusters"]
    n_clusters   = (auto_n_clusters(len(orders), len(vehicles_expanded))
                    if cfg_clusters == "auto" else int(cfg_clusters))

    cfg_workers = CONFIG["parallel_workers"]
    n_workers   = (max(1, multiprocessing.cpu_count() - 1)
                   if cfg_workers == "auto" else int(cfg_workers))

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
    print(f"  Zóna/block:  {zone_label}")
    print(f"  Clustery:    {n_clusters}")
    print(f"  CPU workerů: {n_workers}")

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
    state = phase_c_best_seed(
        orders, vehicles_expanded, distances_km, vehicle_time_by_id,
        n_clusters, int(budget_C), n_workers
    )
    print(f"Phase C: {time.time() - t_after_osrm:.0f} sec | {state.total_cost:,.0f} Kč")

    # ── Phase D: LNS ─────────────────────────────────────────
    print("\n" + "─" * 65)
    print("[D] Cross-cluster LNS")
    print("─" * 65)
    t_d   = time.time()
    state = phase_d_lns(state, distances_km, vehicle_time_by_id, budget_D, n_workers)
    print(f"Phase D: {time.time() - t_d:.0f} sec | {state.total_cost:,.0f} Kč")

    # ── Phase E: Intenzifikace ────────────────────────────────
    print("\n" + "─" * 65)
    print("[E] Finální intenzifikace")
    print("─" * 65)
    t_e   = time.time()
    state = phase_e_intensify(state, distances_km, vehicle_time_by_id, budget_E, n_workers)
    print(f"Phase E: {time.time() - t_e:.0f} sec | {state.total_cost:,.0f} Kč")

    # ── Výstup ────────────────────────────────────────────────
    all_routes  = state.all_routes()
    total_cost  = state.total_cost
    elapsed_min = (time.time() - t_global_start) / 60
    print(f"\nCelková doba: {elapsed_min:.1f} min")

    print_results(all_routes, total_cost)

    from closures_utils import load_active_closures
    active_closures = load_active_closures()

    save_outputs(
        all_routes, total_cost, output_dir, zone_label, elapsed_min,
        orders=orders,
        delivery_date=delivery_date,
        closures=active_closures,
        run_log_path=Path(args.run_log_path),
    )


if __name__ == "__main__":
    multiprocessing.freeze_support()   # nutné na Windows
    main()
