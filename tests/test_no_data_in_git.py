"""
Pojistka: v repu nesmí být nic z datové složky.

Data (objednávky, adresy, GPS, jména řidičů, SPZ) leží mimo repo —
Tracing_Main/{vrp_benchmark, data, UI}. Repo se klonuje na server, takže
jediný omylem přidaný soubor by osobní údaje rozeslal dál a z historie
gitu se špatně maže.

Test je součástí startup brány (běží pod sekundu) — chytí to v okamžiku,
kdy to vznikne, ne až při auditu.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Názvy, které nesmí být v repu ani mimo data/ (kdyby je někdo „dočasně"
# odložil vedle skriptů). Vzory jsou na jméno souboru, ne na cestu.
PII_PATTERNS = [
    re.compile(r"^riro-.*\.csv$", re.I),            # syrové exporty z ESO9
    re.compile(r"^orders_.*\.csv$", re.I),          # prepared objednávky
    re.compile(r"^locations_.*\.csv$", re.I),       # adresy + GPS zákazníků
    re.compile(r"^vehicle_registry.*", re.I),       # jména řidičů + SPZ
    re.compile(r"^vehicles-active.*", re.I),        # auto + řidič z ESO9
    re.compile(r"^historie_.*", re.I),              # historie závozů
    re.compile(r"^address-delivery-days.*", re.I),  # historie adresa × den
]


def git_files() -> list[str]:
    if not (REPO_ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("není git repo (nebo chybí git) — kontrolovat není co")
    r = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        pytest.skip(f"git ls-files selhalo: {r.stderr.strip()}")
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def test_no_file_from_data_tree_is_tracked():
    tracked = git_files()
    offenders = [f for f in tracked
                 if f == "data" or f.startswith("data/") or "/data/" in f]
    assert not offenders, (
        "V gitu jsou soubory z datové složky — data patří VEDLE repa "
        f"(Tracing_Main/data), ne do něj:\n  " + "\n  ".join(offenders[:20]))


def test_no_tracked_file_looks_like_pii():
    tracked = git_files()
    offenders = [f for f in tracked
                 if any(p.match(Path(f).name) for p in PII_PATTERNS)]
    assert not offenders, (
        "V gitu jsou soubory, které podle názvu nesou osobní údaje "
        "(GDPR) — patří do datové složky mimo repo:\n  "
        + "\n  ".join(offenders[:20]))


def test_gitignore_blocks_data_dir():
    """I kdyby si někdo složku 'data' uvnitř repa lokálně vyrobil."""
    gi = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8", errors="replace")
    lines = {line.strip() for line in gi.splitlines()}
    assert "/data/" in lines or "data/" in lines, \
        ".gitignore neblokuje složku data/ — chybí pojistka proti návratu dat do repa"
