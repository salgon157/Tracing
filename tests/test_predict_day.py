"""
test_predict_day.py — testy tenkého predikčního wrapperu (čisté funkce, bez subprocess)
"""
from pathlib import Path

from predict_day import build_depot_commands, depots_with_input, _fmt_num


def _cmds(**kw):
    defaults = dict(budget_min=5.0, force_matrix=False, fresh_osm=False, visualize=True)
    defaults.update(kw)
    return build_depot_commands("CB", "2026-07-14", "1430", **defaults)


class TestBuildDepotCommands:
    def test_prepare_uses_data_root(self):
        cmds, _ = _cmds()
        prepare = cmds[0]
        assert prepare[1] == "prepare_inputs_v6.py"
        assert prepare[2] == "CB"
        assert "--data-root" in prepare
        assert prepare[prepare.index("--data-root") + 1] == "data/prediction"

    def test_solve_paths_under_prediction_root(self):
        cmds, out_dir = _cmds()
        solve = cmds[1]
        assert solve[1] == "vrp_solver_lines_v6.py"
        orders = solve[solve.index("--orders-file") + 1]
        assert orders == "data/prediction/prepared/CB/orders_CB_2026-07-14.csv"
        assert solve[solve.index("--output-dir") + 1] == out_dir.as_posix()
        assert out_dir.as_posix() == "data/prediction/results/CB/2026-07-14_1430"

    def test_solve_run_log_separated(self):
        cmds, _ = _cmds()
        solve = cmds[1]
        log = solve[solve.index("--run-log-path") + 1]
        assert log == "data/prediction/results/run_log.jsonl"

    def test_budget_formatted_like_workflow(self):
        cmds, _ = _cmds(budget_min=5.0)
        solve = cmds[1]
        assert solve[solve.index("--budget-min") + 1] == "5"

    def test_flags_passthrough(self):
        cmds, _ = _cmds(force_matrix=True, fresh_osm=True)
        solve = cmds[1]
        assert "--force-matrix" in solve
        assert "--fresh-osm" in solve
        vis = cmds[2]
        assert "--fresh-osm" in vis

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
            fresh_osm=False, visualize=False, root=Path("data/jinam"))
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
