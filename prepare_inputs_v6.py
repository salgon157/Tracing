
"""
prepare_inputs_v6.py — RiRo -> solver-ready orders per depot
=============================================================

Použití:
  python prepare_inputs_v6.py CB
  python prepare_inputs_v6.py CB --allow-drops                 (jeď i s vadnými řádky)
  python prepare_inputs_v6.py CB --data-root data/prediction --prediction

Vstupy:
  {data-root}/input/{DEPOT}/aktivni/riro-YYYYMMDD-{DEPOT}.csv   (právě jeden soubor)

Výstupy:
  {data-root}/prepared/{DEPOT}/orders_{DEPOT}_{YYYY-MM-DD}.csv
  {data-root}/prepared/{DEPOT}/prepare_stats_{DEPOT}_{YYYY-MM-DD}.json

RiRo z ESO9 je jediný zdroj pravdy — nese GPS (sloupce R/S), předpočítaný čas
zastávky (SEC v payloadu), datum rozvozu (Y) i kg z minulého závozu (AE).
Statická data locations_*.csv už NEJSOU potřeba.

Predikční režim (--prediction): objednávky s DŘÍVĚJŠÍM datem rozvozu jsou
dopredikované a projdou losem podle šance, že adresa v daný den v týdnu
opravdu objedná (spočítáno z historie závozů — viz order_history.py).
Objednávka se do plánu dostane celá, nebo vůbec; kilogramy se neškálují.
Bez tohoto flagu je jiné datum rozvozu chyba exportu a běh skončí.

Přísný režim: když jakýkoliv řádek neprojde validací, skript vypíše které a proč
a SKONČÍ CHYBOU — správně je jen když projdou všechny. --allow-drops to obejde.

--data-root (default "data") přesměruje input/ a prepared/ pod jiný kořen,
např. data/prediction pro predikční běhy.
"""

import csv
import json
import re
import sys
import argparse
from pathlib import Path

# RiRo CSV columns (0-indexed, semicolon-delimited, no header)
COL_RECORD_TYPE    = 0
COL_LOCATION_CODE  = 1
COL_CUSTOMER_NAME  = 2
COL_CITY           = 6
COL_TW1_FROM_SEC   = 11
COL_TW1_TO_SEC     = 12
COL_LON            = 17    # R — dřív rezerva s -1000, od 17.7.2026 nese lon
COL_LAT            = 18    # S — dřív rezerva s -1000, od 17.7.2026 nese lat
COL_ORDER_NUMBER   = 23
COL_DELIVERY_DATE  = 24    # Y — datum ROZVOZU (YYYYMMDD), ne datum zadání
COL_NOTE           = 25
COL_PAYLOAD_RAW    = 26    # "KG:51.475#SEC:261"
COL_CODE_A         = 27
COL_BLOCK_ID       = 28
COL_PREV_KG        = 30    # AE — kg z minulého závozu; -1000 = minule nebyl
# Kontrola struktury: kolik polí musí řádek mít. Formáty se překrývají
# v počtu sloupců, proto se rozlišují i podle OBSAHU (#SEC v payloadu).
EXPECTED_COLS      = 31    # od 28.7.2026: přibyl sloupec AE (kg z minula)
PREVIOUS_COLS      = 30    # 17.–23.7.2026: stejné, ale bez AE
TRANSITIONAL_COLS  = 32    # slepá ulička z 16.7.2026 (GPS nalepené na konec)
EXPECTED_RECORD    = "RIRO_INPUT_LOCATIONSANDORDERS_V3.00"

# Sanity rozsah ČR — chytí prohozené lat/lon i nesmyslné souřadnice
LON_RANGE = (11.0, 20.0)
LAT_RANGE = (47.0, 52.0)

# Hodnota ve sloupci AE, když objednávka minulý závoz neměla.
PREV_KG_NONE = -1000.0

