"""
/api/fleet/* — READ-ONLY pohled na flotilu.

Přesně ten soubor, se kterým solver počítá náklady: nejnovější
vozovy_park/aktivni/vehicle_types-YYYYMMDD.csv (středníky). UI ho jen zobrazuje —
editace se dělá mimo web (validace by chtěla zvláštní pozornost). Archiv
předchozích verzí je vedle.
"""

from __future__ import annotations

import csv

from fastapi import APIRouter

from . import config

router = APIRouter(prefix="/api/fleet")


def _read_fleet(path) -> dict:
    """Řádky vozového parku + souhrn malá/velká. Tolerantní k absenci."""
    if path is None:
        return {"rows": [], "summary": {},
                "error": "žádný vehicle_types-YYYYMMDD.csv ve vozovy_park/aktivni"}
    if not path.exists():
        return {"rows": [], "summary": {}, "error": f"{path.name} neexistuje"}
    try:
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f, delimiter=";"))
    except OSError as e:
        return {"rows": [], "summary": {}, "error": str(e)}

    def _int(v, d=0):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return d

    def _num(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    small = large = small_cnt = large_cnt = 0
    for r in rows:
        cnt = _int(r.get("available_count"))
        if "mal" in (r.get("profiles", "") or "").lower():
            small += 1
            small_cnt += cnt
        else:
            large += 1
            large_cnt += cnt

    total_cnt = small_cnt + large_cnt
    return {
        "rows": rows,
        "fieldnames": list(rows[0].keys()) if rows else [],
        "source_file": path.name,
        "summary": {
            "types": len(rows),
            "vehicles_total": total_cnt,
            "small_types": small, "small_count": small_cnt,
            "large_types": large, "large_count": large_cnt,
            # zdroj cen/počtů = provenance sloupce (pokud jsou vyplněné)
            "cost_source": rows[0].get("cost_per_km_source", "") if rows else "",
            "count_source": rows[0].get("available_count_source", "") if rows else "",
        },
    }


@router.get("")
def fleet() -> dict:
    files = config.vehicle_types_files()
    if len(files) > 1:
        # UI nesmí ukazovat jiný soubor, než se kterým počítá solver —
        # ten při víc souborech odmítne běžet, tak to tu jen zobrazíme.
        return {"rows": [], "summary": {},
                "error": f"ve vozovy_park/aktivni je {len(files)} souborů vozového parku "
                         f"({', '.join(f.name for f in files)}) — nech tam právě jeden"}
    return _read_fleet(files[0] if files else None)


@router.get("/archive")
def archive() -> list[dict]:
    """Seznam archivovaných verzí (jen názvy + mtime, ne obsah)."""
    d = config.VEHICLE_TYPES_ARCHIV
    if not d.is_dir():
        return []
    out = []
    for e in sorted(d.iterdir(), reverse=True):
        if e.is_file() and e.suffix == ".csv":
            try:
                out.append({"name": e.name, "mtime": e.stat().st_mtime})
            except OSError:
                out.append({"name": e.name, "mtime": None})
    return out
