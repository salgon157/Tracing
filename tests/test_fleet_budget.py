"""
test_fleet_budget.py — rozpočet flotily, rezervace velkých aut, rozhodnutí

Jádro predikcí řízeného plánování (plan_day.py): malá auta se v predikci
neomezují (deficit se MĚŘÍ), velká se rezervují podle P1 a ubírají z budgetu.
Rezervace chrání jen depa, která ještě nebyla na řadě.
"""
import pytest

from fleet_budget import (
    DEPOT_ORDER,
    L3_THRESHOLD_PCT,
    SMALL_FLEET_RESERVE,
    UNLIMITED_LARGE_COUNT,
    FleetBudget,
    allocate_reservations,
    available_by_type,
    caps_for_depot,
    count_by_type,
    decide_level,
    is_small,
    load_fleet_rows,
    p1_overrides,
    parse_lines_summary,
    small_type_codes,
    solver_flags_for_level,
    write_fleet_file,
)

HEADER = ("type_code;type_name;max_kg;cost_per_km;start_cost_kc;"
          "available_count;total_count;active_count;profiles;"
          "cost_per_km_source;available_count_source;time_multiplier;"
          "osrm_profile;valid_for_date\n")


def _fleet_file(tmp_path, rows=None):
    rows = rows or [
        "TYPE_01;Dodávka 1.2t;1200;11.0;1000;3;3;3;Malé auto;c;n;1.0;driving;20260805\n",
        "TYPE_02;Dodávka 1.35t;1350;11.0;1000;53;53;53;Malé auto;c;n;1.0;driving;20260805\n",
        "TYPE_03;Nákladní 2t;2000;17.4;1000;1;1;1;Velké auto;c;n;1.0;driving;20260805\n",
        "TYPE_05;Nákladní 3.2t;3200;19.5;1000;2;2;2;Velké auto;c;n;1.0;driving;20260805\n",
        "TYPE_07;Kamion 8.7t;8700;35.0;1000;1;1;1;Velké auto;c;n;1.0;driving-hgv;20260805\n",
    ]
    p = tmp_path / "vehicle_types-test.csv"
    p.write_text(HEADER + "".join(rows), encoding="utf-8")
    return p


def _line(zone="CB", line_id="LINE_01", type_code="TYPE_02", kg=1000.0):
    return {"zone": zone, "line_id": line_id, "type_code": type_code,
            "total_kg": kg}


# ═════════════════════════════════════════════════════════════════════════════
#  Flotila: malá/velká, generované soubory
# ═════════════════════════════════════════════════════════════════════════════

class TestFleetRows:
    def test_small_vs_large_by_max_kg(self, tmp_path):
        rows = load_fleet_rows(_fleet_file(tmp_path))
        assert small_type_codes(rows) == {"TYPE_01", "TYPE_02"}
        assert [r["type_code"] for r in rows if not is_small(r)] == \
            ["TYPE_03", "TYPE_05", "TYPE_07"]

    def test_available_by_type(self, tmp_path):
        rows = load_fleet_rows(_fleet_file(tmp_path))
        assert available_by_type(rows) == {
            "TYPE_01": 3, "TYPE_02": 53, "TYPE_03": 1,
            "TYPE_05": 2, "TYPE_07": 1}

    def test_p1_overrides_only_large(self, tmp_path):
        rows = load_fleet_rows(_fleet_file(tmp_path))
        ov = p1_overrides(rows)
        # malá se nepřepisují — plný sklad je pro jedno depo neomezený
        assert "TYPE_01" not in ov and "TYPE_02" not in ov
        assert ov == {"TYPE_03": UNLIMITED_LARGE_COUNT,
                      "TYPE_05": UNLIMITED_LARGE_COUNT,
                      "TYPE_07": UNLIMITED_LARGE_COUNT}

    def test_p1_override_keeps_higher_available(self, tmp_path):
        rows = load_fleet_rows(_fleet_file(tmp_path, rows=[
            f"TYPE_06;Kamion;8000;35.0;1000;{UNLIMITED_LARGE_COUNT + 5};20;20;"
            f"Velké auto;c;n;1.0;driving-hgv;20260805\n"]))
        assert p1_overrides(rows)["TYPE_06"] == UNLIMITED_LARGE_COUNT + 5

    def test_written_file_loads_in_solver(self, tmp_path):
        # vygenerovaný soubor musí projít ostrým loaderem solveru
        from vrp_solver_lines_v6 import load_vehicle_types_db
        rows = load_fleet_rows(_fleet_file(tmp_path))
        out = write_fleet_file(rows, tmp_path / "gen" / "vehicle_types-p2.csv",
                               overrides={"TYPE_02": 7, "TYPE_05": 0})
        vehicles = load_vehicle_types_db(str(out))
        by_type = count_by_type(
            [{"type_code": v["type_code"]} for v in vehicles])
        assert by_type["TYPE_02"] == 7
        assert "TYPE_05" not in by_type          # 0 kusů = nepoužitelný
        assert by_type["TYPE_01"] == 3           # bez override beze změny

    def test_missing_columns_rejected(self, tmp_path):
        p = tmp_path / "vehicle_types-bad.csv"
        p.write_text("type_code;type_name\nT1;X\n", encoding="utf-8")
        with pytest.raises(ValueError, match="povinné sloupce"):
            load_fleet_rows(p)


