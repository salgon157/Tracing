"""
manage_closures.py — správa uzavírek pro VRP solver
=====================================================

Uzavírka = GPS úsečka (začátek + konec zavřeného úseku) + buffer zóna.
Solver automaticky penalizuje trasy procházející aktivními uzavírkami.

Použití:
  python manage_closures.py list
  python manage_closures.py add
  python manage_closures.py toggle CLO_001
  python manage_closures.py remove CLO_001
  python manage_closures.py test --orders-file data/prepared/CB/orders_CB_2026-04-10.csv
"""

import json
import re
import argparse
import csv
import requests
from pathlib import Path
from datetime import date

CLOSURES_FILE = Path("data/static/closures.json")


# ============================================================
#  LOAD / SAVE
# ============================================================

def load_data() -> dict:
    if not CLOSURES_FILE.exists():
        return {"version": 1, "closures": []}
    with open(CLOSURES_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict) -> None:
    CLOSURES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CLOSURES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  Uloženo: {CLOSURES_FILE}")


def next_id(closures: list) -> str:
    nums = [int(m.group(1)) for c in closures
            if (m := re.match(r"CLO_(\d+)", c["id"]))]
    return f"CLO_{(max(nums) + 1) if nums else 1:03d}"


# ============================================================
#  PŘÍKAZY
# ============================================================

def cmd_list(_args):
    data = load_data()
    cl = data["closures"]
    if not cl:
        print("Žádné uzavírky v databázi.")
        return

    today = str(date.today())
    print(f"\n{'ID':<10} {'Stav':<12} {'Název':<35} {'Platnost':<24} {'Buffer'}")
    print("─" * 100)
    for c in cl:
        expired = c.get("valid_to") and c["valid_to"] < today
        future  = c.get("valid_from") and c["valid_from"] > today
        if not c["active"]:
            status = "✗ neaktivní"
        elif expired:
            status = "⚠ expirováno"
        elif future:
            status = "⏳ budoucí"
        else:
            status = "✓ aktivní"
        valid = f"{c.get('valid_from','?')} → {c.get('valid_to') or '∞'}"
        buf = f"{c.get('buffer_km', 0.1)} km"
        print(f"{c['id']:<10} {status:<18} {c['name']:<35} {valid:<24} {buf}")

    active = sum(1 for c in cl if c["active"]
                 and not (c.get("valid_to") and c["valid_to"] < today)
                 and not (c.get("valid_from") and c["valid_from"] > today))
    print(f"\nCelkem: {len(cl)} uzavírek | {active} dnes aktivních")


def cmd_add(_args):
    data = load_data()

    print("\n══════════════════════════════════════════")
    print("  Přidat novou uzavírku")
    print("══════════════════════════════════════════")
    print("Jak najít GPS souřadnice:")
    print("  openstreetmap.org → pravý klik na bod → 'Show address'")
    print("  nebo použij mapy.cz → klik → souřadnice v URL\n")

    def ask(prompt, required=True):
        while True:
            val = input(prompt).strip()
            if val or not required:
                return val
            print("  (povinné pole)")

    def ask_float(prompt):
        while True:
            raw = ask(prompt).replace(",", ".")
            try:
                return float(raw)
            except ValueError:
                print("  Neplatná hodnota — zadej číslo, např. 49.6712")

    name = ask("Název uzavírky (např. 'Uzavírka most Golčův Jeníkov'): ")

    print("\nZačátek uzavřeného úseku:")
    lat1 = ask_float("  Šířka / lat (např. 49.6712): ")
    lon1 = ask_float("  Délka / lon (např. 15.4832): ")

    print("\nKonec uzavřeného úseku:")
    lat2 = ask_float("  Šířka / lat: ")
    lon2 = ask_float("  Délka / lon: ")

    buf_raw = input("\nBuffer zóna v km — jak daleko od úsečky se detekuje [0.15]: ").strip() or "0.15"
    try:
        buffer_km = float(buf_raw.replace(",", "."))
    except ValueError:
        buffer_km = 0.15

    valid_from = input(f"\nPlatná od (YYYY-MM-DD) [dnes = {date.today()}]: ").strip() or str(date.today())
    valid_to   = input("Platná do  (YYYY-MM-DD) [prázdné = bez konce]: ").strip() or None
    notes      = ask("Poznámka [volitelné]: ", required=False)

    new_c = {
        "id":         next_id(data["closures"]),
        "name":       name,
        "active":     True,
        "created":    str(date.today()),
        "valid_from": valid_from,
        "valid_to":   valid_to,
        "segment": {
            "from": {"lat": lat1, "lon": lon1},
            "to":   {"lat": lat2, "lon": lon2},
        },
        "buffer_km": buffer_km,
        "notes":     notes,
    }

    data["closures"].append(new_c)
    save_data(data)

    seg = new_c["segment"]
    print(f"\n✓ Přidáno {new_c['id']}: {name}")
    print(f"  Úsek: ({seg['from']['lat']}, {seg['from']['lon']}) → ({seg['to']['lat']}, {seg['to']['lon']})")
    print(f"  Buffer: {buffer_km} km | Platnost: {valid_from} → {valid_to or '∞'}")


