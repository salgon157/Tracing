"""
convert_to_riro.py — Převede lines_stops.csv do formátu RiRo pro testování v RiNkai
=====================================================================================

Vezme výsledky VRP solveru a zkonvertuje je do formátu, který importuje RiNkai.
Formát: středníkový CSV, 30 sloupců, záhlaví jen na posledních 2 (Linka, Pořadí).
Nic v existujících skriptech se nemění — jde o samostatný konverzní nástroj.

Vstupy:
  data/results/CB/2026-04-10/lines_stops.csv        ← výstup solveru
  data/static/locations_lookup.csv                  ← adresy, ZIP, město

Výstup:
  data/results/CB/2026-04-10/converted/riro_CB_2026-04-10.csv

Použití:
  python convert_to_riro.py data/results/CB/2026-04-10
  python convert_to_riro.py data/results/CB/2026-04-10 --encoding cp1250
  python convert_to_riro.py data/results/HK/2026-04-16 --locations-file data/static/locations_HK.csv
"""

import csv
import re
import argparse
from pathlib import Path
from collections import defaultdict


RIRO_VERSION = "RIRO_INPUT_LOCATIONSANDORDERS_V3.00"


# ── Pomocné funkce ────────────────────────────────────────────────────────────

def hhmm_to_seconds(s: str) -> int:
    """
    'HH:MM' → sekundy od půlnoci.
    Vrátí -1 pro prázdné nebo neparsovatelné vstupy.
    """
    if not s or not s.strip():
        return -1
    parts = s.strip().split(":")
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60
    except (ValueError, IndexError):
        return -1


def parse_window(window: str) -> tuple:
    """
    '08:00–12:00' nebo '08:00-12:00' → (sec_from, sec_to).
    Podporuje en-dash (–) i obyčejnou pomlčku (-).
    """
    if not window or not window.strip():
        return (-1, -1)
    w = window.strip().replace("–", "-").replace("—", "-")
    parts = w.split("-", 1)
    if len(parts) != 2:
        return (-1, -1)
    return (hhmm_to_seconds(parts[0]), hhmm_to_seconds(parts[1]))


def parse_address(address: str) -> tuple:
    """
    Parsuje adresní řetězec z locations_lookup.csv.
    Očekávaný formát: 'Město, Ulice číslo, PSČ, CZ'
    Vrátí (street, zip_code, city) — prázdné stringy pro chybějící data.
    """
    if not address or not address.strip():
        return ("", "", "")
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 4:
        # Formát: Město, Ulice, PSČ, CZ
        city    = parts[0]
        street  = parts[1]
        zip_code = parts[2]
    elif len(parts) == 3:
        city    = parts[0]
        street  = parts[1]
        zip_code = parts[2]
    elif len(parts) == 2:
        city    = parts[0]
        street  = parts[1]
        zip_code = ""
    else:
        city    = parts[0]
        street  = ""
        zip_code = ""
    return (street, zip_code, city)


def line_id_to_number(line_id: str) -> int:
    """
    'LINE_01' → 1, 'LINE_12' → 12, atd.
    Extrahuje číslo z konce identifikátoru linky.
    """
    m = re.search(r"(\d+)$", line_id.strip())
    return int(m.group(1)) if m else 0


def load_locations(path: Path) -> dict:
    """
    Načte locations_lookup.csv.
    Vrátí dict {location_code_lower: row_dict}.
    """
    locs = {}
    if not path.exists():
        print(f"  [WARN] Locations soubor nenalezen: {path} — adresní pole budou prázdná")
        return locs
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row.get("location_code", "").strip().lower()
            if code:
                locs[code] = row
    return locs


# ── Hlavní konverze ───────────────────────────────────────────────────────────

def is_depot_row(stop: dict) -> bool:
    """Vrátí True pro řádky skladu (začátek/konec trasy), které se do výstupu nepřidávají."""
    order_id = stop.get("order_id", "").strip()
    stop_seq = stop.get("stop_seq", "0").strip()
    place    = stop.get("place", "").lower()
    # Depot = žádné order_id, nebo stop_seq=0, nebo "sklad" v názvu místa
    return not order_id or stop_seq == "0" or "sklad" in place


