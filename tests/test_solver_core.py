"""
test_solver_core.py — unit testy pro vrp_solver_lines_v6.py

Testované funkce (pure, bez OSRM/OR-Tools volání):
  time_to_minutes, service_time_min, auto_n_clusters,
  cluster_profile, expected_vehicle_need, build_data_model
"""
import json
import math
from pathlib import Path

import pytest
import numpy as np

import vrp_solver_lines_v6 as solver_mod
from vrp_solver_lines_v6 import (
    time_to_minutes,
    service_time_min,
    auto_n_clusters,
    cluster_profile,
    expected_vehicle_need,
    build_data_model,
    _sanitize_matrix,
    UNREACHABLE_TIME_MIN,
    UNREACHABLE_MATRIX_FAIL_PCT,
    CONFIG,
)


# ── Helper factories ─────────────────────────────────────────────────────────

def _make_order(lat=50.0, lon=14.0, time_from="08:00", time_to="16:00",
                weight_kg=300.0, service_sec=600):
    return {
        "lat": lat, "lon": lon,
        "time_from": time_from, "time_to": time_to,
        "weight_kg": weight_kg,
        "service_sec": service_sec,
        "order_number": "O1",
    }


def _make_vehicle(max_kg=1400, cost_per_km=10.0, start_cost=0):
    return {
        "vehicle_id": "V1",
        "type_code": "TYPE_02",
        "max_kg": max_kg,
        "cost_per_km": cost_per_km,
        "start_cost": start_cost,
        "max_duration_h": 10,
        "time_multiplier": 1.0,
        "osrm_profile": "driving",
    }


def _identity_matrix(n):
    """n×n nulová numpy matice (cestovní časy 0 — vhodné pro unit testy)."""
    return np.zeros((n, n), dtype=float)


# ═════════════════════════════════════════════════════════════════════════════
#  time_to_minutes
# ═════════════════════════════════════════════════════════════════════════════

class TestTimeToMinutes:
    def test_midnight(self):
        assert time_to_minutes("00:00") == 0

    def test_eight_thirty(self):
        assert time_to_minutes("08:30") == 510

    def test_twelve(self):
        assert time_to_minutes("12:00") == 720

    def test_end_of_day(self):
        assert time_to_minutes("23:59") == 1439

    def test_with_whitespace(self):
        assert time_to_minutes("  08:00  ") == 480

    def test_24_00(self):
        assert time_to_minutes("24:00") == 1440


# ═════════════════════════════════════════════════════════════════════════════
#  service_time_min
# ═════════════════════════════════════════════════════════════════════════════

class TestServiceTimeMin:
    """SEC z ESO9 je KOMPLETNÍ čas zastávky — žádná kg složka, žádný fallback."""

    def test_exact_minutes(self):
        assert service_time_min(_make_order(service_sec=600)) == 10

    def test_ceil_to_whole_minutes(self):
        # 261 s = 4.35 min → 5
        assert service_time_min(_make_order(service_sec=261)) == 5

    def test_weight_does_not_affect_result(self):
        light = _make_order(weight_kg=1.0, service_sec=300)
        heavy = _make_order(weight_kg=900.0, service_sec=300)
        assert service_time_min(light) == service_time_min(heavy) == 5

    def test_result_is_int(self):
        assert isinstance(service_time_min(_make_order(service_sec=123)), int)

    def test_string_sec_accepted(self):
        # z CSV chodí str — musí projít
        assert service_time_min(_make_order(service_sec="300")) == 5

    def test_missing_sec_raises(self):
        order = _make_order()
        del order["service_sec"]
        with pytest.raises(ValueError, match="service_sec"):
            service_time_min(order)

    def test_zero_sec_raises(self):
        with pytest.raises(ValueError, match="service_sec"):
            service_time_min(_make_order(service_sec=0))

    def test_garbage_sec_raises(self):
        with pytest.raises(ValueError, match="service_sec"):
            service_time_min(_make_order(service_sec="neco"))


# ═════════════════════════════════════════════════════════════════════════════
#  auto_n_clusters
# ═════════════════════════════════════════════════════════════════════════════

class TestAutoNClusters:
    def test_zero_orders(self):
        assert auto_n_clusters(0, 5) == 2

    def test_small_exactly_100(self):
        assert auto_n_clusters(100, 5) == 2

    def test_medium_just_over_100(self):
        assert auto_n_clusters(101, 5) == 3

    def test_medium_exactly_300(self):
        assert auto_n_clusters(300, 5) == 3

    def test_large_just_over_300(self):
        assert auto_n_clusters(301, 5) == 4

    def test_very_large(self):
        assert auto_n_clusters(1000, 20) == 4

    def test_n_vehicles_ignored(self):
        # n_vehicles je rezerva pro budoucí použití — výsledek se nemění
        assert auto_n_clusters(50, 1) == auto_n_clusters(50, 100)


# ═════════════════════════════════════════════════════════════════════════════
#  cluster_profile
# ═════════════════════════════════════════════════════════════════════════════

