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


class TestL3StateAndExcludes:
    """L3: kamiony vyhrazené kamionové trase nesmí večer dostat depa."""

    def test_fresh_state_subtracts_l3_trucks(self, tmp_path):
        rows = [{"type_code": "TYPE_02", "max_kg": "1350",
                 "available_count": "53"},
                {"type_code": "TYPE_06", "max_kg": "8700",
                 "available_count": "1"}]
        decision = {"solver_flags": {"capacity_multiplier": 1.03,
                                     "double_runs": True},
                    "l3": {"trucks": {"TYPE_06": 1}}}
        state = plan_day.load_real_state(tmp_path / "neni.json", rows, decision)
        assert state["remaining"]["TYPE_06"] == 0
        assert state["remaining"]["TYPE_02"] == 53

    def test_fresh_state_without_l3_untouched(self, tmp_path):
        rows = [{"type_code": "TYPE_06", "max_kg": "8700",
                 "available_count": "1"}]
        decision = {"solver_flags": {"capacity_multiplier": 1.0,
                                     "double_runs": False}, "l3": None}
        state = plan_day.load_real_state(tmp_path / "neni.json", rows, decision)
        assert state["remaining"]["TYPE_06"] == 1

    def test_orders_by_depot_split(self):
        import l3_planner as l3
        block = {"orders": [
            {"order_number": "O1", "depot": "CB", "location_code": "a", "kg": 1},
            {"order_number": "O2", "depot": "HK", "location_code": "b", "kg": 1},
            {"order_number": "O3", "depot": "CB", "location_code": "c", "kg": 1}]}
        assert l3.orders_by_depot(block) == {"CB": ["O1", "O3"], "HK": ["O2"]}


class TestL3TrucksAndFallback:
    """L3 kamiony z flotily, typy z binů, alert při neplánovatelné trase."""

    ROWS = [
        {"type_code": "TYPE_02", "max_kg": "1350", "cost_per_km": "11",
         "start_cost_kc": "1000", "available_count": "10"},
        {"type_code": "TYPE_04", "max_kg": "3200", "cost_per_km": "19.5",
         "start_cost_kc": "1000", "available_count": "2"},
        {"type_code": "TYPE_05", "max_kg": "8000", "cost_per_km": "28",
         "start_cost_kc": "1000", "available_count": "1"},
        {"type_code": "TYPE_06", "max_kg": "8700", "cost_per_km": "28",
         "start_cost_kc": "1000", "available_count": "1"},
    ]

    def test_truck_units_only_trucks_expanded_desc(self):
        units = plan_day.l3_truck_units(
            self.ROWS, {"TYPE_02": 5, "TYPE_04": 2, "TYPE_05": 1, "TYPE_06": 1})
        assert [u["type_code"] for u in units] == ["TYPE_06", "TYPE_05"]
        assert units[0]["max_kg"] == 8700 and units[0]["cost_per_km"] == 28.0
        assert units[0]["start_cost"] == 1000.0

    def test_truck_units_zero_when_none_left(self):
        assert plan_day.l3_truck_units(self.ROWS, {"TYPE_05": 0}) == []

    def test_trucks_by_type_from_bins(self):
        units = [{"type_code": "TYPE_06"}, {"type_code": "TYPE_05"}]
        assert plan_day.l3_trucks_by_type_from_bins(units, [[{"x": 1}], []]) == \
            {"TYPE_06": 1}
        assert plan_day.l3_trucks_by_type_from_bins(units, [[{"x": 1}], [{"y": 2}]]) == \
            {"TYPE_06": 1, "TYPE_05": 1}

    def test_unplanned_alert_writes_json_and_cleans_dir(self, tmp_path):
        block = {"orders": [
            {"order_number": "O1", "depot": "CB", "location_code": "a", "kg": 100},
            {"order_number": "O2", "depot": "HK", "location_code": "b", "kg": 200}]}
        locs = [{"kg": 100}, {"kg": 200}]
        out_dir = tmp_path / "results" / "L3" / "2026-08-17"
        out_dir.mkdir(parents=True)
        state_dir = tmp_path / "state"
        with pytest.raises(SystemExit) as e:
            plan_day._l3_unplanned_alert(block, locs, tmp_path / "m.csv",
                                         state_dir, "2026-08-17",
                                         reason="test", out_dir=out_dir)
        msg = str(e.value)
        assert "NEVYŠLA" in msg and "CB: 1 obj" in msg and "HK: 1 obj" in msg
        assert "O2" in msg
        assert not out_dir.exists()                    # prázdná složka pryč
        j = state_dir / "l3_unplanned_2026-08-17.json"
        assert j.exists()
        import json
        data = json.loads(j.read_text(encoding="utf-8"))
        assert data["orders_by_depot"]["HK"][0]["order_number"] == "O2"

    def test_unplanned_alert_keeps_nonempty_dir(self, tmp_path):
        out_dir = tmp_path / "L3"
        out_dir.mkdir()
        (out_dir / "něco.txt").write_text("x", encoding="utf-8")
        with pytest.raises(SystemExit):
            plan_day._l3_unplanned_alert({"orders": []}, [], tmp_path / "m.csv",
                                         tmp_path / "s", "2026-08-17",
                                         reason="t", out_dir=out_dir)
        assert out_dir.exists()


