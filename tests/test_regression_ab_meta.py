"""regression_ab.py — meta.json: výsledky A/B musí být samopopisné (commit, args,
nastavení), aby přežily smazání dočasného baseline worktree."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import regression_ab as R  # noqa: E402


def test_git_describe_working_copy_has_commit():
    g = R.git_describe(ROOT)
    assert set(g) == {"commit", "commit_full", "branch", "dirty", "subject"}
    # v repu projektu musí git fungovat; commit je krátký hash
    assert g["commit"] and len(g["commit"]) >= 7
    assert g["commit_full"].startswith(g["commit"])
    assert g["dirty"] in (True, False)


def test_git_describe_outside_repo_is_none(tmp_path):
    g = R.git_describe(tmp_path)
    assert g["commit"] is None and g["dirty"] is None


def test_write_meta_roundtrip_utf8(tmp_path):
    meta = {"kind": "regression_ab", "sides": {"A": {"label": "baseline", "extra_args": []},
                                                "B": {"label": "kandidát", "extra_args": ["--cost-matrix-mode", "exact"]}},
            "verdict": None, "poznámka": "české znaky — ě š č"}
    p = R.write_meta(tmp_path, meta)
    assert p.name == "meta.json"
    back = json.loads(p.read_text(encoding="utf-8"))
    assert back == meta
    assert "kandidát" in p.read_text(encoding="utf-8")   # ne \u escapes


def test_main_writes_meta_before_and_after_run(tmp_path, monkeypatch):
    """main() zapíše meta.json hned na začátku (přežije přerušení) a na konci doplní verdikt."""
    fake_base = tmp_path / "base"; fake_base.mkdir()
    (fake_base / "vrp_solver_lines_v6.py").write_text("# fake", encoding="utf-8")
    out = tmp_path / "out"
    # žádné případy (neexistující prepared) → main proběhne bez solveru
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, "argv", ["regression_ab.py", "--baseline-dir", str(fake_base),
                                      "--dates", "1999-01-01", "--depots", "CB", "--reps", "1",
                                      "--budget", "0.1", "--out", str(out), "--no-reprice-hgv",
                                      "--fleet-file", "x.csv", "--label-b", "kandidát"])
    try:
        R.main()
    except SystemExit:
        pass
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "regression_ab"
    assert meta["sides"]["A"]["dir"] == str(fake_base.resolve())
    assert meta["sides"]["B"]["git"]["commit"]            # kandidát = pracovní kopie → má commit
    assert meta["sides"]["A"]["git"]["commit"] is None    # fake baseline mimo git
    assert meta["budget_min"] == 0.1 and meta["reps"] == 1
    assert meta["criteria"]["cost_median_tol"] == R.TOL_MEDIAN
    assert meta["started"] and meta["finished"]           # doplněno i na konci
    assert meta["verdict"] == "n/a" and meta["runs_done"] == 0
    assert (out / "report.md").exists()
