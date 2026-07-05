"""
test_prepare_inputs.py — unit testy pro prepare_inputs_v6.py

Testované funkce (pure, bez sítě a bez disku):
  seconds_to_hhmm, parse_payload, parse_min_from_hhmm, transform
"""
import pytest

from prepare_inputs_v6 import (
    seconds_to_hhmm,
    parse_payload,
    parse_min_from_hhmm,
    transform,
)


# ═════════════════════════════════════════════════════════════════════════════
#  seconds_to_hhmm
# ═════════════════════════════════════════════════════════════════════════════

class TestSecondsToHhmm:
    def test_midnight_zero(self):
        assert seconds_to_hhmm("0") == "00:00"

    def test_one_hour(self):
        assert seconds_to_hhmm("3600") == "01:00"

    def test_eight_thirty(self):
        assert seconds_to_hhmm("30600") == "08:30"

    def test_end_of_day_86400(self):
        assert seconds_to_hhmm("86400") == "24:00"

    def test_over_86400_returns_none(self):
        assert seconds_to_hhmm("86401") is None

    def test_negative_returns_none(self):
        assert seconds_to_hhmm("-1") is None

    def test_non_numeric_returns_none(self):
        assert seconds_to_hhmm("abc") is None

    def test_empty_string_returns_none(self):
        assert seconds_to_hhmm("") is None

    def test_whitespace_stripped(self):
        assert seconds_to_hhmm(" 3600 ") == "01:00"

    def test_noon(self):
        assert seconds_to_hhmm("43200") == "12:00"

    def test_23_59(self):
        # 23*3600 + 59*60 = 82800 + 3540 = 86340
        assert seconds_to_hhmm("86340") == "23:59"


# ═════════════════════════════════════════════════════════════════════════════
#  parse_payload
# ═════════════════════════════════════════════════════════════════════════════

class TestParsePayload:
    def test_empty_string_returns_empty_dict(self):
        assert parse_payload("") == {}

    def test_none_equivalent_empty(self):
        # None-like: prázdný string
        assert parse_payload("   ") == {}

    def test_single_key_integer(self):
        assert parse_payload("KG:100") == {"KG": 100.0}

    def test_decimal_comma_converted(self):
        result = parse_payload("KG:25,5")
        assert result["KG"] == pytest.approx(25.5)

    def test_decimal_dot_works(self):
        result = parse_payload("KG:25.5")
        assert result["KG"] == pytest.approx(25.5)

    def test_multiple_keys(self):
        result = parse_payload("KG:100;CNT:5")
        assert result["KG"] == 100.0
        assert result["CNT"] == 5.0

    def test_invalid_value_is_none(self):
        result = parse_payload("KG:abc")
        assert result["KG"] is None

    def test_part_without_colon_skipped(self):
        result = parse_payload("KG:100;BADPART;CNT:2")
        assert "KG" in result
        assert "CNT" in result
        assert "BADPART" not in result

    def test_keys_uppercased(self):
        result = parse_payload("kg:50")
        assert "KG" in result

    def test_zero_value(self):
        result = parse_payload("KG:0")
        assert result["KG"] == 0.0


# ═════════════════════════════════════════════════════════════════════════════
#  parse_min_from_hhmm
# ═════════════════════════════════════════════════════════════════════════════

class TestParseMinFromHhmm:
    def test_none_returns_zero(self):
        assert parse_min_from_hhmm(None) == 0

    def test_empty_returns_zero(self):
        assert parse_min_from_hhmm("") == 0

    def test_nan_returns_zero(self):
        assert parse_min_from_hhmm("nan") == 0

    def test_NaN_case_insensitive(self):
        assert parse_min_from_hhmm("NaN") == 0

    def test_midnight(self):
        assert parse_min_from_hhmm("00:00") == 0

    def test_eight_hours(self):
        assert parse_min_from_hhmm("08:00") == 480

    def test_twelve_thirty(self):
        assert parse_min_from_hhmm("12:30") == 750

    def test_23_59(self):
        assert parse_min_from_hhmm("23:59") == 1439

    def test_with_seconds_zero(self):
        assert parse_min_from_hhmm("12:30:00") == 750

    def test_with_nonzero_seconds_adds_one_minute(self):
        # Sekundy > 0 → zaokrouhlení nahoru (přidá 1 minutu)
        assert parse_min_from_hhmm("12:30:45") == 751

    def test_raw_minutes_numeric(self):
        assert parse_min_from_hhmm("480") == 480


