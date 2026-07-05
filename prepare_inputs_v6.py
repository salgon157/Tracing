
"""
prepare_inputs_v6.py — RiRo -> solver-ready orders per depot
=============================================================

Použití:
  python prepare_inputs_v6.py CB
  python prepare_inputs_v6.py HK

Vstupy:
  data/input/{DEPOT}/aktivni/riro-YYYYMMDD-{DEPOT}-POB.csv   (právě jeden soubor)
  data/static/locations_{DEPOT}.csv  (fallback: locations_lookup.csv)

Výstupy:
  data/prepared/{DEPOT}/orders_{DEPOT}_{YYYY-MM-DD}.csv
  data/prepared/{DEPOT}/missing_locs_{DEPOT}_{YYYY-MM-DD}.txt
"""

import csv
import re
import argparse
from pathlib import Path

# RiRo CSV columns (0-indexed, semicolon-delimited, no header)
COL_RECORD_TYPE    = 0
COL_LOCATION_CODE  = 1
COL_CUSTOMER_NAME  = 2
COL_TW1_FROM_SEC   = 11
COL_TW1_TO_SEC     = 12
COL_ORDER_NUMBER   = 23
COL_NOTE           = 25
COL_PAYLOAD_RAW    = 26
COL_CODE_A         = 27
COL_BLOCK_ID       = 28
EXPECTED_COLS      = 30
EXPECTED_RECORD    = "RIRO_INPUT_LOCATIONSANDORDERS_V3.00"

DATA_DIR           = Path("data")
STATIC_DIR         = DATA_DIR / "static"
INPUT_DIR          = DATA_DIR / "input"
PREPARED_DIR       = DATA_DIR / "prepared"

def seconds_to_hhmm(sec_str: str) -> str | None:
    try:
        sec = int(str(sec_str).strip())
        if sec < 0 or sec > 86400:          # max 24:00 (konec dne)
            return None
        h = sec // 3600
        m = (sec % 3600) // 60
        return f"{h:02d}:{m:02d}"
    except Exception:
        return None

def parse_payload(payload_raw: str) -> dict:
    result = {}
    if not payload_raw:
        return result
    for part in str(payload_raw).strip().split(";"):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        key = key.strip().upper()
        try:
            result[key] = float(value.strip().replace(",", "."))
        except Exception:
            result[key] = None
    return result

def parse_min_from_hhmm(value: str | None) -> int:
    if value is None:
        return 0
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return 0
    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2:
            h, m = parts
            return int(h) * 60 + int(m)
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 60 + int(m) + (1 if int(sec) > 0 else 0)
    return int(float(s))

def load_locations_lookup(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"[CHYBA] Chybí statický soubor lokací: {path}")

    locations = {}
    skipped_gps = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"location_code", "lat", "lon"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"[CHYBA] {path} nemá povinné sloupce: {sorted(required)}")
        for row in reader:
            code = str(row.get("location_code", "")).strip().lower()
            if not code:
                continue
            try:
                lat = float(row["lat"])
                lon = float(row["lon"])
            except Exception:
                skipped_gps.append(code)
                continue

            service_time_default = row.get("service_time_default", "")
            admin_time_default   = row.get("admin_time_default", "")
            base_service_min = parse_min_from_hhmm(service_time_default) or parse_min_from_hhmm(admin_time_default) or 4

            address = str(row.get("address", "")).strip()
            city = ""
            if address:
                city = address.split(",")[0].strip()

            locations[code] = {
                "lat": lat,
                "lon": lon,
                "name": str(row.get("name", "")).strip(),
                "address": address,
                "city": city,
                "comment": str(row.get("comment", "")).strip(),
                "base_service_min": int(base_service_min),
                "riro_vehicle_type_code": str(row.get("riro_vehicle_type_code", "")).strip(),
            }
    if skipped_gps:
        print(f"  [WARN] {len(skipped_gps)} lokací v {path.name} přeskočeno — "
              f"chybí/nevalidní GPS: {', '.join(skipped_gps[:10])}"
              f"{'...' if len(skipped_gps) > 10 else ''}")
    return locations

