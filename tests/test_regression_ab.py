"""
regression_ab — cesty do solveru musí být absolutní.

Vzniklo z chyby 22. 8. 2026: obě strany běží ze SVÉ složky (kandidát z kořene
repa, baseline z worktree), takže relativní `--out ..\\data\\results\\...` se
u baseline rozvinulo vůči worktree. Strana A pak psala do
`vrp_benchmark/data/`, harness ji hledal v `Tracing_Main/data/` a neviděl
z ní ani řádek — porovnání by tiše běželo nad polovinou dat.
"""

import subprocess
from pathlib import Path

import pytest

import regression_ab as R


@pytest.fixture
def spy_run(monkeypatch):
    """Zachytí argv a cwd místo spuštění solveru."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(R.subprocess, "run", fake_run)
    return seen


def _call(tmp_path, spy_run, **over):
    script_dir = tmp_path / "_baseline"
    script_dir.mkdir(exist_ok=True)
    kw = dict(
        script_dir=script_dir,
        orders=Path("../data/prepared/CB/orders_CB_2026-08-17.csv"),
        out_dir=Path("../data/results/_regression/x/A/CB_r1"),
        fleet_file=Path("../data/static/vehicle_types-20260821.csv"),
        budget=1, osm="current",
        run_log=Path("../data/results/_regression/x/run_log_A.jsonl"),
        console_log=tmp_path / "konzole.log",
        extra_args=[], env={},
    )
    kw.update(over)
    R.run_solver(**kw)
    return spy_run


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class TestSolverCmdPaths:
    @pytest.mark.parametrize("flag", [
        "--orders-file", "--output-dir", "--run-log-path", "--vehicle-types-file"])
    def test_every_path_arg_is_absolute(self, tmp_path, spy_run, flag):
        seen = _call(tmp_path, spy_run)
        value = _arg(seen["cmd"], flag)
        assert Path(value).is_absolute(), (
            f"{flag}={value} je relativní — u baseline (cwd=worktree) by "
            f"ukázalo jinam než u kandidáta")

    def test_runs_from_its_own_directory(self, tmp_path, spy_run):
        seen = _call(tmp_path, spy_run)
        assert seen["cwd"] == tmp_path / "_baseline"

    def test_same_paths_regardless_of_side(self, tmp_path, spy_run, monkeypatch):
        """Táž relativní zadání → táž absolutní cesta pro obě strany."""
        a = dict(_call(tmp_path, spy_run))["cmd"]
        side_b = tmp_path / "kandidat"
        side_b.mkdir()
        b = dict(_call(tmp_path, spy_run, script_dir=side_b))["cmd"]
        for flag in ("--output-dir", "--orders-file", "--run-log-path"):
            assert _arg(a, flag) == _arg(b, flag)


class TestBaselineOutsideRepo:
    def test_worktree_inside_repo_is_refused(self):
        """Checkout starého commitu nese staré data/static — do repa nesmí."""
        import paths
        src = Path(R.__file__).read_text(encoding="utf-8")
        assert "is_relative_to(paths.REPO_ROOT)" in src, \
            "chybí závora proti baseline worktree uvnitř repa"
        assert paths.REPO_ROOT.name == "vrp_benchmark"