class TestDecideAfterSolver:
    """Audit 1.4: eskalace porušení JEN na exit 3 (řešení neexistuje)."""

    def test_rc0_ok_keeps_flags(self):
        import fleet_budget as fb
        flags = {"capacity_multiplier": 1.0, "double_runs": False}
        assert fb.decide_after_solver(0, flags) == ("ok", flags)

    def test_rc3_escalates_l0_to_l1l2(self):
        import fleet_budget as fb
        out, harder = fb.decide_after_solver(3, {"capacity_multiplier": 1.0,
                                                 "double_runs": False})
        assert out == "escalate" and harder["double_runs"] is True

    def test_rc3_on_l1l2_gives_up(self):
        import fleet_budget as fb
        assert fb.decide_after_solver(3, {"capacity_multiplier": 1.03,
                                          "double_runs": True}) == ("give_up", None)

    def test_rc2_data_error_never_escalates(self):
        import fleet_budget as fb
        assert fb.decide_after_solver(2, {"double_runs": False}) == ("data_error", None)

    def test_rc1_technical_never_escalates(self):
        import fleet_budget as fb
        assert fb.decide_after_solver(1, {"double_runs": False}) == ("error", None)
        assert fb.decide_after_solver(137, {"double_runs": False}) == ("error", None)

    def test_status_hint_reads_run_status(self, tmp_path):
        import json
        (tmp_path / "run_status.json").write_text(json.dumps({
            "status": "data_error", "reason": "[CHYBA] VADNÉ ŘÁDKY",
            "orders": ["O1", "O2"]}), encoding="utf-8")
        hint = plan_day._solver_status_hint(tmp_path)
        assert "data_error" in hint and "O1, O2" in hint
        assert plan_day._solver_status_hint(tmp_path / "neni") == ""

    def test_rescue_extra_min_passthrough(self):
        import argparse
        args = argparse.Namespace(budget=5.0, label="", run_log_path="",
                                  osm_source="current", force_matrix=False,
                                  seed_finalists="", rescue_extra_min=2.5)
        cmd = plan_day.build_real_solver_cmd("PR", "2026-08-17", Path("f.csv"),
                                             {"capacity_multiplier": 1.0,
                                              "double_runs": False}, args)
        assert cmd[cmd.index("--rescue-extra-min") + 1] == "2.5"
        args.rescue_extra_min = 0
        cmd = plan_day.build_real_solver_cmd("PR", "2026-08-17", Path("f.csv"),
                                             {"capacity_multiplier": 1.0,
                                              "double_runs": False}, args)
        assert "--rescue-extra-min" not in cmd