# ═════════════════════════════════════════════════════════════════════════════
#  lines_summary parsování
# ═════════════════════════════════════════════════════════════════════════════

class TestParseLinesSummary:
    def test_lines_parsed_and_celkem_skipped(self, tmp_path):
        p = tmp_path / "lines_summary.csv"
        p.write_text(
            "zone,line_id,vehicle_id,vehicle_type,cost_per_km,total_km,"
            "duration_h,total_kg,total_cost_kc\n"
            "CB,LINE_01,TYPE_02_01,Dodávka 1.35t,11.0,200.0,8.0,1250.5,3200\n"
            "CB,LINE_02,TYPE_05_01,Nákladní 3.2t,19.5,180.0,7.0,3100.0,4510\n"
            "CELKEM,2 linek,,,,380.0,15.0,4350.5,7710\n",
            encoding="utf-8")
        lines = parse_lines_summary(p)
        assert len(lines) == 2                     # CELKEM přeskočen
        assert lines[0]["type_code"] == "TYPE_02"  # z vehicle_id, ne z názvu
        assert lines[1]["type_code"] == "TYPE_05"
        assert lines[0]["total_kg"] == pytest.approx(1250.5)


# ═════════════════════════════════════════════════════════════════════════════
#  Rezervace z P1
# ═════════════════════════════════════════════════════════════════════════════

class TestAllocateReservations:
    def test_no_overflow_wishes_pass_whole(self):
        result = allocate_reservations(
            {"CB": [_line(type_code="TYPE_05", kg=3000)],
             "MO": [_line(zone="MO", type_code="TYPE_05", kg=2000)]},
            large_available={"TYPE_05": 2, "TYPE_07": 1})
        assert result["reservations"]["CB"] == {"TYPE_05": 1}
        assert result["reservations"]["MO"] == {"TYPE_05": 1}
        assert result["free_pool"] == {"TYPE_05": 0, "TYPE_07": 1}
        assert result["truncated"] == []

    def test_overflow_ranked_by_kg(self):
        # 3 přání na 2 kusy — kus dostanou dvě nejnaloženější linky
        result = allocate_reservations(
            {"CB": [_line(type_code="TYPE_05", kg=3100)],
             "MO": [_line(zone="MO", type_code="TYPE_05", kg=2100)],
             "HK": [_line(zone="HK", type_code="TYPE_05", kg=2900)]},
            large_available={"TYPE_05": 2})
        assert result["reservations"]["CB"] == {"TYPE_05": 1}
        assert result["reservations"]["HK"] == {"TYPE_05": 1}
        assert result["reservations"]["MO"] == {}
        assert result["truncated"] == [
            {"type": "TYPE_05", "wanted": 3, "available": 2}]
        assert result["free_pool"]["TYPE_05"] == 0

    def test_one_depot_may_take_all_units(self):
        # obě nejnaloženější linky z téhož depa -> dostane oba kusy
        result = allocate_reservations(
            {"CB": [_line(line_id="LINE_01", type_code="TYPE_05", kg=3100),
                    _line(line_id="LINE_02", type_code="TYPE_05", kg=3000)],
             "MO": [_line(zone="MO", type_code="TYPE_05", kg=1500)]},
            large_available={"TYPE_05": 2})
        assert result["reservations"]["CB"] == {"TYPE_05": 2}
        assert result["reservations"]["MO"] == {}

    def test_tie_broken_deterministically(self):
        # stejné kg -> rozhoduje abecedně depo (stabilní opakovatelný výsledek)
        result = allocate_reservations(
            {"MO": [_line(zone="MO", type_code="TYPE_07", kg=5000)],
             "CB": [_line(type_code="TYPE_07", kg=5000)]},
            large_available={"TYPE_07": 1})
        assert result["reservations"]["CB"] == {"TYPE_07": 1}

    def test_small_lines_ignored(self):
        result = allocate_reservations(
            {"CB": [_line(type_code="TYPE_02", kg=1300),
                    _line(line_id="LINE_02", type_code="TYPE_07", kg=6000)]},
            large_available={"TYPE_07": 1})
        assert result["wishes"]["CB"] == {"TYPE_07": 1}
        assert result["reservations"]["CB"] == {"TYPE_07": 1}


