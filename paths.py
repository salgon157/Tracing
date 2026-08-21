"""
paths.py — jediné místo pravdy pro umístění datové složky.

Struktura nasazení (oddělení kódu od dat, 8/2026):

    Tracing_Main/
    ├── vrp_benchmark/   ← kód, verzovaný gitem (tento soubor = jeho kořen)
    ├── data/            ← všechna data (vstupy, výstupy, PII) — NIKDY v gitu
    └── UI/              ← serverové UI (mimo tento projekt)

DATA_ROOT je defaultně ../data vedle repa; jiné umístění nastaví env
proměnná VRP_DATA_ROOT (absolutní cesta). Kořen se odvozuje z umístění
TOHOTO souboru, nikdy z cwd — příkazy se dál spouští z kořene repa, ale
cesty fungují odkudkoli.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("VRP_DATA_ROOT") or REPO_ROOT.parent / "data")

# ── Odvozené kořeny (ostrý strom) ────────────────────────────────────────────
STATIC_DIR      = DATA_ROOT / "static"        # vehicle_types-*.csv, closures.json
INPUT_ROOT      = DATA_ROOT / "input"         # RiRo exporty per depo
PREPARED_ROOT   = DATA_ROOT / "prepared"      # jediný autor: prepare_inputs_v6
RESULTS_ROOT    = DATA_ROOT / "results"       # výstupy běhů (čte serverové UI)
RUN_LOG_PATH    = RESULTS_ROOT / "run_log.jsonl"

# ── Predikční strom (paralelní k ostrému) ────────────────────────────────────
PREDICTION_ROOT = DATA_ROOT / "prediction"

# ── Historie a registr řidičů (PII) ──────────────────────────────────────────
HISTORIE_OBJEDNAVKY_DIR = DATA_ROOT / "historie_objednavky"
HISTORIE_RIDICI_DIR     = DATA_ROOT / "historie_ridici"
RIDICI_DIR              = DATA_ROOT / "ridici"


def ensure_data_root() -> Path:
    """Tvrdý stop se srozumitelnou hláškou, když datová složka neexistuje.

    Volají CLI vstupní body — ať běh nespadne až uprostřed na FileNotFound.
    """
    if not DATA_ROOT.is_dir():
        raise SystemExit(
            f"[CHYBA] Datová složka neexistuje: {DATA_ROOT}\n"
            f"        Očekávaná struktura: Tracing_Main/{{vrp_benchmark, data}}"
            f" — data leží VEDLE repa, ne uvnitř.\n"
            f"        Jiné umístění nastav env proměnnou VRP_DATA_ROOT.")
    return DATA_ROOT


def resolve_legacy(p: str | Path) -> Path:
    """Přechodová pojistka pro cesty psané ještě podle staré struktury.

    Do 21. 8. 2026 data ležela uvnitř repa, takže se všude psalo
    'data/prepared/CB/orders_CB_….csv' relativně ke cwd. Kdo (nebo co —
    třeba serverové UI) takovou cestu předá dnes, dostane FileNotFound.
    Když soubor na zadané cestě NEEXISTUJE a cesta začíná 'data/', zkusíme
    ji pod novým datovým kořenem; volající na to upozorní hláškou.

    Vrací původní cestu, když se nic lepšího nenašlo — chyba pak vznikne
    normálně na zadané cestě.
    """
    orig = Path(p)
    if orig.exists() or orig.is_absolute():
        return orig
    parts = orig.parts
    if parts and parts[0] == "data":
        alt = DATA_ROOT.joinpath(*parts[1:])
        if alt.exists():
            return alt
    return orig
