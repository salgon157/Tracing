"""
test_order_history.py — šance závozu z historie objednávek

Predikce nově nezvedá kilogramy koeficientem, ale hází kostkou: dopredikovaná
objednávka se do plánu dostane celá, nebo vůbec. Testuje se výpočet šance
(okno, pauza, svátky, den v týdnu), determinismus losu a načtení xlsx.
"""
from datetime import date, datetime, timedelta

import openpyxl
import pytest

from order_history import (
    HISTORY_SHEET_FALLBACK,
    PAUSE_GAP_DAYS,
    WINDOW_DAYS,
    History,
    build_prediction_stats,
    czech_holidays,
    delivery_chance,
    easter_sunday,
    evaluate_predictions,
    find_history_files,
    format_prediction_report,
    is_holiday,
    load_history,
    prediction_draw,
)

HEADER = ["ID", "Datum", "Cislo", "Zkratka", "zem_sirka", "zem_delka",
          "GPS_prijezd", "GPS_odjezd", "BTTO", "kod_skupiny"]


def _write_xlsx(path, rows, header=HEADER, sheet=HISTORY_SHEET_FALLBACK):
    """Mini xlsx ve tvaru reálného exportu; `rows` = (datum, zkratka)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for i, (day, loc) in enumerate(rows, start=1):
        line = [None] * len(header)
        line[0] = i
        if "Datum" in header:
            line[header.index("Datum")] = day
        if "Zkratka" in header:
            line[header.index("Zkratka")] = loc
        ws.append(line)
    wb.save(path)
    return path


def _mondays(start: date, count: int) -> list[date]:
    """`count` po sobě jdoucích pondělí od `start` (včetně)."""
    first = start + timedelta(days=(0 - start.weekday()) % 7)
    return [first + timedelta(days=7 * i) for i in range(count)]


# ═════════════════════════════════════════════════════════════════════════════
#  Svátky
# ═════════════════════════════════════════════════════════════════════════════

class TestHolidays:
    def test_easter_sunday_known_years(self):
        assert easter_sunday(2025) == date(2025, 4, 20)
        assert easter_sunday(2026) == date(2026, 4, 5)
        assert easter_sunday(2024) == date(2024, 3, 31)

    def test_easter_derived_holidays(self):
        h = czech_holidays(2026)
        assert date(2026, 4, 3) in h      # Velký pátek
        assert date(2026, 4, 6) in h      # Velikonoční pondělí
        assert date(2026, 4, 5) not in h  # neděle sama svátkem není

    def test_fixed_holidays(self):
        h = czech_holidays(2026)
        for d in (date(2026, 1, 1), date(2026, 5, 1), date(2026, 7, 6),
                  date(2026, 11, 17), date(2026, 12, 26)):
            assert d in h

    def test_thirteen_holidays_per_year(self):
        assert len(czech_holidays(2025)) == 13
        assert len(czech_holidays(2026)) == 13

    def test_is_holiday(self):
        assert is_holiday(date(2026, 7, 6))       # Jan Hus, pondělí
        assert not is_holiday(date(2026, 7, 13))  # obyčejné pondělí


# ═════════════════════════════════════════════════════════════════════════════
#  Načtení historie
# ═════════════════════════════════════════════════════════════════════════════

class TestLoadHistory:
    def test_basic_load(self, tmp_path):
        p = _write_xlsx(tmp_path / "2026.xlsx", [
            ("2026-01-05 00:00:00.000", "j tabor"),
            ("2026-01-12 00:00:00.000", "j tabor"),
            ("2026-01-12 00:00:00.000", "zs jiraskova"),
        ])
        h = load_history([p])
        assert h.by_location["j tabor"] == (date(2026, 1, 5), date(2026, 1, 12))
        assert h.max_date == date(2026, 1, 12)
        assert h.files == ("2026.xlsx",)

    def test_location_key_normalized(self, tmp_path):
        p = _write_xlsx(tmp_path / "h.xlsx",
                        [("2026-01-05 00:00:00.000", "  E 5660 Stredni Skola ")])
        h = load_history([p])
        assert "e 5660 stredni skola" in h.by_location
        # dotaz snese jakoukoli podobu zápisu
        assert h.dates_for("E 5660 STREDNI SKOLA") == (date(2026, 1, 5),)

    def test_datetime_cells_accepted(self, tmp_path):
        p = _write_xlsx(tmp_path / "h.xlsx",
                        [(datetime(2026, 3, 2, 0, 0), "j tabor")])
        assert load_history([p]).dates_for("j tabor") == (date(2026, 3, 2),)

    def test_duplicate_day_counted_once(self, tmp_path):
        p = _write_xlsx(tmp_path / "h.xlsx", [
            ("2026-01-05 00:00:00.000", "j tabor"),
            ("2026-01-05 00:00:00.000", "j tabor"),   # dvě objednávky týž den
        ])
        assert load_history([p]).dates_for("j tabor") == (date(2026, 1, 5),)

    def test_merges_files_and_global_max(self, tmp_path):
        a = _write_xlsx(tmp_path / "2025.xlsx", [("2025-12-29 00:00:00.000", "j tabor")])
        b = _write_xlsx(tmp_path / "2026.xlsx", [("2026-01-05 00:00:00.000", "zs jiraskova")])
        h = load_history([a, b])
        assert h.dates_for("j tabor") == (date(2025, 12, 29),)
        # max je GLOBÁLNÍ, ne per adresa — konec pokrytí dat
        assert h.max_date == date(2026, 1, 5)

    def test_missing_column_is_explicit_error(self, tmp_path):
        p = _write_xlsx(tmp_path / "h.xlsx", [("2026-01-05", "x")],
                        header=["ID", "Datum", "Cislo"])
        with pytest.raises(ValueError, match="Zkratka"):
            load_history([p])

    def test_garbage_rows_skipped(self, tmp_path):
        p = _write_xlsx(tmp_path / "h.xlsx", [
            ("nesmysl", "j tabor"),
            (None, "j tabor"),
            ("2026-01-05 00:00:00.000", None),
            ("2026-01-05 00:00:00.000", "j tabor"),
        ])
        assert load_history([p]).dates_for("j tabor") == (date(2026, 1, 5),)

    def test_find_history_files(self, tmp_path):
        (tmp_path / "2026.xlsx").touch()
        (tmp_path / "2025.xlsx").touch()
        (tmp_path / "~$2026.xlsx").touch()      # zámek otevřeného Excelu
        (tmp_path / "poznamky.txt").touch()
        assert [p.name for p in find_history_files(tmp_path)] == ["2025.xlsx", "2026.xlsx"]

    def test_missing_dir_and_empty_dir_fail_clearly(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="neexistuje"):
            find_history_files(tmp_path / "neni")
        with pytest.raises(FileNotFoundError, match="xlsx"):
            find_history_files(tmp_path)


# ═════════════════════════════════════════════════════════════════════════════
#  Výpočet šance
# ═════════════════════════════════════════════════════════════════════════════

class TestDeliveryChance:
    def test_basic_ratio(self):
        target = date(2026, 6, 1)                       # pondělí
        mondays = _mondays(date(2026, 5, 4), 4)         # 4.5, 11.5, 18.5, 25.5
        got = delivery_chance(mondays[:3], target, history_max_date=date(2026, 5, 31))
        assert got["delivered"] == 3 and got["eligible"] == 4
        assert got["chance"] == pytest.approx(0.75)

    def test_other_weekdays_ignored(self):
        target = date(2026, 6, 1)                       # pondělí
        # samé úterky — pro pondělí je čitatel nulový
        tuesdays = [date(2026, 5, 5), date(2026, 5, 12), date(2026, 5, 19)]
        got = delivery_chance(tuesdays, target, history_max_date=date(2026, 5, 31))
        assert got["delivered"] == 0 and got["eligible"] > 0
        assert got["chance"] == 0.0

    def test_holidays_removed_from_both_sides(self):
        # Varianta příkladu ze zadání: sváteční pondělí (6.7. Jan Hus) se
        # nepočítá do jmenovatele ANI do čitatele, i když se ten den jelo.
        target = date(2026, 7, 13)
        mondays = _mondays(date(2026, 6, 1), 6)         # 1.6 … 6.7 (6.7 = svátek)
        assert date(2026, 7, 6) in mondays
        got = delivery_chance(mondays, target, history_max_date=date(2026, 7, 12))
        assert got["eligible"] == 5          # 6 pondělí − 1 svátek
        assert got["delivered"] == 5         # závoz ve svátek se nepočítá
        assert got["chance"] == 1.0

    def test_holiday_only_delivery_gives_zero(self):
        # jelo se JEN ve svátek → čitatel 0, jmenovatel bez svátku
        target = date(2026, 7, 13)
        got = delivery_chance([date(2026, 7, 6)], target,
                              history_max_date=date(2026, 7, 12))
        assert got["delivered"] == 0

    def test_window_ends_at_history_coverage(self):
        # Historie končí 28.7., plánujeme 3.8. — pondělí 3.8. ani dny po 28.7.
        # nesmí spadnout do jmenovatele (nejsou „nezavezeno", ale „nevíme").
        target = date(2026, 8, 3)
        mondays = _mondays(date(2026, 7, 1), 4)          # 6.7(svátek), 13.7, 20.7, 27.7
        got = delivery_chance(mondays, target, history_max_date=date(2026, 7, 28))
        assert got["window_end"] == date(2026, 7, 28)
        assert got["eligible"] == 3          # 13.7, 20.7, 27.7
        assert got["chance"] == 1.0

    def test_future_dates_ignored_for_backtest(self):
        # predikci lze pustit zpětně — závozy po cílovém datu se nepočítají
        target = date(2026, 6, 1)
        dates = _mondays(date(2026, 5, 4), 2) + [date(2026, 6, 8), date(2026, 6, 15)]
        got = delivery_chance(dates, target, history_max_date=date(2026, 6, 30))
        assert got["window_end"] == date(2026, 5, 31)
        assert got["delivered"] == 2

    def test_pause_discards_older_history(self):
        target = date(2026, 6, 1)
        old = _mondays(date(2026, 1, 5), 4)              # dávná série
        gap_start = old[-1] + timedelta(days=PAUSE_GAP_DAYS + 1)
        fresh = [gap_start + timedelta(days=7 * i) for i in range(3)]
        got = delivery_chance(old + fresh, target, history_max_date=date(2026, 5, 31))
        assert got["pause_applied"] is True
        assert got["window_start"] == gap_start
        assert got["delivered"] == sum(1 for d in fresh if d.weekday() == 0)

    def test_gap_exactly_at_limit_is_not_a_pause(self):
        target = date(2026, 8, 31)
        first = date(2026, 3, 2)
        second = first + timedelta(days=PAUSE_GAP_DAYS)   # přesně na hranici
        got = delivery_chance([first, second], target, history_max_date=date(2026, 8, 30))
        assert got["pause_applied"] is False
        assert got["window_start"] == first

    def test_last_pause_wins(self):
        target = date(2026, 12, 7)
        a = date(2026, 1, 5)
        b = a + timedelta(days=PAUSE_GAP_DAYS + 1)
        c = b + timedelta(days=PAUSE_GAP_DAYS + 1)
        got = delivery_chance([a, b, c], target, history_max_date=date(2026, 12, 6))
        assert got["window_start"] == c

    def test_year_window_caps_old_history(self):
        # souvislá historie bez pauzy, delší než rok → okno se ořízne na rok
        target = date(2026, 6, 1)
        weekly = [target - timedelta(days=7 * i) for i in range(1, 70)]   # ~482 dní
        got = delivery_chance(weekly, target, history_max_date=date(2026, 5, 31))
        assert got["window_start"] == target - timedelta(days=WINDOW_DAYS)
        assert got["pause_applied"] is False
        assert got["eligible"] <= 53          # nejvýš rok pondělí

    def test_no_history_is_certain(self):
        got = delivery_chance([], date(2026, 6, 1), history_max_date=date(2026, 5, 31))
        assert got["chance"] == 1.0 and got["no_history"] is True

    def test_no_eligible_day_is_certain(self):
        # první závoz teprve minulé úterý, plánujeme pondělí → 0 způsobilých dnů
        target = date(2026, 6, 1)
        got = delivery_chance([date(2026, 5, 26)], target,
                              history_max_date=date(2026, 5, 31))
        assert got["eligible"] == 0 and got["chance"] == 1.0

    def test_fresh_customer_one_of_one(self):
        target = date(2026, 6, 1)
        got = delivery_chance([date(2026, 5, 25)], target,
                              history_max_date=date(2026, 5, 31))
        assert (got["delivered"], got["eligible"]) == (1, 1)
        assert got["chance"] == 1.0

    def test_duplicates_do_not_inflate(self):
        target = date(2026, 6, 1)
        mondays = _mondays(date(2026, 5, 4), 4)[:3]
        got = delivery_chance(mondays + mondays, target,
                              history_max_date=date(2026, 5, 31))
        assert got["delivered"] == 3


# ═════════════════════════════════════════════════════════════════════════════
#  Los
# ═════════════════════════════════════════════════════════════════════════════

class TestDraw:
    def test_deterministic(self):
        a = prediction_draw("2026-08-03", "j tabor")
        b = prediction_draw("2026-08-03", "j tabor")
        assert a == b and 0.0 <= a < 1.0

    def test_differs_per_location_and_date(self):
        assert prediction_draw("2026-08-03", "j tabor") != \
               prediction_draw("2026-08-03", "zs jiraskova")
        assert prediction_draw("2026-08-03", "j tabor") != \
               prediction_draw("2026-08-10", "j tabor")

    def test_location_case_insensitive(self):
        assert prediction_draw("2026-08-03", " J Tabor ") == \
               prediction_draw("2026-08-03", "j tabor")


# ═════════════════════════════════════════════════════════════════════════════
#  Vyhodnocení + výstupy
# ═════════════════════════════════════════════════════════════════════════════

def _history(**by_location) -> History:
    return History(by_location={k.replace("_", " "): tuple(v)
                                for k, v in by_location.items()},
                   max_date=date(2026, 7, 28), files=("2026.xlsx",))


def _raw(line, loc, order="O1"):
    return {"_line": line, "location_code": loc, "order_number": order,
            "customer_name": "Firma s.r.o."}


class TestEvaluate:
    def test_certain_orders_always_selected(self):
        h = _history()                                  # nikdo v historii
        recs = evaluate_predictions([_raw(1, "novy zakaznik")], h, "2026-08-03")
        assert recs[0]["selected"] is True
        assert recs[0]["chance"] == 1.0 and recs[0]["no_history"] is True

    def test_zero_chance_never_selected(self):
        # samé úterky v historii, plánujeme pondělí → 0 %
        h = _history(j_tabor=[date(2026, 7, 7), date(2026, 7, 14), date(2026, 7, 21)])
        recs = evaluate_predictions([_raw(1, "j tabor")], h, "2026-08-03")
        assert recs[0]["chance"] == 0.0 and recs[0]["selected"] is False

    def test_same_address_shares_one_draw(self):
        # dvě objednávky téže adresy = jeden los, obě dovnitř nebo obě ven
        h = _history(j_tabor=[date(2026, 7, 13), date(2026, 7, 20)])
        recs = evaluate_predictions([_raw(1, "j tabor", "A"),
                                     _raw(2, "j tabor", "B")], h, "2026-08-03")
        assert recs[0]["draw"] == recs[1]["draw"]
        assert recs[0]["selected"] == recs[1]["selected"]

    def test_run_is_reproducible(self):
        h = _history(j_tabor=[date(2026, 7, 13)], zs_jiraskova=[date(2026, 7, 20)])
        rows = [_raw(1, "j tabor"), _raw(2, "zs jiraskova")]
        first = [r["selected"] for r in evaluate_predictions(rows, h, "2026-08-03")]
        second = [r["selected"] for r in evaluate_predictions(rows, h, "2026-08-03")]
        assert first == second

    def test_record_carries_context(self):
        h = _history(j_tabor=[date(2026, 7, 13)])
        r = evaluate_predictions([_raw(9, "j tabor", "O123")], h, "2026-08-03")[0]
        assert r["line"] == 9 and r["order_number"] == "O123"
        assert r["window_end"] == date(2026, 7, 28)     # konec pokrytí historie


class TestReportAndStats:
    def _records(self):
        h = _history(j_tabor=[date(2026, 7, 7), date(2026, 7, 14)],   # úterky → 0 %
                     zs_jiraskova=[date(2026, 7, 13), date(2026, 7, 20)])
        return evaluate_predictions([_raw(1, "j tabor"), _raw(2, "zs jiraskova"),
                                     _raw(3, "novy")], h, "2026-08-03"), h

    def test_report_lists_outcome(self):
        recs, _ = self._records()
        out = format_prediction_report(recs, "2026-08-03")
        assert "VYNECHÁNA" in out and "VYBRÁNA" in out
        assert "j tabor" in out and "pondělí" in out
        assert "Dopredikovaných: 3" in out

    def test_report_handles_empty(self):
        assert "žádné dopredikované" in format_prediction_report([], "2026-08-03")

    def test_stats_counts_add_up(self):
        recs, h = self._records()
        s = build_prediction_stats(recs, h)
        assert s["predicted_orders"] == s["included"] + s["skipped_by_chance"]
        assert s["predicted_orders"] == 3
        assert s["history_max_date"] == "2026-07-28"
        assert s["history_files"] == ["2026.xlsx"]
        assert len(s["orders"]) == 3

    def test_stats_are_json_serializable(self):
        import json
        recs, h = self._records()
        json.dumps(build_prediction_stats(recs, h), ensure_ascii=False)