# Horní mez času zastávky. Legitimní SEC z ESO9 nikdy nepřekročil ~1,5 h
# (max 5 160 s, 17.–28. 7. 2026); 31. 7. přišly vadné hodnoty až 96 742 s
# (26,9 h) a nesplnitelná objednávka tehdy tiše shodila celý cluster v solveru.
# Řádek nad limit = vadný payload -> přísný režim soubor odmítne celý.
SERVICE_SEC_MAX = 7200      # 2 h

# Koeficient nárůstu/poklesu kg (jen predikční režim): suma dnes / suma minule
# ze spárovaných objednávek. Ořez chrání před nesmyslným plánem ze špatných dat,
# minimum párů před tím, aby pár výjimek určilo celý den.
KG_COEF_MIN   = 0.5
KG_COEF_MAX   = 2.0
KG_COEF_MIN_PAIRS = 10

DATA_DIR           = Path("data")
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
    """"KG:51.475#SEC:261" -> {"KG": 51.475, "SEC": 261.0}. Oddělovač je '#'."""
    result = {}
    if not payload_raw:
        return result
    for part in str(payload_raw).strip().split("#"):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        key = key.strip().upper()
        try:
            result[key] = float(value.strip().replace(",", "."))
        except Exception:
            result[key] = None
    return result

def find_active_riro_file(depot_code: str, input_dir: Path = INPUT_DIR) -> tuple[Path, str]:
    """Find the single active RiRo file in {input_dir}/{DEPOT}/aktivni/.
    Returns (file_path, date_str) where date_str is 'YYYY-MM-DD'."""
    aktivni_dir = input_dir / depot_code / "aktivni"
    if not aktivni_dir.exists():
        raise FileNotFoundError(
            f"[CHYBA] Složka neexistuje: {aktivni_dir}\n"
            f"  Vytvoř ji a vlož tam RiRo soubor: riro-YYYYMMDD-{depot_code}.csv"
        )

    files = [f for f in aktivni_dir.iterdir() if f.is_file() and f.suffix == ".csv"]
    if len(files) == 0:
        raise FileNotFoundError(
            f"[CHYBA] Žádný CSV soubor v: {aktivni_dir}\n"
            f"  Vlož tam RiRo soubor: riro-YYYYMMDD-{depot_code}.csv"
        )
    if len(files) > 1:
        names = ", ".join(f.name for f in sorted(files))
        raise ValueError(
            f"[CHYBA] V {aktivni_dir} je více než jeden soubor: {names}\n"
            f"  Nech tam právě JEDEN aktivní RiRo soubor."
        )

    riro_path = files[0]
    return riro_path, parse_date_from_filename(riro_path.name)


def parse_date_from_filename(name: str) -> str:
    """'riro-20260803-CB.csv' -> '2026-08-03'. Datum závozu je vždy z NÁZVU."""
    m = re.match(r"riro-(\d{4})(\d{2})(\d{2})-", name, re.IGNORECASE)
    if not m:
        raise ValueError(
            f"[CHYBA] Název souboru '{name}' nesedí na pattern riro-YYYYMMDD-*.csv"
        )
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

def check_row_format(row: list, line_no: int) -> None:
    """Struktura JEDNOHO řádku — počet sloupců."""
    if len(row) == TRANSITIONAL_COLS:
        raise ValueError(
            f"[CHYBA] Řádek {line_no} má {TRANSITIONAL_COLS} sloupců — to je "
            f"přechodný formát z 16.7.2026 (GPS nalepené na konci).\n"
            f"        Ten už není podporovaný. Exportuj finální formát z ESO9."
        )
    if len(row) == PREVIOUS_COLS:
        raise ValueError(
            f"[CHYBA] Řádek {line_no} má {PREVIOUS_COLS} sloupců — to je formát "
            f"ze 17.–23.7.2026, kterému chybí sloupec AE (kg z minulého závozu).\n"
            f"        Exportuj aktuální formát z ESO9 ({EXPECTED_COLS} sloupců)."
        )
    if len(row) != EXPECTED_COLS:
        raise ValueError(
            f"[CHYBA] Řádek {line_no} nemá {EXPECTED_COLS} sloupců, ale {len(row)}. "
            "RiRo formát nesedí."
        )


