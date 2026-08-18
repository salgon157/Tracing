"""
test_cost_matrix.py — režim nákladové matice (audit 1.1 + 2.9 + 1.6, vlna 4)

  legacy (default): Python callbacky, sazba int(cost_per_km) → objektiv ≠
                    vykázaná cena; km z jedné (driving) matice pro všechny
  exact:            RegisterTransitMatrix per typ/profil, přesná sazba
                    (Kč×100), km per profil vozidla
Malé OR-Tools modely (time_limit 1–2 s), bez OSRM.
"""
import numpy as np
import pytest

import vrp_solver_lines_v6 as S
from vrp_solver_lines_v6 import CONFIG, solve_cluster


def _order(no, lat, lon, kg=100.0):
    return {"order_number": no, "id": no, "name": no, "customer_name": no,
            "location_code": no.lower(), "time_from": "06:00",
            "time_to": "18:00", "weight_kg": kg, "lat": lat, "lon": lon,
            "service_sec": 60}


def _veh(vid, cpk, profile="driving", max_kg=1350, start=1000):
    return {"id": vid, "type": "t", "type_code": vid.rsplit("_", 1)[0],
            "max_kg": max_kg, "cost_per_km": cpk, "start_cost": start,
            "osrm_profile": profile, "time_multiplier": 1.0}


@pytest.fixture
def mode():
    saved = CONFIG.get("cost_matrix_mode")
    yield
    CONFIG["cost_matrix_mode"] = saved


class TestExactVsLegacyObjective:
    ORDERS = [_order("O1", 50.0, 14.0), _order("O2", 50.1, 14.1), _order("O3", 50.2, 14.0)]
    DIST = [[0, 10.3, 20.7, 15.1], [10.3, 0, 12.4, 18.9],
            [20.7, 12.4, 0, 9.6], [15.1, 18.9, 9.6, 0]]
    TIME = [[0, 15, 30, 22], [15, 0, 18, 27], [30, 18, 0, 14], [22, 27, 14, 0]]

    def _solve(self, mode_name):
        CONFIG["cost_matrix_mode"] = mode_name
        veh = [_veh("TYPE_04_01", 19.5, max_kg=8000)]
        routes, cost = solve_cluster(self.ORDERS, veh, self.DIST, [self.TIME], 2)
        return routes, cost, CONFIG["_last_objective_kc"]

    def test_exact_mode_cost_matches_reported_cost(self, mode):
        routes, cost, objective = self._solve("exact")
        assert routes and len(routes) == 1
        km = routes[0]["total_km"]
        expected = 1000 + km * 19.5
        assert abs(objective - expected) < 1.0          # setiny + zaokrouhlení
        assert abs(cost - expected) < 1.0

    def test_legacy_mode_unchanged(self, mode):
        routes, cost, objective = self._solve("legacy")
        km = routes[0]["total_km"]
        # objektiv počítá se sazbou int(19,5) = 19 → o km × 0,5 níž než vykázaná
        assert abs(objective - (1000 + km * 19.0)) < 1.0
        assert abs(cost - (1000 + km * 19.5)) < 1.0
        assert objective < cost

    def test_both_modes_find_same_small_route(self, mode):
        r_leg, c_leg, _ = self._solve("legacy")
        r_ex, c_ex, _ = self._solve("exact")
        # 3 zastávky, 1 auto: optimum je stejné → stejné km/cena
        assert abs(c_leg - c_ex) < 1.0
        assert r_leg[0]["total_km"] == r_ex[0]["total_km"]


class TestPerProfileKm:
    ORDERS = [_order("O1", 50.0, 14.0, kg=3000.0), _order("O2", 50.1, 14.1, kg=200.0)]
    TIME = [[0, 20, 20], [20, 0, 10], [20, 10, 0]]
    CAR_KM = [[0, 10.0, 10.0], [10.0, 0, 5.0], [10.0, 5.0, 0]]
    HGV_KM = [[0, 14.0, 14.0], [14.0, 0, 7.0], [14.0, 7.0, 0]]     # kamion delší trasy

    def _vehicles(self):
        return [_veh("TYPE_02_01", 11.0, "driving", max_kg=1350),
                _veh("TYPE_05_01", 28.0, "driving-hgv", max_kg=8000)]

    def test_truck_km_from_hgv_matrix(self, mode):
        CONFIG["cost_matrix_mode"] = "exact"
        routes, _ = solve_cluster(self.ORDERS, self._vehicles(), self.CAR_KM,
                                  [self.TIME, self.TIME], 2,
                                  distances_km_list=[self.CAR_KM, self.HGV_KM])
        by_type = {r["type_code"]: r for r in routes}
        assert "TYPE_05" in by_type                       # 3 000 kg unese jen kamion
        truck = by_type["TYPE_05"]
        # kamion vykazuje km ze SVÉ (hgv) matice: 14 + 14 = 28 (O1 sám) nebo 14+7+14
        assert truck["total_km"] in (28.0, 35.0)
        assert all(s.get("leg_km", 0) in (0.0, 14.0, 7.0) for s in truck["stops"])

    def test_legacy_truck_km_from_car_matrix(self, mode):
        CONFIG["cost_matrix_mode"] = "legacy"
        routes, _ = solve_cluster(self.ORDERS, self._vehicles(), self.CAR_KM,
                                  [self.TIME, self.TIME], 2,
                                  distances_km_list=[self.CAR_KM, self.HGV_KM])
        truck = {r["type_code"]: r for r in routes}["TYPE_05"]
        assert truck["total_km"] in (20.0, 25.0)          # legacy ignoruje hgv km

    def test_sentinel_in_car_matrix_does_not_hit_truck(self, mode):
        CONFIG["cost_matrix_mode"] = "exact"
        car = [row[:] for row in self.CAR_KM]
        car[1][2] = car[2][1] = S.UNREACHABLE_TIME_MIN     # osobní matice: pár nedosažitelný
        routes, cost = solve_cluster(self.ORDERS, self._vehicles(), car,
                                     [self.TIME, self.TIME], 2,
                                     distances_km_list=[car, self.HGV_KM])
        # kamion smí O1→O2 po své matici; cena nesmí obsahovat sentinel
        assert cost < 100_000
        truck = {r["type_code"]: r for r in routes}["TYPE_05"]
        assert truck["total_km"] < 100


class TestConfigAndCli:
    def test_default_is_legacy(self):
        assert CONFIG["cost_matrix_mode"] == "legacy"

    def test_run_log_carries_mode(self):
        from pathlib import Path
        src = Path(S.__file__).read_text(encoding="utf-8")
        assert '"cost_matrix_mode":' in src and "--cost-matrix-mode" in src
