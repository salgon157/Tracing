"""
test_scenarios.py — smíšené scénáře napříč body auditu (interakce oprav).

Každý scénář popisuje situaci z provozu a ověřuje, jak spolu opravené
části reagují — ne jednotlivou funkci. Syntetická data, malé OR-Tools
modely (time_limit ≤ 2 s), žádné OSRM.
"""
import numpy as np
import pytest

import vrp_solver_lines_v6 as solver_mod
from vrp_solver_lines_v6 import CONFIG, solve_cluster


def _order(no, time_from, time_to, lat=50.0, lon=14.0, kg=100.0, service_sec=60):
    return {"order_number": no, "id": no, "name": no, "customer_name": no,
            "location_code": no.lower(), "time_from": time_from,
            "time_to": time_to, "weight_kg": kg, "lat": lat, "lon": lon,
            "service_sec": service_sec}


def _van(n=1):
    return [{"id": f"TYPE_02_{k+1:02d}", "type": "d", "type_code": "TYPE_02",
             "max_kg": 1350, "cost_per_km": 11.0, "start_cost": 1000,
             "osrm_profile": "driving", "time_multiplier": 1.0}
            for k in range(n)]


# ═════════════════════════════════════════════════════════════════════════════
#  Scénář 7 (audit 2.2): slack 120 — auto smí počkat na pozdější okno až 2 h,
#  místo aby na ně solver poslal druhé auto
# ═════════════════════════════════════════════════════════════════════════════

class TestScenarioSlackWaiting:
    def _solve(self, gap_min):
        # A: 06:00–07:00, B: okno začíná až gap minut po dojezdu z A
        b_from = 7 * 60 + 10 + gap_min           # A hotová ~07:00, jízda 10 min
        b = _order("B", f"{b_from // 60:02d}:{b_from % 60:02d}",
                   f"{(b_from + 60) // 60:02d}:{(b_from + 60) % 60:02d}")
        a = _order("A", "06:00", "07:00")
        dist = [[0, 20, 20], [20, 0, 10], [20, 10, 0]]
        times = [[0, 20, 20], [20, 0, 10], [20, 10, 0]]
        routes, _ = solve_cluster([a, b], _van(2), dist, [times, times], 2)
        return routes

    def test_gap_within_slack_is_one_line(self):
        # slack se čte z CONFIG — test drží pod ním (100 min ≤ 120)
        assert CONFIG["time_slack_max_min"] >= 100, "test počítá se slackem ≥ 100 min"
        routes = self._solve(gap_min=100)
        assert len(routes) == 1, [r["vehicle_id"] for r in routes]

    def test_gap_beyond_slack_needs_second_line(self):
        gap = int(CONFIG["time_slack_max_min"]) + 60
        routes = self._solve(gap_min=gap)
        assert len(routes) == 2


# ═════════════════════════════════════════════════════════════════════════════
#  Scénář 1 + 2 (audit 1.2/1.4/2.11): reakce večera na exit kódy solveru
#  — eskalace porušení jen když řešení neexistuje, nikdy na vadná data
# ═════════════════════════════════════════════════════════════════════════════

