"""
test_driver_assignment.py — parser dnů, mapování typů, tightness,
hard filtry, celodenní maďarské přiřazení. Fixtures syntetické — žádná
reálná jména (startup testy, PII nesmí do repa).
"""
import pytest

import driver_assignment as da
from driver_assignment import (
    build_assignment,
    line_tightness,
    map_type,
    parse_days,
    percentile_ranks,
    plan_deficit,
    result_rows,
)


# ─────────────────────────────────────────────────────────────────────────────
#  parse_days — lomítko dělí týden/víkend, obě části se sjednocují
# ─────────────────────────────────────────────────────────────────────────────

class TestParseDays:
    def test_full_week(self):
        assert parse_days("Po-Pá/So-Ne") == {0, 1, 2, 3, 4, 5, 6}

    def test_two_days_no_weekend(self):
        assert parse_days("Po,Pá") == {0, 4}

    def test_list_plus_weekend(self):
        assert parse_days("Po,St,Pá/So-Ne") == {0, 2, 4, 5, 6}

    def test_single_day_plus_weekend(self):
        assert parse_days("Po/So-Ne") == {0, 5, 6}

    def test_four_days_plus_weekend(self):
        assert parse_days("Po,Út,St,Pá/So-Ne") == {0, 1, 2, 4, 5, 6}

    def test_unknown_token_raises(self):
        with pytest.raises(ValueError):
            parse_days("Pondělí-Pátek")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_days("")


# ─────────────────────────────────────────────────────────────────────────────
#  map_type — (Typ, Nosnost) -> TYPE kód
# ─────────────────────────────────────────────────────────────────────────────

class TestMapType:
    @pytest.mark.parametrize("typ,nosnost,expected", [
        ("do 3t", 1200, "TYPE_01"), ("do 3t", 1350.0, "TYPE_02"),
        ("do 7t", 3000, "TYPE_03"), ("do 7t", 3200, "TYPE_04"),
        ("do 18t", 8000, "TYPE_05"), ("do 18t", 8700, "TYPE_06"),
        ("do 4t", 2000, "TYPE_07"),
    ])
    def test_known_combos(self, typ, nosnost, expected):
        assert map_type(typ, nosnost) == expected

    def test_unknown_combo_raises(self):
        with pytest.raises(ValueError, match="Neznámá kombinace"):
            map_type("do 3t", 999)


# ─────────────────────────────────────────────────────────────────────────────
#  line_tightness — vlastnost od uživatele: pozice váží, ale nepřebije počet
# ─────────────────────────────────────────────────────────────────────────────

def _stops(n, tight_idx, n_total=None):
    """n zastávek s velkou rezervou; na indexech tight_idx rezerva 0."""
    n_total = n_total or n
    return [{"location_code": f"loc{i}",
             "arrival_min": 600,
             "window_end_min": 600 + (0 if i in tight_idx else 300)}
            for i in range(n_total)]


class TestLineTightness:
    def test_no_tight_stops_zero(self):
        assert line_tightness(_stops(5, set())) == 0.0

    def test_user_property_end_beats_start_same_count(self):
        # 5 tight na KONCI linky > 5 tight na ZAČÁTKU (20 zastávek)
        start = line_tightness(_stops(20, set(range(5)), 20))
        end = line_tightness(_stops(20, set(range(15, 20)), 20))
        assert end > start

    def test_user_property_count_beats_position(self):
        # ale 5 tight na konci < 7 tight na začátku
        end5 = line_tightness(_stops(20, set(range(15, 20)), 20))
        start7 = line_tightness(_stops(20, set(range(7)), 20))
        assert end5 < start7

    def test_arrival_after_window_end_is_tight(self):
        stops = [{"location_code": "x", "arrival_min": 700,
                  "window_end_min": 650}]        # 50 min po konci okna
        assert line_tightness(stops) > 0

    def test_slack_over_threshold_not_tight(self):
        stops = [{"location_code": "x", "arrival_min": 600,
                  "window_end_min": 600 + da.CONFIG["tight_slack_min"] + 1}]
        assert line_tightness(stops) == 0.0

    def test_missing_window_ignored(self):
        stops = [{"location_code": "x", "arrival_min": 600,
                  "window_end_min": None}]
        assert line_tightness(stops) == 0.0

    def test_single_stop_gets_full_position_weight(self):
        stops = [{"location_code": "x", "arrival_min": 600,
                  "window_end_min": 600}]
        assert line_tightness(stops) == pytest.approx(
            1.0 + da.CONFIG["tight_pos_coef"])