def find_active_riro_file(depot_code: str) -> tuple[Path, str]:
    """Find the single active RiRo file in data/input/{DEPOT}/aktivni/.
    Returns (file_path, date_str) where date_str is 'YYYY-MM-DD'."""
    aktivni_dir = INPUT_DIR / depot_code / "aktivni"
    if not aktivni_dir.exists():
        raise FileNotFoundError(
            f"[CHYBA] Složka neexistuje: {aktivni_dir}\n"
            f"  Vytvoř ji a vlož tam RiRo soubor: riro-YYYYMMDD-{depot_code}-POB.csv"
        )

    files = [f for f in aktivni_dir.iterdir() if f.is_file() and f.suffix == ".csv"]
    if len(files) == 0:
        raise FileNotFoundError(
            f"[CHYBA] Žádný CSV soubor v: {aktivni_dir}\n"
            f"  Vlož tam RiRo soubor: riro-YYYYMMDD-{depot_code}-POB.csv"
        )
    if len(files) > 1:
        names = ", ".join(f.name for f in sorted(files))
        raise ValueError(
            f"[CHYBA] V {aktivni_dir} je více než jeden soubor: {names}\n"
            f"  Nech tam právě JEDEN aktivní RiRo soubor."
        )

    riro_path = files[0]
    m = re.match(r"riro-(\d{4})(\d{2})(\d{2})-", riro_path.name, re.IGNORECASE)
    if not m:
        raise ValueError(
            f"[CHYBA] Název souboru '{riro_path.name}' nesedí na pattern riro-YYYYMMDD-*.csv"
        )
    date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return riro_path, date_str

def load_riro_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=";")
        for line_no, row in enumerate(reader, 1):
            if not row or not "".join(row).strip():
                continue

            if len(row) != EXPECTED_COLS:
                raise ValueError(
                    f"[CHYBA] Řádek {line_no} nemá {EXPECTED_COLS} sloupců, ale {len(row)}. "
                    "RiRo formát nesedí."
                )

            record_type = str(row[COL_RECORD_TYPE]).strip()
            if record_type != EXPECTED_RECORD:
                raise ValueError(
                    f"[CHYBA] Řádek {line_no} má neočekávaný record_type '{record_type}', "
                    f"čekám '{EXPECTED_RECORD}'."
                )

            rows.append({
                "_line": line_no,
                "record_type": record_type,
                "location_code": str(row[COL_LOCATION_CODE]).strip().lower(),
                "customer_name": str(row[COL_CUSTOMER_NAME]).strip(),
                "tw1_from_sec": str(row[COL_TW1_FROM_SEC]).strip(),
                "tw1_to_sec": str(row[COL_TW1_TO_SEC]).strip(),
                "order_number": str(row[COL_ORDER_NUMBER]).strip(),
                "note": str(row[COL_NOTE]).strip(),
                "payload_raw": str(row[COL_PAYLOAD_RAW]).strip(),
                "code_a": str(row[COL_CODE_A]).strip(),
            })
    return rows

def transform(raw_rows: list[dict], locations: dict, depot_code: str) -> tuple[list[dict], list[str]]:
    orders = []
    missing_codes = []
    seen_missing = set()
    skipped_tw = []
    payload_warnings = []

    for raw in raw_rows:
        loc_code = raw["location_code"]
        loc = locations.get(loc_code.strip().lower())
        if loc is None:
            if loc_code not in seen_missing:
                seen_missing.add(loc_code)
                missing_codes.append(loc_code)
            continue

        time_from = seconds_to_hhmm(raw["tw1_from_sec"])
        time_to   = seconds_to_hhmm(raw["tw1_to_sec"])
        if time_from is None or time_to is None:
            skipped_tw.append((raw["order_number"], loc_code,
                               f"nevalidní čas: from={raw['tw1_from_sec']}, to={raw['tw1_to_sec']}"))
            continue
        if time_from >= time_to:
            skipped_tw.append((raw["order_number"], loc_code,
                               f"noční/obrácené okno: {time_from}–{time_to}"))
            continue

        parsed_payload = parse_payload(raw["payload_raw"])
        weight_kg = parsed_payload.get("KG", 0.0) or 0.0
        if weight_kg == 0.0 and raw["payload_raw"]:
            payload_warnings.append((raw["order_number"], loc_code, raw["payload_raw"]))

        orders.append({
            "order_number": raw["order_number"],
            "location_code": loc_code,
            "customer_name": raw["customer_name"],
            "block_id": depot_code,
            "time_from": time_from,
            "time_to": time_to,
            "payload_raw": raw["payload_raw"],
            "weight_kg": round(float(weight_kg), 3),
            "lat": loc["lat"],
            "lon": loc["lon"],
            "city": loc.get("city", ""),
            "note": raw.get("note", ""),
            "base_service_min": int(loc.get("base_service_min", 4)),
            "code_a": raw.get("code_a", ""),
            "riro_vehicle_type_code": loc.get("riro_vehicle_type_code", ""),
        })

    if skipped_tw:
        print(f"\n  [WARN] {len(skipped_tw)} objednávek přeskočeno — nevalidní časová okna:")
        for order_num, code, reason in skipped_tw[:10]:
            print(f"         #{order_num} [{code}]: {reason}")
        if len(skipped_tw) > 10:
            print(f"         ... a {len(skipped_tw) - 10} dalších")

    if payload_warnings:
        print(f"\n  [WARN] {len(payload_warnings)} objednávek s nulovou vahou (KG=0 nebo chybí v payloadu):")
        for order_num, code, payload in payload_warnings[:10]:
            print(f"         #{order_num} [{code}]: payload={payload!r}")
        if len(payload_warnings) > 10:
            print(f"         ... a {len(payload_warnings) - 10} dalších")

    return orders, missing_codes