# ═════════════════════════════════════════════════════════════════════════════
#  Budget + caps
# ═════════════════════════════════════════════════════════════════════════════

class TestFleetBudget:
    def test_consume_and_negative_guard(self):
        b = FleetBudget({"TYPE_05": 2, "TYPE_07": 1})
        b.consume({"TYPE_05": 1})
        assert b.remaining == {"TYPE_05": 1, "TYPE_07": 1}
        with pytest.raises(ValueError, match="přetekl do mínusu"):
            b.consume({"TYPE_07": 2}, context="MO")

    def test_unknown_type_rejected(self):
        with pytest.raises(ValueError, match="nezná typ"):
            FleetBudget({"TYPE_05": 1}).consume({"TYPE_99": 1})

    def test_save_load_roundtrip(self, tmp_path):
        b = FleetBudget({"TYPE_05": 2})
        b.save(tmp_path / "budget.json")
        assert FleetBudget.load(tmp_path / "budget.json").remaining == \
            {"TYPE_05": 2}


class TestCapsForDepot:
    SMALL = {"TYPE_01", "TYPE_02"}
    SMALL_FULL = {"TYPE_01": 3, "TYPE_02": 53}

    def _caps(self, depot, budget, reservations):
        return caps_for_depot(depot, DEPOT_ORDER, budget, reservations,
                              self.SMALL, self.SMALL_FULL)

    def test_future_reservation_protected(self):
        # 2 kusy TYPE_05: 1 rezervace PR -> CB smí sáhnout jen na 1
        budget = FleetBudget({"TYPE_02": 53, "TYPE_05": 2})
        caps = self._caps("CB", budget, {"PR": {"TYPE_05": 1}})
        assert caps["TYPE_05"] == 1

    def test_own_reservation_available(self):
        budget = FleetBudget({"TYPE_02": 53, "TYPE_05": 2})
        caps = self._caps("MO", budget, {"MO": {"TYPE_05": 1},
                                         "PR": {"TYPE_05": 1}})
        assert caps["TYPE_05"] == 1     # svoje 1; PR kus chráněný

    def test_released_after_depot_planned(self):
        # CB kus nevyužilo -> po jeho tahu (rezervace CB už se nepočítá)
        # ho MO dostane přes volný pool
        budget = FleetBudget({"TYPE_02": 53, "TYPE_05": 2})
        caps = self._caps("MO", budget, {"CB": {"TYPE_05": 1},
                                         "PR": {"TYPE_05": 1}})
        assert caps["TYPE_05"] == 1     # CB nevyužitý kus + PR chráněný

    def test_last_depot_takes_everything(self):
        budget = FleetBudget({"TYPE_02": 53, "TYPE_05": 2, "TYPE_07": 1})
        caps = self._caps("PR", budget, {"PR": {"TYPE_05": 1}})
        assert caps["TYPE_05"] == 2 and caps["TYPE_07"] == 1

    def test_free_pool_usable_without_reservation(self):
        # HK si nic nepřálo, ale volný kus (nikým nerezervovaný) použít smí
        budget = FleetBudget({"TYPE_02": 53, "TYPE_07": 1})
        caps = self._caps("HK", budget, {})
        assert caps["TYPE_07"] == 1

    def test_small_always_full_in_prediction(self):
        # malá se z budgetu NEodečítají — P2 měří jejich skutečnou potřebu
        budget = FleetBudget({"TYPE_01": 3, "TYPE_02": 53, "TYPE_05": 2})
        budget.consume({"TYPE_05": 1})
        caps = self._caps("PR", budget, {})
        assert caps["TYPE_02"] == 53 and caps["TYPE_01"] == 3
        assert caps["TYPE_05"] == 1

    def test_consumed_by_earlier_depot_gone(self):
        budget = FleetBudget({"TYPE_02": 53, "TYPE_05": 2})
        budget.consume({"TYPE_05": 2}, context="CB")
        caps = self._caps("MO", budget, {})
        assert caps["TYPE_05"] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  Rozhodnutí o porušení