class TestScenarioEveningReactsToExitCodes:
    L0 = {"capacity_multiplier": 1.0, "double_runs": False}

    def _run(self, rcs):
        import plan_day
        calls, escalations = [], []
        it = iter(rcs)

        def run_once(flags):
            calls.append(dict(flags))
            return next(it)
        outcome, flags, rc = plan_day.solve_depot_with_escalation(
            run_once, dict(self.L0),
            on_escalate=lambda old, new: escalations.append((old, new)))
        return outcome, flags, rc, calls, escalations

    def test_bad_prepared_row_no_escalation(self):
        # 1.2 → solver exit 2 (vadný řádek prepared) → večer NEeskaluje,
        # flags zůstávají L0, jediný běh
        outcome, flags, rc, calls, esc = self._run([2])
        assert outcome == "data_error" and rc == 2
        assert flags == self.L0 and len(calls) == 1 and esc == []

    def test_infeasible_escalates_once_then_ok(self):
        outcome, flags, rc, calls, esc = self._run([3, 0])
        assert outcome == "ok" and flags["double_runs"] is True
        assert len(calls) == 2 and len(esc) == 1
        assert calls[1]["double_runs"] is True           # druhý běh už na L1+L2

    def test_infeasible_twice_gives_up_not_infinite(self):
        outcome, flags, rc, calls, esc = self._run([3, 3])
        assert outcome == "give_up" and len(calls) == 2 and len(esc) == 1

    def test_technical_error_no_escalation(self):
        outcome, flags, rc, calls, esc = self._run([1])
        assert outcome == "error" and esc == [] and flags == self.L0


# ═════════════════════════════════════════════════════════════════════════════
#  Scénář 3 (audit 2.4 + 2.5): poslední depo, dvojlinky, nečinná auta jinde
# ═════════════════════════════════════════════════════════════════════════════

class TestScenarioDoubleRunWithIdleCarsElsewhere:
    def test_plan_survives_when_other_cluster_left_cars_idle(self):
        from vrp_solver_lines_v6 import (assign_vehicles_to_clusters,
                                         build_double_run_vehicles,
                                         is_virtual_vehicle, pair_double_runs)
        physical = [{"id": f"TYPE_02_{k:02d}", "type_code": "TYPE_02", "type": "d",
                     "max_kg": 1350, "cost_per_km": 11.0, "start_cost": 1000,
                     "osrm_profile": "driving", "time_multiplier": 1.0,
                     "driver": ""} for k in range(1, 7)]
        fleet = physical + build_double_run_vehicles(physical)
        # dva clustery: A hustý s odpolední prací (dostane dvojlinky), B lehký
        A = [_order(f"A{i}", "06:00", "13:00", lat=49.0 + i * 0.001) for i in range(20)]
        B = [_order(f"B{i}", "10:00", "14:00", lat=51.0 + i * 0.001) for i in range(4)]
        asg = assign_vehicles_to_clusters([A, B], fleet)
        # 2.5 (dnešní): dvojlinky rozprostřené, každý cluster má fyzická auta
        assert all(any(not is_virtual_vehicle(v) for v in a) for a in asg)

        # simulace výsledku solveru: A jelo všemi svými fyzickými + jednou
        # virtuální (výjezd 12:00, ale všechna auta A se vrací až 14:00);
        # B použilo jediné auto, ostatní auta B stála
        def rt(vid, dep, ret):
            return {"vehicle_id": vid, "type_code": "TYPE_02", "driver": "",
                    "stops": [{"stop": "Sklad", "arrival": dep, "kg": 0},
                              {"stop": "Z", "id": "x", "arrival": dep, "kg": 100},
                              {"stop": "Sklad (návrat)", "arrival": ret, "kg": 0}]}
        phys_A = [v for v in asg[0] if not is_virtual_vehicle(v)]
        virt_A = [v for v in asg[0] if is_virtual_vehicle(v)]
        phys_B = [v for v in asg[1] if not is_virtual_vehicle(v)]
        assert virt_A and len(phys_B) >= 2
        routes = [rt(v["id"], "06:00", "14:00") for v in phys_A]
        routes.append(rt(virt_A[0]["id"], "12:00", "16:00"))
        routes.append(rt(phys_B[0]["id"], "10:00", "13:00"))

        out = pair_double_runs(routes, fleet)             # nesmí spadnout
        conv = [r for r in out if r["stops"][0]["arrival"] == "12:00"][0]
        assert conv["vehicle_id"] in {v["id"] for v in phys_B[1:]} or \
            conv["vehicle_id"] in {v["id"] for v in physical}
        assert not is_virtual_vehicle({"id": conv["vehicle_id"]})
        assert conv["double_run"] is False