class TestClusterProfile:
    def test_empty_cluster_all_zeros(self):
        p = cluster_profile([])
        assert p["kg"] == 0.0
        assert p["tightness"] == 0.0
        assert p["radial_km"] == 0.0
        assert p["stops"] == 0
        assert p["demand_score"] == 0.0

    def test_single_order_stops_is_one(self):
        o = _make_order()
        p = cluster_profile([o])
        assert p["stops"] == 1

    def test_kg_is_sum_of_weights(self):
        orders = [_make_order(weight_kg=300), _make_order(weight_kg=200)]
        p = cluster_profile(orders)
        assert p["kg"] == pytest.approx(500.0)

    def test_tightness_positive(self):
        o = _make_order(time_from="08:00", time_to="12:00")
        p = cluster_profile([o])
        assert p["tightness"] > 0.0

    def test_tighter_window_higher_tightness(self):
        o_tight = _make_order(time_from="08:00", time_to="09:00")   # 60 min
        o_wide  = _make_order(time_from="08:00", time_to="20:00")   # 720 min
        p_tight = cluster_profile([o_tight])
        p_wide  = cluster_profile([o_wide])
        assert p_tight["tightness"] > p_wide["tightness"]

    def test_demand_score_positive_for_non_empty(self):
        o = _make_order()
        p = cluster_profile([o])
        assert p["demand_score"] > 0.0

    def test_more_stops_higher_demand_score(self):
        orders_1 = [_make_order()]
        orders_3 = [_make_order()] * 3
        assert cluster_profile(orders_3)["demand_score"] > cluster_profile(orders_1)["demand_score"]


# ═════════════════════════════════════════════════════════════════════════════
#  expected_vehicle_need
# ═════════════════════════════════════════════════════════════════════════════

class TestExpectedVehicleNeed:
    def test_empty_cluster_returns_zero(self):
        vehicles = [_make_vehicle()]
        assert expected_vehicle_need([], vehicles) == 0.0

    def test_single_light_order_at_least_one(self):
        orders = [_make_order(weight_kg=100.0)]
        vehicles = [_make_vehicle(max_kg=1400)]
        need = expected_vehicle_need(orders, vehicles)
        assert need >= 1.0

    def test_heavy_load_needs_more_than_one(self):
        # Kapacita 1400 kg, 10 objednávek po 500 kg = 5000 kg → potřeba > 1
        orders = [_make_order(weight_kg=500.0) for _ in range(10)]
        vehicles = [_make_vehicle(max_kg=1400)]
        need = expected_vehicle_need(orders, vehicles)
        assert need > 1.0

    def test_returns_float(self):
        orders = [_make_order()]
        vehicles = [_make_vehicle()]
        assert isinstance(expected_vehicle_need(orders, vehicles), float)


# ═════════════════════════════════════════════════════════════════════════════
#  build_data_model
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildDataModel:
    """
    build_data_model přijímá hotové numpy matice — žádné volání OSRM.
    Testujeme s miniaturní 3×3 maticí (depot + 2 zastávky) a 1 vozidlem.
    """

    def _build(self, orders=None, vehicles=None):
        if orders is None:
            orders = [
                _make_order(time_from="08:00", time_to="12:00", weight_kg=300, service_sec=900),
                _make_order(time_from="10:00", time_to="16:00", weight_kg=600, service_sec=1200),
            ]
        if vehicles is None:
            vehicles = [_make_vehicle()]
        n = len(orders) + 1  # +1 pro depot
        dist = _identity_matrix(n)
        dur  = _identity_matrix(n)
        durations_min_list = [dur for _ in vehicles]
        return build_data_model(orders, vehicles, dist, durations_min_list)

    def test_returns_dict(self):
        data = self._build()
        assert isinstance(data, dict)

    def test_required_keys_present(self):
        data = self._build()
        required = {"dist_int", "time_int_list", "time_windows", "demands",
                    "service_times", "capacities", "num_vehicles", "depot",
                    "max_dur_min", "cost_scale"}
        assert required.issubset(data.keys())

    def test_depot_is_zero(self):
        assert self._build()["depot"] == 0

    def test_num_vehicles_matches_input(self):
        data = self._build(vehicles=[_make_vehicle(), _make_vehicle()])
        assert data["num_vehicles"] == 2

    def test_time_windows_length_is_orders_plus_depot(self):
        orders = [_make_order(), _make_order()]
        data = self._build(orders=orders)
        assert len(data["time_windows"]) == len(orders) + 1

    def test_demands_first_element_is_zero(self):
        # Index 0 = depot → nulová poptávka
        data = self._build()
        assert data["demands"][0] == 0

    def test_demands_length_equals_time_windows(self):
        data = self._build()
        assert len(data["demands"]) == len(data["time_windows"])

    def test_service_times_first_element_is_zero(self):
        data = self._build()
        assert data["service_times"][0] == 0

    def test_tw_expansion_applied(self):
        """Časová okna musí být rozšířena o tw_expand_before/after z CONFIG."""
        orders = [_make_order(time_from="08:00", time_to="12:00")]
        data = self._build(orders=orders)
        tw = data["time_windows"]
        raw_from = time_to_minutes("08:00")
        raw_to   = time_to_minutes("12:00")
        before   = CONFIG.get("tw_expand_before_min", 0)
        after    = CONFIG.get("tw_expand_after_min", 0)
        # Index 1 = první objednávka
        assert tw[1][0] == max(0, raw_from - before)
        assert tw[1][1] == raw_to + after

    def test_dist_int_scaled_by_100(self):
        """Vzdálenosti se přenásobí 100 pro integer reprezentaci."""
        orders = [_make_order()]
        vehicles = [_make_vehicle()]
        n = len(orders) + 1
        dist = np.ones((n, n), dtype=float) * 2.5   # 2.5 km
        np.fill_diagonal(dist, 0)
        durations = [_identity_matrix(n) for _ in vehicles]
        data = build_data_model(orders, vehicles, dist, durations)
        # 2.5 km × 100 = 250
        assert data["dist_int"][0][1] == 250

    def test_time_int_list_has_one_matrix_per_vehicle(self):
        vehicles = [_make_vehicle(), _make_vehicle()]
        data = self._build(vehicles=vehicles)
        assert len(data["time_int_list"]) == 2

    def test_cost_scale_is_100(self):
        assert self._build()["cost_scale"] == 100

    def test_capacities_match_vehicles(self):
        vehicles = [_make_vehicle(max_kg=1400), _make_vehicle(max_kg=800)]
        data = self._build(vehicles=vehicles)
        assert data["capacities"] == [1400, 800]