def cmd_toggle(args):
    data = load_data()
    found = next((c for c in data["closures"] if c["id"] == args.id), None)
    if not found:
        print(f"[CHYBA] Uzavírka {args.id} nenalezena.")
        return
    found["active"] = not found["active"]
    save_data(data)
    stav = "✓ aktivována" if found["active"] else "✗ deaktivována"
    print(f"{stav}: {found['id']} — {found['name']}")


def cmd_remove(args):
    data = load_data()
    before = len(data["closures"])
    data["closures"] = [c for c in data["closures"] if c["id"] != args.id]
    if len(data["closures"]) == before:
        print(f"[CHYBA] Uzavírka {args.id} nenalezena.")
        return
    save_data(data)
    print(f"✓ Uzavírka {args.id} odstraněna.")


def cmd_test(args):
    """Otestuje uzavírky proti orders souboru přes stejnou logiku jako solver."""
    from closures_utils import (
        build_closure_candidate_sets,
        confirm_closure_candidates,
        load_active_closures,
    )

    orders_path = Path(args.orders_file)
    if not orders_path.exists():
        print(f"[CHYBA] {orders_path} nenalezen.")
        return

    closures = load_active_closures()
    if not closures:
        print("Žádné aktivní uzavírky dnes.")
        return

    # Načti lokace z orders
    locations = [(49.5062, 15.5950)]  # depot jako první
    with open(orders_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                locations.append((float(row["lat"]), float(row["lon"])))
            except (ValueError, KeyError):
                pass

    osrm_url = getattr(args, "osrm_url", "http://localhost:5000")
    if len(locations) > 1:
        probe_from = locations[0]
        probe_to = locations[1]
        probe_url = (
            f"{osrm_url}/route/v1/driving/"
            f"{probe_from[1]},{probe_from[0]};{probe_to[1]},{probe_to[0]}"
            f"?overview=false"
        )
        try:
            resp = requests.get(probe_url, timeout=3)
            if resp.status_code != 200:
                print(f"[CHYBA] OSRM neodpovídá správně ({resp.status_code}).")
                print("Spusť nejdřív routing backend a pak test opakuj.")
                return
        except Exception as exc:
            print(f"[CHYBA] OSRM není dostupný pro potvrzovací test: {exc}")
            print("Spusť nejdřív routing backend a pak test opakuj.")
            return

    n = len(locations)
    print(f"\nTestuji {len(closures)} uzavírek proti {n} lokacím ({n*n} párů)...")

    all_candidates, per_closure_candidates = build_closure_candidate_sets(locations, closures)
    print(f"  Broad kandidáti celkem: {len(all_candidates)}")

    confirmed, per_closure_confirmed = confirm_closure_candidates(
        sorted(all_candidates),
        locations,
        closures,
        matrix_profile="driving",
        osrm_url=osrm_url,
        closure_route_profile="driving-hgv",
    )
    print(f"  Potvrzené páry celkem: {len(confirmed)}")

    for closure in closures:
        candidate_count = len(per_closure_candidates.get(closure["id"], set()))
        confirmed_count = len(per_closure_confirmed.get(closure["id"], set()))
        print(f"\n[{closure['id']}] {closure['name']}")
        print(f"  Broad kandidátní páry: {candidate_count}")
        print(f"  Potvrzeno přes baseline geometrii: {confirmed_count}")
        if confirmed_count == 0:
            print("  → Žádné potvrzené ovlivněné páry.")


# ============================================================
#  MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Správa uzavírek pro VRP solver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", metavar="příkaz")

    sub.add_parser("list",   help="Zobraz všechny uzavírky")
    sub.add_parser("add",    help="Přidej novou uzavírku (interaktivní)")

    p_tog = sub.add_parser("toggle", help="Aktivuj/deaktivuj uzavírku")
    p_tog.add_argument("id", help="ID uzavírky, např. CLO_001")

    p_rem = sub.add_parser("remove", help="Odstraň uzavírku trvale")
    p_rem.add_argument("id", help="ID uzavírky")

    p_test = sub.add_parser("test", help="Otestuj uzavírky proti orders souboru")
    p_test.add_argument("--orders-file", required=True)
    p_test.add_argument("--osrm-url", default="http://localhost:5000")

    args = parser.parse_args()

    dispatch = {
        "list":   cmd_list,
        "add":    cmd_add,
        "toggle": cmd_toggle,
        "remove": cmd_remove,
        "test":   cmd_test,
    }

    if args.cmd in dispatch:
        dispatch[args.cmd](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
