"""
Read-only skenery: vstupní soubory (aktivni/) a strom výsledků (data/results/**).

Nikdy nepadá na chybějících/nedostupných složkách — vrací prázdné seznamy.
Cesty v JSON jsou vždy s dopřednými lomítky (as_posix), relativní k data/results.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

from . import config

# Povolené přípony pro download přes /api/results/file.
DOWNLOAD_MEDIA_TYPES = {
    ".csv":  "text/csv; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".txt":  "text/plain; charset=utf-8",
}


class UnsafePath(ValueError):
    """Cesta se pokouší uniknout z data/results nebo je absolutní/diskovo-relativní."""


def resolve_results_path(rel: str) -> Path:
    """
    Bezpečně přeloží klientem zadanou relativní cestu na absolutní uvnitř
    data/results. Odmítne absolutní cesty, písmena disků a traversal (..).

    Přijímá dopředná i zpětná lomítka. Nekontroluje existenci — jen bezpečnost.
    Vyhodí UnsafePath při porušení.
    """
    raw = (rel or "").strip()
    if not raw:
        raise UnsafePath("prázdná cesta")
    norm = raw.replace("\\", "/")
    # absolutní ('/...'), disk ('C:...') nebo UNC
    if norm.startswith("/") or ":" in norm or PurePosixPath(norm).is_absolute():
        raise UnsafePath(f"absolutní cesta / disk není povolen: {rel!r}")
    root      = config.RESULTS_ROOT.resolve()
    candidate = (root / norm).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        raise UnsafePath(f"cesta míří mimo data/results: {rel!r}")
    return candidate


def run_detail(run_dir: Path) -> dict:
    """zone_summary (tolerantní k absenci) + seznam souborů s velikostmi."""
    files = []
    for e in _safe_scandir(run_dir):
        if not e.is_file():
            continue
        try:
            size = e.stat().st_size
        except OSError:
            size = None
        files.append({"name": e.name, "size": size})
    return {
        "path":         run_dir.relative_to(config.RESULTS_ROOT).as_posix(),
        "zone_summary": _read_zone_summary(run_dir),
        "files":        files,
    }


def _safe_scandir(path: Path) -> list[os.DirEntry]:
    """os.scandir tolerantní k chybějící/nedostupné složce."""
    try:
        with os.scandir(path) as it:
            return sorted(it, key=lambda e: e.name)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return []


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ── Vstupy ───────────────────────────────────────────────────────────────────

def list_input(depot: str) -> list[dict]:
    """Soubory v data/input/{depot}/aktivni/. Tolerantní k absenci složky."""
    aktivni = config.INPUT_ROOT / depot / "aktivni"
    out = []
    for e in _safe_scandir(aktivni):
        if not e.is_file():
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        out.append({
            "name":  e.name,
            "size":  st.st_size,
            "mtime": st.st_mtime,
            "date":  config.date_from_riro_name(e.name),
        })
    return out


# ── Výsledky ─────────────────────────────────────────────────────────────────

def _is_run_dir(path: Path) -> bool:
    return (path / "zone_summary.json").exists() or (path / "lines_summary.csv").exists()


def _read_zone_summary(path: Path) -> dict | None:
    zs = path / "zone_summary.json"
    if not zs.exists():
        return None
    try:
        return json.loads(zs.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _run_record(run_dir: Path, depot: str) -> dict:
    return {
        "path":         run_dir.relative_to(config.RESULTS_ROOT).as_posix(),
        "depot":        depot,
        "label":        run_dir.name,
        "mtime":        _mtime(run_dir),
        "has_map":      (run_dir / "routes_map.html").exists(),
        "has_xlsx":     (run_dir / "lines_plan.xlsx").exists(),
        "zone_summary": _read_zone_summary(run_dir),
    }


def _depot_runs(depot: str) -> list[dict]:
    runs = []
    for e in _safe_scandir(config.RESULTS_ROOT / depot):
        if e.is_dir() and _is_run_dir(Path(e.path)):
            runs.append(_run_record(Path(e.path), depot))
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return runs


def _all_runs() -> list[dict]:
    """ALL běhy — přímé podsložky i o 1 úroveň hlouběji (plán Krok 2)."""
    out = []
    for e in _safe_scandir(config.RESULTS_ROOT / "ALL"):
        if not e.is_dir():
            continue
        p = Path(e.path)
        if _is_run_dir(p):
            out.append(_run_record(p, "ALL"))
        else:
            for e2 in _safe_scandir(p):
                if e2.is_dir() and _is_run_dir(Path(e2.path)):
                    out.append(_run_record(Path(e2.path), "ALL"))
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def _benchmark_sessions() -> list[dict]:
    sessions = []
    for e in _safe_scandir(config.RESULTS_ROOT / "ALL_BENCHMARK"):
        p = Path(e.path)
        if not (e.is_dir() and (p / "benchmark_plan.json").exists()):
            continue
        variants = sorted(v.name for v in _safe_scandir(p) if v.is_dir())
        sessions.append({
            "path":     p.relative_to(config.RESULTS_ROOT).as_posix(),
            "name":     p.name,
            "mtime":    _mtime(p),
            "variants": variants,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def scan_results() -> dict:
    """Kompletní strom výsledků: per-depo běhy, ALL běhy, benchmark sessiony."""
    return {
        "depots":             {d["code"]: _depot_runs(d["code"]) for d in config.DEPOTS},
        "all":                _all_runs(),
        "benchmark_sessions": _benchmark_sessions(),
    }
