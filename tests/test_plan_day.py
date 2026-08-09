"""
test_plan_day.py — orchestrátor predikcí řízeného plánování (čisté funkce)

Subprocess běhy testuje E2E; tady jen skládání příkazů, řešení dat
a pořadí dep. Logika budgetu/rezervací/rozhodnutí je v test_fleet_budget.py.
"""
from pathlib import Path

import pytest

import plan_day
from plan_day import build_solver_cmd, resolve_depots_and_date


class TestBuildSolverCmd:
    def _cmd(self, **kw):
        defaults = dict(depot="CB", date_str="2026-08-05",
                        out_dir=Path("data/prediction/results/CB/x_P1"),
                        fleet_file=Path("session/fleet_P1.csv"),
                        budget_min=5.0, osm_source="current",
                        force_matrix=False)
        defaults.update(kw)
        return build_solver_cmd(**defaults)

    def test_l0_capacity_forced(self):
        # P1/P2 jedou na L0 — 100 % nosnosti explicitně, ať nezávisí
        # na aktuálním defaultu CONFIG (ten se mění až ve vlně 3)
        cmd = self._cmd()
        idx = cmd.index("--capacity-multiplier")
        assert cmd[idx + 1] == "1.0"

    def test_fleet_file_passed(self):
        cmd = self._cmd(fleet_file=Path("s/fleet_P2_MO.csv"))
        assert cmd[cmd.index("--vehicle-types-file") + 1] == "s/fleet_P2_MO.csv"

    def test_prediction_run_log(self):
        cmd = self._cmd()
        assert cmd[cmd.index("--run-log-path") + 1] == \
            "data/prediction/results/run_log.jsonl"

    def test_orders_file_from_prediction_root(self):
        cmd = self._cmd(depot="PR", date_str="2026-08-03")
        assert cmd[cmd.index("--orders-file") + 1] == \
            "data/prediction/prepared/PR/orders_PR_2026-08-03.csv"

    def test_force_matrix_optional(self):
        assert "--force-matrix" not in self._cmd()
        assert "--force-matrix" in self._cmd(force_matrix=True)


class TestResolveDepotsAndDate:
    def _fake_finder(self, dates_by_depot):
        def finder(depot, _root):
            if depot not in dates_by_depot:
                raise FileNotFoundError(depot)
            return Path(f"riro-x-{depot}.csv"), dates_by_depot[depot]
        return finder

    def test_orders_by_uzaverka_not_input(self, monkeypatch):
        monkeypatch.setattr(plan_day, "find_active_riro_file",
                            self._fake_finder({d: "2026-08-05"
                                               for d in ["CB", "MO", "HK", "PR"]}))
        depots, date = resolve_depots_and_date(["PR", "CB", "HK", "MO"])
        assert depots == ["CB", "MO", "HK", "PR"]      # pořadí uzávěrek
        assert date == "2026-08-05"

    def test_subset_keeps_order(self, monkeypatch):
        monkeypatch.setattr(plan_day, "find_active_riro_file",
                            self._fake_finder({"PR": "2026-08-05",
                                               "MO": "2026-08-05"}))
        depots, _ = resolve_depots_and_date(["PR", "MO"])
        assert depots == ["MO", "PR"]

    def test_mixed_dates_fatal(self, monkeypatch):
        # ruzná data závozu napříč depy = nekonzistentní predikce dne
        monkeypatch.setattr(plan_day, "find_active_riro_file",
                            self._fake_finder({"CB": "2026-08-05",
                                               "MO": "2026-08-06"}))
        with pytest.raises(SystemExit, match="různá data"):
            resolve_depots_and_date(["CB", "MO"])

    def test_unknown_depot_fatal(self):
        with pytest.raises(SystemExit, match="Neznámá depa"):
            resolve_depots_and_date(["XX"])


# ═════════════════════════════════════════════════════════════════════════════
#  Vlna 3: real fáze (čisté funkce)
# ═════════════════════════════════════════════════════════════════════════════

class TestRealHelpers:
    def _args(self, **kw):
        import argparse
        base = dict(budget=30.0, label="", run_log_path="",
                    osm_source="current", force_matrix=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_l0_cmd_no_double_runs(self):
        cmd = plan_day.build_real_solver_cmd(
            "CB", "2026-08-05", Path("s/fleet_CB.csv"),
            {"capacity_multiplier": 1.0, "double_runs": False}, self._args())
        assert cmd[cmd.index("--capacity-multiplier") + 1] == "1"
        assert "--double-runs" not in cmd
        assert "--output-dir" not in cmd          # default = auto-detekce

    def test_l1_l2_cmd(self):
        cmd = plan_day.build_real_solver_cmd(
            "CB", "2026-08-05", Path("s/fleet_CB.csv"),
            {"capacity_multiplier": 1.03, "double_runs": True}, self._args())
        assert cmd[cmd.index("--capacity-multiplier") + 1] == "1.03"
        assert "--double-runs" in cmd

    def test_label_redirects_outputs(self):
        args = self._args(label="test", run_log_path="x/log.jsonl")
        cmd = plan_day.build_real_solver_cmd(
            "MO", "2026-08-05", Path("f.csv"),
            {"capacity_multiplier": 1.0, "double_runs": False}, args)
        assert cmd[cmd.index("--output-dir") + 1] == \
            "data/results/MO/2026-08-05_test"
        assert cmd[cmd.index("--run-log-path") + 1] == "x/log.jsonl"

    def test_orders_from_real_prepared(self):
        cmd = plan_day.build_real_solver_cmd(
            "PR", "2026-08-05", Path("f.csv"),
            {"capacity_multiplier": 1.0, "double_runs": False}, self._args())
        assert cmd[cmd.index("--orders-file") + 1] == \
            "data/prepared/PR/orders_PR_2026-08-05.csv"

    def test_state_roundtrip(self, tmp_path):
        state = {"remaining": {"TYPE_02": 40}, "planned": ["CB"],
                 "flags": {"capacity_multiplier": 1.0, "double_runs": False},
                 "escalated": False}
        p = tmp_path / "state.json"
        plan_day.save_real_state(p, state)
        loaded = plan_day.load_real_state(p, fleet_rows=[], decision={})
        assert loaded == state

    def test_fresh_state_from_decision(self, tmp_path):
        import fleet_budget as fb
        rows = [{"type_code": "TYPE_02", "max_kg": "1350",
                 "available_count": "53"}]
        decision = {"solver_flags": {"capacity_multiplier": 1.03,
                                     "double_runs": True}}
        state = plan_day.load_real_state(tmp_path / "neni.json", rows, decision)
        assert state["remaining"] == {"TYPE_02": 53}
        assert state["planned"] == []
        assert state["flags"]["double_runs"] is True

    def test_missing_decision_fatal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(plan_day, "PREDICTION_ROOT", tmp_path)
        with pytest.raises(SystemExit, match="plan_day.py predict"):
            plan_day.load_decision("2026-08-05")