# ═════════════════════════════════════════════════════════════════════════════
#  _sanitize_matrix — detekce NaN/inf v OSRM/ORS maticích
# ═════════════════════════════════════════════════════════════════════════════

def _sample_locations(n):
    """n dvojic (lat, lon) — deterministické pro předvídatelné výpisy v testech."""
    return [(50.0 + i * 0.01, 14.0 + i * 0.01) for i in range(n)]


class TestSanitizeMatrix:
    def test_clean_matrix_unchanged(self):
        """Matice bez NaN/inf projde beze změny."""
        dur = np.array([[0.0, 10.0, 20.0],
                        [10.0, 0.0, 15.0],
                        [20.0, 15.0, 0.0]])
        dist = dur.copy()
        out_dur, out_dist = _sanitize_matrix(dur.copy(), dist.copy(),
                                             _sample_locations(3), "driving")
        assert np.array_equal(out_dur, dur)
        assert np.array_equal(out_dist, dist)

    def test_nan_on_diagonal_ignored(self):
        """Diagonála se stejně přepíše na 0 — NaN tam nesmí triggerovat fail."""
        dur = np.array([[np.nan, 10.0, 20.0],
                        [10.0, np.nan, 15.0],
                        [20.0, 15.0, np.nan]])
        dist = np.zeros_like(dur)
        # Nesmí vyhodit SystemExit ani varovat (diagonála ignorována)
        out_dur, _ = _sanitize_matrix(dur, dist,
                                      _sample_locations(3), "driving")
        # Off-diagonal hodnoty zůstaly
        assert out_dur[0, 1] == 10.0
        assert out_dur[1, 2] == 15.0

    def test_single_nan_replaced_with_sentinel(self, monkeypatch):
        """NaN pár se nahradí sentinelem. Práh je zde vypnutý — testujeme
        NÁHRADU, ne limit (jinak by test padal při každém ladění prahu)."""
        monkeypatch.setattr(solver_mod, "FORCE_MATRIX", True)
        n = 15
        dur = np.ones((n, n), dtype=float) * 10.0
        np.fill_diagonal(dur, 0.0)
        dur[2, 5] = np.nan
        dist = np.ones_like(dur) * 5.0
        np.fill_diagonal(dist, 0.0)
        out_dur, _ = _sanitize_matrix(dur, dist,
                                      _sample_locations(n), "driving")
        # NaN pár byl nahrazen sentinelem
        assert out_dur[2, 5] == UNREACHABLE_TIME_MIN
        # Ostatní hodnoty nezměněné
        assert out_dur[0, 1] == 10.0

    def test_inf_treated_same_as_nan(self, monkeypatch):
        """+inf a -inf jsou také 'bad' — nahrazují se sentinelem."""
        monkeypatch.setattr(solver_mod, "FORCE_MATRIX", True)
        n = 15
        dur = np.ones((n, n), dtype=float) * 10.0
        np.fill_diagonal(dur, 0.0)
        dur[1, 3] = np.inf
        dur[4, 7] = -np.inf
        dist = np.ones_like(dur) * 5.0
        np.fill_diagonal(dist, 0.0)
        out_dur, _ = _sanitize_matrix(dur, dist,
                                      _sample_locations(n), "driving")
        assert out_dur[1, 3] == UNREACHABLE_TIME_MIN
        assert out_dur[4, 7] == UNREACHABLE_TIME_MIN

    def test_above_threshold_raises_systemexit(self):
        """Víc než 1 % rozbitých párů → hard fail."""
        n = 10  # 90 off-diag entries → threshold = 0.9 párů
        dur = np.ones((n, n), dtype=float) * 10.0
        np.fill_diagonal(dur, 0.0)
        # 5 NaN = 5/90 ≈ 5.5 % > 1 %
        for (i, j) in [(0, 1), (0, 2), (1, 3), (2, 4), (3, 5)]:
            dur[i, j] = np.nan
        dist = np.ones_like(dur) * 5.0
        with pytest.raises(SystemExit) as exc_info:
            _sanitize_matrix(dur, dist, _sample_locations(n), "driving")
        # Chybová hláška obsahuje info o profilu a počtu
        assert "driving" in str(exc_info.value)

    def test_distances_nan_also_replaced(self, monkeypatch):
        """NaN v distance matrix (ne v durations) se taky nahrazuje."""
        monkeypatch.setattr(solver_mod, "FORCE_MATRIX", True)
        n = 15
        dur = np.ones((n, n), dtype=float) * 10.0
        np.fill_diagonal(dur, 0.0)
        dist = np.ones_like(dur) * 5.0
        np.fill_diagonal(dist, 0.0)
        dist[0, 1] = np.nan
        # dur je čistá → nespustí se durations warning, ale distances se sanitizuje
        _, out_dist = _sanitize_matrix(dur, dist,
                                       _sample_locations(n), "driving")
        assert np.isfinite(out_dist[0, 1])
        assert out_dist[0, 1] == UNREACHABLE_TIME_MIN

    def test_cross_matrix_consistency_nan_only_in_distances(self, monkeypatch):
        """
        Pokud je pár rozbitý v distance (ale ne v duration), musí se OBĚ matice
        nastavit na sentinel na stejné pozici — jinak by solver viděl
        protimluv: finite time + infinite distance.
        """
        monkeypatch.setattr(solver_mod, "FORCE_MATRIX", True)
        n = 15
        dur = np.ones((n, n), dtype=float) * 10.0
        np.fill_diagonal(dur, 0.0)
        dist = np.ones_like(dur) * 5.0
        np.fill_diagonal(dist, 0.0)
        dist[3, 7] = np.nan
        out_dur, out_dist = _sanitize_matrix(dur, dist,
                                             _sample_locations(n), "driving")
        # Obě matice mají sentinel na stejné pozici
        assert out_dist[3, 7] == UNREACHABLE_TIME_MIN
        assert out_dur[3, 7] == UNREACHABLE_TIME_MIN
        # Ostatní pozice nezměněné
        assert out_dur[0, 1] == 10.0
        assert out_dist[0, 1] == 5.0

    def test_cross_matrix_consistency_nan_only_in_durations(self, monkeypatch):
        """Symetricky: NaN jen v durations → sentinel v obou maticích."""
        monkeypatch.setattr(solver_mod, "FORCE_MATRIX", True)
        n = 15
        dur = np.ones((n, n), dtype=float) * 10.0
        np.fill_diagonal(dur, 0.0)
        dist = np.ones_like(dur) * 5.0
        np.fill_diagonal(dist, 0.0)
        dur[4, 8] = np.nan
        out_dur, out_dist = _sanitize_matrix(dur, dist,
                                             _sample_locations(n), "driving")
        assert out_dur[4, 8] == UNREACHABLE_TIME_MIN
        assert out_dist[4, 8] == UNREACHABLE_TIME_MIN

    def test_sentinel_constant_is_large(self):
        """Sentinel musí být dost velký aby OR-Tools nepoužil hranu,
        ale musí být v rámci int32 (aby .astype(int) neoverflowovalo)."""
        assert UNREACHABLE_TIME_MIN > 100_000      # ≈ 1666 hodin+
        assert UNREACHABLE_TIME_MIN < 2**31 - 1    # int32 max
        # Prakticky reprezentuje "prohibitivně drahé"
        assert UNREACHABLE_TIME_MIN / 60 > 1000    # víc než 1000 hodin

    def test_fail_threshold_is_small_pct(self):
        """Hard-fail práh má být malý (< 10 %), jinak by maskoval problémy."""
        assert 0 < UNREACHABLE_MATRIX_FAIL_PCT < 1.0


