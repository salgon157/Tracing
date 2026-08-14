"""
test_l3_planner.py — výběr rampových objednávek pro kamion (L3).

Syntetická data; pravidla z diskuse 14. 8. 2026: jen rampa + skutečné,
cíl = missing + max(10 %, 500), blízkost se cení, „vzít co je",
binování do kamionů bez dělení lokace.
"""
import csv

import pytest

import l3_planner as l3
from l3_planner import (
    build_l3_decision_block,
    haversine_km,
    l3_target_kg,
    load_l3_candidates,
    merge_l3_orders,
    orders_by_depot,
    select_locations,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Cíl kg + vzdálenost
# ─────────────────────────────────────────────────────────────────────────────

class TestTargetKg:
    def test_small_missing_uses_min_buffer(self):
        # 10 % z 2000 = 200 < 500 → buffer 500
        assert l3_target_kg(2000.0) == pytest.approx(2500.0)

    def test_large_missing_uses_pct(self):
        # 10 % z 10000 = 1000 > 500
        assert l3_target_kg(10_000.0) == pytest.approx(11_000.0)


class TestHaversine:
    def test_zero_distance(self):
        assert haversine_km(49.4, 15.6, 49.4, 15.6) == pytest.approx(0.0)

    def test_praha_brno_ballpark(self):
        # vzdušně ~185 km
        assert haversine_km(50.08, 14.42, 49.19, 16.61) == pytest.approx(185, abs=10)


# ─────────────────────────────────────────────────────────────────────────────
#  Kandidáti z prepared
# ─────────────────────────────────────────────────────────────────────────────

PREP_HEADER = ("order_number,location_code,customer_name,block_id,time_from,"
               "time_to,payload_raw,weight_kg,lat,lon,city,note,service_sec,"
               "street,zip,country,eso_col7,eso_col13,ramp,predicted")


def _prep_row(order="O1", loc="loc1", kg=100.0, ramp=1, predicted=0,
              lat=49.4, lon=15.6):
    return (f"{order},{loc},Zakaznik,CB,06:00,10:00,KG:{kg}#SEC:300,{kg},"
            f"{lat},{lon},Mesto,,300,Ulice 1,58601,CZ,1,2,{ramp},{predicted}")


def _write_prepared(tmp_path, depot, rows, date="2026-08-14"):
    d = tmp_path / depot
    d.mkdir(parents=True, exist_ok=True)
    (d / f"orders_{depot}_{date}.csv").write_text(
        PREP_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


class TestLoadCandidates:
    def test_only_ramp_and_real(self, tmp_path):
        _write_prepared(tmp_path, "CB", [
            _prep_row("O1", "ramp_real", ramp=1, predicted=0),
            _prep_row("O2", "bez_rampy", ramp=0, predicted=0),
            _prep_row("O3", "ramp_predikce", ramp=1, predicted=1),
        ])
        cands = load_l3_candidates(tmp_path, ["CB"], "2026-08-14")
        assert [c["location_code"] for c in cands] == ["ramp_real"]

    def test_aggregates_orders_per_location(self, tmp_path):
        _write_prepared(tmp_path, "CB", [
            _prep_row("O1", "velka", kg=800.0),
            _prep_row("O2", "velka", kg=700.0),
            _prep_row("O3", "mala", kg=100.0),
        ])
        cands = load_l3_candidates(tmp_path, ["CB"], "2026-08-14")
        assert cands[0]["location_code"] == "velka"     # nejtěžší první
        assert cands[0]["kg"] == pytest.approx(1500.0)
        assert len(cands[0]["orders"]) == 2

    def test_missing_prepared_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_l3_candidates(tmp_path, ["CB"], "2026-08-14")


# ─────────────────────────────────────────────────────────────────────────────
#  Výběr lokací
# ─────────────────────────────────────────────────────────────────────────────

def _loc(code, kg, lat=49.4, lon=15.6, depot="CB"):
    return {"location_code": code, "depot": depot, "customer_name": code,
            "kg": kg, "lat": lat, "lon": lon,
            "orders": [{"order_number": f"O_{code}", "kg": kg}]}


class TestSelectLocations:
    def test_reaches_target_with_heaviest(self):
        cands = [_loc("a", 2000), _loc("b", 1500), _loc("c", 100)]
        sel = select_locations(cands, target_kg=3000, truck_caps_kg=[8000])
        assert [l["location_code"] for l in sel["selected"]] == ["a", "b"]
        assert sel["exhausted"] is False

    def test_take_what_is_there_when_short(self):
        cands = [_loc("a", 400), _loc("b", 300)]
        sel = select_locations(cands, target_kg=5000, truck_caps_kg=[8000])
        assert sel["selected_kg"] == pytest.approx(700.0)
        assert sel["exhausted"] is True

    def test_proximity_preferred_between_similar_kg(self):
        # seed = nejtěžší (Jihlava); "blizko" je o fous lehčí než "daleko",
        # ale stokrát blíž → vyhrává
        cands = [_loc("seed", 3000, lat=49.40, lon=15.60),
                 _loc("blizko", 1000, lat=49.42, lon=15.62),
                 _loc("daleko", 1050, lat=50.70, lon=13.80)]
        sel = select_locations(cands, target_kg=3800, truck_caps_kg=[9000])
        assert [l["location_code"] for l in sel["selected"]] == \
            ["seed", "blizko"]

    def test_location_never_split_across_trucks(self):
        # 5000 se nevejde do zaplněného 8000 (zbývá 3000) ani do 4000
        cands = [_loc("a", 5000), _loc("b", 5000), _loc("c", 5000)]
        sel = select_locations(cands, target_kg=99_999,
                               truck_caps_kg=[8000, 4000])
        assert sel["selected_kg"] == pytest.approx(5000.0)
        assert [len(b) for b in sel["bins"]] == [1, 0]

    def test_two_trucks_bin_packing(self):
        cands = [_loc("a", 7000), _loc("b", 6000), _loc("c", 1500)]
        sel = select_locations(cands, target_kg=99_999,
                               truck_caps_kg=[8000, 8700])
        assert sel["selected_kg"] == pytest.approx(14_500.0)
        assert sum(len(b) for b in sel["bins"]) == 3

    def test_no_trucks_no_selection(self):
        sel = select_locations([_loc("a", 1000)], 500, truck_caps_kg=[])
        assert sel["selected"] == []

    def test_deterministic(self):
        cands = [_loc(c, kg) for c, kg in
                 [("a", 900), ("b", 900), ("c", 800)]]
        s1 = select_locations(list(cands), 2000, [8000])
        s2 = select_locations(list(reversed(cands)), 2000, [8000])
        assert [l["location_code"] for l in s1["selected"]] == \
            [l["location_code"] for l in s2["selected"]]


# ─────────────────────────────────────────────────────────────────────────────
#  Decision blok + merge
# ─────────────────────────────────────────────────────────────────────────────

class TestDecisionBlockAndMerge:
    def test_block_carries_orders_and_trucks(self):
        sel = select_locations([_loc("a", 2000, depot="HK")], 1500, [8000])
        block = build_l3_decision_block(sel, missing_kg=1000,
                                        trucks_by_type={"TYPE_06": 1})
        assert block["orders"][0]["order_number"] == "O_a"
        assert block["orders"][0]["depot"] == "HK"
        assert block["trucks"] == {"TYPE_06": 1}
        assert block["trucks_used"] == 1
        assert orders_by_depot(block) == {"HK": ["O_a"]}

    def test_merge_overrides_window_and_block(self, tmp_path):
        _write_prepared(tmp_path, "CB", [_prep_row("O1", "x", kg=500.0)])
        src = tmp_path / "CB" / "orders_CB_2026-08-14.csv"
        out = tmp_path / "L3" / "orders_L3_2026-08-14.csv"
        n = merge_l3_orders([src], out)
        assert n == 1
        row = next(csv.DictReader(open(out, encoding="utf-8")))
        assert row["block_id"] == "L3"
        assert row["time_from"] == l3.L3_CONFIG["window_from"]
        assert row["time_to"] == l3.L3_CONFIG["window_to"]

    def test_merge_empty_raises(self, tmp_path):
        empty = tmp_path / "empty.csv"
        empty.write_text(PREP_HEADER + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            merge_l3_orders([empty], tmp_path / "out.csv")