# ═════════════════════════════════════════════════════════════════════════════
#  transform
# ═════════════════════════════════════════════════════════════════════════════

def _make_locations(*codes):
    """Vytvoří mock locations dict pro zadané kódy."""
    locs = {}
    for i, code in enumerate(codes):
        locs[code.lower()] = {
            "lat": 50.0 + i * 0.01,
            "lon": 14.0 + i * 0.01,
            "city": f"City_{code}",
            "name": f"Name_{code}",
            "address": f"Street {i}, City_{code}",
            "comment": "",
            "base_service_min": 10,
            "riro_vehicle_type_code": "",
        }
    return locs


def _make_raw_row(loc_code="LOC1", from_sec="28800", to_sec="43200",
                  payload="KG:300", order_no="ORD001", customer="Firma s.r.o.",
                  note="", code_a=""):
    return {
        "location_code": loc_code,
        "customer_name": customer,
        "tw1_from_sec": from_sec,
        "tw1_to_sec": to_sec,
        "order_number": order_no,
        "note": note,
        "payload_raw": payload,
        "code_a": code_a,
    }


class TestTransform:
    def test_valid_row_included(self):
        locations = _make_locations("LOC1")
        rows = [_make_raw_row("LOC1")]
        orders, missing = transform(rows, locations, "CB")
        assert len(orders) == 1
        assert missing == []

    def test_missing_location_adds_to_missing_codes(self):
        locations = _make_locations("LOC1")
        rows = [_make_raw_row("UNKNOWN")]
        orders, missing = transform(rows, locations, "CB")
        assert len(orders) == 0
        assert "UNKNOWN" in missing

    def test_missing_location_only_once(self):
        """Duplicitní missing code se do missing_codes přidá jen jednou."""
        locations = _make_locations("LOC1")
        rows = [_make_raw_row("UNKNOWN"), _make_raw_row("UNKNOWN", order_no="ORD002")]
        orders, missing = transform(rows, locations, "CB")
        assert missing.count("UNKNOWN") == 1

    def test_inverted_time_window_skipped(self):
        locations = _make_locations("LOC1")
        # from=12:00 (43200s), to=08:00 (28800s) — obrácené okno
        rows = [_make_raw_row("LOC1", from_sec="43200", to_sec="28800")]
        orders, missing = transform(rows, locations, "CB")
        assert len(orders) == 0

    def test_equal_time_window_skipped(self):
        locations = _make_locations("LOC1")
        # from == to
        rows = [_make_raw_row("LOC1", from_sec="28800", to_sec="28800")]
        orders, missing = transform(rows, locations, "CB")
        assert len(orders) == 0

    def test_weight_parsed_from_payload(self):
        locations = _make_locations("LOC1")
        rows = [_make_raw_row("LOC1", payload="KG:750")]
        orders, _ = transform(rows, locations, "CB")
        assert orders[0]["weight_kg"] == pytest.approx(750.0)

    def test_block_id_set_to_depot_code(self):
        locations = _make_locations("LOC1")
        rows = [_make_raw_row("LOC1")]
        orders, _ = transform(rows, locations, "HK")
        assert orders[0]["block_id"] == "HK"

    def test_output_contains_lat_lon(self):
        locations = _make_locations("LOC1")
        rows = [_make_raw_row("LOC1")]
        orders, _ = transform(rows, locations, "CB")
        assert "lat" in orders[0]
        assert "lon" in orders[0]

    def test_time_from_to_format(self):
        locations = _make_locations("LOC1")
        rows = [_make_raw_row("LOC1", from_sec="28800", to_sec="43200")]
        orders, _ = transform(rows, locations, "CB")
        assert orders[0]["time_from"] == "08:00"
        assert orders[0]["time_to"] == "12:00"

    def test_multiple_valid_rows(self):
        locations = _make_locations("LOC1", "LOC2", "LOC3")
        rows = [
            _make_raw_row("LOC1", order_no="O1"),
            _make_raw_row("LOC2", order_no="O2"),
            _make_raw_row("LOC3", order_no="O3"),
        ]
        orders, missing = transform(rows, locations, "CB")
        assert len(orders) == 3
        assert missing == []

    def test_invalid_time_sec_skipped(self):
        locations = _make_locations("LOC1")
        rows = [_make_raw_row("LOC1", from_sec="-1", to_sec="28800")]
        orders, _ = transform(rows, locations, "CB")
        assert len(orders) == 0