# ═════════════════════════════════════════════════════════════════════════════
#  build_data_model — defense-in-depth proti NaN v time matrix
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildDataModelNaNSafety:
    """
    Pokud by NaN prokázaly do build_data_model (bug jinde v pipeline),
    nesmí solver spadnout na .astype(int) → musí fallback na sentinel.
    """

    def test_nan_in_duration_matrix_replaced_not_crash(self):
        orders = [_make_order(), _make_order()]
        vehicles = [_make_vehicle()]
        n = len(orders) + 1
        dist = _identity_matrix(n)
        dur = _identity_matrix(n)
        # Injektovat NaN (simuluje bug jinde v pipeline)
        dur[0, 1] = np.nan
        data = build_data_model(orders, vehicles, dist, [dur])
        # Bez crashe — hodnota je konečný integer (sentinel)
        val = data["time_int_list"][0][0][1]
        assert isinstance(val, int)
        assert val > 0
        assert val >= UNREACHABLE_TIME_MIN - 1   # zaokrouhlení tolerováno

    def test_inf_in_duration_matrix_replaced_not_crash(self):
        orders = [_make_order()]
        vehicles = [_make_vehicle()]
        n = len(orders) + 1
        dist = _identity_matrix(n)
        dur = _identity_matrix(n)
        dur[0, 1] = np.inf
        data = build_data_model(orders, vehicles, dist, [dur])
        val = data["time_int_list"][0][0][1]
        assert isinstance(val, int)
        assert val > 0   # ne INT_MIN ani 0

    def test_no_nan_no_warning_clean_fast_path(self):
        """Čistá matice projde bez jakéhokoliv zásahu (fast path)."""
        orders = [_make_order()]
        vehicles = [_make_vehicle()]
        n = len(orders) + 1
        dist = _identity_matrix(n)
        dur = np.ones((n, n), dtype=float) * 5.0
        np.fill_diagonal(dur, 0.0)
        data = build_data_model(orders, vehicles, dist, [dur])
        # 5.0 min → int → 5
        assert data["time_int_list"][0][0][1] == 5

    def test_nan_in_distance_matrix_replaced_not_crash(self):
        """
        Symetricky s duration: NaN v distance matrix by bez defense-in-depth
        produkovalo INT_MIN po .astype(int). Ověřujeme, že fallback funguje.
        """
        orders = [_make_order(), _make_order()]
        vehicles = [_make_vehicle()]
        n = len(orders) + 1
        dist = _identity_matrix(n)
        dist[0, 1] = np.nan
        dur = _identity_matrix(n)
        data = build_data_model(orders, vehicles, dist, [dur])
        val = data["dist_int"][0][1]
        # Výsledek je validní positive int, ne INT_MIN
        assert isinstance(val, int)
        assert val > 0

    def test_inf_in_distance_matrix_replaced_not_crash(self):
        orders = [_make_order()]
        vehicles = [_make_vehicle()]
        n = len(orders) + 1
        dist = _identity_matrix(n)
        dist[0, 1] = np.inf
        dur = _identity_matrix(n)
        data = build_data_model(orders, vehicles, dist, [dur])
        val = data["dist_int"][0][1]
        assert isinstance(val, int)
        assert val > 0


# ═════════════════════════════════════════════════════════════════════════════
#  run log — parametr log_path (--run-log-path, predikční režim)
# ═════════════════════════════════════════════════════════════════════════════