class TestDecisionStateIdentity:
    """Audit 1.3: state.json nese identitu decision + vozového parku;
    real/l3 při neshodě zastaví (--force = vědomé pokračování)."""

    def _decision(self, **over):
        d = {"date": "2026-08-17", "session": "1602",
             "created_at": "2026-08-16T16:39:40", "depots": ["CB", "MO"],
             "level": 1, "solver_flags": {"capacity_multiplier": 1.03,
                                          "double_runs": True},
             "reservations": {"CB": {}, "MO": {}}, "l3": None,
             "fleet_file": "vehicle_types-20260817.csv",
             "runs": {"P1": {"CB": "x/1602_P1"}}}
        d.update(over)
        return d

    def test_decision_id_stable_for_same_content(self):
        import fleet_budget as fb
        a = self._decision()
        b = self._decision(created_at="2026-08-16T18:00:00", session="1800",
                           runs={"P1": {"CB": "x/1800_P1"}})
        assert fb.decision_fingerprint(a) == fb.decision_fingerprint(b)
        c = self._decision(l3={"orders": [{"order_number": "O1"}]})
        assert fb.decision_fingerprint(a) != fb.decision_fingerprint(c)
        assert len(fb.decision_fingerprint(a)) == 16

    def test_state_carries_decision_identity(self, tmp_path):
        rows = [{"type_code": "TYPE_02", "max_kg": "1350", "available_count": "5"}]
        d = self._decision()
        st = plan_day.load_real_state(tmp_path / "neni.json", rows, d,
                                      fleet_file_name="vehicle_types-20260817.csv")
        import fleet_budget as fb
        assert st["decision_id"] == fb.decision_fingerprint(d)
        assert st["decision_created_at"] == "2026-08-16T16:39:40"
        assert st["fleet_file"] == "vehicle_types-20260817.csv"

    def test_real_stops_on_decision_mismatch(self, capsys):
        import fleet_budget as fb
        d_old = self._decision()
        d_new = self._decision(created_at="2026-08-16T18:30:00",
                               l3={"orders": [{"order_number": "O9"}]})
        state = {"planned": ["CB"], **fb.decision_identity(d_old)}
        with pytest.raises(SystemExit) as e:
            plan_day.guard_state_identity(state, d_new, "vehicle_types-20260817.csv",
                                          force=False, what="real")
        msg = str(e.value)
        assert "PŘEGENEROVANÁ" in msg and "16:39:40" in msg and "18:30:00" in msg
        assert "--force" in msg
        # s --force projde a jen varuje
        plan_day.guard_state_identity(state, d_new, "vehicle_types-20260817.csv",
                                      force=True, what="real")
        assert "pokračuji na vlastní riziko" in capsys.readouterr().out

    def test_l3_uses_same_guard(self):
        import fleet_budget as fb
        d_old = self._decision(); d_new = self._decision(level=0)
        state = {"planned": ["CB", "MO"], **fb.decision_identity(d_old)}
        with pytest.raises(SystemExit) as e:
            plan_day.guard_state_identity(state, d_new, "vehicle_types-20260817.csv",
                                          force=False, what="l3")
        assert "l3:" in str(e.value)

    def test_state_stops_on_fleet_file_mismatch(self):
        import fleet_budget as fb
        d = self._decision()
        state = {"planned": ["CB"], **fb.decision_identity(d)}
        with pytest.raises(SystemExit) as e:
            plan_day.guard_state_identity(state, d, "vehicle_types-20260818.csv",
                                          force=False, what="real")
        assert "vozový park se změnil" in str(e.value)

    def test_matching_state_passes_silently(self, capsys):
        import fleet_budget as fb
        d = self._decision()
        state = {"planned": [], **fb.decision_identity(d)}
        plan_day.guard_state_identity(state, d, "vehicle_types-20260817.csv",
                                      force=False, what="real")
        assert capsys.readouterr().out == ""

    def test_old_state_without_id_is_tolerated_with_warning(self, capsys):
        d = self._decision()
        state = {"planned": ["CB"], "remaining": {}, "flags": {}}
        plan_day.guard_state_identity(state, d, "vehicle_types-20260817.csv",
                                      force=False, what="real")
        assert "nenese identitu" in capsys.readouterr().out
