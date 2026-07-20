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
    staging.validate_riro_name("CB", "riro-20260717-CB.csv")   # nesmí vyhodit


@pytest.mark.parametrize("bad", [
    "neco.csv",
    "riro-2026-CB.csv",                  # špatné datum
    "riro-20260717-XX.csv",              # neznámé depo
    "riro-20260429-CB-POB.csv",          # starý název s -POB (token "CB-POB")
    "orders_CB_2026-04-29.csv",
])
def test_bad_name_rejected(bad):
    with pytest.raises(staging.StagingError) as ei:
        staging.validate_riro_name("CB", bad)
    assert ei.value.status_code == 400


def test_depot_token_mismatch():
    with pytest.raises(staging.StagingError):
        staging.validate_riro_name("CB", "riro-20260717-MO.csv")


@pytest.mark.parametrize("depot,fname", [
    ("CB", "riro-20260717-CB.csv"),
    ("MO", "riro-20260717-Morava.csv"),
    ("PR", "riro-20260717-Praha.csv"),
    ("HK", "riro-20260717-Hradec Králové.csv"),
    ("HK", "riro-20260717-hradec kralove.csv"),   # bez diakritiky + malá písmena
])
def test_full_depot_name_accepted(depot, fname):
    staging.validate_riro_name(depot, fname)           # nesmí vyhodit


@pytest.mark.parametrize("depot,fname", [
    ("MO", "riro-20260717-Praha.csv"),             # Praha do MO
    ("CB", "riro-20260717-Morava.csv"),            # Morava do CB
    ("PR", "riro-20260717-Brno.csv"),              # neznámé město
])
def test_full_depot_name_wrong_depot_rejected(depot, fname):
    with pytest.raises(staging.StagingError):
        staging.validate_riro_name(depot, fname)


# ── Upload flow ──────────────────────────────────────────────────────────────

def test_upload_into_empty(tmp_path):
    r = staging.stage_upload("CB", "riro-20260717-CB.csv", _stream(), base=tmp_path)
    assert r["saved"] == "riro-20260717-CB.csv"
    assert r["archived"] == []
    assert staging.list_active("CB", base=tmp_path) == ["riro-20260717-CB.csv"]


def test_conflict_without_force_409(tmp_path):
    staging.stage_upload("CB", "riro-20260717-CB.csv", _stream(), base=tmp_path)
    with pytest.raises(staging.StagingError) as ei:
        staging.stage_upload("CB", "riro-20260718-CB.csv", _stream(), base=tmp_path)
    assert ei.value.status_code == 409
    assert "riro-20260717-CB.csv" in ei.value.detail["existing"]


def test_force_archives_old(tmp_path):
    staging.stage_upload("CB", "riro-20260717-CB.csv", _stream(), base=tmp_path)
    r = staging.stage_upload("CB", "riro-20260718-CB.csv", _stream(),
                             force=True, base=tmp_path)
    assert r["active"] == ["riro-20260718-CB.csv"]
    assert len(r["archived"]) == 1
    arch = tmp_path / "CB" / "archiv_webui"
    archived = list(arch.iterdir())
    assert len(archived) == 1
    assert archived[0].name.endswith("_riro-20260717-CB.csv")   # nesmazáno, odsunuto


def test_atomic_no_part_leftover(tmp_path):
    staging.stage_upload("CB", "riro-20260717-CB.csv", _stream(), base=tmp_path)
    ak = tmp_path / "CB" / "aktivni"
    assert [p.name for p in ak.iterdir()] == ["riro-20260717-CB.csv"]
    assert not any(p.suffix == ".part" for p in ak.iterdir())


def test_upload_content_preserved(tmp_path):
    payload = b"header;x\n1;2\n\xc5\x99\n"      # vč. UTF-8 bajtu
    staging.stage_upload("MO", "riro-20260717-MO.csv", _stream(payload), base=tmp_path)
    saved = tmp_path / "MO" / "aktivni" / "riro-20260717-MO.csv"
    assert saved.read_bytes() == payload
