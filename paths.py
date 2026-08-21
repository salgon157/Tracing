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


def resolve_data_path(p: str | Path) -> Path:
    """Cesta ze staršího záznamu (run log, state) → dnešní umístění.

    Záznamy z doby před restrukturalizací nesou relativní cesty typu
    'data/results/…' (tehdy vůči kořeni repa). Data se přestěhovala o úroveň
    výš, takže relativní 'data/…' dnes znamená DATA_ROOT/…; absolutní cesty
    a nové záznamy projdou beze změny.
    """
    p = Path(p)
    if p.is_absolute():
        return p
    parts = p.parts
    if parts and parts[0] == "data":
        return DATA_ROOT.joinpath(*parts[1:])
    return REPO_ROOT / p