# ═════════════════════════════════════════════════════════════════════════════

class TestDecideLevel:
    def _small_lines(self, kgs, zone="CB"):
        return [_line(zone=zone, line_id=f"LINE_{i:02d}", kg=kg)
                for i, kg in enumerate(kgs, start=1)]

    def test_enough_cars_level_0(self):
        d = decide_level(self._small_lines([1000] * 50), small_available=56,
                         day_kg=60000)
        assert d["level"] == 0 and d["dvojlinky"] is False
        assert d["l3_needed"] is False and d["deficit"] == 0

    def test_reserve_counts(self):
        # 56 linek na 56 aut: bez rezervy OK, s rezervou 1 už deficit
        d = decide_level(self._small_lines([1000] * 56), small_available=56,
                         day_kg=60000, reserve=1)
        assert d["deficit"] == 1 and d["level"] == 1

    def test_small_deficit_l1_l2(self):
        # deficit 2, nejméně naložené 300+400=700 kg z 50 000 = 1,4 % <= 3 %
        lines = self._small_lines([300, 400] + [1200] * 55)
        d = decide_level(lines, small_available=56, day_kg=50000)
        assert d["deficit"] == 2 and d["x_need"] == 2
        assert d["missing_kg"] == pytest.approx(700)
        assert d["level"] == 1 and d["dvojlinky"] is True
        assert d["l3_needed"] is False

    def test_big_deficit_needs_l3(self):
        # deficit 3 × ~1200 kg z 20 000 = 18 % > 3 %
        lines = self._small_lines([1150, 1200, 1250] + [1300] * 55)
        d = decide_level(lines, small_available=56, day_kg=20000)
        assert d["l3_needed"] is True
        assert d["dvojlinky"] is True          # L1+L2 jedou i tak

    def test_x_need_takes_least_loaded(self):
        # 58 linek na 55 použitelných (56 − rezerva 1) = deficit 3
        lines = self._small_lines([900, 100, 500] + [1300] * 55)
        d = decide_level(lines, small_available=56, day_kg=60000)
        assert d["deficit"] == 3
        assert d["missing_kg"] == pytest.approx(1500)     # 100 + 500 + 900
        assert {l["total_kg"] for l in d["x_need_lines"]} == {100, 500, 900}

    def test_threshold_boundary(self):
        # přesně 3,0 % ještě NENÍ potřeba L3 (hranice je ">"):
        # 56 linek na 55 použitelných = deficit 1, chybí 300 kg z 10 000
        lines = self._small_lines([300] + [1300] * 55)
        d = decide_level(lines, small_available=56, day_kg=10000,
                         reserve=1, threshold_pct=3.0)
        assert d["deficit"] == 1
        assert d["missing_pct"] == pytest.approx(3.0)
        assert d["l3_needed"] is False

    def test_zero_day_kg_guard(self):
        d = decide_level(self._small_lines([100] * 57), small_available=56,
                         day_kg=0)
        assert d["l3_needed"] is True          # 100 % > práh, žádné dělení nulou

    def test_solver_flags(self):
        assert solver_flags_for_level({"level": 0}) == \
            {"capacity_multiplier": 1.0, "double_runs": False}
        assert solver_flags_for_level({"level": 1}) == \
            {"capacity_multiplier": 1.03, "double_runs": True}