# ─────────────────────────────────────────────────────────────────────────────
#  percentile_ranks + plan_deficit
# ─────────────────────────────────────────────────────────────────────────────

class TestRanksAndDeficit:
    def test_ranks_span_zero_to_one(self):
        assert percentile_ranks([10, 20, 30]) == [0.0, 0.5, 1.0]

    def test_ties_share_rank(self):
        r = percentile_ranks([10, 10, 30])
        assert r[0] == r[1] < r[2]

    def test_single_value_neutral(self):
        assert percentile_ranks([42]) == [0.5]

    def test_deficit_behind_plan_positive(self):
        row = {"plan_rok": 36500.0, "aktual_rok": 5000.0}
        # den 100: očekáváno 10 000, najeto 5 000 -> skluz kladný
        assert plan_deficit(row, 100) > 0

    def test_deficit_no_data_none(self):
        assert plan_deficit({"plan_rok": None, "aktual_rok": None}, 100) is None
        assert plan_deficit({"plan_rok": 0, "aktual_rok": 100.0}, 100) is None


# ─────────────────────────────────────────────────────────────────────────────
#  build_assignment — hard filtry, jeden řidič jednou, globální optimum
# ─────────────────────────────────────────────────────────────────────────────

def _row(driver, type_code="TYPE_02", days=None, available=True, active=True,
         dojezd=10.0, kvalita="Standart", vehicle_no=None,
         plan_rok=None, aktual_rok=None):
    return {
        "vehicle_no": vehicle_no or f"V_{driver}_{type_code}",
        "vehicle_name": f"{driver}_auto", "driver": driver, "dopravce": "X",
        "type_code": type_code, "days": days if days is not None else set(range(7)),
        "available": available, "active": active, "dojezd_km": dojezd,
        "kvalita": kvalita, "plan_rok": plan_rok, "plan_mes": None,
        "aktual_rok": aktual_rok, "aktual_mes": None,
    }


def _unit(depot="CB", vid="TYPE_02_01", km=100.0, tight=0.0, lines=None,
          locations=frozenset()):
    return {"depot": depot, "vehicle_id": vid,
            "type_code": vid.rsplit("_", 1)[0],
            "line_ids": lines or [f"LINE_{vid[-2:]}"], "km": km,
            "tightness_raw": tight, "stops_total": 3,
            "locations": set(locations)}


WEDNESDAY = "2026-08-12"   # středa (weekday 2)


class TestHardConstraints:
    def test_wrong_day_excluded(self):
        r = build_assignment([_unit()], [_row("A", days={0, 4})], WEDNESDAY)
        assert r["assigned"] == [] and len(r["uncovered"]) == 1

    def test_unavailable_excluded(self):
        r = build_assignment([_unit()], [_row("A", available=False)], WEDNESDAY)
        assert len(r["uncovered"]) == 1

    def test_inactive_excluded(self):
        r = build_assignment([_unit()], [_row("A", active=False)], WEDNESDAY)
        assert len(r["uncovered"]) == 1

    def test_wrong_type_excluded(self):
        r = build_assignment([_unit(vid="TYPE_05_01")],
                             [_row("A", type_code="TYPE_02")], WEDNESDAY)
        assert len(r["uncovered"]) == 1

    def test_driver_with_many_vehicles_used_once_across_depots(self):
        # řidič se 2 auty správných typů smí jet jen JEDNU linku dne
        rows = [_row("A", type_code="TYPE_02", vehicle_no="V1"),
                _row("A", type_code="TYPE_02", vehicle_no="V2"),
                _row("B", type_code="TYPE_02")]
        units = [_unit("CB", "TYPE_02_01"), _unit("HK", "TYPE_02_02")]
        r = build_assignment(units, rows, WEDNESDAY)
        assert len(r["assigned"]) == 2
        assert sorted(a["driver"] for a in r["assigned"]) == ["A", "B"]

    def test_shortage_alerts_and_partial_assignment(self):
        units = [_unit(vid="TYPE_02_01"), _unit(vid="TYPE_02_02")]
        r = build_assignment(units, [_row("A")], WEDNESDAY)
        assert len(r["assigned"]) == 1 and len(r["uncovered"]) == 1
        assert any("ALERT" in w for w in r["warnings"])


