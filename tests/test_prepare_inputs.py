"""
test_prepare_inputs.py — unit testy pro prepare_inputs_v6.py

Finální RiRo formát z ESO9 (od 17.7.2026): GPS ve sloupcích R/S (idx 17/18),
payload "KG:x#SEC:y" v AA (idx 26). Locations už se nepoužívají.
Testované funkce jsou pure (bez sítě, bez disku kromě tmp_path).
"""
import pytest

from prepare_inputs_v6 import (
    EXPECTED_COLS,
    COL_PAYLOAD_RAW,
    build_prepare_stats,
    check_file_format,
    check_row_format,
    find_active_riro_file,
    format_dropped_report,
    parse_gps,
    parse_payload,
    seconds_to_hhmm,
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
        assert seconds_to_hhmm("86340") == "23:59"


# ═════════════════════════════════════════════════════════════════════════════
#  parse_payload — oddělovač '#', klíče KG a SEC
# ═════════════════════════════════════════════════════════════════════════════

class TestParsePayload:
    def test_empty_string_returns_empty_dict(self):
        assert parse_payload("") == {}

    def test_whitespace_only(self):
        assert parse_payload("   ") == {}

    def test_kg_and_sec(self):
        result = parse_payload("KG:51.475#SEC:261")
        assert result["KG"] == pytest.approx(51.475)
        assert result["SEC"] == pytest.approx(261.0)

    def test_kg_only_no_sec(self):
        # starý formát — SEC chybí, transform to musí zachytit
        assert parse_payload("KG:14.290") == {"KG": pytest.approx(14.29)}

    def test_decimal_comma_converted(self):
        assert parse_payload("KG:25,5")["KG"] == pytest.approx(25.5)

    def test_invalid_value_is_none(self):
        assert parse_payload("KG:abc#SEC:300")["KG"] is None

    def test_part_without_colon_skipped(self):
        result = parse_payload("KG:100#BADPART#SEC:240")
        assert result["KG"] == 100.0
        assert result["SEC"] == 240.0
        assert "BADPART" not in result

    def test_keys_uppercased(self):
        assert "KG" in parse_payload("kg:50#sec:60")

    def test_semicolon_is_not_separator(self):
        # dřív se splitovalo na ';' — teď je oddělovač '#'
        result = parse_payload("KG:100;CNT:5#SEC:300")
        assert result["SEC"] == 300.0
        assert "CNT" not in result


# ═════════════════════════════════════════════════════════════════════════════
#  check_row_format — rozlišení formátů podle OBSAHU (počet sloupců nestačí)
# ═════════════════════════════════════════════════════════════════════════════

def _raw_cols(payload="KG:51.475#SEC:261", n=EXPECTED_COLS):
    row = [""] * n
    if n > COL_PAYLOAD_RAW:
        row[COL_PAYLOAD_RAW] = payload
    return row


class TestCheckRowFormat:
    """Struktura řádku — jen počet sloupců."""

    def test_final_format_passes(self):
        check_row_format(_raw_cols(), 1)   # nesmí vyhodit

    def test_transitional_32_cols_rejected(self):
        with pytest.raises(ValueError, match="přechodný formát"):
            check_row_format(_raw_cols(n=32), 5)

    def test_wrong_column_count_rejected(self):
        with pytest.raises(ValueError, match="nemá 30 sloupců"):
            check_row_format(_raw_cols(n=25), 5)

    def test_error_mentions_line_number(self):
        with pytest.raises(ValueError, match="Řádek 42"):
            check_row_format(_raw_cols(n=25), 42)

    def test_missing_sec_is_not_structural_error(self):
        # chybějící SEC na řádku = vada DAT (řeší transform), ne struktury
        check_row_format(_raw_cols(payload="KG:14.290"), 5)   # nesmí vyhodit


class TestCheckFileFormat:
    """Formát souboru — pozná se podle prvního datového řádku."""

    def test_final_format_passes(self):
        check_file_format(_raw_cols())     # nesmí vyhodit

    def test_old_format_without_sec_rejected(self):
        # starý má taky 30 sloupců — pozná se jen podle chybějícího SEC
        with pytest.raises(ValueError, match="starém formátu"):
            check_file_format(_raw_cols(payload="KG:14.290"))

    def test_error_explains_fix(self):
        with pytest.raises(ValueError, match="Exportuj finální formát z ESO9"):
            check_file_format(_raw_cols(payload="KG:1"))


# ═════════════════════════════════════════════════════════════════════════════
#  parse_gps — sanity rozsah ČR (chytí i prohozené lat/lon)
# ═════════════════════════════════════════════════════════════════════════════

class TestParseGps:
    def test_valid_cz_coords(self):
        assert parse_gps({"lon": "15.586947", "lat": "49.395796"}) == \
            (pytest.approx(49.395796), pytest.approx(15.586947))

    def test_placeholder_minus_1000_rejected(self):
        # starý formát měl v R/S -1000
        assert parse_gps({"lon": "-1000", "lat": "-1000"}) is None

    def test_swapped_lat_lon_rejected(self):
        # lon=49.4 je mimo ČR rozsah → chyceno
        assert parse_gps({"lon": "49.395796", "lat": "15.586947"}) is None

    def test_empty_rejected(self):
        assert parse_gps({"lon": "", "lat": ""}) is None

    def test_garbage_rejected(self):
        assert parse_gps({"lon": "abc", "lat": "49.4"}) is None

    def test_missing_keys_rejected(self):
        assert parse_gps({}) is None


# ═════════════════════════════════════════════════════════════════════════════
#  transform — GPS a SEC z riro, žádné locations
# ═════════════════════════════════════════════════════════════════════════════

def _make_raw_row(loc_code="loc1", from_sec="28800", to_sec="43200",
                  payload="KG:300#SEC:600", order_no="ORD001",
                  customer="Firma s.r.o.", note="", code_a="",
                  lon="15.586947", lat="49.395796", city="Jihlava", line=1):
    return {
        "_line": line,
        "location_code": loc_code,
        "customer_name": customer,
        "city": city,
        "tw1_from_sec": from_sec,
        "tw1_to_sec": to_sec,
        "lon": lon,
        "lat": lat,
        "order_number": order_no,
        "note": note,
        "payload_raw": payload,
        "code_a": code_a,
    }


class TestTransform:
    def test_valid_row_included(self):
        orders, dropped = transform([_make_raw_row()], "CB")
        assert len(orders) == 1
        assert dropped == []

    def test_gps_from_riro_columns(self):
        orders, _ = transform([_make_raw_row(lon="14.259084", lat="48.809679")], "CB")
        assert orders[0]["lat"] == pytest.approx(48.809679)
        assert orders[0]["lon"] == pytest.approx(14.259084)

    def test_service_sec_from_payload(self):
        orders, _ = transform([_make_raw_row(payload="KG:51.475#SEC:261")], "CB")
        assert orders[0]["service_sec"] == 261
        assert orders[0]["weight_kg"] == pytest.approx(51.475)

    def test_city_from_column_g(self):
        orders, _ = transform([_make_raw_row(city="Kájov")], "CB")
        assert orders[0]["city"] == "Kájov"

    def test_block_id_set_to_depot(self):
        orders, _ = transform([_make_raw_row()], "HK")
        assert orders[0]["block_id"] == "HK"

    def test_bad_gps_dropped_with_reason(self):
        orders, dropped = transform([_make_raw_row(lon="-1000", lat="-1000")], "CB")
        assert orders == []
        assert dropped[0]["reason"] == "vadná GPS"
        assert "-1000" in dropped[0]["detail"]

    def test_gps_outside_cz_dropped(self):
        orders, dropped = transform([_make_raw_row(lon="2.35", lat="48.85")], "CB")
        assert orders == []
        assert dropped[0]["reason"] == "vadná GPS"

    def test_missing_sec_dropped(self):
        orders, dropped = transform([_make_raw_row(payload="KG:300")], "CB")
        assert orders == []
        assert dropped[0]["reason"] == "vadný payload"
        assert "SEC" in dropped[0]["detail"]

    def test_zero_sec_dropped(self):
        _, dropped = transform([_make_raw_row(payload="KG:300#SEC:0")], "CB")
        assert dropped[0]["reason"] == "vadný payload"

    def test_missing_kg_dropped(self):
        _, dropped = transform([_make_raw_row(payload="SEC:300")], "CB")
        assert dropped[0]["reason"] == "vadný payload"
        assert "KG" in dropped[0]["detail"]

    def test_inverted_time_window_dropped(self):
        _, dropped = transform([_make_raw_row(from_sec="43200", to_sec="28800")], "CB")
        assert dropped[0]["reason"] == "vadné časové okno"

    def test_equal_time_window_dropped(self):
        _, dropped = transform([_make_raw_row(from_sec="28800", to_sec="28800")], "CB")
        assert dropped[0]["reason"] == "vadné časové okno"

    def test_negative_time_dropped(self):
        _, dropped = transform([_make_raw_row(from_sec="-1", to_sec="-1")], "CB")
        assert dropped[0]["reason"] == "vadné časové okno"

    def test_dropped_carries_identification(self):
        _, dropped = transform([_make_raw_row(order_no="O999", loc_code="konibar",
                                              customer="Konibar", line=42,
                                              lon="-1000", lat="-1000")], "CB")
        d = dropped[0]
        assert d["line"] == 42
        assert d["order_number"] == "O999"
        assert d["location_code"] == "konibar"
        assert d["customer_name"] == "Konibar"

    def test_mixed_rows_partial(self):
        rows = [_make_raw_row(order_no="OK1"),
                _make_raw_row(order_no="BAD", lon="-1000", lat="-1000"),
                _make_raw_row(order_no="OK2")]
        orders, dropped = transform(rows, "CB")
        assert [o["order_number"] for o in orders] == ["OK1", "OK2"]
        assert len(dropped) == 1


# ═════════════════════════════════════════════════════════════════════════════
#  build_prepare_stats + format_dropped_report
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildPrepareStats:
    def _dropped(self, reason, n=1):
        return [{"line": i, "order_number": f"O{i}", "location_code": "x",
                 "customer_name": "Y", "reason": reason, "detail": "d"}
                for i in range(n)]

    def test_counts_by_reason(self):
        dropped = (self._dropped("vadná GPS", 3)
                   + self._dropped("vadné časové okno", 2)
                   + self._dropped("vadný payload", 1))
        s = build_prepare_stats("CB", "2026-07-17", "riro-20260717-CB.csv",
                                raw_rows=245, orders_count=239, dropped=dropped)
        assert s["excluded_total"] == 6
        assert s["excluded_invalid_gps_rows"] == 3
        assert s["excluded_invalid_time_window_rows"] == 2
        assert s["excluded_invalid_payload_rows"] == 1
        assert s["raw_rows"] == 245
        assert s["orders_count"] == 239

    def test_clean_run(self):
        s = build_prepare_stats("CB", "2026-07-17", "riro-20260717-CB.csv",
                                raw_rows=245, orders_count=245, dropped=[])
        assert s["excluded_total"] == 0
        assert s["excluded_rows"] == []

    def test_keeps_excluded_total_key_for_compare_prediction(self):
        # compare_prediction.excluded_for() na tomto klíči stojí
        s = build_prepare_stats("CB", "2026-07-17", "x.csv", raw_rows=10,
                                orders_count=9, dropped=self._dropped("vadná GPS"))
        assert "excluded_total" in s


class TestFormatDroppedReport:
    def test_report_names_rows_and_reasons(self):
        dropped = [{"line": 12, "order_number": "O126103248",
                    "location_code": "rest u toma", "customer_name": "Restaurace",
                    "reason": "vadná GPS", "detail": "sloupec R (lon)='-1000'"}]
        out = format_dropped_report(dropped, 245)
        assert "VYŘAZENO 1 z 245" in out
        assert "12" in out and "O126103248" in out
        assert "vadná GPS" in out
        assert "-1000" in out


# ═════════════════════════════════════════════════════════════════════════════
#  find_active_riro_file — parametr input_dir (--data-root, predikční režim)
# ═════════════════════════════════════════════════════════════════════════════

class TestFindActiveRiroFileInputDir:
    def _make_riro(self, root, depot="CB", name="riro-20260717-CB.csv"):
        aktivni = root / "input" / depot / "aktivni"
        aktivni.mkdir(parents=True)
        (aktivni / name).write_text("", encoding="utf-8")
        return aktivni / name

    def test_custom_input_dir_finds_file(self, tmp_path):
        expected = self._make_riro(tmp_path)
        path, date_str = find_active_riro_file("CB", tmp_path / "input")
        assert path == expected
        assert date_str == "2026-07-17"

    def test_full_depot_name_in_filename(self, tmp_path):
        self._make_riro(tmp_path, depot="HK", name="riro-20260717-Hradec Králové.csv")
        _, date_str = find_active_riro_file("HK", tmp_path / "input")
        assert date_str == "2026-07-17"

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            find_active_riro_file("CB", tmp_path / "input")

    def test_two_files_raise(self, tmp_path):
        self._make_riro(tmp_path)
        aktivni = tmp_path / "input" / "CB" / "aktivni"
        (aktivni / "riro-20260718-CB.csv").write_text("", encoding="utf-8")
        with pytest.raises(ValueError):
            find_active_riro_file("CB", tmp_path / "input")