def convert(result_dir: Path, locations_file: Path, encoding: str = "utf-8") -> Path:
    stops_file = result_dir / "lines_stops.csv"
    if not stops_file.exists():
        raise SystemExit(f"\n[CHYBA] Vstupní soubor nenalezen: {stops_file}\n"
                         f"Spusť nejdříve solver pro daný den/depot.")

    # Detekce depotu a data z cesty složky (…/results/CB/2026-04-10)
    date_str    = result_dir.name                       # "2026-04-10"
    depot       = result_dir.parent.name                # "CB"
    date_compact = date_str.replace("-", "")            # "20260410"

    # ── Načti data ──────────────────────────────────────────────
    stops = []
    with open(stops_file, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            stops.append(row)

    locs = load_locations(locations_file)

    # Filtruj jen doručovací zastávky (bez skladu)
    delivery_stops = [s for s in stops if not is_depot_row(s)]

    if not delivery_stops:
        raise SystemExit(f"\n[CHYBA] Žádné doručovací zastávky nalezeny v {stops_file}")

    # Seřadit pro jistotu: linka → stop_seq
    delivery_stops.sort(key=lambda s: (s.get("line_id", ""),
                                        int(s.get("stop_seq", 0))))

    # Počet zastávek per linka (col 27)
    stops_per_line: dict = defaultdict(int)
    for s in delivery_stops:
        stops_per_line[s["line_id"]] += 1

    # ── Vytvoř výstupní soubor ──────────────────────────────────
    out_dir  = result_dir / "converted"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"riro_{depot}_{date_str}.csv"

    # Záhlaví: 28 prázdných + Linka + Pořadí (30 sloupců celkem)
    header = [""] * 28 + ["Linka", "Pořadí"]

    with open(out_file, "w", newline="", encoding=encoding,
              errors="replace") as f:
        writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)

        rows_written = 0
        for stop in delivery_stops:
            loc_code = stop.get("location_code", "").strip().lower()
            loc_data = locs.get(loc_code, {})
            street, zip_code, city = parse_address(loc_data.get("address", ""))

            t_from, t_to = parse_window(stop.get("window", ""))
            note         = stop.get("note", "").strip()
            order_id     = stop.get("order_id", "").strip()
            line_id      = stop.get("line_id", "").strip()
            stop_seq     = int(stop.get("stop_seq", 0))

            try:
                kg_float    = float(stop.get("kg", "0") or 0)
                payload_raw = f"KG:{kg_float:.3f}"
            except ValueError:
                payload_raw = "KG:0.000"

            row = [
                RIRO_VERSION,                      # 0  — identifikátor formátu
                stop.get("location_code", ""),     # 1  — kód lokace
                stop.get("place", ""),             # 2  — název zákazníka
                street,                            # 3  — ulice a číslo
                "",                                # 4  — adresa 2 (prázdné)
                zip_code,                          # 5  — PSČ
                city,                              # 6  — město
                "CZ",                              # 7  — stát
                note,                              # 8  — poznámka
                "",                                # 9  — email (nemáme)
                "",                                # 10 — telefon (nemáme)
                t_from,                            # 11 — čas_od (sekundy)
                t_to,                              # 12 — čas_do (sekundy)
                -1, -1, -1, -1,                    # 13–16 (GPS/routing, nepoužíváme)
                -1000, -1000,                      # 17–18
                -1, -1, -1, -1,                    # 19–22
                order_id,                          # 23 — číslo objednávky
                date_compact,                      # 24 — datum YYYYMMDD
                "",                                # 25 — prázdné
                payload_raw,                       # 26 — hmotnost KG:XX.XXX
                stops_per_line[line_id],           # 27 — počet zastávek na lince
                line_id_to_number(line_id),        # 28 — číslo linky (Linka)
                stop_seq,                          # 29 — pořadí zastávky (Pořadí)
            ]
            writer.writerow(row)
            rows_written += 1

    # ── Souhrn ─────────────────────────────────────────────────
    lines_count = len(stops_per_line)
    print(f"\nKonverze dokončena")
    print(f"  Depot:      {depot} | Datum: {date_str}")
    print(f"  Zastávky:   {rows_written}")
    print(f"  Linky:      {lines_count}")
    print(f"  Kódování:   {encoding}")
    print(f"  Výstup:     {out_file}")
    return out_file


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Převede lines_stops.csv do formátu RiRo pro testování v RiNkai"
    )
    parser.add_argument(
        "result_dir",
        help="Složka s výsledky solveru, např. data/results/CB/2026-04-10"
    )
    parser.add_argument(
        "--locations-file",
        default="data/static/locations_lookup.csv",
        help="CSV s adresami a GPS lokací (default: data/static/locations_lookup.csv)"
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="Kódování výstupního souboru (default: utf-8). "
             "Pokud RiNkai nečte správně češtinu, zkus --encoding cp1250"
    )
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    if not result_dir.is_dir():
        raise SystemExit(f"\n[CHYBA] Složka neexistuje: {result_dir}")

    convert(result_dir, Path(args.locations_file), encoding=args.encoding)


if __name__ == "__main__":
    main()
