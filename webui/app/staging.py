"""
Staging RiRo uploadů do data/input/{depot}/aktivni/.

Pravidla:
- Název musí sedět na riro-YYYYMMDD-{DEPOT}-POB.csv A depot token = cílové depo.
- Neprázdná aktivni/ + force=False → 409 (seznam existujících).
- force=True → existující se PŘESUNE do data/input/{depot}/archiv_webui/
  ({stamp}_{název}) — NIKDY se nemaže.
- Zápis atomicky: stream do {název}.part, pak os.replace.

`base` je injektovatelný (testy přes tmp_path). data/input/ je gitignored.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from . import config


class StagingError(Exception):
    """Nese HTTP status a detail (str nebo dict) pro API vrstvu."""

    def __init__(self, status_code: int, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


def _base(base: Path | None) -> Path:
    return base if base is not None else config.INPUT_ROOT


def aktivni_dir(depot: str, base: Path | None = None) -> Path:
    return _base(base) / depot / "aktivni"


def archiv_dir(depot: str, base: Path | None = None) -> Path:
    return _base(base) / depot / "archiv_webui"


def list_active(depot: str, base: Path | None = None) -> list[str]:
    d = aktivni_dir(depot, base)
    if not d.exists():
        return []
    return sorted(e.name for e in d.iterdir() if e.is_file())


def validate_riro_name(depot: str, filename: str) -> None:
    m = config.RIRO_GENERIC_RE.match(filename or "")
    if not m:
        raise StagingError(
            400,
            f"Název '{filename}' neodpovídá formátu riro-YYYYMMDD-DEPO-POB.csv "
            f"(např. riro-20260710-{depot}-POB.csv).",
        )
    token = m.group(2)
    # Token depa může být kód (CB) i plný název (Morava, Hradec Králové, Praha).
    if not config.riro_token_matches_depot(depot, token):
        raise StagingError(
            400,
            f"Depo v názvu souboru ('{token}') neodpovídá cílovému depu {depot}. "
            f"Očekávám: {config.depot_tokens_hint(depot)}.",
        )


def stage_upload(depot: str, filename: str, source_stream, *,
                 force: bool = False, base: Path | None = None) -> dict:
    """
    Ulož nahraný RiRo soubor do aktivni/. Existující při force přesuň do archivu.
    source_stream: file-like s .read(). Vrací {saved, archived, active}.
    """
    validate_riro_name(depot, filename)
    ak = aktivni_dir(depot, base)
    ak.mkdir(parents=True, exist_ok=True)

    existing = [e for e in ak.iterdir() if e.is_file()]
    if existing and not force:
        raise StagingError(409, {
            "message": f"V data/input/{depot}/aktivni/ už je soubor. "
                       f"Přesunout do archivu a nahradit?",
            "existing": sorted(e.name for e in existing),
        })

    archived: list[str] = []
    if existing:
        arch = archiv_dir(depot, base)
        arch.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for e in existing:
            target = arch / f"{stamp}_{e.name}"
            shutil.move(str(e), str(target))       # NIKDY nemazat — jen odsun
            archived.append(target.name)

    # Atomický zápis přes .part → os.replace
    part = ak / (filename + ".part")
    with open(part, "wb") as f:
        shutil.copyfileobj(source_stream, f)
    os.replace(part, ak / filename)

    return {
        "saved":    filename,
        "archived": archived,
        "active":   list_active(depot, base),
    }
