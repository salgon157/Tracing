"""
test_prepare_prediction.py — predikční režim prepare_inputs_v6.py

Tři věci, které ostrý běh nemá:
  1. sloupec Y (datum ROZVOZU) — dřívější datum = dopredikovaná objednávka,
     zatímco v ostrém běhu je jakékoli jiné datum vada exportu
  2. LOS podle šance z historie závozů — objednávka jde do plánu celá,
     nebo vůbec (viz tests/test_order_history.py)
  3. KOEFICIENT kg — vahám objednávek, které losem prošly, se přenásobí
     poměrem suma(dnes)/suma(minule); jejich váha je z minulého týdne

Pořadí je los → koeficient. SEC se nemění nikdy (neznáme vzorec ESO9).
"""
import pytest

from prepare_inputs_v6 import (
    KG_COEF_MAX,
    KG_COEF_MIN,
    KG_COEF_MIN_PAIRS,
    build_prepare_stats,
    check_delivery_dates,
    compute_kg_coefficient,
    find_suspect_prev_kg,
    format_kg_coefficient_summary,
    format_prev_kg_warning,
    parse_prev_kg,
    transform,
)


def _row(line=1, payload="KG:300#SEC:600", order_no="ORD001",
         delivery_date="20260728", prev_kg="-1000"):
    return {
        "_line": line,
        "delivery_date": delivery_date,
        "prev_kg": prev_kg,
        "location_code": "loc1",
        "customer_name": "Firma s.r.o.",
        "city": "Jihlava",
        "tw1_from_sec": "28800",
        "tw1_to_sec": "43200",
        "lon": "15.586947",
        "lat": "49.395796",
        "order_number": order_no,
        "note": "",
        "payload_raw": payload,
        "ramp": "0",
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Datum rozvozu (sloupec Y)
# ═════════════════════════════════════════════════════════════════════════════

class TestCheckDeliveryDates:
    def test_all_matching_passes_both_modes(self):
        rows = [_row(line=i) for i in range(3)]
        assert check_delivery_dates(rows, "2026-07-28", prediction=False) == []
        assert check_delivery_dates(rows, "2026-07-28", prediction=True) == []

    def test_production_rejects_older_date(self):
        rows = [_row(line=1), _row(line=2, delivery_date="20260721")]
        with pytest.raises(ValueError, match="JINÝM datem rozvozu"):
            check_delivery_dates(rows, "2026-07-28", prediction=False)

    def test_production_error_names_row_and_fix(self):
        rows = [_row(line=42, delivery_date="20260721", order_no="O999")]
        with pytest.raises(ValueError) as e:
            check_delivery_dates(rows, "2026-07-28", prediction=False)
        msg = str(e.value)
        assert "42" in msg and "O999" in msg and "ESO9" in msg

    def test_prediction_returns_older_rows(self):
        rows = [_row(line=1),
                _row(line=2, delivery_date="20260721"),
                _row(line=3, delivery_date="20260714")]
        out = check_delivery_dates(rows, "2026-07-28", prediction=True)
        assert {r["_line"] for r in out} == {2, 3}

    def test_future_date_rejected_in_both_modes(self):
        rows = [_row(line=1, delivery_date="20260801")]
        for pred in (False, True):
            with pytest.raises(ValueError, match="BUDOUCNU"):
                check_delivery_dates(rows, "2026-07-28", prediction=pred)

    def test_empty_date_ignored(self):
        assert check_delivery_dates([_row(delivery_date="")],
                                    "2026-07-28", prediction=False) == []


# ═════════════════════════════════════════════════════════════════════════════
#  Koeficient kg
# ═════════════════════════════════════════════════════════════════════════════

class TestParsePrevKg:
    def test_valid(self):
        assert parse_prev_kg("359.30") == pytest.approx(359.30)

    def test_minus_1000_is_none(self):
        assert parse_prev_kg("-1000") is None

    def test_zero_and_negative_none(self):
        assert parse_prev_kg("0") is None
        assert parse_prev_kg("-5") is None

    def test_garbage_none(self):
        assert parse_prev_kg("abc") is None
        assert parse_prev_kg("") is None


class TestComputeKgCoefficient:
    def _pair(self, line, now, prev):
        return _row(line=line, payload=f"KG:{now}#SEC:300", prev_kg=str(prev))

    def test_weighted_ratio(self):
        # zadání: 10 objednávek, minule 10 kg -> dnes 12 kg = 120 %
        rows = [self._pair(i, 12, 10) for i in range(KG_COEF_MIN_PAIRS)]
        c = compute_kg_coefficient(rows)
        assert c["coefficient"] == pytest.approx(1.2)
        assert c["pairs"] == KG_COEF_MIN_PAIRS
        assert c["applied"] is True

    def test_big_orders_weigh_more(self):
        # suma/suma: velká objednávka rozhoduje víc než deset malých
        rows = [self._pair(1, 1000, 1000)] + [self._pair(i, 3, 1) for i in range(2, 12)]
        c = compute_kg_coefficient(rows)
        assert c["coefficient"] == pytest.approx(1030 / 1010, abs=1e-3)

    def test_unpaired_rows_ignored(self):
        rows = ([self._pair(i, 12, 10) for i in range(KG_COEF_MIN_PAIRS)]
                + [_row(line=99, payload="KG:500#SEC:300", prev_kg="-1000")])
        c = compute_kg_coefficient(rows)
        assert c["pairs"] == KG_COEF_MIN_PAIRS
        assert c["coefficient"] == pytest.approx(1.2)

    def test_too_few_pairs_not_applied(self):
        rows = [self._pair(i, 20, 10) for i in range(KG_COEF_MIN_PAIRS - 1)]
        c = compute_kg_coefficient(rows)
        assert c["applied"] is False
        assert c["coefficient"] == 1.0          # neutrální
        assert c["raw"] == pytest.approx(2.0)   # ale spočítaný je vidět

    def test_clamped_high(self):
        rows = [self._pair(i, 100, 10) for i in range(KG_COEF_MIN_PAIRS)]
        c = compute_kg_coefficient(rows)
        assert c["coefficient"] == KG_COEF_MAX
        assert c["clamped"] is True

    def test_clamped_low(self):
        rows = [self._pair(i, 1, 10) for i in range(KG_COEF_MIN_PAIRS)]
        c = compute_kg_coefficient(rows)
        assert c["coefficient"] == KG_COEF_MIN
        assert c["clamped"] is True

    def test_no_pairs_neutral(self):
        c = compute_kg_coefficient([_row(prev_kg="-1000")])
        assert c["coefficient"] == 1.0
        assert c["applied"] is False


# ═════════════════════════════════════════════════════════════════════════════
#  Aplikace koeficientu v transform
# ═════════════════════════════════════════════════════════════════════════════

class TestCoefficientApplication:
    def test_predicted_weight_scaled(self):
        orders, _ = transform([_row(line=1, payload="KG:100#SEC:600")], "CB",
                              predicted_lines={1}, kg_coefficient=0.8)
        assert orders[0]["weight_kg"] == pytest.approx(80.0)

    def test_service_sec_unchanged(self):
        # SEC se ZÁMĚRNĚ nepřepočítává — neznáme vzorec, kterým ho ESO9 počítá
        orders, _ = transform([_row(line=1, payload="KG:100#SEC:600")], "CB",
                              predicted_lines={1}, kg_coefficient=0.5)
        assert orders[0]["service_sec"] == 600

    def test_only_predicted_touched(self):
        rows = [_row(line=1, payload="KG:100#SEC:600", order_no="REAL"),
                _row(line=2, payload="KG:100#SEC:600", order_no="PRED")]
        orders, _ = transform(rows, "CB", predicted_lines={2}, kg_coefficient=2.0)
        by_id = {o["order_number"]: o for o in orders}
        assert by_id["REAL"]["weight_kg"] == pytest.approx(100.0)
        assert by_id["PRED"]["weight_kg"] == pytest.approx(200.0)

    def test_coefficient_one_changes_nothing(self):
        orders, _ = transform([_row(line=1, payload="KG:123.456#SEC:600")], "CB",
                              predicted_lines={1}, kg_coefficient=1.0)
        assert orders[0]["weight_kg"] == pytest.approx(123.456)

    def test_default_is_production_mode(self):
        # bez parametrů = ostrý běh, žádné škálování
        orders, _ = transform([_row(line=1, payload="KG:100#SEC:600")], "CB")
        assert orders[0]["weight_kg"] == pytest.approx(100.0)


class TestPrepareStatsPrediction:
    def test_prediction_block_present(self):
        block = {"mode": "chance_from_history", "predicted_orders": 5,
                 "included": 3, "skipped_by_chance": 2}
        s = build_prepare_stats("CB", "2026-07-28", "riro.csv", raw_rows=125,
                                orders_count=123, dropped=[], prediction=True,
                                prediction_block=block)
        assert s["prediction"]["predicted_orders"] == 5
        assert s["prediction"]["skipped_by_chance"] == 2

    def test_production_has_no_prediction_block(self):
        s = build_prepare_stats("CB", "2026-07-28", "riro.csv", raw_rows=129,
                                orders_count=129, dropped=[])
        assert "prediction" not in s

    def test_skipped_by_chance_not_counted_as_excluded(self):
        # losem vynechaná objednávka NENÍ vadný řádek — kdyby se přičetla do
        # excluded_total, compare_prediction.py by hlásil neexistující chyby dat
        block = {"predicted_orders": 10, "included": 7, "skipped_by_chance": 3}
        s = build_prepare_stats("CB", "2026-07-28", "riro.csv", raw_rows=100,
                                orders_count=97, dropped=[], prediction=True,
                                prediction_block=block)
        assert s["excluded_total"] == 0
        assert s["excluded_rows"] == []

    def test_empty_block_tolerated(self):
        s = build_prepare_stats("CB", "2026-07-28", "riro.csv", raw_rows=10,
                                orders_count=10, dropped=[], prediction=True)
        assert s["prediction"] == {}


# ═════════════════════════════════════════════════════════════════════════════
#  Kombinace los + koeficient (srpen 2026)
# ═════════════════════════════════════════════════════════════════════════════

class TestLosPlusCoefficient:
    """Objednávka, která projde losem, dostane přenásobenou váhu.
    Ta, která neprojde, do transform vůbec nevstoupí."""

    def _pairs(self, n=KG_COEF_MIN_PAIRS, now=12, prev=10):
        # reálné objednávky na dnešek — z nich se počítá koeficient
        return [_row(line=1000 + i, payload=f"KG:{now}#SEC:300",
                     prev_kg=str(prev), order_no=f"REAL{i}")
                for i in range(n)]

    def test_only_selected_rows_are_scaled(self):
        # los vybral řádek 1, řádek 2 vyřadil (do transform nejde)
        rows = self._pairs() + [
            _row(line=1, payload="KG:100#SEC:600", order_no="VYBRANA",
                 delivery_date="20260721"),
        ]
        coef = compute_kg_coefficient(rows)
        assert coef["coefficient"] == pytest.approx(1.2)

        orders, _ = transform(rows, "CB", predicted_lines={1},
                              kg_coefficient=coef["coefficient"])
        by_id = {o["order_number"]: o for o in orders}
        assert by_id["VYBRANA"]["weight_kg"] == pytest.approx(120.0)
        assert by_id["REAL0"]["weight_kg"] == pytest.approx(12.0)   # reálná beze změny

    def test_service_sec_survives_both_steps(self):
        rows = self._pairs() + [_row(line=1, payload="KG:100#SEC:777",
                                     order_no="P", delivery_date="20260721")]
        orders, _ = transform(rows, "CB", predicted_lines={1}, kg_coefficient=2.0)
        assert [o for o in orders if o["order_number"] == "P"][0]["service_sec"] == 777

    def test_unapplied_coefficient_leaves_weights_alone(self):
        # málo párů -> koeficient 1.0 -> los rozhoduje, váhy zůstávají
        rows = self._pairs(n=KG_COEF_MIN_PAIRS - 1, now=20, prev=10)
        rows.append(_row(line=1, payload="KG:100#SEC:600", order_no="P",
                         delivery_date="20260721"))
        coef = compute_kg_coefficient(rows)
        assert coef["applied"] is False
        orders, _ = transform(rows, "CB", predicted_lines={1},
                              kg_coefficient=coef["coefficient"])
        assert [o for o in orders if o["order_number"] == "P"][0]["weight_kg"] \
            == pytest.approx(100.0)


class TestCoefficientSummary:
    def _coef(self, **kw):
        base = {"pairs": 110, "kg_now": 14196.0, "kg_prev": 12999.0,
                "raw": 1.0921, "coefficient": 1.0921, "applied": True,
                "clamped": False}
        base.update(kw)
        return base

    def test_shows_source_and_effect(self):
        rows = [_row(line=1, payload="KG:100#SEC:600"),
                _row(line=2, payload="KG:200#SEC:600")]
        out = format_kg_coefficient_summary(self._coef(), rows, {1, 2})
        assert "1.0921" in out
        assert "110 spárovaných" in out
        assert "300 kg" in out and "328 kg" in out        # 300 -> 327.6
        assert "SEC" in out

    def test_counts_only_selected(self):
        rows = [_row(line=1, payload="KG:100#SEC:600"),
                _row(line=2, payload="KG:900#SEC:600")]
        out = format_kg_coefficient_summary(self._coef(), rows, {1})
        assert "na 1 dopredikovaných" in out
        assert "100 kg" in out

    def test_not_applied_explains_why(self):
        out = format_kg_coefficient_summary(
            self._coef(applied=False, pairs=3), [], set())
        assert "NEPOUŽIT" in out
        assert "3 spárovaných" in out
        assert str(KG_COEF_MIN_PAIRS) in out

    def test_clamped_is_visible(self):
        out = format_kg_coefficient_summary(
            self._coef(raw=3.5, coefficient=KG_COEF_MAX, clamped=True),
            [_row(line=1, payload="KG:100#SEC:600")], {1})
        assert "OŘEZÁNO" in out
        assert str(KG_COEF_MAX) in out


# ═════════════════════════════════════════════════════════════════════════════
#  Varování na vadný sloupec AE (stopa po Excelu)
# ═════════════════════════════════════════════════════════════════════════════

class TestSuspectPrevKg:
    def test_valid_values_are_quiet(self):
        rows = [_row(line=1, prev_kg="359.30"),      # kg z minula
                _row(line=2, prev_kg="-1000"),       # minule bez závozu
                _row(line=3, prev_kg="")]            # údaj nedodán
        assert find_suspect_prev_kg(rows) == []

    def test_excel_date_flagged(self):
        # reálný nález z MO 5. 8. 2026 — Excel přeformátoval číslo na datum
        rows = [_row(line=7, prev_kg="XII.00", order_no="O123")]
        out = find_suspect_prev_kg(rows)
        assert len(out) == 1
        assert out[0]["line"] == 7
        assert out[0]["value"] == "XII.00"
        assert "Excelu" in out[0]["reason"]

    def test_zero_and_other_negatives_flagged(self):
        rows = [_row(line=1, prev_kg="0"), _row(line=2, prev_kg="-5")]
        out = find_suspect_prev_kg(rows)
        assert {o["line"] for o in out} == {1, 2}
        assert "nula" in out[0]["reason"]

    def test_minus_1000_never_flagged(self):
        # smluvená značka, ne vada — nesmí zahltit varování
        rows = [_row(line=i, prev_kg="-1000") for i in range(50)]
        assert find_suspect_prev_kg(rows) == []

    def test_warning_names_rows_and_cause(self):
        suspect = find_suspect_prev_kg([_row(line=7, prev_kg="XII.00",
                                             order_no="O123")])
        out = format_prev_kg_warning(suspect, raw_rows=163)
        assert "VAROVÁNÍ" in out and "163" in out
        assert "O123" in out and "XII.00" in out
        assert "Excelem" in out

    def test_warning_truncates_long_list(self):
        suspect = find_suspect_prev_kg(
            [_row(line=i, prev_kg="XII.00") for i in range(25)])
        out = format_prev_kg_warning(suspect, raw_rows=100)
        assert "a dalších 15" in out