class TestRunLogPath:
    def _record(self, zone="CB", date="2026-07-14"):
        return {"run_id": "t", "input": {"zone": zone, "delivery_date": date},
                "results": {"total_cost_kc": 1}}

    def test_append_writes_to_custom_path(self, tmp_path):
        from vrp_solver_lines_v6 import append_run_log
        log = tmp_path / "sub" / "run_log.jsonl"          # rodič neexistuje → vytvoří
        append_run_log(self._record(), log_path=log)
        append_run_log(self._record(), log_path=log)
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_load_previous_from_custom_path(self, tmp_path):
        from vrp_solver_lines_v6 import append_run_log, _load_previous_run
        log = tmp_path / "run_log.jsonl"
        append_run_log(self._record(zone="CB"), log_path=log)
        append_run_log(self._record(zone="HK"), log_path=log)
        rec = _load_previous_run("CB", "2026-07-14", log_path=log)
        assert rec is not None
        assert rec["input"]["zone"] == "CB"
        assert _load_previous_run("PR", "2026-07-14", log_path=log) is None

    def test_load_previous_missing_file_none(self, tmp_path):
        from vrp_solver_lines_v6 import _load_previous_run
        assert _load_previous_run("CB", "2026-07-14",
                                  log_path=tmp_path / "neni.jsonl") is None


class TestOrdersFileMeta:
    def test_standard_name(self):
        from vrp_solver_lines_v6 import orders_file_meta
        assert orders_file_meta("orders_CB_2026-07-15.csv") == ("CB", "2026-07-15")

    def test_nonmatching_name(self):
        from vrp_solver_lines_v6 import orders_file_meta
        assert orders_file_meta("neco_jineho.csv") == ("", "")


class TestUnreachableThresholds:
    """Práh nedosažitelných párů je PER PROFIL. Bezpečnost nedělá práh,
    ale sentinel proti stropu délky trasy."""

    def test_driving_is_strict(self):
        # dodávky dojedou všude — naměřeno 0,000 % na všech 4 depech
        from vrp_solver_lines_v6 import unreachable_fail_pct
        assert unreachable_fail_pct("driving") == pytest.approx(0.001)

    def test_driving_rejects_single_isolated_node(self):
        # 1 izolovaný bod = 0,94–1,36 % matice (dle velikosti depa),
        # což je záměrně NAD prahem — taková objednávka je neobsloužitelná
        from vrp_solver_lines_v6 import unreachable_fail_pct
        for n in (147, 213):                      # nejmenší a největší depo
            one_node_pct = 2 * (n - 1) / (n * n - n)
            assert one_node_pct > unreachable_fail_pct("driving")

    def test_hgv_is_looser(self):
        # kamiony legitimně nedojedou do center měst (CB 1,14 %, PR 2,04 %)
        from vrp_solver_lines_v6 import unreachable_fail_pct
        assert unreachable_fail_pct("driving-hgv") == pytest.approx(0.05)

    def test_hgv_threshold_covers_measured_reality(self):
        from vrp_solver_lines_v6 import unreachable_fail_pct
        assert unreachable_fail_pct("driving-hgv") > 0.0228   # nejhorší naměřené

    def test_unknown_profile_falls_back_to_default(self):
        from vrp_solver_lines_v6 import unreachable_fail_pct
        assert unreachable_fail_pct("neznamy") == pytest.approx(0.001)

    def test_force_matrix_disables_all_profiles(self, monkeypatch):
        import vrp_solver_lines_v6 as solver
        monkeypatch.setattr(solver, "FORCE_MATRIX", True)
        assert solver.unreachable_fail_pct("driving") == 1.0
        assert solver.unreachable_fail_pct("driving-hgv") == 1.0

    def test_sentinel_exceeds_route_duration_cap(self):
        # Jádro bezpečnosti: nedosažitelný úsek se do trasy nevejde,
        # takže ho solver nepoužije ani při vysokém prahu.
        from vrp_solver_lines_v6 import UNREACHABLE_TIME_MIN, CONFIG
        assert UNREACHABLE_TIME_MIN > CONFIG["latest_return_h"] * 60


class TestLoadOrdersDayRamp:
    """Rampa (0/1) z prepared CSV — pasivní pole, jen se veze do výstupů.
    Musí být VOLITELNÁ: starší prepared soubory (do 12.8.2026) sloupec nemají."""

    HEADER = ("order_number,location_code,customer_name,block_id,time_from,"
              "time_to,payload_raw,weight_kg,lat,lon,city,note,service_sec")
    ROW = ("O1,loc1,Firma,CB,08:00,12:00,KG:100#SEC:300,100.0,"
           "49.4,15.6,Jihlava,,300")

    def _load(self, tmp_path, header, row):
        from vrp_solver_lines_v6 import load_orders_day
        p = tmp_path / "orders_CB_2026-08-13.csv"
        p.write_text(header + "\n" + row + "\n", encoding="utf-8")
        return load_orders_day(str(p))

    def test_ramp_one(self, tmp_path):
        orders = self._load(tmp_path, self.HEADER + ",ramp", self.ROW + ",1")
        assert orders[0]["ramp"] == 1

    def test_ramp_zero(self, tmp_path):
        orders = self._load(tmp_path, self.HEADER + ",ramp", self.ROW + ",0")
        assert orders[0]["ramp"] == 0

    def test_missing_ramp_column_defaults_to_zero(self, tmp_path):
        # zpětná kompatibilita se starými prepared soubory
        orders = self._load(tmp_path, self.HEADER, self.ROW)
        assert orders[0]["ramp"] == 0


