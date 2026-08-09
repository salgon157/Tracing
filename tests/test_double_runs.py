"""
test_double_runs.py — dvojlinky (--double-runs, porušení L2)

Auto smí naložit ve skladu 2× za den. Virtuální „druhá jízda" vozidla
platí PLNÝ druhý výjezd (+1 Kč preference fyzických), smí vyjet od
CONFIG double_run_earliest a po solve se párují na fyzická auta:
návrat + depot_loading_min <= výjezd druhé jízdy, jinak běh spadne.
"""
import numpy as np
import pytest

import vrp_solver_lines_v6 as solver_mod
from vrp_solver_lines_v6 import (
    CONFIG,
    build_double_run_vehicles,
    is_double_run_vehicle,
    pair_double_runs,
    solve_cluster,
    time_to_minutes,
)


def _vehicle(type_code="TYPE_02", n=1, max_kg=1350, start_cost=1000):
    return {
        "id": f"{type_code}_{n:02d}", "type_code": type_code,
        "type": "Dodávka", "driver": "",
        "max_kg": max_kg, "cost_per_km": 11.0, "start_cost": start_cost,
        "time_multiplier": 1.0, "osrm_profile": "driving",
    }


def _route(vehicle_id="TYPE_02_01", type_code="TYPE_02",
           departure="06:00", ret="09:00"):
    return {
        "vehicle_id": vehicle_id, "type_code": type_code, "driver": "Karel",
        "stops": [
            {"stop": "Sklad", "arrival": departure, "kg": 0},
            {"stop": "Zákazník", "id": "O1", "arrival": departure, "kg": 100},
            {"stop": "Sklad (návrat)", "arrival": ret, "kg": 0},
        ],
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Stavba virtuálních vozidel
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildDoubleRunVehicles:
    def test_only_small_types(self):
        fleet = [_vehicle("TYPE_02", 1), _vehicle("TYPE_06", 1, max_kg=8000)]
        virtuals = build_double_run_vehicles(fleet)
        assert all(v["type_code"] == "TYPE_02" for v in virtuals)

    def test_id_format_keeps_type_parseable(self):
        virtuals = build_double_run_vehicles([_vehicle("TYPE_02", 1)])
        assert virtuals[0]["id"] == "TYPE_02_2R01"
        # fleet_budget čte typ přes rsplit("_", 1) — nesmí se rozbít
        assert virtuals[0]["id"].rsplit("_", 1)[0] == "TYPE_02"
        assert is_double_run_vehicle(virtuals[0]["id"])
        assert not is_double_run_vehicle("TYPE_02_01")

    def test_full_second_fix_plus_preference(self):
        # plný druhý výjezd (rozhodnutí uživatele) + 1 Kč, aby solver
        # preferoval fyzická auta a druhá jízda bez první nevznikala
        virtuals = build_double_run_vehicles([_vehicle(start_cost=1000)])
        assert virtuals[0]["start_cost"] == 1001

    def test_earliest_start_from_config(self):
        virtuals = build_double_run_vehicles([_vehicle()])
        assert virtuals[0]["earliest_start_min"] == \
            time_to_minutes(CONFIG["double_run_earliest"])

    def test_capped_by_config_and_physical_count(self):
        fleet = [_vehicle("TYPE_02", i) for i in range(1, 30)]
        assert len(build_double_run_vehicles(fleet)) == \
            CONFIG["double_runs_max"]
        # 2 fyzická auta -> max 2 dvojlinky, i když config dovoluje víc
        assert len(build_double_run_vehicles(fleet[:2])) == 2

    def test_biggest_small_type_first(self):
        fleet = ([_vehicle("TYPE_01", i, max_kg=1200) for i in range(1, 3)]
                 + [_vehicle("TYPE_02", i) for i in range(1, 25)])
        virtuals = build_double_run_vehicles(fleet)
        assert virtuals[0]["type_code"] == "TYPE_02"


# ═════════════════════════════════════════════════════════════════════════════
#  Párování po solve
# ═════════════════════════════════════════════════════════════════════════════

class TestPairDoubleRuns:
    def test_no_virtuals_noop(self):
        routes = [_route()]
        assert pair_double_runs(routes) == routes

    def test_pairs_and_rewrites_vehicle(self):
        routes = [_route(ret="09:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="10:00",
                         ret="14:00")]
        out = pair_double_runs(routes)
        second = [r for r in out if r.get("double_run")]
        assert len(second) == 1
        assert second[0]["vehicle_id"] == "TYPE_02_01"   # fyzické auto
        assert second[0]["driver"] == "Karel"

    def test_reload_boundary(self):
        # návrat 09:20 + 40 min nakládka = 10:00 -> výjezd 10:00 PROJDE
        ok = [_route(ret="09:20"),
              _route(vehicle_id="TYPE_02_2R01", departure="10:00")]
        assert pair_double_runs(ok)[1]["double_run"] is True
        # návrat 09:21 -> chybí minuta -> spadne
        bad = [_route(ret="09:21"),
               _route(vehicle_id="TYPE_02_2R01", departure="10:00")]
        with pytest.raises(SystemExit, match="NEJDOU SPÁROVAT"):
            pair_double_runs(bad)

    def test_same_type_only(self):
        routes = [_route(vehicle_id="TYPE_01_01", type_code="TYPE_01",
                         ret="08:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="10:00")]
        with pytest.raises(SystemExit):
            pair_double_runs(routes)

    def test_physical_hosts_max_one_second_run(self):
        routes = [_route(ret="08:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="10:00"),
                  _route(vehicle_id="TYPE_02_2R02", departure="11:00")]
        with pytest.raises(SystemExit, match="obsazeno"):
            pair_double_runs(routes)

    def test_greedy_leaves_late_returns_for_late_departures(self):
        # brzká dvojlinka si vezme brzký návrat; pozdní zůstane pozdní
        routes = [_route(vehicle_id="TYPE_02_01", ret="09:00"),
                  _route(vehicle_id="TYPE_02_02", ret="11:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="12:00"),
                  _route(vehicle_id="TYPE_02_2R02", departure="10:00")]
        out = pair_double_runs(routes)
        by_dep = {r["stops"][0]["arrival"]: r for r in out
                  if r.get("double_run")}
        assert by_dep["10:00"]["vehicle_id"] == "TYPE_02_01"   # 09:00 návrat
        assert by_dep["12:00"]["vehicle_id"] == "TYPE_02_02"   # 11:00 návrat

    def test_second_run_without_first_fatal(self):
        # dvojlinka jede, ale žádné fyzické auto téhož typu nevyjelo
        routes = [_route(vehicle_id="TYPE_02_2R01", departure="10:00")]
        with pytest.raises(SystemExit, match="žádná"):
            pair_double_runs(routes)


# ═════════════════════════════════════════════════════════════════════════════
#  Integrace: skutečný mini-solve (OR-Tools, bez OSRM)
# ═════════════════════════════════════════════════════════════════════════════

class TestSolveWithDoubleRun:
    def _orders(self):
        # 2 objednávky po 900 kg -> jedno auto (1000 kg) je nepobere najednou
        return [
            {"order_number": "OA", "id": "OA", "name": "Ranní",
             "customer_name": "Ranní", "location_code": "ra",
             "time_from": "06:00", "time_to": "08:00",
             "weight_kg": 900.0, "lat": 50.0, "lon": 14.0,
             "service_sec": 600},
            {"order_number": "OB", "id": "OB", "name": "Odpolední",
             "customer_name": "Odpolední", "location_code": "od",
             "time_from": "12:00", "time_to": "14:00",
             "weight_kg": 900.0, "lat": 50.1, "lon": 14.1,
             "service_sec": 600},
        ]

    def test_double_run_solves_and_pairs(self):
        # 1 fyzické auto + jeho dvojlinka zvládnou den, který by jinak
        # potřeboval 2 auta
        physical = _vehicle(max_kg=1000)
        virtual = build_double_run_vehicles([physical])[0]
        vehicles = [physical, virtual]
        n = 3                                    # sklad + 2 objednávky
        dist = np.full((n, n), 5.0); np.fill_diagonal(dist, 0)
        times = np.full((n, n), 10.0); np.fill_diagonal(times, 0)

        routes, cost = solve_cluster(
            self._orders(), vehicles, dist, [times, times],
            time_limit_sec=5)
        assert len(routes) == 2, "očekávám první jízdu + dvojlinku"

        virtual_routes = [r for r in routes
                          if is_double_run_vehicle(r["vehicle_id"])]
        assert len(virtual_routes) == 1
        # dvojlinka nesměla vyjet před double_run_earliest
        dep = time_to_minutes(virtual_routes[0]["stops"][0]["arrival"])
        assert dep >= time_to_minutes(CONFIG["double_run_earliest"])

        paired = pair_double_runs(routes)
        second = [r for r in paired if r.get("double_run")][0]
        assert second["vehicle_id"] == "TYPE_02_01"

    def test_without_flag_same_day_needs_two_vehicles(self):
        # kontrolní běh: bez dvojlinky ten samý den vyžaduje 2 fyzická auta
        vehicles = [_vehicle(max_kg=1000, n=1), _vehicle(max_kg=1000, n=2)]
        n = 3
        dist = np.full((n, n), 5.0); np.fill_diagonal(dist, 0)
        times = np.full((n, n), 10.0); np.fill_diagonal(times, 0)
        routes, _ = solve_cluster(self._orders(), vehicles, dist,
                                  [times, times], time_limit_sec=5)
        assert len(routes) == 2
        assert not any(is_double_run_vehicle(r["vehicle_id"]) for r in routes)
