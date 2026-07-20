"""
webui config — detekce kořene repa, cesty, konstanty.

Nikdy nespoléhá na cwd. REPO_ROOT se odvozuje z umístění tohoto souboru
(webui/app/config.py → o dvě úrovně výš = kořen repa) a při importu se
sanity-checkuje přítomností solveru.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

# webui/app/config.py  →  parents[0]=app, parents[1]=webui, parents[2]=REPO ROOT
REPO_ROOT = Path(__file__).resolve().parents[2]

# Sanity-check při importu — musíme sedět vedle solveru.
_SOLVER_MARKER = REPO_ROOT / "vrp_solver_lines_v6.py"
if not _SOLVER_MARKER.exists():
    raise RuntimeError(
        f"webui config: REPO_ROOT={REPO_ROOT} nevypadá jako kořen VRP repa "
        f"(chybí vrp_solver_lines_v6.py). Zkontroluj umístění složky webui/."
    )

# ── Cesty ────────────────────────────────────────────────────────────────────
DATA_ROOT     = REPO_ROOT / "data"
INPUT_ROOT    = DATA_ROOT / "input"
PREPARED_ROOT = DATA_ROOT / "prepared"
RESULTS_ROOT  = DATA_ROOT / "results"
RUN_LOG_PATH  = RESULTS_ROOT / "run_log.jsonl"

WEBUI_DIR  = Path(__file__).resolve().parents[1]   # .../webui
STATIC_DIR = WEBUI_DIR / "static"
JOBS_DIR   = WEBUI_DIR / "jobs"                     # runtime, gitignored

# ── Depa ─────────────────────────────────────────────────────────────────────
DEPOTS = [
    {"code": "CB", "name": "České Budějovice"},
    {"code": "HK", "name": "Hradec Králové"},
    {"code": "MO", "name": "Morava"},
    {"code": "PR", "name": "Praha"},
]
DEPOT_CODES = frozenset(d["code"] for d in DEPOTS)

# ── Síť ──────────────────────────────────────────────────────────────────────
# 8777 = volný port (8765 má closure_map_editor, 5000/5001 OSRM, 8080/8081 ORS).
HOST = "127.0.0.1"
PORT = int(os.environ.get("VRP_WEBUI_PORT", "8777"))

# ── RiRo názvy ───────────────────────────────────────────────────────────────
# Finální export z ESO9 (od 17.7.2026) je BEZ přípony '-POB', pod kódem
# i plným názvem depa, např.:
#   riro-20260717-CB.csv, riro-20260717-Morava.csv,
#   riro-20260717-Hradec Králové.csv, riro-20260717-Praha.csv
# prepare_inputs_v6.py stejně token depa v názvu ignoruje (depo bere z CLI arg,
# datum z 'riro-YYYYMMDD-'), takže upload jen ověřuje datum + shodu tokenu s depem.
# Starý název ('...-CB-POB.csv') vyrobí token 'CB-POB', který neprojde kontrolou
# depa → odmítnut. Formát OBSAHU stejně validuje až prepare.

# Přijímané tokeny depa v názvu: kód i plný název.
DEPOT_FILE_TOKENS = {
    "CB": ("CB", "České Budějovice"),
    "HK": ("HK", "Hradec Králové"),
    "MO": ("MO", "Morava"),
    "PR": ("PR", "Praha"),
}

# Obecný RiRo pattern: datum + depot token (kód/plný název).
RIRO_GENERIC_RE = re.compile(r"^riro-(\d{8})-(.+)\.csv$", re.IGNORECASE)
# Tolerantní pattern jen pro extrakci data (Krok 2). Sedí na 'riro-YYYYMMDD-...'.
RIRO_DATE_RE = re.compile(r"^riro-(\d{8})-", re.IGNORECASE)


def _fold(s: str) -> str:
    """Case + diakritika insensitive normalizace pro porovnání tokenů."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


def riro_token_matches_depot(depot: str, token: str) -> bool:
    """True pokud token z názvu souboru odpovídá depu (kód nebo plný název)."""
    accepted = {_fold(t) for t in DEPOT_FILE_TOKENS.get(depot, (depot,))}
    return _fold(token) in accepted


def depot_tokens_hint(depot: str) -> str:
    return " nebo ".join(DEPOT_FILE_TOKENS.get(depot, (depot,)))


def date_from_riro_name(name: str) -> str | None:
    """Vrátí 'YYYY-MM-DD' z názvu RiRo souboru, nebo None."""
    m = RIRO_DATE_RE.match(name)
    if not m:
        return None
    ymd = m.group(1)
    return f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"
