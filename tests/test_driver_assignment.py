"""
test_driver_assignment.py — registr auto+řidič (vehicles-active csv), mapování
typů podle vehicle_types dne, dostupnost od/do, tier „naše auta poslední",
plnění plánu rok+měsíc, familiarity jako pořadí z historie řidič×adresa,
kontrola flotily, tightness, celodenní maďarské přiřazení. Fixtures
syntetické — žádná reálná jména (startup testy, PII nesmí do repa).
"""
from datetime import date
from pathlib import Path

import pytest

import driver_assignment as da
from driver_assignment import (
    build_assignment,
    familiarity_ranks,
    fleet_mismatches,
    is_available,
    line_tightness,
    load_history,
    load_registry,
    load_type_map,
    map_type,
    parse_days,
    percentile_ranks,
    plan_deficit,
    plan_deficit_month,
    plan_scores,
    result_rows,
)

WEDNESDAY = "2026-08-19"   # středa (weekday 2)
WED = date(2026, 8, 19)


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures: vozový park dne + registr + historie (syntetické)
# ─────────────────────────────────────────────────────────────────────────────

VT_HEADER = ("type_code;type_name;max_kg;cost_per_km;start_cost_kc;available_count;"
             "total_count;active_count;profiles;cost_per_km_source;"
             "available_count_source;time_multiplier;osrm_profile;valid_for_date")


def _vt_file(tmp_path: Path, rows=None, day="2026-08-19") -> Path:
    rows = rows if rows is not None else [
        ("TYPE_01", "do 3t", 1200, 1), ("TYPE_02", "do 3t", 1350, 3),
        ("TYPE_04", "do 7t", 3200, 1), ("TYPE_05", "do 18t", 8000, 1),
        ("TYPE_06", "do 4t", 2000, 1),
    ]
    lines = [VT_HEADER] + [
        f"{c};{n};{kg};11.00;1000;{cnt};{cnt};{cnt};"
        f"{'Velké auto' if kg >= 8000 else 'Malé auto'};src;src;1.0;driving;{day}"
        for c, n, kg, cnt in rows]
    p = tmp_path / f"vehicle_types-{day.replace('-', '')}.csv"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


REG_HEADER = ("id;vehicle_code;vehicle_name;vehicle_comp;driver;driver_name;"
              "vehicle_type;vehicle_profile;dny_pouzitelnosti;dostupnost_od;"
              "dostupnost_do;km_aktual_mes;km_aktual_rok;km_plan_mes;km_plan_rok;"
              "driver_quality;driver_km_to_depot;valid_for_date;max_kg")


def _reg_line(i, driver, typ="do 3t", kg=1350, days="Po-Pá/So-Ne", od="2026-08-01",
              do="", akt_mes="500", akt_rok="20000", plan_mes="6000",
              plan_rok="72000", q="Standart", dojezd=10, day="2026-08-19"):
    return (f"{i};{100 + i};Auto {i};firma{i};{driver};Ridic {driver};{typ};"
            f"{'Velké auto' if kg >= 8000 else 'Malé auto'};{days};{od};{do};"
            f"{akt_mes};{akt_rok};{plan_mes};{plan_rok};{q};{dojezd};{day};{kg}")