class TestDriverBreaks:
    """--driver-breaks: EU zjednodušeně — žádných 4,5 h jízdy bez 45 min
    pauzy. Malý reálný solve (1 auto, 2 zastávky, jízda 6 h > limit)."""

    def _solve(self, breaks_enabled):
        from vrp_solver_lines_v6 import solve_cluster
        orders = [
            _make_order(lat=50.0, lon=14.0, time_from="00:00", time_to="23:00",
                        weight_kg=100.0, service_sec=60),
            _make_order(lat=51.0, lon=15.0, time_from="00:00", time_to="23:00",
                        weight_kg=100.0, service_sec=60),
        ]
        for i, o in enumerate(orders):
            o["id"] = o["order_number"] = f"O{i}"
            o["name"] = f"stop{i}"
        vehicles = [{"id": "TYPE_05_01", "type": "kamion", "type_code": "TYPE_05",
                     "max_kg": 8000, "cost_per_km": 28.0, "start_cost": 1000,
                     "osrm_profile": "driving-hgv", "time_multiplier": 1.0}]
        # jízda: sklad->A 150 min, A->B 150, B->sklad 120  (celkem 420 > 270)
        dist = [[0, 100, 200], [100, 0, 100], [200, 100, 0]]
        times = [[0, 150, 260], [150, 0, 150], [260, 120, 0]]
        old = solver_mod.CONFIG.get("_driver_breaks_enabled")
        solver_mod.CONFIG["_driver_breaks_enabled"] = breaks_enabled
        try:
            routes, _ = solve_cluster(orders, vehicles, dist, [times],
                                      time_limit_sec=3)
        finally:
            if old is None:
                solver_mod.CONFIG.pop("_driver_breaks_enabled", None)
            else:
                solver_mod.CONFIG["_driver_breaks_enabled"] = old
        return routes

    def test_breaks_extend_route_duration(self):
        base = self._solve(breaks_enabled=False)
        with_breaks = self._solve(breaks_enabled=True)
        assert base and with_breaks
        # 420 min jízdy => aspoň jedna 45min pauza navíc v trvání trasy
        assert with_breaks[0]["duration_h"] >= base[0]["duration_h"] + 0.7

    def test_default_off_deterministic(self):
        # bez flagu je malý model deterministický — cesta pauz se nedotkla
        a = self._solve(breaks_enabled=False)
        b = self._solve(breaks_enabled=False)
        assert a[0]["duration_h"] == b[0]["duration_h"]
        assert a[0]["total_km"] == b[0]["total_km"]

    # ── denní limit jízdy (Drive dimenze) + regres indexování pauz ────────

    def _solve_drive(self, times, breaks_enabled=True, max_drive_h=None,
                     n_vehicles=1):
        """Model s libovolnou časovou maticí (minuty). Vrací routes nebo []."""
        from vrp_solver_lines_v6 import solve_cluster
        n = len(times) - 1
        orders = []
        for i in range(n):
            o = _make_order(lat=50.0 + i * 0.5, lon=14.0, time_from="00:00",
                            time_to="23:00", weight_kg=100.0, service_sec=60)
            o["id"] = o["order_number"] = f"O{i}"
            o["name"] = f"stop{i}"
            orders.append(o)
        vehicles = [{"id": f"TYPE_05_{k+1:02d}", "type": "kamion",
                     "type_code": "TYPE_05", "max_kg": 8000,
                     "cost_per_km": 28.0, "start_cost": 1000,
                     "osrm_profile": "driving-hgv", "time_multiplier": 1.0}
                    for k in range(n_vehicles)]
        dist = [[t / 1.0 for t in row] for row in times]      # 1 km/min — jedno
        saved = {k: solver_mod.CONFIG.get(k)
                 for k in ("_driver_breaks_enabled", "driver_max_drive_h")}
        solver_mod.CONFIG["_driver_breaks_enabled"] = breaks_enabled
        if max_drive_h is not None:
            solver_mod.CONFIG["driver_max_drive_h"] = max_drive_h
        try:
            routes, _ = solve_cluster(orders, vehicles, dist,
                                      [times] * n_vehicles, time_limit_sec=2)
        finally:
            for k, v in saved.items():
                if v is None:
                    solver_mod.CONFIG.pop(k, None)
                else:
                    solver_mod.CONFIG[k] = v
        return routes

    def test_drive_over_daily_limit_is_infeasible(self):
        # sklad -> A 300 min, A -> B 300, B -> sklad 300 = 15 h čisté jízdy;
        # okna dovolí (00:00–23:00), pauzy by se vešly — ale 15 h > 9 h
        times = [[0, 300, 600], [300, 0, 300], [600, 300, 0]]
        assert self._solve_drive(times, breaks_enabled=True, max_drive_h=9.0) == []
        # bez režimu řidiče trasa existuje (jen okna a strop 23,5 h)
        assert self._solve_drive(times, breaks_enabled=False)

    def test_drive_under_daily_limit_is_feasible(self):
        # trojúhelník 150/150/150 = 7,5 h jízdy < 9 h; s pauzami trasa vyjde
        times = [[0, 150, 150], [150, 0, 150], [150, 150, 0]]
        routes = self._solve_drive(times, breaks_enabled=True, max_drive_h=9.0)
        assert routes and len(routes) == 1

    def test_drive_limit_splits_across_trucks(self):
        # 2 zastávky, každá 4 h od skladu a 8 h od sebe: jeden kamion
        # 4+8+4 = 16 h jízdy nesmí; dva kamiony po 8 h ano
        times = [[0, 240, 240], [240, 0, 480], [240, 480, 0]]
        assert self._solve_drive(times, True, 9.0, n_vehicles=1) == []
        routes = self._solve_drive(times, True, 9.0, n_vehicles=2)
        assert len(routes) == 2

    def test_break_transits_indexed_by_routing_index(self):
        # Regres 1.5: node_visit_transits musí mít délku routing.Size()
        # (uzly + start/end per vozidlo). Ověřuje se přímo na modelu —
        # dřív se předával seznam v prostoru uzlů (kratší) → UB v OR-Tools.
        from ortools.constraint_solver import pywrapcp
        from vrp_solver_lines_v6 import _add_driver_breaks, build_data_model
        orders = []
        for i in range(3):
            o = _make_order(lat=50.0 + i * 0.1, lon=14.0, service_sec=600)
            o["id"] = o["order_number"] = f"O{i}"
            orders.append(o)
        vehicles = [{"id": f"V{k}", "type": "kamion", "type_code": "TYPE_05",
                     "max_kg": 8000, "cost_per_km": 28.0, "start_cost": 0,
                     "osrm_profile": "driving-hgv", "time_multiplier": 1.0}
                    for k in range(2)]
        times = [[0, 10, 20, 30], [10, 0, 10, 20], [20, 10, 0, 10], [30, 20, 10, 0]]
        data = build_data_model(orders, vehicles, times, [times, times])
        n = len(data["demands"])
        manager = pywrapcp.RoutingIndexManager(n, 2, 0)
        routing = pywrapcp.RoutingModel(manager)
        cb = routing.RegisterTransitCallback(
            lambda fi, ti: times[manager.IndexToNode(fi)][manager.IndexToNode(ti)])
        routing.AddDimension(cb, 60, 1440, False, "Time")
        time_dim = routing.GetDimensionOrDie("Time")
        captured = {}
        orig = time_dim.SetBreakIntervalsOfVehicle

        def spy(intervals, v_idx, transits):
            captured[v_idx] = list(transits)
            return orig(intervals, v_idx, transits)
        time_dim.SetBreakIntervalsOfVehicle = spy
        _add_driver_breaks(routing, manager, time_dim, data)
        assert set(captured) == {0, 1}
        for tr in captured.values():
            assert len(tr) == routing.Size()          # ne len(orders)+1
            # start/end indexy 0, zákaznické uzly = servis (10 min)
            for idx in range(routing.Size()):
                if routing.IsStart(idx) or routing.IsEnd(idx):
                    assert tr[idx] == 0
                else:
                    assert tr[idx] == 10


