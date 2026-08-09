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