def _reg_file(tmp_path: Path, lines: list[str], header=REG_HEADER,
              name="vehicles-active-20260819.csv") -> Path:
    p = tmp_path / name
    p.write_text(header + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return p


def _hist_file(tmp_path: Path, rows: list[tuple]) -> Path:
    p = tmp_path / "driver-address-visits.csv"
    p.write_text("driver_code;id_subj_adr;adress_note;visit_count\n"
                 + "\n".join(f"{d};{a};{n};{c}" for d, a, n, c in rows) + "\n",
                 encoding="utf-8")
    return p


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

    def test_four_days_plus_weekend(self):
        assert parse_days("Po,Út,St,Pá/So-Ne") == {0, 1, 2, 4, 5, 6}

    def test_unknown_token_raises(self):
        with pytest.raises(ValueError):
            parse_days("Pondělí-Pátek")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_days("")


# ─────────────────────────────────────────────────────────────────────────────
#  Typ auta podle vozového parku DNE (žádná natvrdo psaná tabulka)
# ─────────────────────────────────────────────────────────────────────────────

class TestTypeMap:
    def test_map_follows_file_content(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        assert map_type("do 3t", 1200, tm) == "TYPE_01"
        assert map_type("do 3t", "1350.0", tm) == "TYPE_02"
        assert map_type("do 4t", 2000, tm) == "TYPE_06"     # 19. 8. přečíslováno

    def test_renumbered_fleet_changes_mapping(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path, [("TYPE_07", "do 4t", 2000, 1)]))
        assert map_type("do 4t", 2000, tm) == "TYPE_07"

    def test_unknown_combo_raises_with_known_list(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        with pytest.raises(ValueError, match="do 3t"):
            map_type("do 3t", 999, tm)

    def test_duplicate_combo_in_fleet_is_error(self, tmp_path):
        p = _vt_file(tmp_path, [("TYPE_01", "do 3t", 1200, 1), ("TYPE_09", "do 3t", 1200, 1)])
        with pytest.raises(SystemExit):
            load_type_map(p)


# ─────────────────────────────────────────────────────────────────────────────
#  Registr auto+řidič
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_loads_rows_with_types_and_flags(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        p = _reg_file(tmp_path, [
            _reg_line(1, "A"),
            _reg_line(2, "B", typ="do 18t", kg=8000, plan_mes="0", plan_rok="0",
                      akt_mes="0", akt_rok="0"),
            _reg_line(3, "C", days="Po,St,Pá/So-Ne", do="2026-08-31"),
        ])
        rows = load_registry(p, tm)
        assert [r["type_code"] for r in rows] == ["TYPE_02", "TYPE_05", "TYPE_02"]
        assert rows[0]["own_fleet"] is False and rows[1]["own_fleet"] is True
        assert rows[2]["days"] == {0, 2, 4, 5, 6}
        assert rows[2]["avail_to"] == date(2026, 8, 31) and rows[0]["avail_to"] is None
        assert rows[0]["driver"] == "A" and rows[0]["driver_name"] == "Ridic A"
        assert rows[0]["plan_rok"] == 72000 and rows[0]["aktual_mes"] == 500

    def test_missing_capacity_column_is_fatal(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        header = REG_HEADER.rsplit(";", 1)[0]          # bez max_kg
        line = _reg_line(1, "A").rsplit(";", 1)[0]
        with pytest.raises(SystemExit, match="NOSNOST"):
            load_registry(_reg_file(tmp_path, [line], header=header), tm)

    def test_capacity_column_alias_nosnost(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        header = REG_HEADER.replace(";max_kg", ";nosnost")
        rows = load_registry(_reg_file(tmp_path, [_reg_line(1, "A", kg=1200)], header=header), tm)
        assert rows[0]["type_code"] == "TYPE_01"

    def test_missing_required_column_fatal(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        header = REG_HEADER.replace("driver_quality;", "kvalita;")
        with pytest.raises(SystemExit, match="driver_quality"):
            load_registry(_reg_file(tmp_path, [_reg_line(1, "A")], header=header), tm)

    def test_unknown_capacity_row_fatal_and_named(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        p = _reg_file(tmp_path, [_reg_line(1, "A"), _reg_line(2, "B", kg=1300)])
        with pytest.raises(SystemExit, match="id 2"):
            load_registry(p, tm)

    def test_empty_plan_is_not_own_fleet(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        rows = load_registry(_reg_file(tmp_path, [
            _reg_line(1, "A", plan_mes="", plan_rok="", akt_mes="", akt_rok="")]), tm)
        assert rows[0]["own_fleet"] is False and rows[0]["plan_rok"] is None

    def test_find_registry_requires_exactly_one_and_explains_xlsx(self, tmp_path):
        d = tmp_path / "aktivni"; d.mkdir()
        with pytest.raises(SystemExit):
            da.find_registry_file(d)
        (d / "Auta - Ridici - Eso.xlsx").write_bytes(b"x")
        with pytest.raises(SystemExit, match="xlsx"):
            da.find_registry_file(d)
        (d / "vehicles-active-20260819.csv").write_text("x", encoding="utf-8")
        assert da.find_registry_file(d).name == "vehicles-active-20260819.csv"
        (d / "vehicles-active-20260820.csv").write_text("x", encoding="utf-8")
        with pytest.raises(SystemExit):
            da.find_registry_file(d)


class TestAvailability:
    def test_window_from_to(self):
        r = {"avail_from": date(2026, 8, 10), "avail_to": date(2026, 8, 20)}
        assert is_available(r, date(2026, 8, 10)) and is_available(r, date(2026, 8, 20))
        assert not is_available(r, date(2026, 8, 9)) and not is_available(r, date(2026, 8, 21))

    def test_open_end(self):
        r = {"avail_from": date(2026, 8, 10), "avail_to": None}
        assert is_available(r, date(2030, 1, 1))

    def test_no_from_means_available(self):
        assert is_available({"avail_from": None, "avail_to": None}, WED)


# ─────────────────────────────────────────────────────────────────────────────
#  Kontrola: registr použitelný v den závozu == vehicle_types available_count
# ─────────────────────────────────────────────────────────────────────────────

class TestFleetConsistency:
    def test_matching_counts_no_mismatch(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path, [("TYPE_02", "do 3t", 1350, 2),
                                               ("TYPE_05", "do 18t", 8000, 1)]))
        reg = load_registry(_reg_file(tmp_path, [
            _reg_line(1, "A"), _reg_line(2, "B"),
            _reg_line(3, "T", typ="do 18t", kg=8000)]), tm)
        assert fleet_mismatches(reg, tm, WED) == []

    def test_unavailable_or_wrong_day_rows_do_not_count(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path, [("TYPE_02", "do 3t", 1350, 3)]))
        reg = load_registry(_reg_file(tmp_path, [
            _reg_line(1, "A"),
            _reg_line(2, "B", days="Po,Pá"),                # středa nejede
            _reg_line(3, "C", do="2026-08-18"),             # dostupnost skončila
        ]), tm)
        m = fleet_mismatches(reg, tm, WED)
        assert m == [{"type_code": "TYPE_02", "planned": 3, "registry": 1}]

    def test_more_cars_than_planned_is_also_mismatch(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path, [("TYPE_02", "do 3t", 1350, 1)]))
        reg = load_registry(_reg_file(tmp_path, [_reg_line(1, "A"), _reg_line(2, "B")]), tm)
        assert fleet_mismatches(reg, tm, WED)[0]["registry"] == 2


# ─────────────────────────────────────────────────────────────────────────────
#  Tightness, ranky, plnění plánu (rok + měsíc)
# ─────────────────────────────────────────────────────────────────────────────

def _stop(arr, end):
    return {"arrival_min": arr, "window_end_min": end}


class TestLineTightness:
    def test_no_tight_stops_zero(self):
        assert line_tightness([_stop(600, 700), _stop(700, 800)], 15, 0.3) == 0.0

    def test_five_at_end_beats_five_at_start_but_not_seven(self):
        n = 20
        start5 = [_stop(600, 605)] * 5 + [_stop(600, 700)] * 15
        end5 = [_stop(600, 700)] * 15 + [_stop(600, 605)] * 5
        start7 = [_stop(600, 605)] * 7 + [_stop(600, 700)] * 13
        assert len(start5) == len(end5) == len(start7) == n
        t_s5, t_e5, t_s7 = (line_tightness(x, 15, 0.3) for x in (start5, end5, start7))
        assert t_e5 > t_s5 and t_e5 < t_s7

    def test_late_arrival_is_tight(self):
        assert line_tightness([_stop(700, 690)], 15, 0.3) > 0


class TestRanksAndDeficit:
    def test_ranks_span_zero_to_one(self):
        assert percentile_ranks([10, 20, 30]) == [0.0, 0.5, 1.0]

    def test_ties_share_rank(self):
        r = percentile_ranks([10, 10, 30])
        assert r[0] == r[1] < r[2]

    def test_single_value_neutral(self):
        assert percentile_ranks([42]) == [0.5]

    def test_year_deficit_behind_plan_positive(self):
        assert plan_deficit({"plan_rok": 36500.0, "aktual_rok": 5000.0}, 100) > 0

    def test_month_deficit(self):
        # plán 3000/měs, den 15 z 30 -> očekáváno 1500; najeto 500 -> skluz +1/3
        assert plan_deficit_month({"plan_mes": 3000, "aktual_mes": 500}, 15, 30) == pytest.approx(1 / 3)

    def test_deficit_no_data_none(self):
        assert plan_deficit({"plan_rok": None, "aktual_rok": None}, 100) is None
        assert plan_deficit({"plan_rok": 0, "aktual_rok": 100.0}, 100) is None
        assert plan_deficit_month({"plan_mes": 0, "aktual_mes": 1}, 1, 31) is None


class TestPlanScores:
    def _rows(self):
        # A: rok v předstihu, měsíc velký skluz; B: rok skluz, měsíc v předstihu
        a = {"plan_rok": 72000, "aktual_rok": 60000, "plan_mes": 6000, "aktual_mes": 0, "own_fleet": False}
        b = {"plan_rok": 72000, "aktual_rok": 10000, "plan_mes": 6000, "aktual_mes": 6000, "own_fleet": False}
        return a, b

    def test_year_weighs_more_than_month(self):
        a, b = self._rows()
        s, _ = plan_scores([a, b], WED, year_share=0.65)
        assert s[id(b)] > s[id(a)]                 # roční skluz B převáží
        s2, _ = plan_scores([a, b], WED, year_share=0.35)
        assert s2[id(a)] > s2[id(b)]               # kdyby vážil měsíc víc, obráceně

    def test_only_month_data_uses_month(self):
        a = {"plan_rok": None, "aktual_rok": None, "plan_mes": 6000, "aktual_mes": 0, "own_fleet": False}
        b = {"plan_rok": None, "aktual_rok": None, "plan_mes": 6000, "aktual_mes": 6000, "own_fleet": False}
        s, w = plan_scores([a, b], WED)
        assert s[id(a)] == 1.0 and s[id(b)] == 0.0

    def test_no_data_neutral_with_warning(self):
        a = {"plan_rok": None, "aktual_rok": None, "plan_mes": None, "aktual_mes": None, "own_fleet": False}
        s, w = plan_scores([a], WED)
        assert s[id(a)] == 0.5 and any("BEZ DAT" in x for x in w)

    def test_own_fleet_neutral(self):
        a, b = self._rows()
        o = {"plan_rok": 0, "aktual_rok": 0, "plan_mes": 0, "aktual_mes": 0, "own_fleet": True}
        s, _ = plan_scores([a, b, o], WED)
        assert s[id(o)] == 0.5


# ─────────────────────────────────────────────────────────────────────────────
#  Historie + familiarity jako pořadí
# ─────────────────────────────────────────────────────────────────────────────

class TestFamiliarity:
    def test_load_history_both_keys_and_stats(self, tmp_path):
        fam = load_history(_hist_file(tmp_path, [
            ("30", "46542", "2 ms breznice", 12), ("25", "46542", "2 ms breznice", 5),
            ("30", "777", "Hospoda U Lip", 1)]))
        assert fam["id:46542"] == {"30": 12, "25": 5}
        assert fam["note:2 ms breznice"] == {"30": 12, "25": 5}
        assert fam["note:hospoda u lip"] == {"30": 1}
        assert fam["_stats"]["drivers"] == 2 and fam["_stats"]["addresses"] == 2

    def test_history_missing_column_fatal(self, tmp_path):
        p = tmp_path / "h.csv"
        p.write_text("driver;adresa;n\n1;x;2\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            load_history(p)

    def test_ranking_matches_business_example(self):
        # 6 řidičů: jeden 5×, dva 4×, tři 0× -> body 6, 4.5, 4.5, 2, 2, 2
        fam = {"id:1": {"A": 5, "B": 4, "C": 4}}
        drivers = ["A", "B", "C", "D", "E", "F"]
        r = familiarity_ranks(fam, {"id:1"}, drivers)["id:1"]
        points = {d: round(r[d] * 5 + 1, 2) for d in drivers}
        assert points == {"A": 6.0, "B": 4.5, "C": 4.5, "D": 2.0, "E": 2.0, "F": 2.0}

    def test_address_nobody_visited_is_neutral(self):
        r = familiarity_ranks({}, {"id:9"}, ["A", "B", "C"])["id:9"]
        assert set(r.values()) == {0.5}

    def test_find_history_none_or_exactly_one(self, tmp_path):
        d = tmp_path / "h"; d.mkdir()
        assert da.find_history_file(d) is None
        (d / "a.csv").write_text("x", encoding="utf-8")
        assert da.find_history_file(d).name == "a.csv"
        (d / "b.csv").write_text("x", encoding="utf-8")
        with pytest.raises(SystemExit):
            da.find_history_file(d)


# ─────────────────────────────────────────────────────────────────────────────
#  build_assignment — hard filtry, tier, jeden řidič jednou, globální optimum
# ─────────────────────────────────────────────────────────────────────────────

def _row(driver, type_code="TYPE_02", days=None, avail_from=None, avail_to=None,
         dojezd=10.0, kvalita="Standart", vehicle_code=None, own=False,
         plan_rok=None, aktual_rok=None, plan_mes=None, aktual_mes=None):
    return {
        "row_id": f"{driver}_{type_code}", "vehicle_code": vehicle_code or f"V_{driver}",
        "vehicle_name": f"{driver}_auto", "dopravce": "X", "driver": driver,
        "driver_name": f"Ridic {driver}", "type_code": type_code,
        "days": days if days is not None else set(range(7)),
        "avail_from": avail_from, "avail_to": avail_to, "dojezd_km": dojezd,
        "kvalita": kvalita, "plan_rok": plan_rok, "plan_mes": plan_mes,
        "aktual_rok": aktual_rok, "aktual_mes": aktual_mes, "own_fleet": own,
        "valid_for_date": WEDNESDAY,
    }


def _unit(depot="CB", vid="TYPE_02_01", km=100.0, tight=0.0, lines=None,
          stop_keys=()):
    return {"depot": depot, "vehicle_id": vid,
            "type_code": vid.rsplit("_", 1)[0],
            "line_ids": lines or [f"LINE_{vid[-2:]}"], "km": km,
            "tightness_raw": tight, "stops_total": max(3, len(stop_keys)),
            "stop_keys": list(stop_keys)}


class TestHardConstraints:
    def test_wrong_day_excluded(self):
        r = build_assignment([_unit()], [_row("A", days={0, 4})], WEDNESDAY)
        assert r["assigned"] == [] and len(r["uncovered"]) == 1

    def test_not_yet_available_excluded(self):
        r = build_assignment([_unit()], [_row("A", avail_from=date(2026, 8, 20))], WEDNESDAY)
        assert len(r["uncovered"]) == 1

    def test_availability_ended_excluded(self):
        r = build_assignment([_unit()], [_row("A", avail_to=date(2026, 8, 18))], WEDNESDAY)
        assert len(r["uncovered"]) == 1

    def test_wrong_type_excluded_even_if_bigger(self):
        # na linku malého auta NIKDY kamion (ani naše)
        r = build_assignment([_unit(vid="TYPE_02_01")],
                             [_row("T", type_code="TYPE_05", own=True)], WEDNESDAY)
        assert len(r["uncovered"]) == 1

    def test_driver_with_many_vehicles_used_once_across_depots(self):
        rows = [_row("A", type_code="TYPE_02", vehicle_code="V1"),
                _row("A", type_code="TYPE_02", vehicle_code="V2"),
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


class TestOwnFleetTier:
    def test_own_car_not_used_while_contracted_available(self):
        # naše auto má "lepší" soft skóre (blíž, zná adresy) — přesto jede smluvní
        fam = {"id:1": {"O": 20}}
        rows = [_row("S", dojezd=40.0), _row("O", dojezd=1.0, own=True)]
        r = build_assignment([_unit(km=10.0, stop_keys=["id:1"])], rows, WEDNESDAY,
                             familiarity=fam)
        assert r["assigned"][0]["driver"] == "S"
        assert r["assigned"][0]["breakdown"]["tier"] == "smluvní"

    def test_own_car_used_when_lines_exceed_contracted_of_type(self):
        rows = [_row("S"), _row("O", own=True)]
        units = [_unit(vid="TYPE_02_01"), _unit(vid="TYPE_02_02")]
        r = build_assignment(units, rows, WEDNESDAY)
        assert len(r["assigned"]) == 2 and not r["uncovered"]
        assert {a["driver"] for a in r["assigned"]} == {"S", "O"}
        assert any("Nasazeno 1 našich" in w for w in r["warnings"])

    def test_own_small_car_not_replaced_by_own_truck(self):
        # 2 malé linky, 1 smluvní malé, 1 naše malé, 1 náš kamion -> naše malé jede,
        # kamion zůstává (typ nesedí)
        rows = [_row("S"), _row("O", own=True), _row("T", type_code="TYPE_05", own=True)]
        units = [_unit(vid="TYPE_02_01"), _unit(vid="TYPE_02_02")]
        r = build_assignment(units, rows, WEDNESDAY)
        assert {a["driver"] for a in r["assigned"]} == {"S", "O"}

    def test_tier_maximizes_contracted_count_globally(self):
        # 2 linky, smluvní S umí jen TYPE_02, naše O umí TYPE_02 i (jiný řádek) TYPE_04.
        # Bez tieru by mohl S zůstat doma; s tierem jede S na TYPE_02 a O na TYPE_04.
        rows = [_row("S", type_code="TYPE_02"),
                _row("O", type_code="TYPE_02", own=True, vehicle_code="O1"),
                _row("O", type_code="TYPE_04", own=True, vehicle_code="O2")]
        units = [_unit(vid="TYPE_02_01"), _unit(vid="TYPE_04_01")]
        r = build_assignment(units, rows, WEDNESDAY)
        by_unit = {a["unit"]["vehicle_id"]: a["driver"] for a in r["assigned"]}
        assert by_unit == {"TYPE_02_01": "S", "TYPE_04_01": "O"}

    def test_tier_can_be_switched_off(self):
        fam = {"id:1": {"O": 20}}
        rows = [_row("S", dojezd=40.0), _row("O", dojezd=1.0, own=True)]
        r = build_assignment([_unit(km=10.0, stop_keys=["id:1"])], rows, WEDNESDAY,
                             familiarity=fam, own_fleet_last=False)
        assert r["assigned"][0]["driver"] == "O"


class TestSoftScoring:
    def test_global_beats_greedy(self):
        rows = [_row("F", dojezd=40.0), _row("N", dojezd=5.0)]
        units = [_unit("CB", "TYPE_02_01", km=200.0),
                 _unit("HK", "TYPE_02_02", km=300.0)]
        r = build_assignment(units, rows, WEDNESDAY)
        by_unit = {a["unit"]["vehicle_id"]: a["driver"] for a in r["assigned"]}
        assert by_unit == {"TYPE_02_01": "N", "TYPE_02_02": "F"}

    def test_quality_matches_tightness(self):
        rows = [_row("R", kvalita="Rychlý"), _row("P", kvalita="Pomalý")]
        units = [_unit(vid="TYPE_02_01", tight=8.0), _unit(vid="TYPE_02_02", tight=0.0)]
        r = build_assignment(units, rows, WEDNESDAY)
        by_unit = {a["unit"]["vehicle_id"]: a["driver"] for a in r["assigned"]}
        assert by_unit == {"TYPE_02_01": "R", "TYPE_02_02": "P"}

    def test_plan_deficit_drives_choice(self):
        rows = [_row("Z", plan_rok=72000, aktual_rok=1000, plan_mes=6000, aktual_mes=100),
                _row("D", plan_rok=72000, aktual_rok=60000, plan_mes=6000, aktual_mes=5000)]
        r = build_assignment([_unit()], rows, WEDNESDAY)
        assert r["assigned"][0]["driver"] == "Z"

    def test_year_deficit_outweighs_month(self):
        # A: rok předstih / měsíc skluz; B: rok skluz / měsíc předstih -> B
        rows = [_row("A", plan_rok=72000, aktual_rok=60000, plan_mes=6000, aktual_mes=0),
                _row("B", plan_rok=72000, aktual_rok=10000, plan_mes=6000, aktual_mes=6000)]
        r = build_assignment([_unit()], rows, WEDNESDAY)
        assert r["assigned"][0]["driver"] == "B"

    def test_no_plan_data_neutral_with_warning(self):
        r = build_assignment([_unit()], [_row("A"), _row("B")], WEDNESDAY)
        assert any("Plnění plánu BEZ DAT" in w for w in r["warnings"])
        assert r["assigned"][0]["breakdown"]["plneni"] == 0.5

    def test_familiarity_prefers_driver_who_goes_there(self):
        fam = {"id:1": {"A": 7, "B": 1}, "id:2": {"A": 3}}
        rows = [_row("A"), _row("B")]
        u = _unit(stop_keys=["id:1", "id:2"])
        r = build_assignment([u], rows, WEDNESDAY, familiarity=fam)
        assert r["assigned"][0]["driver"] == "A"
        assert r["assigned"][0]["breakdown"]["familiarity"] == 1.0
        assert r["assigned"][0]["breakdown"]["fam_known"] == 2

    def test_familiarity_by_note_key_when_id_unknown(self):
        fam = {"note:hospoda": {"B": 4}}
        rows = [_row("A"), _row("B")]
        r = build_assignment([_unit(stop_keys=["note:hospoda"])], rows, WEDNESDAY, familiarity=fam)
        assert r["assigned"][0]["driver"] == "B"

    def test_no_familiarity_warning(self):
        r = build_assignment([_unit()], [_row("A")], WEDNESDAY, familiarity=None)
        assert any("Familiarity BEZ DAT" in w for w in r["warnings"])


class TestDoubleRunAndOutput:
    def test_double_run_unit_one_driver_two_rows(self):
        u = _unit(lines=["LINE_03", "LINE_07"])
        r = build_assignment([u], [_row("A")], WEDNESDAY)
        rows = result_rows(r)
        assert len(rows) == 2
        assert rows[0]["driver_code"] == rows[1]["driver_code"] == "A"
        assert rows[0]["driver"] == "Ridic A" and rows[0]["dvojlinka"] == "ano"

    def test_uncovered_marked_in_rows(self):
        r = build_assignment([_unit()], [_row("A", days={0})], WEDNESDAY)
        rows = result_rows(r)
        assert "NEPŘIŘAZENO" in rows[0]["driver"]
        assert set(rows[0]) == set(da.CSV_HEADER)

    def test_rows_carry_tier_and_plan_columns(self):
        r = build_assignment([_unit()], [_row("O", own=True, plan_rok=0, plan_mes=0,
                                              aktual_rok=0, aktual_mes=0)], WEDNESDAY)
        row = result_rows(r)[0]
        assert row["tier"] == "naše" and row["plan_rok"] == "0"
        assert set(row) == set(da.CSV_HEADER)

    def test_deterministic_output_order(self):
        rows_reg = [_row("A"), _row("B")]
        units = [_unit("HK", "TYPE_02_02"), _unit("CB", "TYPE_02_01")]
        r1 = result_rows(build_assignment(units, rows_reg, WEDNESDAY))
        r2 = result_rows(build_assignment(list(reversed(units)), rows_reg, WEDNESDAY))
        assert [x["line_id"] for x in r1] == [x["line_id"] for x in r2]


# ─────────────────────────────────────────────────────────────────────────────
#  Linky dne: klíč adresy per zastávka (id z prepared, jinak location_code)
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadDepotLines:
    def _results(self, tmp_path):
        d = tmp_path / "CB" / "2026-08-19"; d.mkdir(parents=True)
        (d / "lines_summary.csv").write_text(
            "line_id,vehicle_id,total_km\nL1,TYPE_02_01,120.5\nL2,TYPE_02_01,30\n,,\n",
            encoding="utf-8")
        (d / "lines_stops.csv").write_text(
            "line_id,order_id,location_code,arrival,window\n"
            "L1,,SKLAD,05:00,\n"
            "L1,O1,Damartie,08:00,07:00–09:00\n"
            "L1,O2,Nautilus,09:00,08:00–09:05\n"
            "L2,O3,Konibar,12:00,11:00–13:00\n", encoding="utf-8")
        return d

    def test_keys_use_id_when_known_else_note(self, tmp_path):
        d = self._results(tmp_path)
        units = da.load_depot_lines(d, "CB", {"O1": "44088"})
        assert len(units) == 1
        u = units[0]
        assert u["line_ids"] == ["L1", "L2"] and u["km"] == 150.5
        assert u["stop_keys"] == ["id:44088", "note:nautilus", "note:konibar"]
        assert u["stops_total"] == 3

    def test_order_addresses_from_prepared(self, tmp_path):
        p = tmp_path / "prep" / "CB"; p.mkdir(parents=True)
        (p / "orders_CB_2026-08-19.csv").write_text(
            "order_number,location_code,eso_col7\nO1,damartie,44088\nO2,x,\n", encoding="utf-8")
        assert da.load_order_addresses(tmp_path / "prep", "CB", "2026-08-19") == {"O1": "44088"}
        assert da.load_order_addresses(tmp_path / "prep", "MO", "2026-08-19") == {}


# ─────────────────────────────────────────────────────────────────────────────
#  Registr s vlastním type_code (export od 20. 8. 2026) — křížová kontrola
# ─────────────────────────────────────────────────────────────────────────────

REG_HEADER_TC = REG_HEADER + ";type_code"


def _reg_line_tc(i, driver, code, **kw):
    return _reg_line(i, driver, **kw) + f";{code}"


class TestRegistryTypeCodeColumn:
    def test_type_code_from_export_used_when_consistent(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        rows = load_registry(_reg_file(tmp_path, [
            _reg_line_tc(1, "A", "TYPE_02"),
            _reg_line_tc(2, "T", "TYPE_05", typ="do 18t", kg=8000),
        ], header=REG_HEADER_TC), tm)
        assert [r["type_code"] for r in rows] == ["TYPE_02", "TYPE_05"]
        assert rows[1]["max_kg"] == 8000

    def test_inconsistent_code_is_fatal_when_strict(self, tmp_path):
        # registr (jiný den) čísluje 18t jako TYPE_04; vozový park dne má TYPE_04 = 7t/3200
        tm = load_type_map(_vt_file(tmp_path))
        p = _reg_file(tmp_path, [_reg_line_tc(1, "T", "TYPE_04", typ="do 18t", kg=8000)],
                      header=REG_HEADER_TC)
        with pytest.raises(SystemExit, match="TYPE_04"):
            load_registry(p, tm, strict_types=True)

    def test_inconsistent_code_tolerated_with_force(self, tmp_path, capsys):
        tm = load_type_map(_vt_file(tmp_path))
        p = _reg_file(tmp_path, [_reg_line_tc(1, "T", "TYPE_04", typ="do 18t", kg=8000)],
                      header=REG_HEADER_TC)
        rows = load_registry(p, tm, strict_types=False)
        assert rows[0]["type_code"] == "TYPE_04"          # kód z registru, tak jak je
        assert "--force" in capsys.readouterr().out

    def test_unknown_code_is_fatal(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        p = _reg_file(tmp_path, [_reg_line_tc(1, "A", "TYPE_99")], header=REG_HEADER_TC)
        with pytest.raises(SystemExit, match="TYPE_99"):
            load_registry(p, tm)

    def test_only_type_code_without_capacity(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        header = REG_HEADER.rsplit(";", 1)[0] + ";type_code"        # bez max_kg
        line = _reg_line(1, "A").rsplit(";", 1)[0] + ";TYPE_01"
        rows = load_registry(_reg_file(tmp_path, [line], header=header), tm)
        assert rows[0]["type_code"] == "TYPE_01" and rows[0]["max_kg"] == 1200   # kg doplněno z parku

    def test_neither_code_nor_capacity_is_fatal(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path))
        header = REG_HEADER.rsplit(";", 1)[0]
        line = _reg_line(1, "A").rsplit(";", 1)[0]
        with pytest.raises(SystemExit, match="TYPE kód"):
            load_registry(_reg_file(tmp_path, [line], header=header), tm)

    def test_fleet_consistency_uses_export_codes(self, tmp_path):
        tm = load_type_map(_vt_file(tmp_path, [("TYPE_02", "do 3t", 1350, 1),
                                               ("TYPE_05", "do 18t", 8000, 1)]))
        reg = load_registry(_reg_file(tmp_path, [
            _reg_line_tc(1, "A", "TYPE_02"),
            _reg_line_tc(2, "T", "TYPE_05", typ="do 18t", kg=8000)], header=REG_HEADER_TC), tm)
        assert fleet_mismatches(reg, tm, WED) == []