# ═════════════════════════════════════════════════════════════════════════════
#  Audit 2.12: print_run_diff nesmí shodit běh po uložení výstupů
#  Audit 2.1:  latest_return_h (nejzazší návrat), ne „délka trasy"
# ═════════════════════════════════════════════════════════════════════════════

def _saved_route():
    return {
        "vehicle_id": "TYPE_02_01", "vehicle_type": "Dodávka", "type_code": "TYPE_02",
        "cost_per_km": 11.0, "total_km": 42.0, "total_kg": 300.0,
        "total_kc": 1462.0, "duration_h": 3.5,
        "stops": [
            {"stop": "Sklad", "arrival": "06:00", "departure": "06:00", "kg": 0},
            {"stop": "Z1", "id": "O1", "location_code": "l1", "arrival": "07:00",
             "departure": "07:10", "kg": 300, "lat": 49.4, "lon": 15.6,
             "service_min": 10, "window": "06:00-10:00", "leg_km": 21.0},
            {"stop": "Sklad (návrat)", "arrival": "08:00", "departure": "08:00", "kg": 0},
        ],
    }


class TestRunDiffGuard:
    def test_print_run_diff_exception_does_not_propagate(self, tmp_path, monkeypatch, capsys):
        from vrp_solver_lines_v6 import save_outputs
        # předchozí záznam bez klíče 'results' → print_run_diff by vyhodil KeyError
        log = tmp_path / "run_log.jsonl"
        log.write_text(json.dumps({"run_id": "old", "input": {
            "zone": "CB", "delivery_date": "2026-08-17"}}) + "\n", encoding="utf-8")
        monkeypatch.setattr(solver_mod, "find_vehicle_types_file",
                            lambda: str(tmp_path / "vt.csv"))
        (tmp_path / "vt.csv").write_text(
            "type_code;type_name;max_kg;valid_for_date\nTYPE_02;Dodávka;1350;20260817\n",
            encoding="utf-8")
        out = tmp_path / "out"
        save_outputs([_saved_route()], 1462.0, out, "CB", 1.0,
                     orders=[], delivery_date="2026-08-17", closures=[],
                     run_log_path=log)                        # nesmí vyhodit
        assert (out / "lines_summary.csv").exists()
        assert (out / "zone_summary.json").exists()
        text = capsys.readouterr().out
        assert "Porovnání s minulým během se nepovedlo" in text
        # nový záznam se přesto zapsal
        assert sum(1 for _ in open(log, encoding="utf-8")) == 2

    def test_print_run_diff_itself_still_reports_when_valid(self, capsys):
        from vrp_solver_lines_v6 import print_run_diff
        base = {"run_id": "x", "results": {"total_cost_kc": 100.0, "lines_count": 2,
                                           "total_km": 10.0, "total_hours": 1.0,
                                           "avg_kg_per_line": 50.0, "avg_km_per_line": 5.0,
                                           "vehicle_type_mix": {}, "elapsed_min": 1.0,
                                           "output_dir": "", "finalists": []}}
        cur = json.loads(json.dumps(base)); cur["results"]["total_cost_kc"] = 90.0
        print_run_diff(cur, base)
        assert "SROVNÁNÍ" in capsys.readouterr().out


