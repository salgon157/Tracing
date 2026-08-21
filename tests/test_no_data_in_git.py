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

import paths

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


# ═════════════════════════════════════════════════════════════════════════════
#  Datový kořen a přechodová pojistka pro staré cesty
# ═════════════════════════════════════════════════════════════════════════════

class TestDataRoot:
    def test_data_root_is_outside_repo(self):
        assert not paths.DATA_ROOT.is_relative_to(REPO_ROOT), \
            f"data ({paths.DATA_ROOT}) nesmí ležet uvnitř repa ({REPO_ROOT})"

    def test_env_override(self, monkeypatch, tmp_path):
        # VRP_DATA_ROOT se čte při importu — ověřujeme samotné pravidlo
        monkeypatch.setenv("VRP_DATA_ROOT", str(tmp_path))
        import importlib
        reloaded = importlib.reload(paths)
        try:
            assert reloaded.DATA_ROOT == tmp_path
            assert reloaded.PREPARED_ROOT == tmp_path / "prepared"
        finally:
            monkeypatch.delenv("VRP_DATA_ROOT")
            importlib.reload(paths)

    def test_ensure_data_root_explains_structure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "DATA_ROOT", tmp_path / "neni")
        with pytest.raises(SystemExit) as e:
            paths.ensure_data_root()
        assert "Tracing_Main" in str(e.value)


class TestResolveLegacy:
    def test_old_style_path_redirected_when_file_exists(self, monkeypatch, tmp_path):
        (tmp_path / "prepared" / "CB").mkdir(parents=True)
        target = tmp_path / "prepared" / "CB" / "orders_CB_2026-08-21.csv"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
        assert paths.resolve_legacy(
            "data/prepared/CB/orders_CB_2026-08-21.csv") == target

    def test_missing_file_kept_as_is(self, monkeypatch, tmp_path):
        # nic nenajde → vrátí zadanou cestu, chyba vznikne normálně tam
        monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
        p = paths.resolve_legacy("data/prepared/CB/neni.csv")
        assert p == Path("data/prepared/CB/neni.csv")

    def test_absolute_path_untouched(self, tmp_path):
        assert paths.resolve_legacy(tmp_path) == tmp_path

    def test_non_data_relative_path_untouched(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
        assert paths.resolve_legacy("neco/jineho.csv") == Path("neco/jineho.csv")
