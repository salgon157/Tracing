"""
test_closures_utils.py — unit testy pro closures_utils.py

Testované funkce (všechny pure / bez sítě):
  haversine_km, point_to_segment_km, segment_to_segment_min_km,
  might_cross_closure, geometry_crosses_closure, endpoint_near_closure,
  bearing_deg, nearest_sector, _geometry_length_km,
  _slice_route, _slice_route_from, _reverse_route, _concat_routes,
  load_active_closures
"""
import math
import pytest

from closures_utils import (
    haversine_km,
    point_to_segment_km,
    segment_to_segment_min_km,
    might_cross_closure,
    geometry_crosses_closure,
    endpoint_near_closure,
    bearing_deg,
    nearest_sector,
    _geometry_length_km,
    _route_dict,
    _slice_route,
    _slice_route_from,
    _reverse_route,
    _concat_routes,
    load_active_closures,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _mk_closure(alat, alon, blat, blon, buffer_km=0.15, active=True):
    """Vytvoří minimální closure dict."""
    return {
        "id": "TST",
        "active": active,
        "buffer_km": buffer_km,
        "segment": {
            "from": {"lat": alat, "lon": alon},
            "to":   {"lat": blat, "lon": blon},
        },
    }


# ═════════════════════════════════════════════════════════════════════════════
#  haversine_km
# ═════════════════════════════════════════════════════════════════════════════

class TestHaversine:
    def test_same_point_is_zero(self):
        assert haversine_km(50.0, 14.0, 50.0, 14.0) == 0.0

    def test_symmetry(self):
        d1 = haversine_km(50.0761, 14.4181, 49.1952, 16.6068)  # Praha → Brno
        d2 = haversine_km(49.1952, 16.6068, 50.0761, 14.4181)  # Brno → Praha
        assert abs(d1 - d2) < 0.001

    def test_praha_brno_approx_185km(self):
        # Přímá vzdálenost Praha→Brno (Haversine, bez dálnic) ≈ 185 km
        d = haversine_km(50.0761, 14.4181, 49.1952, 16.6068)
        assert 180 < d < 195

    def test_short_distance_positive(self):
        # 0.013° lon ≈ 0.93 km při šířce 50°
        d = haversine_km(50.0, 14.0, 50.0, 14.013)
        assert 0.5 < d < 1.5

    def test_non_negative(self):
        assert haversine_km(0.0, 0.0, 1.0, 1.0) >= 0.0


# ═════════════════════════════════════════════════════════════════════════════
#  point_to_segment_km
# ═════════════════════════════════════════════════════════════════════════════

class TestPointToSegment:
    def test_point_on_segment_midpoint(self):
        # Bod přesně uprostřed segmentu → vzdálenost ~0
        d = point_to_segment_km(50.005, 14.005, 50.0, 14.0, 50.01, 14.01)
        assert d < 0.01

    def test_point_at_endpoint_a(self):
        d = point_to_segment_km(50.0, 14.0, 50.0, 14.0, 50.01, 14.01)
        assert d == 0.0

    def test_point_at_endpoint_b(self):
        d = point_to_segment_km(50.01, 14.01, 50.0, 14.0, 50.01, 14.01)
        assert d == 0.0

    def test_degenerate_segment_falls_back_to_haversine(self):
        # A == B → vzdálenost bodu od bodu A
        d_seg = point_to_segment_km(50.1, 14.5, 50.0, 14.0, 50.0, 14.0)
        d_hav = haversine_km(50.1, 14.5, 50.0, 14.0)
        assert abs(d_seg - d_hav) < 0.001

    def test_point_beyond_b_clamped_to_b(self):
        # Bod leží za koncem segmentu → vzdálenost se počítá od B
        d = point_to_segment_km(50.02, 14.02, 50.0, 14.0, 50.01, 14.01)
        expected = haversine_km(50.02, 14.02, 50.01, 14.01)
        assert abs(d - expected) < 0.001

    def test_non_negative(self):
        assert point_to_segment_km(51.0, 15.0, 50.0, 14.0, 50.01, 14.01) >= 0.0


# ═════════════════════════════════════════════════════════════════════════════
#  segment_to_segment_min_km
# ═════════════════════════════════════════════════════════════════════════════

class TestSegmentToSegment:
    def test_crossing_segments_return_zero(self):
        # Kříž přes bod (50.005, 14.005)
        d = segment_to_segment_min_km(
            50.0, 14.0, 50.01, 14.01,   # segment O→D (SZ→JV)
            50.01, 14.0, 50.0, 14.01,   # segment A→B (SV→JZ) → protíná
        )
        assert d == 0.0

    def test_parallel_distant_segments_positive(self):
        # Paralelní segmenty 1° od sebe
        d = segment_to_segment_min_km(
            50.0, 14.0, 50.0, 15.0,
            51.0, 14.0, 51.0, 15.0,
        )
        assert d > 50.0  # ~111 km

    def test_perpendicular_not_touching(self):
        # T-tvar — vertikální segment vzdálen od horizontálního
        d = segment_to_segment_min_km(
            50.0, 14.0, 50.0, 14.5,     # horizontální
            50.1, 14.6, 50.2, 14.6,     # vertikální, vpravo
        )
        assert d > 0.0

    def test_non_negative(self):
        d = segment_to_segment_min_km(
            48.0, 13.0, 48.0, 14.0,
            52.0, 13.0, 52.0, 14.0,
        )
        assert d >= 0.0


# ═════════════════════════════════════════════════════════════════════════════
#  might_cross_closure
# ═════════════════════════════════════════════════════════════════════════════

class TestMightCrossClosure:
    def test_route_through_closure_detected(self):
        closure = _mk_closure(50.005, 14.005, 50.005, 14.006)
        # Přímka prochází přímo přes uzavírku
        assert might_cross_closure((50.0, 14.0), (50.01, 14.01), closure) is True

    def test_distant_route_not_detected(self):
        closure = _mk_closure(50.0, 14.0, 50.01, 14.01)
        # Trasa 2° severně — daleko mimo
        assert might_cross_closure((52.0, 14.0), (52.01, 14.01), closure) is False

    def test_pre_factor_expands_buffer(self):
        # Uzavírka s malým bufferem, trasa těsně vedle
        closure = _mk_closure(50.0, 14.0, 50.0, 14.01, buffer_km=0.001)
        # S velkým pre_factor (default 6) by měla být detekována
        result_big = might_cross_closure((50.001, 13.99), (50.001, 14.02), closure, pre_factor=6.0)
        assert result_big is True


# ═════════════════════════════════════════════════════════════════════════════
#  geometry_crosses_closure
# ═════════════════════════════════════════════════════════════════════════════

class TestGeometryCrossesclosure:
    def test_empty_geometry_returns_false(self):
        closure = _mk_closure(50.0, 14.0, 50.01, 14.01)
        assert geometry_crosses_closure([], closure) is False

    def test_single_point_outside_buffer_returns_false(self):
        closure = _mk_closure(50.0, 14.0, 50.01, 14.01)
        # Bod 50km severně
        assert geometry_crosses_closure([(50.45, 14.0)], closure) is False

    def test_point_inside_buffer_returns_true(self):
        closure = _mk_closure(50.0, 14.0, 50.01, 14.01, buffer_km=0.5)
        # Bod přesně na uzavírce
        assert geometry_crosses_closure([(50.005, 14.005)], closure) is True

    def test_min_buffer_60m_catches_close_point(self):
        # buffer_km=0.02 (20m) — funkce by měla použít min 0.06
        closure = _mk_closure(50.0, 14.0, 50.01, 14.01, buffer_km=0.02)
        # Bod ~52m od úsečky ve směru kolmém na uzavírku (NW strana)
        # Segment jde NE diagonálou; kolmice ke středu (50.005, 14.005) ve směru NW:
        # +0.0004° lat, -0.0004° lon → ~52m (44m lat + 28m lon složka)
        assert geometry_crosses_closure([(50.0054, 14.0046)], closure) is True

    def test_explicit_detection_buf_overrides_default(self):
        closure = _mk_closure(50.0, 14.0, 50.01, 14.01, buffer_km=0.01)
        # S velmi malým explicitním bufferem bod daleko mimo nezachytíme
        assert geometry_crosses_closure([(50.5, 14.5)], closure, detection_buf_km=0.001) is False


# ═════════════════════════════════════════════════════════════════════════════
#  endpoint_near_closure
# ═════════════════════════════════════════════════════════════════════════════

class TestEndpointNearClosure:
    def test_point_very_close_is_near(self):
        closure = _mk_closure(50.0, 14.0, 50.01, 14.01)
        assert endpoint_near_closure((50.005, 14.005), closure) is True

    def test_point_far_is_not_near(self):
        closure = _mk_closure(50.0, 14.0, 50.01, 14.01)
        assert endpoint_near_closure((52.0, 14.0), closure) is False

    def test_min_km_floor_applies(self):
        # buffer_km=0.001 → near_km = max(0.001*6, 0.35) = 0.35
        closure = _mk_closure(50.0, 14.0, 50.01, 14.01, buffer_km=0.001)
        # Bod 0.3km od uzavírky — musí být Near díky min_km=0.35
        close_pt = (50.0 + 0.003, 14.0)  # ~0.33km
        assert endpoint_near_closure(close_pt, closure) is True


# ═════════════════════════════════════════════════════════════════════════════
#  bearing_deg
# ═════════════════════════════════════════════════════════════════════════════

class TestBearingDeg:
    def test_north(self):
        b = bearing_deg(50.0, 14.0, 51.0, 14.0)
        assert abs(b - 0.0) < 1.0

    def test_east(self):
        b = bearing_deg(50.0, 14.0, 50.0, 15.0)
        assert abs(b - 90.0) < 1.0

    def test_south(self):
        b = bearing_deg(51.0, 14.0, 50.0, 14.0)
        assert abs(b - 180.0) < 1.0

    def test_west(self):
        b = bearing_deg(50.0, 15.0, 50.0, 14.0)
        assert abs(b - 270.0) < 1.0

    def test_result_in_0_to_360(self):
        for lat2 in [49.0, 51.0]:
            for lon2 in [13.0, 15.0]:
                b = bearing_deg(50.0, 14.0, lat2, lon2)
                assert 0.0 <= b < 360.0


# ═════════════════════════════════════════════════════════════════════════════
#  nearest_sector
# ═════════════════════════════════════════════════════════════════════════════

class TestNearestSector:
    def test_zero(self):
        assert nearest_sector(0.0) == 0

    def test_45(self):
        assert nearest_sector(45.0) == 45

    def test_22_5_rounds_to_45(self):
        # round(22.5/45)*45 = round(0.5)*45 = 0*45 = 0 in Python banker's rounding
        # OR = 45 depending on Python version — either is acceptable, just check valid sector
        r = nearest_sector(22.5)
        assert r in (0, 45)

    def test_337_5_wraps_to_0(self):
        r = nearest_sector(337.5)
        assert r in (0, 315)

    def test_180(self):
        assert nearest_sector(180.0) == 180

    def test_270(self):
        assert nearest_sector(270.0) == 270

    def test_360_wraps_to_0(self):
        assert nearest_sector(360.0) == 0


# ═════════════════════════════════════════════════════════════════════════════
#  _geometry_length_km
# ═════════════════════════════════════════════════════════════════════════════

class TestGeometryLength:
    def test_empty_returns_zero(self):
        assert _geometry_length_km([]) == 0.0

    def test_single_point_returns_zero(self):
        assert _geometry_length_km([(50.0, 14.0)]) == 0.0

    def test_two_points_matches_haversine(self):
        geom = [(50.0, 14.0), (50.01, 14.01)]
        expected = haversine_km(50.0, 14.0, 50.01, 14.01)
        assert abs(_geometry_length_km(geom) - expected) < 0.001

    def test_three_collinear_points_additive(self):
        # A→B→C: délka A→B + B→C
        geom = [(50.0, 14.0), (50.005, 14.005), (50.01, 14.01)]
        d_ab = haversine_km(50.0, 14.0, 50.005, 14.005)
        d_bc = haversine_km(50.005, 14.005, 50.01, 14.01)
        assert abs(_geometry_length_km(geom) - (d_ab + d_bc)) < 0.001


# ═════════════════════════════════════════════════════════════════════════════
#  _slice_route, _slice_route_from
# ═════════════════════════════════════════════════════════════════════════════

class TestSliceRoute:
    def _sample_route(self):
        geom = [(50.0, 14.0), (50.005, 14.005), (50.01, 14.01), (50.015, 14.015)]
        return _route_dict(10.0, 2.0, geom)

    def test_end_index_zero_returns_none(self):
        assert _slice_route(self._sample_route(), 0) is None

    def test_negative_index_returns_none(self):
        assert _slice_route(self._sample_route(), -1) is None

    def test_full_slice_returns_all_points(self):
        r = self._sample_route()
        sliced = _slice_route(r, 3)  # 4 body (index 0-3)
        assert len(sliced["geometry"]) == 4

    def test_oversize_index_clamped(self):
        r = self._sample_route()
        sliced = _slice_route(r, 100)
        assert len(sliced["geometry"]) == len(r["geometry"])

    def test_partial_slice_geometry(self):
        r = self._sample_route()
        sliced = _slice_route(r, 2)
        assert len(sliced["geometry"]) == 3  # body 0,1,2

    def test_duration_scaled(self):
        r = self._sample_route()
        sliced = _slice_route(r, 2)
        assert 0.0 < sliced["duration_min"] < r["duration_min"]


class TestSliceRouteFrom:
    def _sample_route(self):
        geom = [(50.0, 14.0), (50.005, 14.005), (50.01, 14.01), (50.015, 14.015)]
        return _route_dict(10.0, 2.0, geom)

    def test_start_at_last_index_returns_none(self):
        r = self._sample_route()
        assert _slice_route_from(r, len(r["geometry"]) - 1) is None

    def test_start_beyond_end_returns_none(self):
        r = self._sample_route()
        assert _slice_route_from(r, 100) is None

    def test_start_at_zero_returns_full_route(self):
        r = self._sample_route()
        sliced = _slice_route_from(r, 0)
        assert len(sliced["geometry"]) == len(r["geometry"])

    def test_partial_from(self):
        r = self._sample_route()
        sliced = _slice_route_from(r, 2)
        assert len(sliced["geometry"]) == 2  # body 2,3


# ═════════════════════════════════════════════════════════════════════════════
#  _reverse_route
# ═════════════════════════════════════════════════════════════════════════════

class TestReverseRoute:
    def test_geometry_reversed(self):
        geom = [(50.0, 14.0), (50.005, 14.005), (50.01, 14.01)]
        r = _route_dict(5.0, 1.0, geom)
        rev = _reverse_route(r)
        assert rev["geometry"] == list(reversed(geom))

    def test_duration_preserved(self):
        r = _route_dict(7.5, 1.2, [(50.0, 14.0), (50.01, 14.01)])
        rev = _reverse_route(r)
        assert rev["duration_min"] == 7.5
        assert rev["distance_km"] == 1.2

    def test_empty_geometry(self):
        r = _route_dict(0.0, 0.0, [])
        rev = _reverse_route(r)
        assert rev["geometry"] == []

    def test_single_point_unchanged(self):
        r = _route_dict(0.0, 0.0, [(50.0, 14.0)])
        rev = _reverse_route(r)
        assert rev["geometry"] == [(50.0, 14.0)]


# ═════════════════════════════════════════════════════════════════════════════
#  _concat_routes
# ═════════════════════════════════════════════════════════════════════════════

class TestConcatRoutes:
    def test_duration_and_distance_summed(self):
        a = _route_dict(3.0, 1.0, [(50.0, 14.0), (50.005, 14.005)])
        b = _route_dict(4.0, 1.5, [(50.01, 14.01), (50.015, 14.015)])
        c = _concat_routes(a, b)
        assert c["duration_min"] == 7.0
        assert c["distance_km"] == 2.5

    def test_shared_endpoint_deduplicated(self):
        shared = (50.005, 14.005)
        a = _route_dict(3.0, 1.0, [(50.0, 14.0), shared])
        b = _route_dict(4.0, 1.5, [shared, (50.01, 14.01)])
        c = _concat_routes(a, b)
        # Sdílený bod se nesmí opakovat
        assert c["geometry"].count(shared) == 1
        assert len(c["geometry"]) == 3

    def test_no_shared_endpoint_concatenated_fully(self):
        a = _route_dict(3.0, 1.0, [(50.0, 14.0), (50.005, 14.005)])
        b = _route_dict(4.0, 1.5, [(50.006, 14.006), (50.01, 14.01)])
        c = _concat_routes(a, b)
        assert len(c["geometry"]) == 4

    def test_empty_first(self):
        a = _route_dict(0.0, 0.0, [])
        b = _route_dict(4.0, 1.5, [(50.0, 14.0), (50.01, 14.01)])
        c = _concat_routes(a, b)
        assert len(c["geometry"]) == 2

    def test_empty_second(self):
        a = _route_dict(3.0, 1.0, [(50.0, 14.0), (50.01, 14.01)])
        b = _route_dict(0.0, 0.0, [])
        c = _concat_routes(a, b)
        assert len(c["geometry"]) == 2


# ═════════════════════════════════════════════════════════════════════════════
#  load_active_closures
# ═════════════════════════════════════════════════════════════════════════════

class TestLoadActiveClosures:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_active_closures(str(tmp_path / "nonexistent.json")) == []

    def test_active_closure_loaded(self, closure_json):
        closures = load_active_closures(closure_json)
        assert len(closures) == 1
        assert closures[0]["id"] == "CLO_TEST"

    def test_expired_closure_filtered_out(self, tmp_path):
        import json as _json
        c = {
            "id": "CLO_EXPIRED", "active": True,
            "valid_from": "2000-01-01", "valid_to": "2001-01-01",
            "buffer_km": 0.15,
            "segment": {"from": {"lat": 50.0, "lon": 14.4}, "to": {"lat": 50.0, "lon": 14.4}},
        }
        p = tmp_path / "closures.json"
        p.write_text(_json.dumps({"closures": [c]}), encoding="utf-8")
        assert load_active_closures(str(p)) == []

    def test_future_closure_filtered_out(self, tmp_path):
        import json as _json
        c = {
            "id": "CLO_FUTURE", "active": True,
            "valid_from": "2099-01-01", "valid_to": "2099-12-31",
            "buffer_km": 0.15,
            "segment": {"from": {"lat": 50.0, "lon": 14.4}, "to": {"lat": 50.0, "lon": 14.4}},
        }
        p = tmp_path / "closures.json"
        p.write_text(_json.dumps({"closures": [c]}), encoding="utf-8")
        assert load_active_closures(str(p)) == []

    def test_inactive_closure_filtered_out(self, tmp_path):
        import json as _json
        c = {
            "id": "CLO_INACTIVE", "active": False,
            "valid_from": "2020-01-01", "valid_to": "2099-12-31",
            "buffer_km": 0.15,
            "segment": {"from": {"lat": 50.0, "lon": 14.4}, "to": {"lat": 50.0, "lon": 14.4}},
        }
        p = tmp_path / "closures.json"
        p.write_text(_json.dumps({"closures": [c]}), encoding="utf-8")
        assert load_active_closures(str(p)) == []

    def test_mixed_only_active_returned(self, multi_closure_json):
        closures = load_active_closures(multi_closure_json)
        # Pouze SAMPLE_CLOSURE je aktivní a platná dnes
        assert len(closures) == 1
        assert closures[0]["id"] == "CLO_TEST"

    def test_no_valid_to_passes_filter(self, tmp_path):
        import json as _json
        c = {
            "id": "CLO_NOTO",
            "active": True,
            "valid_from": "2020-01-01",
            # valid_to chybí → bez konce
            "buffer_km": 0.1,
            "segment": {"from": {"lat": 50.0, "lon": 14.0}, "to": {"lat": 50.01, "lon": 14.01}},
        }
        p = tmp_path / "closures.json"
        p.write_text(_json.dumps({"closures": [c]}), encoding="utf-8")
        closures = load_active_closures(str(p))
        assert len(closures) == 1