def save_orders(orders: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "order_number", "location_code", "customer_name", "block_id",
        "time_from", "time_to",
        "payload_raw", "weight_kg",
        "lat", "lon", "city", "note",
        "base_service_min", "code_a", "riro_vehicle_type_code",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(orders)

def save_missing(missing_codes: list[str], output_path: Path) -> None:
    if not missing_codes:
        if output_path.exists():
            output_path.unlink()
        return
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# location_code missing in locations_lookup.csv\n")
        for code in sorted(missing_codes):
            f.write(f"{code}\n")


# ============================================================
#  STARTUP TESTY
# ============================================================

def run_startup_tests():
    """
    Spustí pytest test suite před zpracováním dat.
    Pokud jakýkoliv test selže, skript se nespustí.
    Lze přeskočit nastavením env proměnné SKIP_STARTUP_TESTS=1.
    """
    import subprocess
    import sys
    import os
    from pathlib import Path as _Path

    if os.environ.get("SKIP_STARTUP_TESTS", "").strip() == "1":
        return

    tests_dir = _Path(__file__).parent / "tests"
    if not tests_dir.exists():
        print("[WARN] tests/ složka nenalezena — přeskakuji startup testy.")
        return

    print("[TEST] Spouštím startup testy...")
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            str(tests_dir),
            "--ignore", str(tests_dir / "test_ors_hgv_integration.py"),
            "-x", "-q", "--tb=short", "--no-header",
        ],
        capture_output=False,
        # Routing integrační testy (test_ors_hgv_integration.py) se nespouštějí
        # v prepare_inputs — ten routing nevyužívá. Spouští je vrp_solver_lines_v6.py
        # automaticky po ověření že Docker kontejnery běží.
    )
    if result.returncode != 0:
        print("\n[ABORT] Startup testy selhaly — skript se nespustí.")
        print("        Oprav chybu výše nebo spusť: pytest tests/ -v")
        sys.exit(1)
    print()


def main():
    run_startup_tests()
    parser = argparse.ArgumentParser()
    parser.add_argument("depot_code", help="Kód depa, např. CB, HK, MO, PR")
    parser.add_argument("--locations-file", default=None,
                        help="Cesta k CSV lokací. Výchozí: data/static/locations_{DEPOT}.csv, "
                             "fallback na locations_lookup.csv")
    args = parser.parse_args()

    depot_code = args.depot_code.upper()
    riro_path, date_str = find_active_riro_file(depot_code)

    if args.locations_file:
        locations_path = Path(args.locations_file)
    else:
        depot_path = STATIC_DIR / f"locations_{depot_code}.csv"
        if depot_path.exists():
            locations_path = depot_path
        else:
            locations_path = STATIC_DIR / "locations_lookup.csv"
            print(f"  [WARN] Soubor locations_{depot_code}.csv nenalezen, "
                  f"používám fallback: {locations_path}")

    locations = load_locations_lookup(locations_path)
    raw_rows = load_riro_csv(riro_path)

    print("=" * 64)
    print("prepare_inputs_v6.py — RiRo -> orders per depot")
    print("=" * 64)
    print(f"Depo:       {depot_code}")
    print(f"Datum:      {date_str}")
    print(f"Vstup:      {riro_path}")
    print(f"Lokace DB:  {locations_path}")
    print(f"Raw rows:   {len(raw_rows)}")
    print(f"GPS lookup: {len(locations)} lokací")

    orders, missing = transform(raw_rows, locations, depot_code)

    output_dir = PREPARED_DIR / depot_code
    output_file = output_dir / f"orders_{depot_code}_{date_str}.csv"
    missing_file = output_dir / f"missing_locs_{depot_code}_{date_str}.txt"

    if not orders:
        raise ValueError(f"[CHYBA] Pro depo {depot_code} nevznikly žádné objednávky.")

    save_orders(orders, output_file)
    save_missing(missing, missing_file)

    total_kg = sum(o["weight_kg"] for o in orders)
    print(f"Objednávky: {len(orders)}")
    print(f"Celkem kg:  {total_kg:,.1f}")
    print(f"Výstup:     {output_file}")
    if missing:
        print(f"Chybějící lokace: {len(missing)} → {missing_file}")

if __name__ == "__main__":
    main()
