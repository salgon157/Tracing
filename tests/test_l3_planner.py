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


# ─────────────────────────────────────────────────────────────────────────────
#  VRP výběr (hlavní cesta) — sjízdnost celé smyčky, ne okruh
# ─────────────────────────────────────────────────────────────────────────────

import math  # noqa: E402

DRIVER = {"break_after_h": 4.5, "break_min": 45, "max_drive_h": 9.0,
          "max_stops": 0, "slack_min": 60}
TRUCK = {"type_code": "TYPE_05", "max_kg": 8000, "cost_per_km": 28.0,
         "start_cost": 1000}


def _cand(code, kg, x_km, y_km, depot="CB", service_sec=600):
    # rovina v km; lat/lon jen formálně (VRP je nepoužívá)
    return {"location_code": code, "depot": depot, "customer_name": code,
            "kg": float(kg), "service_sec": service_sec,
            "lat": 49.0 + y_km / 111.0, "lon": 15.0 + x_km / 71.0,
            "_xy": (x_km, y_km),
            "orders": [{"order_number": f"O_{code}", "kg": float(kg)}]}


def _matrices(cands, speed_kmh=60.0):
    """Euklidovská matice v km + minuty při dané rychlosti; uzel 0 = sklad (0,0)."""
    pts = [(0.0, 0.0)] + [c["_xy"] for c in cands]
    n = len(pts)
    dist = [[math.dist(pts[i], pts[j]) for j in range(n)] for i in range(n)]
    dur = [[d / speed_kmh * 60 for d in row] for row in dist]
    return dist, dur


def _vrp(cands, trucks, target, missing, **kw):
    dist, dur = _matrices(cands)
    kw.setdefault("time_limit_sec", 1)
    kw.setdefault("driver", DRIVER)
    return l3.select_locations_vrp(cands, dist, dur, trucks, target, missing, **kw)


class TestSelectLocationsVrp:
    def test_square_corners_beat_light_cluster(self):
        # Zadání uživatele 16. 8. 2026: 3 × 1 000 kg v rozích 100km čtverce
        # (smyčka ~400 km ≈ 6,7 h) je lepší náklad než 20 × 70 kg v okruhu
        # 40 km — pokud je smyčka sjízdná. Obojí najednou se do 9 h nevejde.
        heavy = [_cand("H1", 1000, 0, 100), _cand("H2", 1000, 100, 100),
                 _cand("H3", 1000, 100, 0)]
        light = [_cand(f"L{i:02d}", 70, 30 * math.cos(a), 30 * math.sin(a))
                 for i, a in enumerate(
                     [k * 2 * math.pi / 20 for k in range(20)])]
        sel = _vrp(heavy + light, [TRUCK], target=6000, missing=5000)
        codes = {c["location_code"] for c in sel["selected"]}
        assert {"H1", "H2", "H3"} <= codes, codes
        assert sel["selected_kg"] >= 3000
        r = sel["routes"][0]
        assert r["drive_min"] <= DRIVER["max_drive_h"] * 60
        assert r["span_min"] <= 16 * 60 + 1          # okno 04:00–20:00
        # když je všech 20 lehkých na dosah, těžké rohy stejně nesmí chybět —
        # to je přesně to, co okruh 40 km neuměl

    def test_far_locations_over_drive_limit_are_dropped(self):
        # každá 330 km daleko = 11 h tam a zpět > 9 h → nic není sjízdné
        cands = [_cand(f"F{i}", 1500, 330 * math.cos(a), 330 * math.sin(a))
                 for i, a in enumerate([0, 1.5, 3.0, 4.5])]
        sel = _vrp(cands, [TRUCK], target=4000, missing=3000)
        assert sel["selected"] == []
        assert sel["exhausted"] is True
        assert len(sel["dropped"]) == 4

    def test_two_trucks_take_two_far_clusters(self):
        # dva shluky po ~7 h jízdy na opačných stranách: 1 kamion zvládne
        # jeden, 2 kamiony oba
        east = [_cand(f"E{i}", 900, 200 + 5 * i, 5 * i) for i in range(3)]
        west = [_cand(f"W{i}", 900, -200 - 5 * i, 5 * i) for i in range(3)]
        one = _vrp(east + west, [TRUCK], target=9000, missing=5400,
                   kg_value_kc=50)
        two = _vrp(east + west, [TRUCK, dict(TRUCK)], target=9000,
                   missing=5400, kg_value_kc=50)
        assert one["selected_kg"] <= 2700 + 1e-6
        assert two["selected_kg"] == pytest.approx(5400)
        assert len(two["routes"]) == 2
        for r in two["routes"]:
            assert r["drive_min"] <= DRIVER["max_drive_h"] * 60

    def test_cap_at_target_stops_overloading(self):
        # 5 × 1 000 kg vedle skladu, cíl 2 500 → nejvýš 2 000 kg
        cands = [_cand(f"N{i}", 1000, 5 + i, 5) for i in range(5)]
        sel = _vrp(cands, [TRUCK], target=2500, missing=2000)
        assert sel["selected_kg"] <= 2500
        assert sel["selected_kg"] == pytest.approx(2000)
        assert sel["exhausted"] is False

    def test_lambda_escalation_reaches_missing(self):
        # jediná lokace 150 km daleko (300 km × 28 = 8 400 Kč) s 1 000 kg:
        # λ=6 → penále 6 000 < 8 400 → vynechá; eskalace ×3 → 18 000 → vezme
        cands = [_cand("X", 1000, 150, 0)]
        sel = _vrp(cands, [TRUCK], target=1600, missing=1000,
                   kg_value_kc=6.0, escalation=(1, 3, 10))
        assert [c["location_code"] for c in sel["selected"]] == ["X"]
        assert sel["kg_value_kc"] == pytest.approx(18.0)
        assert sel["exhausted"] is False
        # bez eskalace zůstane vynechaná
        no = _vrp(cands, [TRUCK], target=1600, missing=1000,
                  kg_value_kc=6.0, escalation=(1,))
        assert no["selected"] == [] and no["exhausted"] is True

    def test_breaks_counted_on_long_route(self):
        # 3 h tam, 3 h zpět = 6 h jízdy → v elapsed režimu aspoň 1 pauza
        cands = [_cand("D", 2000, 180, 0)]
        sel = _vrp(cands, [TRUCK], target=3000, missing=1000, kg_value_kc=50)
        assert sel["selected"] and sel["routes"][0]["breaks"] >= 1
        assert sel["routes"][0]["span_min"] >= 6 * 60 + 45

    def test_mandatory_mode_flags_infeasible(self):
        far = [_cand("F", 500, 330, 0)]
        chk = l3.check_l3_feasible(far, *_matrices(far), [TRUCK],
                                   time_limit_sec=1, driver=DRIVER)
        assert chk["feasible"] is False
        near = [_cand("N", 500, 20, 0), _cand("M", 400, 25, 5)]
        chk2 = l3.check_l3_feasible(near, *_matrices(near), [TRUCK],
                                    time_limit_sec=1, driver=DRIVER)
        assert chk2["feasible"] is True and len(chk2["selected"]) == 2

    def test_no_trucks_or_no_candidates(self):
        cands = [_cand("A", 100, 1, 1)]
        assert _vrp(cands, [], 500, 100)["selected"] == []
        assert _vrp([], [TRUCK], 500, 100)["selected"] == []

    def test_decision_block_carries_routes(self):
        cands = [_cand("A", 1000, 10, 0), _cand("B", 800, 12, 3)]
        sel = _vrp(cands, [TRUCK], target=2500, missing=1500)
        block = build_l3_decision_block(sel, 1500, {"TYPE_05": 1})
        assert block["method"] == "vrp"
        assert block["routes"] and block["routes"][0]["km"] > 0
        assert block["params"]["kg_value_kc"] == l3.L3_CONFIG["kg_value_kc"]

    def test_format_routes_flags_over_limit(self):
        txt = l3.format_routes([{"truck_idx": 0, "type_code": "TYPE_05",
                                 "locations": ["a"], "kg": 100, "km": 700,
                                 "drive_min": 800, "span_min": 900,
                                 "start": "04:00", "end": "19:00",
                                 "breaks": 2}], max_drive_h=9.0)
        assert "přes denní limit" in txt


