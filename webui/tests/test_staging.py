"""
Staging RiRo uploadů — injektovaný base dir (tmp_path), žádný dopad na reálná data.

Spouštět:  python -m pytest webui/tests -q
"""

import io

import pytest

from webui.app import staging


def _stream(data: bytes = b"col1;col2\na;b\n") -> io.BytesIO:
    return io.BytesIO(data)


# ── Validace názvu ───────────────────────────────────────────────────────────

def test_valid_name_ok():
    staging.validate_riro_name("CB", "riro-20260429-CB-POB.csv")   # nesmí vyhodit


@pytest.mark.parametrize("bad", [
    "neco.csv",
    "riro-2026-CB-POB.csv",              # špatné datum
    "riro-20260429-CB.csv",              # chybí -POB
    "riro-20260429-XX-POB.csv",          # neznámé depo
    "orders_CB_2026-04-29.csv",
])
def test_bad_name_rejected(bad):
    with pytest.raises(staging.StagingError) as ei:
        staging.validate_riro_name("CB", bad)
    assert ei.value.status_code == 400


def test_depot_token_mismatch():
    with pytest.raises(staging.StagingError):
        staging.validate_riro_name("CB", "riro-20260429-MO-POB.csv")


@pytest.mark.parametrize("depot,fname", [
    ("CB", "riro-20260710-CB-POB.csv"),
    ("MO", "riro-20260710-Morava-POB.csv"),
    ("PR", "riro-20260710-Praha-POB.csv"),
    ("HK", "riro-20260710-Hradec Králové-POB.csv"),
    ("HK", "riro-20260710-hradec kralove-POB.csv"),   # bez diakritiky + malá písmena
])
def test_full_depot_name_accepted(depot, fname):
    staging.validate_riro_name(depot, fname)           # nesmí vyhodit


@pytest.mark.parametrize("depot,fname", [
    ("MO", "riro-20260710-Praha-POB.csv"),             # Praha do MO
    ("CB", "riro-20260710-Morava-POB.csv"),            # Morava do CB
    ("PR", "riro-20260710-Brno-POB.csv"),              # neznámé město
])
def test_full_depot_name_wrong_depot_rejected(depot, fname):
    with pytest.raises(staging.StagingError):
        staging.validate_riro_name(depot, fname)


# ── Upload flow ──────────────────────────────────────────────────────────────

def test_upload_into_empty(tmp_path):
    r = staging.stage_upload("CB", "riro-20260429-CB-POB.csv", _stream(), base=tmp_path)
    assert r["saved"] == "riro-20260429-CB-POB.csv"
    assert r["archived"] == []
    assert staging.list_active("CB", base=tmp_path) == ["riro-20260429-CB-POB.csv"]


def test_conflict_without_force_409(tmp_path):
    staging.stage_upload("CB", "riro-20260429-CB-POB.csv", _stream(), base=tmp_path)
    with pytest.raises(staging.StagingError) as ei:
        staging.stage_upload("CB", "riro-20260430-CB-POB.csv", _stream(), base=tmp_path)
    assert ei.value.status_code == 409
    assert "riro-20260429-CB-POB.csv" in ei.value.detail["existing"]


def test_force_archives_old(tmp_path):
    staging.stage_upload("CB", "riro-20260429-CB-POB.csv", _stream(), base=tmp_path)
    r = staging.stage_upload("CB", "riro-20260430-CB-POB.csv", _stream(),
                             force=True, base=tmp_path)
    assert r["active"] == ["riro-20260430-CB-POB.csv"]
    assert len(r["archived"]) == 1
    arch = tmp_path / "CB" / "archiv_webui"
    archived = list(arch.iterdir())
    assert len(archived) == 1
    assert archived[0].name.endswith("_riro-20260429-CB-POB.csv")   # nesmazáno, odsunuto


def test_atomic_no_part_leftover(tmp_path):
    staging.stage_upload("CB", "riro-20260429-CB-POB.csv", _stream(), base=tmp_path)
    ak = tmp_path / "CB" / "aktivni"
    assert [p.name for p in ak.iterdir()] == ["riro-20260429-CB-POB.csv"]
    assert not any(p.suffix == ".part" for p in ak.iterdir())


def test_upload_content_preserved(tmp_path):
    payload = b"header;x\n1;2\n\xc5\x99\n"      # vč. UTF-8 bajtu
    staging.stage_upload("MO", "riro-20260429-MO-POB.csv", _stream(payload), base=tmp_path)
    saved = tmp_path / "MO" / "aktivni" / "riro-20260429-MO-POB.csv"
    assert saved.read_bytes() == payload