class TestSoftScoring:
    def test_global_beats_greedy(self):
        # Greedy po depech: CB(200 km) si vezme vzdáleného řidiče F...
        # Globálně: F (dojezd 40) patří na 300km HK linku, N (5) na 200km.
        rows = [_row("F", dojezd=40.0), _row("N", dojezd=5.0)]
        units = [_unit("CB", "TYPE_02_01", km=200.0),
                 _unit("HK", "TYPE_02_02", km=300.0)]
        r = build_assignment(units, rows, WEDNESDAY)
        by_unit = {a["unit"]["vehicle_id"]: a["driver"] for a in r["assigned"]}
        assert by_unit == {"TYPE_02_01": "N", "TYPE_02_02": "F"}

    def test_quality_matches_tightness(self):
        rows = [_row("R", kvalita="Rychlý"), _row("P", kvalita="Pomalý")]
        units = [_unit(vid="TYPE_02_01", tight=8.0),    # napjatá linka
                 _unit(vid="TYPE_02_02", tight=0.0)]    # volná
        r = build_assignment(units, rows, WEDNESDAY)
        by_unit = {a["unit"]["vehicle_id"]: a["driver"] for a in r["assigned"]}
        assert by_unit == {"TYPE_02_01": "R", "TYPE_02_02": "P"}

    def test_plan_deficit_drives_choice_when_data_present(self):
        # Z (zaostává za plánem) přebije D (v předstihu) při jinak shodě
        rows = [_row("Z", plan_rok=36500, aktual_rok=1000),
                _row("D", plan_rok=36500, aktual_rok=30000)]
        r = build_assignment([_unit()], rows, WEDNESDAY)
        assert r["assigned"][0]["driver"] == "Z"

    def test_no_plan_data_neutral_with_warning(self):
        r = build_assignment([_unit()], [_row("A"), _row("B")], WEDNESDAY)
        assert any("Plnění plánu BEZ DAT" in w for w in r["warnings"])
        assert r["assigned"][0]["breakdown"]["plneni"] == 0.5

    def test_familiarity_used_when_available(self):
        fam = {"loc1": {"A"}, "loc2": {"A"}}
        rows = [_row("A"), _row("B")]
        u = _unit(locations={"loc1", "loc2"})
        r = build_assignment([u], rows, WEDNESDAY, familiarity=fam)
        assert r["assigned"][0]["driver"] == "A"
        assert r["assigned"][0]["breakdown"]["familiarity"] == 1.0

    def test_no_familiarity_warning(self):
        r = build_assignment([_unit()], [_row("A")], WEDNESDAY,
                             familiarity=None)
        assert any("Familiarity BEZ DAT" in w for w in r["warnings"])


class TestDoubleRunAndOutput:
    def test_double_run_unit_one_driver_two_rows(self):
        u = _unit(lines=["LINE_03", "LINE_07"])
        r = build_assignment([u], [_row("A")], WEDNESDAY)
        rows = result_rows(r)
        assert len(rows) == 2
        assert rows[0]["driver"] == rows[1]["driver"] == "A"
        assert rows[0]["dvojlinka"] == "ano"

    def test_uncovered_marked_in_rows(self):
        r = build_assignment([_unit()], [_row("A", available=False)], WEDNESDAY)
        rows = result_rows(r)
        assert "NEPŘIŘAZENO" in rows[0]["driver"]

    def test_deterministic_output_order(self):
        rows_reg = [_row("A"), _row("B")]
        units = [_unit("HK", "TYPE_02_02"), _unit("CB", "TYPE_02_01")]
        r1 = result_rows(build_assignment(units, rows_reg, WEDNESDAY))
        r2 = result_rows(build_assignment(list(reversed(units)),
                                          rows_reg, WEDNESDAY))
        assert [x["line_id"] for x in r1] == [x["line_id"] for x in r2]
