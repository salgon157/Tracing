"""
test_solver_core.py — unit testy pro vrp_solver_lines_v6.py

Testované funkce (pure, bez OSRM/OR-Tools volání):
  time_to_minutes, service_time_min, auto_n_clusters,
  cluster_profile, expected_vehicle_need, build_data_model
"""
import math
import pytest
import numpy as np

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

    def test_single_nan_below_threshold_replaced_with_sentinel(self):
        """1 NaN v 4×4 matici (1/12 off-diag ≈ 8.3 %) je nad 1 %, vyhodí SystemExit."""
        # Proto vytvořím větší matici kde 1 NaN bude pod prahem
        n = 15  # 15×15 = 225 celkem, 210 off-diag → 1/210 = 0.48 % < 1 %
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

    def test_inf_treated_same_as_nan(self):
        """+inf a -inf jsou také 'bad' — nahrazují se sentinelem."""
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

    def test_distances_nan_also_replaced(self):
        """NaN v distance matrix (ne v durations) se taky nahrazuje."""
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

    def test_cross_matrix_consistency_nan_only_in_distances(self):
        """
        Pokud je pár rozbitý v distance (ale ne v duration), musí se OBĚ matice
        nastavit na sentinel na stejné pozici — jinak by solver viděl
        protimluv: finite time + infinite distance.
        """
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

    def test_cross_matrix_consistency_nan_only_in_durations(self):
        """Symetricky: NaN jen v durations → sentinel v obou maticích."""
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
        assert 0 < UNREACHABLE_MATRIX_FAIL_PCT < 0.1


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