def check_file_format(first_row: list) -> None:
    """Formát CELÉHO souboru — pozná se podle prvního datového řádku.

    Starý i finální formát mají shodně 30 sloupců, takže je rozliší jen obsah:
    finální nese SEC v payloadu. Kontrola je záměrně jen na prvním řádku —
    chybějící SEC na dalších řádcích je vada DAT (řeší přísný režim v transform),
    ne špatný formát souboru.
    """
    if "#SEC:" not in str(first_row[COL_PAYLOAD_RAW]):
        raise ValueError(
            f"[CHYBA] Soubor je ve starém formátu — první řádek nemá SEC v payloadu "
            f"(sloupec AA = {str(first_row[COL_PAYLOAD_RAW])!r}), takže nenese ani "
            f"předpočítaný čas zastávky, ani GPS.\n"
            f"        Ten už není podporovaný. Exportuj finální formát z ESO9."
        )


def load_riro_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=";")
        for line_no, row in enumerate(reader, 1):
            if not row or not "".join(row).strip():
                continue

            check_row_format(row, line_no)
            if not rows:                      # formát souboru — jen 1. datový řádek
                check_file_format(row)

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
                "city": str(row[COL_CITY]).strip(),
                "tw1_from_sec": str(row[COL_TW1_FROM_SEC]).strip(),
                "tw1_to_sec": str(row[COL_TW1_TO_SEC]).strip(),
                "lon": str(row[COL_LON]).strip(),
                "lat": str(row[COL_LAT]).strip(),
                "order_number": str(row[COL_ORDER_NUMBER]).strip(),
                "delivery_date": str(row[COL_DELIVERY_DATE]).strip(),
                "note": str(row[COL_NOTE]).strip(),
                "payload_raw": str(row[COL_PAYLOAD_RAW]).strip(),
                "code_a": str(row[COL_CODE_A]).strip(),
                "prev_kg": str(row[COL_PREV_KG]).strip(),
            })
    return rows


