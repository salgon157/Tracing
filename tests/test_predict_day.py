"""
test_predict_day.py — testy tenkého predikčního wrapperu (čisté funkce, bez subprocess)
"""
from pathlib import Path

import pytest

import paths

from predict_day import (
    build_depot_commands,
    depots_with_input,
    find_riro_for_date,
    _fmt_num,
)


def _cmds(**kw):
    defaults = dict(budget_min=5.0, force_matrix=False,
                    osm_source="current", visualize=True)
    defaults.update(kw)
    return build_depot_commands("CB", "2026-07-14", "1430", **defaults)


class TestBuildDepotCommands:
    def test_prepare_uses_data_root(self):
        cmds, _ = _cmds()
        prepare = cmds[0]
        assert prepare[1] == "prepare_inputs_v6.py"
        assert prepare[2] == "CB"
        assert "--data-root" in prepare
        assert prepare[prepare.index("--data-root") + 1] == \
            paths.PREDICTION_ROOT.as_posix()

    def test_solve_paths_under_prediction_root(self):
        cmds, out_dir = _cmds()
        solve = cmds[1]
        assert solve[1] == "vrp_solver_lines_v6.py"
        orders = solve[solve.index("--orders-file") + 1]
        assert orders == (paths.PREDICTION_ROOT / "prepared" / "CB"
                          / "orders_CB_2026-07-14.csv").as_posix()
        assert solve[solve.index("--output-dir") + 1] == out_dir.as_posix()
        assert out_dir.as_posix() == (paths.PREDICTION_ROOT / "results" / "CB"
                                      / "2026-07-14_1430").as_posix()

    def test_solve_run_log_separated(self):
        cmds, _ = _cmds()
        solve = cmds[1]
        log = solve[solve.index("--run-log-path") + 1]
        assert log == (paths.PREDICTION_ROOT / "results"
                       / "run_log.jsonl").as_posix()

    def test_budget_formatted_like_workflow(self):
        cmds, _ = _cmds(budget_min=5.0)
        solve = cmds[1]
        assert solve[solve.index("--budget-min") + 1] == "5"

    def test_flags_passthrough(self):
        cmds, _ = _cmds(force_matrix=True, osm_source="current")
        solve = cmds[1]
        assert "--force-matrix" in solve
        assert solve[solve.index("--osm-source") + 1] == "current"
        vis = cmds[2]
        assert vis[vis.index("--osm-source") + 1] == "current"

    def test_osm_source_stable_passthrough(self):
        # stable = zamrzlá mapa; musí se propsat do solveru i vizualizace
        cmds, _ = _cmds(osm_source="stable")
        assert cmds[1][cmds[1].index("--osm-source") + 1] == "stable"
        assert cmds[2][cmds[2].index("--osm-source") + 1] == "stable"

    def test_visualize_never_open(self):
        cmds, out_dir = _cmds(visualize=True)
        vis = cmds[2]
        assert vis[1] == "visualize_routes.py"
        assert vis[2] == out_dir.as_posix()
        assert "--open" not in vis

    def test_no_visualize_two_commands(self):
        cmds, _ = _cmds(visualize=False)
        assert len(cmds) == 2

    def test_custom_root(self):
        cmds, out_dir = build_depot_commands(
            "MO", "2026-07-14", "0900", budget_min=10.0, force_matrix=False,
            osm_source="current", visualize=False, root=Path("data/jinam"))
        assert "data/jinam" in cmds[0]
        assert out_dir.as_posix().startswith("data/jinam/results/MO/")


class TestDepotsWithInput:
    def test_detects_only_depots_with_csv(self, tmp_path):
        for depot, make_csv in [("CB", True), ("HK", False), ("MO", True)]:
            d = tmp_path / "input" / depot / "aktivni"
            d.mkdir(parents=True)
            if make_csv:
                (d / f"riro-20260714-{depot}-POB.csv").write_text("", encoding="utf-8")
        assert depots_with_input(tmp_path) == ["CB", "MO"]

    def test_empty_root(self, tmp_path):
        assert depots_with_input(tmp_path) == []


class TestFmtNum:
    def test_int_float(self):
        assert _fmt_num(5.0) == "5"

    def test_fraction(self):
        assert _fmt_num(2.5) == "2.5"


# ═════════════════════════════════════════════════════════════════════════════
#  --label a --input-date (srpen 2026)
#
#  Umožňují pustit dvě verze predikce vedle sebe a přepočítat starší den,
#  aniž by se přehazovaly soubory v aktivni/.
# ═════════════════════════════════════════════════════════════════════════════

class TestLabelAndInputDate:
    def test_label_appended_to_output_dir(self):
        _, out_dir = build_depot_commands(
            "CB", "2026-08-05", "1430", budget_min=5, force_matrix=False,
            osm_source="current", visualize=False, label="s-koeficientem")
        assert out_dir.name == "2026-08-05_1430_s-koeficientem"

    def test_no_label_keeps_original_naming(self):
        _, out_dir = build_depot_commands(
            "CB", "2026-08-05", "1430", budget_min=5, force_matrix=False,
            osm_source="current", visualize=False)
        assert out_dir.name == "2026-08-05_1430"

    def test_riro_file_passed_to_prepare(self, tmp_path):
        riro = tmp_path / "riro-20260803-CB.csv"
        cmds, _ = build_depot_commands(
            "CB", "2026-08-03", "1430", budget_min=5, force_matrix=False,
            osm_source="current", visualize=False, riro_file=riro)
        prepare = cmds[0]
        assert "--riro-file" in prepare
        assert prepare[prepare.index("--riro-file") + 1] == riro.as_posix()

    def test_no_riro_file_means_aktivni(self):
        cmds, _ = build_depot_commands(
            "CB", "2026-08-05", "1430", budget_min=5, force_matrix=False,
            osm_source="current", visualize=False)
        assert "--riro-file" not in cmds[0]

    def test_find_riro_for_date(self, tmp_path):
        depot_dir = tmp_path / "input" / "CB"
        depot_dir.mkdir(parents=True)
        (depot_dir / "riro-20260803-CB.csv").write_text("x", encoding="utf-8")
        (depot_dir / "riro-20260805-CB.csv").write_text("y", encoding="utf-8")
        found = find_riro_for_date("CB", "20260803", root=tmp_path)
        assert found.name == "riro-20260803-CB.csv"

    def test_find_riro_missing_date(self, tmp_path):
        (tmp_path / "input" / "CB").mkdir(parents=True)
        with pytest.raises(FileNotFoundError, match="20260803"):
            find_riro_for_date("CB", "20260803", root=tmp_path)

    def test_find_riro_ambiguous(self, tmp_path):
        depot_dir = tmp_path / "input" / "CB"
        depot_dir.mkdir(parents=True)
        (depot_dir / "riro-20260803-CB.csv").write_text("x", encoding="utf-8")
        (depot_dir / "riro-20260803-Praha.csv").write_text("y", encoding="utf-8")
        with pytest.raises(ValueError, match="víc souborů"):
            find_riro_for_date("CB", "20260803", root=tmp_path)