class TestAggregateLocations:
    def test_sums_kg_and_service_per_location(self):
        rows = {"CB": [
            {"location_code": "x", "customer_name": "X", "weight_kg": "100",
             "service_sec": "300", "lat": "49", "lon": "15", "order_number": "O1"},
            {"location_code": "x", "customer_name": "X", "weight_kg": "50",
             "service_sec": "120", "lat": "49", "lon": "15", "order_number": "O2"},
        ]}
        locs = l3.aggregate_locations(rows)
        assert len(locs) == 1
        assert locs[0]["kg"] == pytest.approx(150)
        assert locs[0]["service_sec"] == 420
        assert [o["order_number"] for o in locs[0]["orders"]] == ["O1", "O2"]


class TestStopCost:
    def test_tiny_location_on_the_way_dropped_with_stop_cost(self):
        # 1 000 kg lokace 50 km daleko + 10 kg drobek přímo na trase:
        # bez ceny zastávky se drobek veze (je zadarmo), se 150 Kč ne
        # (10 kg × λ 6 = 60 Kč < 150 Kč)
        cands = [_cand("BIG", 1000, 50, 0), _cand("TINY", 10, 25, 0)]
        with_cost = _vrp(cands, [TRUCK], target=2000, missing=1000,
                         kg_value_kc=6.0, stop_cost_kc=150.0)
        free = _vrp(cands, [TRUCK], target=2000, missing=1000,
                    kg_value_kc=6.0, stop_cost_kc=0.0)
        assert {c["location_code"] for c in with_cost["selected"]} == {"BIG"}
        assert {c["location_code"] for c in free["selected"]} == {"BIG", "TINY"}


class TestMaxStopsRule:
    def test_selection_respects_max_stops(self):
        # 30 lokací po 100 kg těsně u skladu, strop 20 zastávek → nejvýš 20
        cands = [_cand(f"S{i:02d}", 100, 5 + (i % 6), 5 + i // 6) for i in range(30)]
        rules = dict(DRIVER, max_stops=20)
        sel = _vrp(cands, [TRUCK], target=99_999, missing=3000,
                   kg_value_kc=50, driver=rules)
        assert len(sel["selected"]) <= 20
        assert len(sel["routes"][0]["locations"]) <= 20
        # bez stropu vezme všech 30
        free = _vrp(cands, [TRUCK], target=99_999, missing=3000,
                    kg_value_kc=50, driver=dict(DRIVER, max_stops=0))
        assert len(free["selected"]) == 30

    def test_driver_rules_carry_solver_max_stops(self):
        import vrp_solver_lines_v6 as S
        rules = l3.driver_rules()
        assert rules["max_stops"] == int(S.CONFIG.get("max_stops_per_route") or 0)
        assert rules["max_drive_h"] == float(S.CONFIG["driver_max_drive_h"])