def parse_prev_kg(raw: str) -> float | None:
    """kg z minulého závozu, nebo None když minule nebyl (-1000) / nevalidní."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v == PREV_KG_NONE or v <= 0:
        return None
    return v


def find_suspect_prev_kg(raw_rows: list[dict]) -> list[dict]:
    """
    Řádky, kde sloupec AE není ani kg, ani smluvená značka `-1000`.

    Legitimní hodnoty jsou: kladné číslo (kg z minulého závozu), `-1000`
    (minule bez závozu) a prázdno (údaj nedodán). Cokoli jiného je vada —
    typicky stopa po Excelu, který číslo převedl na datum (`XII.00`).
    Řádek se pak jen nezapočítá do koeficientu, plán se nerozbije, ale
    když takových přibývá, koeficient stojí na míň datech, než si myslíme.
    """
    suspect = []
    for raw in raw_rows:
        value = str(raw.get("prev_kg", "")).strip()
        if not value:
            continue
        try:
            number = float(value)
        except ValueError:
            reason = "nečíselná hodnota (nejspíš datum z Excelu)"
        else:
            if number == PREV_KG_NONE or number > 0:
                continue
            reason = ("nula" if number == 0
                      else f"záporná hodnota jiná než {int(PREV_KG_NONE)}")
        suspect.append({
            "line": raw.get("_line"),
            "order_number": raw.get("order_number", ""),
            "location_code": raw.get("location_code", ""),
            "value": value,
            "reason": reason,
        })
    return suspect


def format_prev_kg_warning(suspect: list[dict], raw_rows: int) -> str:
    """Varování (ne chyba) — vadné AE plán nezastaví, jen zmenší vzorek."""
    lines = [f"\n{'─' * 64}",
             f"[!] VAROVÁNÍ: {len(suspect)} z {raw_rows} řádků má vadný sloupec AE",
             "    (kg z minulého závozu — nezapočítá se do koeficientu)"]
    for s in suspect[:10]:
        lines.append(f"      řádek {s['line']:>4} | obj {s['order_number']} "
                     f"| {s['location_code']}: {s['value']!r} — {s['reason']}")
    if len(suspect) > 10:
        lines.append(f"      … a dalších {len(suspect) - 10}")
    lines.append("    Typická příčina: export prošel Excelem a ten číslo "
                 "přeformátoval na datum.")
    return "\n".join(lines)


def check_delivery_dates(raw_rows: list[dict], date_str: str,
                         prediction: bool) -> list[dict]:
    """Ověří sloupec Y (datum ROZVOZU) proti datu z názvu souboru.

    Ostrý běh: jakékoli jiné datum je vada exportu → fatální chyba.
    Predikce:  dřívější datum = dopredikovaná objednávka (vrátí se jejich seznam),
               ale datum v budoucnu je vada i tady.

    Vrací řádky označené jako dopredikované (v ostrém režimu vždy prázdné).
    """
    expected = date_str.replace("-", "")          # YYYY-MM-DD → YYYYMMDD
    older, newer = [], []
    for raw in raw_rows:
        d = raw.get("delivery_date", "")
        if not d or d == expected:
            continue
        (older if d < expected else newer).append(raw)

    def _fail(rows: list[dict], popis: str) -> None:
        ukazky = "\n".join(
            f"          řádek {r['_line']:>4} | {r['order_number']} | "
            f"{r['delivery_date']} | {r['location_code']}"
            for r in rows[:5])
        raise ValueError(
            f"[CHYBA] Soubor obsahuje {len(rows)} objednávek {popis} "
            f"(očekáváno {expected}, sloupec Y):\n{ukazky}"
            + (f"\n          ... a dalších {len(rows) - 5}" if len(rows) > 5 else "")
            + "\n        Sloupec Y je datum ROZVOZU a musí odpovídat datu závozu."
              "\n        Oprav export z ESO9."
        )

    if newer:
        _fail(newer, "s datem rozvozu v BUDOUCNU")
    if older and not prediction:
        _fail(older, "s JINÝM datem rozvozu")
    return older if prediction else []


def compute_kg_coefficient(raw_rows: list[dict]) -> dict:
    """Koeficient nárůstu/poklesu kg = suma(dnes) / suma(minule).

    Predikce ho používá jako DRUHÝ krok po losu: objednávky, které losem
    prošly, mají váhu z minulého týdne a koeficient ji převede na dnešek.
    (Los podle šance z historie řeší order_history.py.)

    Počítá se ze SPÁROVANÝCH objednávek — těch, které mají ve sloupci AE
    kg z minulého závozu (tj. jely i minule). V praxi jsou to reálné
    objednávky na dnešek; dopredikované řádky v AE nic nemají. Ořezaný
    a s minimem párů, aby pár výjimek nerozhodilo celý den.
    """
    kg_now = kg_prev = 0.0
    pairs = 0
    for raw in raw_rows:
        prev = parse_prev_kg(raw.get("prev_kg", ""))
        if prev is None:
            continue
        now = parse_payload(raw.get("payload_raw", "")).get("KG")
        if now is None or now < 0:
            continue
        kg_now += now
        kg_prev += prev
        pairs += 1

    raw_coef = kg_now / kg_prev if kg_prev > 0 else 1.0
    applied = pairs >= KG_COEF_MIN_PAIRS and kg_prev > 0
    coef = min(KG_COEF_MAX, max(KG_COEF_MIN, raw_coef)) if applied else 1.0
    return {
        "pairs": pairs,
        "kg_now": round(kg_now, 1),
        "kg_prev": round(kg_prev, 1),
        "raw": round(raw_coef, 4),
        "coefficient": round(coef, 4),
        "applied": applied,
        "clamped": applied and abs(coef - raw_coef) > 1e-9,
    }

def parse_gps(raw: dict) -> tuple[float, float] | None:
    """(lat, lon) ze sloupců R/S, nebo None když chybí/jsou mimo ČR.
    Rozsahová kontrola chytí i prohozené pořadí lat↔lon."""
    try:
        lon = float(raw["lon"])
        lat = float(raw["lat"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (LON_RANGE[0] < lon < LON_RANGE[1] and LAT_RANGE[0] < lat < LAT_RANGE[1]):
        return None
    return lat, lon


def transform(raw_rows: list[dict], depot_code: str, *,
              predicted_lines: set | None = None,
              kg_coefficient: float = 1.0) -> tuple[list[dict], list[dict]]:
    """RiRo řádky → solver-ready objednávky.

    Vrací (orders, dropped). `dropped` nese pro každý vyřazený řádek důvod —
    volající rozhodne, jestli to je fatální (přísný režim) nebo jen varování.
    GPS i čas zastávky (SEC) jdou z riro; locations už se nepoužívají.

    Predikční režim: `predicted_lines` je množina čísel řádků dopredikovaných
    objednávek — jejich váha se přenásobí `kg_coefficient`. Čas zastávky (SEC)
    se ZÁMĚRNĚ nemění: neznáme vzorec, kterým ho ESO9 počítá, a lineární
    přepočet by byl nepřesný kvůli fixní složce (příjezd, papíry).
    """
    predicted_lines = predicted_lines or set()
    orders: list[dict] = []
    dropped: list[dict] = []

    def drop(raw: dict, reason: str, detail: str) -> None:
        dropped.append({
            "line": raw.get("_line"),
            "order_number": raw.get("order_number", ""),
            "location_code": raw.get("location_code", ""),
            "customer_name": raw.get("customer_name", ""),
            "reason": reason,
            "detail": detail,
        })

    for raw in raw_rows:
        gps = parse_gps(raw)
        if gps is None:
            drop(raw, "vadná GPS",
                 f"sloupec R (lon)={raw.get('lon')!r}, S (lat)={raw.get('lat')!r}")
            continue
        lat, lon = gps

        time_from = seconds_to_hhmm(raw["tw1_from_sec"])
        time_to   = seconds_to_hhmm(raw["tw1_to_sec"])
        if time_from is None or time_to is None:
            drop(raw, "vadné časové okno",
                 f"sloupce L/M: from={raw['tw1_from_sec']!r}, to={raw['tw1_to_sec']!r}")
            continue
        if time_from >= time_to:
            drop(raw, "vadné časové okno",
                 f"noční/obrácené okno: {time_from}–{time_to}")
            continue

        parsed_payload = parse_payload(raw["payload_raw"])
        service_sec = parsed_payload.get("SEC")
        if service_sec is None or service_sec <= 0:
            drop(raw, "vadný payload",
                 f"chybí/nevalidní SEC: sloupec AA={raw['payload_raw']!r}")
            continue
        if service_sec > SERVICE_SEC_MAX:
            drop(raw, "vadný payload",
                 f"SEC={int(service_sec)} s = {service_sec / 3600:.1f} h zastávky — "
                 f"nad limitem {SERVICE_SEC_MAX // 3600} h, vadný export z ESO9")
            continue
        weight_kg = parsed_payload.get("KG")
        if weight_kg is None or weight_kg < 0:
            drop(raw, "vadný payload",
                 f"chybí/nevalidní KG: sloupec AA={raw['payload_raw']!r}")
            continue

        # Dopredikovaná objednávka: přenásob váhu koeficientem (SEC beze změny).
        is_predicted = raw.get("_line") in predicted_lines
        if is_predicted and kg_coefficient != 1.0:
            weight_kg = float(weight_kg) * kg_coefficient

        orders.append({
            "order_number": raw["order_number"],
            "location_code": raw["location_code"],
            "customer_name": raw["customer_name"],
            "block_id": depot_code,
            "time_from": time_from,
            "time_to": time_to,
            "payload_raw": raw["payload_raw"],
            "weight_kg": round(float(weight_kg), 3),
            "lat": lat,
            "lon": lon,
            "city": raw.get("city", ""),
            "note": raw.get("note", ""),
            "service_sec": int(service_sec),
            "code_a": raw.get("code_a", ""),
            "riro_vehicle_type_code": "",
        })

    return orders, dropped

def save_orders(orders: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "order_number", "location_code", "customer_name", "block_id",
        "time_from", "time_to",
        "payload_raw", "weight_kg",
        "lat", "lon", "city", "note",
        "service_sec", "code_a", "riro_vehicle_type_code",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(orders)


def build_prepare_stats(depot: str, date_str: str, riro_name: str, *,
                        raw_rows: int, orders_count: int,
                        dropped: list[dict],
                        prediction: bool = False,
                        prediction_block: dict | None = None) -> dict:
    """
    Bilance zpracování: kolik řádků riro prošlo a proč které vypadly.

    `excluded_total` je počet řádků vyřazených VALIDACÍ (vadná data). Objednávky,
    které v predikci neprošly losem, se do něj ZÁMĚRNĚ nepočítají — nejsou vadné
    a compare_prediction.py by pak hlásil nesmysly. Ty jsou v bloku `prediction`.
    """
    def _count(reason: str) -> int:
        return sum(1 for d in dropped if d["reason"] == reason)

    stats = {
        "depot": depot,
        "date": date_str,
        "riro_file": riro_name,
        "raw_rows": raw_rows,
        "orders_count": orders_count,
        "excluded_total": len(dropped),
        "excluded_invalid_gps_rows": _count("vadná GPS"),
        "excluded_invalid_payload_rows": _count("vadný payload"),
        "excluded_invalid_time_window_rows": _count("vadné časové okno"),
        "excluded_rows": dropped,
    }
    if prediction:
        stats["prediction"] = prediction_block or {}
    return stats


def format_kg_coefficient_summary(coef: dict, rows: list[dict],
                                  predicted_lines: set[int]) -> str:
    """
    Souhrn za koeficient kg — jeden odstavec pod tabulku losu.

    Ukazuje, z čeho se koeficient spočítal a co udělal s vahami
    dopredikovaných objednávek, které prošly losem.
    """
    touched = [r for r in rows if r.get("_line") in predicted_lines]
    kg_before = 0.0
    for raw in touched:
        kg = parse_payload(raw.get("payload_raw", "")).get("KG")
        if kg is not None and kg >= 0:
            kg_before += float(kg)
    factor = coef["coefficient"]
    delta = kg_before * (factor - 1.0)

    lines = [f"\n{'─' * 64}", "KOEFICIENT KG"]
    if not coef.get("applied"):
        why = (f"jen {coef.get('pairs', 0)} spárovaných objednávek "
               f"(potřeba {KG_COEF_MIN_PAIRS})"
               if coef.get("pairs", 0) < KG_COEF_MIN_PAIRS
               else "chybí kg z minulého závozu")
        lines.append(f"  NEPOUŽIT ({why}) — váhy zůstávají beze změny")
        return "\n".join(lines)

    lines.append(f"  {coef['kg_now']:,.0f} kg dnes / {coef['kg_prev']:,.0f} kg minule "
                 f"= {coef['raw']:.4f}  ({coef['pairs']} spárovaných objednávek)")
    if coef.get("clamped"):
        lines.append(f"  OŘEZÁNO na {factor:.4f} "
                     f"(povolené rozmezí {KG_COEF_MIN}–{KG_COEF_MAX})")
    lines.append(f"  aplikováno ×{factor:.4f} na {len(touched)} dopredikovaných: "
                 f"{kg_before:,.0f} kg → {kg_before * factor:,.0f} kg "
                 f"({delta:+,.0f} kg)")
    lines.append("  čas zastávky (SEC) se nemění")
    return "\n".join(lines)


def format_dropped_report(dropped: list[dict], raw_rows: int) -> str:
    """Lidsky čitelný seznam vyřazených řádků — konkrétní řádek, důvod, hodnoty."""
    lines = [f"\n{'=' * 64}",
             f"VYŘAZENO {len(dropped)} z {raw_rows} řádků",
             "=" * 64]
    by_reason: dict[str, list[dict]] = {}
    for d in dropped:
        by_reason.setdefault(d["reason"], []).append(d)
    for reason, items in sorted(by_reason.items()):
        lines.append(f"\n{reason} ({len(items)}x):")
        for d in items:
            lines.append(f"  řádek {d['line']:>4} | obj {d['order_number']} "
                         f"| {d['location_code']} ({d['customer_name']})")
            lines.append(f"               {d['detail']}")
    return "\n".join(lines)


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
    parser.add_argument("--data-root", default=str(DATA_DIR),
                        help="Kořen složek input/ a prepared/ (default: data). "
                             "Predikční běhy: --data-root data/prediction.")
    parser.add_argument("--allow-drops", action="store_true",
                        help="Pokračuj i když nějaký řádek neprojde validací. "
                             "DEFAULT je hard-fail — správně je jen když projdou "
                             "všechny řádky z ESO9.")
    parser.add_argument("--prediction", action="store_true",
                        help="Predikční režim: objednávky s DŘÍVĚJŠÍM datem rozvozu "
                             "(sloupec Y) jsou dopredikované a projdou losem podle "
                             "šance z historie závozů — do plánu jdou celé, nebo "
                             "vůbec. Bez tohoto flagu je jiné datum rozvozu chyba "
                             "exportu.")
    parser.add_argument("--riro-file", default="",
                        help="Konkrétní RiRo soubor místo jediného z aktivni/. "
                             "Pro přepočet staršího dne, aniž bys přehazoval "
                             "soubory ve složkách. Datum se bere z názvu.")
    args = parser.parse_args()

    depot_code = args.depot_code.upper()
    data_root = Path(args.data_root)
    if args.riro_file:
        # Ruční volba souboru — datum pořád z názvu, ať je pojmenování
        # výstupů a párování v run logu stejné jako u běžného běhu.
        riro_path = Path(args.riro_file)
        if not riro_path.exists():
            raise SystemExit(f"[CHYBA] RiRo soubor neexistuje: {riro_path}")
        date_str = parse_date_from_filename(riro_path.name)
    else:
        riro_path, date_str = find_active_riro_file(depot_code, data_root / "input")
    raw_rows = load_riro_csv(riro_path)

    print("=" * 64)
    print("prepare_inputs_v6.py — RiRo -> orders per depot"
          + ("  [PREDIKCE]" if args.prediction else ""))
    print("=" * 64)
    print(f"Depo:       {depot_code}")
    print(f"Datum:      {date_str}")
    print(f"Vstup:      {riro_path}")
    print(f"Raw rows:   {len(raw_rows)}")

    # Sloupec Y = datum ROZVOZU. V ostrém běhu musí sedět na datum závozu,
    # v predikci označuje dřívější datum dopredikované objednávky.
    predicted_rows = check_delivery_dates(raw_rows, date_str, args.prediction)

    # Predikce běží ve dvou krocích:
    #   1) LOS — každá dopredikovaná objednávka projde losem podle šance, že
    #      adresa v daný den v týdnu opravdu objedná (z historie závozů).
    #      Objednávka jde do plánu CELÁ, nebo vůbec.
    #   2) KOEFICIENT kg — vahám těch, které prošly, se přenásobí poměrem
    #      suma(dnes)/suma(minule) ze spárovaných objednávek (sloupec AE).
    #      Váha dopredikované je z minulého týdne, koeficient ji převede na
    #      dnešek. Čas zastávky (SEC) se nemění — neznáme vzorec ESO9.
    # Los je první schválně: vyloučené řádky do transform vůbec nevstoupí.
    prediction_block = None
    rows_for_transform = raw_rows
    predicted_lines: set[int] = set()
    kg_coef = {"coefficient": 1.0, "applied": False}
    if args.prediction:
        import order_history            # jen pro predikci — ostrý běh historii nečte

        print(f"\nDopredikováno: {len(predicted_rows)} objednávek "
              f"(dřívější datum rozvozu)")
        history = order_history.load_history(order_history.find_history_files())
        print(f"Historie:      {', '.join(history.files)} "
              f"({len(history.by_location):,} adres, data do {history.max_date})")

        records = order_history.evaluate_predictions(predicted_rows, history, date_str)
        print(order_history.format_prediction_report(records, date_str))
        prediction_block = order_history.build_prediction_stats(records, history)

        skipped_lines = {r["line"] for r in records if not r["selected"]}
        rows_for_transform = [r for r in raw_rows if r["_line"] not in skipped_lines]
        predicted_lines = {r["line"] for r in records if r["selected"]}

        # Koeficient se počítá ze VŠECH řádků (páruje jen ty s hodnotou v AE,
        # což jsou v praxi reálné objednávky na dnešek — dopredikované mají -1000).
        suspect_prev_kg = find_suspect_prev_kg(raw_rows)
        if suspect_prev_kg:
            print(format_prev_kg_warning(suspect_prev_kg, len(raw_rows)))

        kg_coef = compute_kg_coefficient(raw_rows)
        kg_coef["suspect_rows"] = suspect_prev_kg
        prediction_block["kg_coefficient"] = kg_coef
        print(format_kg_coefficient_summary(kg_coef, rows_for_transform, predicted_lines))

    orders, dropped = transform(rows_for_transform, depot_code,
                                predicted_lines=predicted_lines,
                                kg_coefficient=kg_coef["coefficient"])

    # Přísný režim: ESO9 garantuje kompletní data, takže jakýkoliv vyřazený
    # řádek = problém ve zdroji, který má někdo opravit — ne tiše přejít.
    if dropped:
        print(format_dropped_report(dropped, len(raw_rows)))
        if not args.allow_drops:
            sys.exit(
                f"\n[ABORT] {len(dropped)} řádků neprošlo validací — nic se neuložilo.\n"
                f"        Oprav data v ESO9 a exportuj riro znovu.\n"
                f"        Vědomě pokračovat i tak: --allow-drops"
            )
        print("\n[!] --allow-drops: pokračuji bez vyřazených řádků.\n")

    if not orders:
        raise ValueError(f"[CHYBA] Pro depo {depot_code} nevznikly žádné objednávky.")

    output_dir = data_root / "prepared" / depot_code
    output_file = output_dir / f"orders_{depot_code}_{date_str}.csv"
    save_orders(orders, output_file)

    # Bilance zpracování — strojově čitelná (čte ji compare_prediction.py a UI)
    stats = build_prepare_stats(
        depot_code, date_str, riro_path.name,
        raw_rows=len(raw_rows), orders_count=len(orders), dropped=dropped,
        prediction=args.prediction,
        prediction_block=prediction_block,
    )
    stats_file = output_dir / f"prepare_stats_{depot_code}_{date_str}.json"
    with open(stats_file, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    total_kg = sum(o["weight_kg"] for o in orders)
    total_service_h = sum(o["service_sec"] for o in orders) / 3600
    print(f"Objednávky: {len(orders)}")
    print(f"Celkem kg:  {total_kg:,.1f}")
    print(f"Servis:     {total_service_h:,.1f} h celkem (předpočítáno v ESO9)")
    print(f"Výstup:     {output_file}")

if __name__ == "__main__":
    main()