class TestLatestReturnRename:
    def test_latest_return_key_present_and_old_key_absent(self):
        assert "latest_return_h" in CONFIG
        assert "max_route_duration_h" not in CONFIG
        # run log záznam používá nový klíč
        from vrp_solver_lines_v6 import _build_run_record
        rec = _build_run_record([_saved_route()], 1462.0, Path("x"), "CB",
                                "2026-08-17", 1.0, [], [])
        assert "latest_return_h" in rec["config"]
        assert "max_route_duration_h" not in rec["config"]

    def test_route_returning_after_latest_return_infeasible(self):
        # zastávka s oknem 23:00–23:30: s nejzazším návratem 22:00 řešení
        # neexistuje, s 23,5 ano — parametr omezuje NÁVRAT, ne délku trasy
        from vrp_solver_lines_v6 import solve_cluster
        o = _make_order(time_from="23:00", time_to="23:30", service_sec=60)
        o["id"] = o["order_number"] = "OL"; o["name"] = "late"
        veh = [{"id": "TYPE_02_01", "type": "d", "type_code": "TYPE_02",
                "max_kg": 1350, "cost_per_km": 11.0, "start_cost": 1000,
                "osrm_profile": "driving", "time_multiplier": 1.0}]
        dist = [[0, 10], [10, 0]]; times = [[0, 10], [10, 0]]
        saved = CONFIG["latest_return_h"]
        try:
            CONFIG["latest_return_h"] = 22.0
            # okno leží celé za nejzazším návratem: OR-Tools buď nenajde
            # řešení, nebo model vůbec nesestaví (CP Solver fail) — obojí
            # znamená „neřešitelné"; v ostrém běhu to chytí
            # validate_orders_servable (bod 5) dřív, než se sem dojde
            try:
                assert solve_cluster([o], veh, dist, [times], 2)[0] == []
            except Exception as e:                    # noqa: BLE001
                assert "fail" in str(e).lower()
            CONFIG["latest_return_h"] = 23.5
            assert solve_cluster([o], veh, dist, [times], 2)[0]
        finally:
            CONFIG["latest_return_h"] = saved


# ═════════════════════════════════════════════════════════════════════════════
#  Audit 2.11: záchranný re-solve boxovaný budgetem, paralelní, volitelné
#  druhé kolo (--rescue-extra-min)
# ═════════════════════════════════════════════════════════════════════════════

class TestRescueBudget:
    def test_rescue_time_capped_by_remaining_budget(self):
        from vrp_solver_lines_v6 import rescue_time_for
        assert rescue_time_for(100, None) == 300          # bez stropu 3×
        assert rescue_time_for(100, 1000) == 300          # zbývá dost
        assert rescue_time_for(100, 120) == 120           # strop = zbytek
        assert rescue_time_for(100, 0) == 0               # nic nezbývá
        assert rescue_time_for(100, -50) == 0             # nikdy záporné

    def _fake_cluster_env(self, monkeypatch, outcomes):
        """Nahradí worker: outcomes = {(cluster_idx): routes|[]} — a zaznamená
        všechny submitované úlohy."""
        submitted = []

        class FakeFuture:
            def __init__(self, res): self._res = res
            def result(self): return self._res

        class FakeExecutor:
            def __init__(self, max_workers=None): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def submit(self, fn, args):
                submitted.append(args)
                routes = outcomes.get(args["cluster_idx"], [])
                return FakeFuture({"seed_name": args["seed_name"],
                                   "cluster_idx": args["cluster_idx"],
                                   "routes": routes, "cost": 100 + args["cluster_idx"],
                                   "strategy": args.get("strategy")})
        monkeypatch.setattr(solver_mod, "ProcessPoolExecutor", FakeExecutor)
        monkeypatch.setattr(solver_mod, "as_completed", lambda d: list(d))
        return submitted

    def _clusters(self):
        o = lambda i: {"id": f"O{i}", "order_number": f"O{i}", "name": f"O{i}",
                       "customer_name": "", "lat": 50.0, "lon": 14.0,
                       "time_from": "06:00", "time_to": "10:00",
                       "weight_kg": 100.0, "service_sec": 60}
        clusters = [[o(1), o(2)], [o(3)]]
        c_indices = [[0, 1], [2]]            # indexy objednávek (uzel = idx + 1)
        veh = {"id": "V1", "type_code": "TYPE_02", "max_kg": 1350,
               "cost_per_km": 11.0, "start_cost": 1000,
               "osrm_profile": "driving", "time_multiplier": 1.0}
        asg = [[veh], [dict(veh, id="V2")]]
        dist = np.ones((4, 4)); times = {"V1": np.ones((4, 4)), "V2": np.ones((4, 4))}
        return clusters, c_indices, asg, dist, times

    def test_rescue_runs_parallel_over_unsolved(self, monkeypatch):
        from vrp_solver_lines_v6 import RESCUE_STRATEGIES, _rescue_unsolved_parallel
        submitted = self._fake_cluster_env(monkeypatch, {0: [{"r": 1}], 1: [{"r": 2}]})
        clusters, c_ix, asg, dist, times = self._clusters()
        found = _rescue_unsolved_parallel([0, 1], "sweep", clusters, c_ix, asg,
                                          dist, times, rescue_time=42, n_workers=4)
        # oba clustery × obě strategie submitované NAJEDNOU, každá se stejným časem
        assert len(submitted) == 2 * len(RESCUE_STRATEGIES)
        assert {a["time_limit_sec"] for a in submitted} == {42}
        assert set(found) == {0, 1}

    def test_rescue_returns_only_solved(self, monkeypatch):
        from vrp_solver_lines_v6 import _rescue_unsolved_parallel
        self._fake_cluster_env(monkeypatch, {0: [{"r": 1}]})      # cluster 1 nevyjde
        clusters, c_ix, asg, dist, times = self._clusters()
        found = _rescue_unsolved_parallel([0, 1], "sweep", clusters, c_ix, asg,
                                          dist, times, rescue_time=30, n_workers=4)
        assert set(found) == {0}

    def test_cli_has_rescue_extra_min_default_zero(self):
        import subprocess, sys
        out = subprocess.run([sys.executable, "vrp_solver_lines_v6.py", "--help"],
                             capture_output=True, encoding="utf-8", errors="replace",
                             env={**__import__("os").environ, "SKIP_STARTUP_TESTS": "1",
                                  "PYTHONIOENCODING": "utf-8"}).stdout or ""
        assert "--rescue-extra-min" in out
