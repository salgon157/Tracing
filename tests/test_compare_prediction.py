"""
test_compare_prediction.py — párování, výběr predikce, delty, upsert.
Syntetické run logy, žádný dotyk s reálnými daty.
"""
import json

import compare_prediction as cp

from compare_prediction import (
    aggregate_mix,
    build_comparison,
    group_by_zone_date,
    load_run_log,
    rec_date,
    rec_label,
    rec_stamp,
    select_run,
    upsert_comparisons,
)


def _rec(run_id, zone, date, out_leaf, lines=10, mix=None, orders=100,
         kg=1000.0, cost=50000.0):
    return {
        "run_id": run_id,
        "input": {"zone": zone, "delivery_date": date,
                  "orders_count": orders, "orders_total_kg": kg},
        "results": {"lines_count": lines,
                    "vehicle_type_mix": mix or {"Type B": lines},
                    "total_cost_kc": cost, "total_km": 500.0,
                    "output_dir": f"data\\prediction\\results\\{zone}\\{out_leaf}"},
    }


PROFILES = {"Type B": "mala", "Type A": "mala", "Type E": "velka", "Type C": "velka"}


class TestRecParsing:
    def test_date_from_input(self):
        r = _rec("t", "CB", "2026-07-15", "2026-07-15_1811")
        assert rec_date(r) == "2026-07-15"

    def test_date_fallback_from_output_dir_backslashes(self):
        # Starší predikční záznamy: delivery_date prázdné, cesta s backslashes
        r = _rec("t", "CB", "", "2026-07-15_1811")
        assert rec_date(r) == "2026-07-15"

    def test_stamp_parsed(self):
        assert rec_stamp(_rec("t", "CB", "", "2026-07-15_1811")) == "1811"

    def test_stamp_missing_for_real_runs(self):
        assert rec_stamp(_rec("t", "CB", "2026-07-15", "2026-07-15")) is None


class TestSelection:
    def _runs(self):
        return [
            _rec("2026-07-14T14:30:00", "CB", "2026-07-15", "2026-07-15_1430", lines=15),
            _rec("2026-07-14T18:11:00", "CB", "2026-07-15", "2026-07-15_1811", lines=16),
        ]

    def test_group_sorted_by_run_id(self):
        groups = group_by_zone_date(list(reversed(self._runs())))
        runs = groups[("CB", "2026-07-15")]
        assert [r["run_id"] for r in runs] == \
            ["2026-07-14T14:30:00", "2026-07-14T18:11:00"]

    def test_default_latest(self):
        assert select_run(self._runs())["results"]["lines_count"] == 16

    def test_select_by_stamp(self):
        assert select_run(self._runs(), stamp="1430")["results"]["lines_count"] == 15

    def test_unknown_stamp_none(self):
        assert select_run(self._runs(), stamp="0900") is None


class TestPhaseSelection:
    """plan_day predict píše P1 i P2 pod stejné razítko — rozliší je label."""

    def _runs(self):
        return [
            _rec("2026-08-12T13:38:00", "CB", "2026-08-10",
                 "2026-08-10_1332_P1", lines=17, cost=59_427.0),
            _rec("2026-08-12T13:56:00", "CB", "2026-08-10",
                 "2026-08-10_1332_P2", lines=17, cost=58_834.0),
        ]

    def test_label_parsed(self):
        assert rec_label(self._runs()[0]) == "P1"
        assert rec_label(self._runs()[1]) == "P2"

    def test_label_none_when_absent(self):
        assert rec_label(_rec("t", "CB", "2026-07-15", "2026-07-15_1811")) is None
        assert rec_label(_rec("t", "CB", "2026-07-15", "2026-07-15")) is None

    def test_stamp_stays_same_for_both_phases(self):
        # proto samotný --pred-stamp fáze nerozliší
        assert {rec_stamp(r) for r in self._runs()} == {"1332"}

    def test_select_p1(self):
        assert select_run(self._runs(), label="P1")["results"]["total_cost_kc"] == 59_427.0

    def test_select_p2(self):
        assert select_run(self._runs(), label="P2")["results"]["total_cost_kc"] == 58_834.0

    def test_default_picks_last_in_time(self):
        # bez labelu vyhrává čas → u plan_day vždycky P2
        assert rec_label(select_run(self._runs())) == "P2"

    def test_label_case_insensitive(self):
        assert select_run(self._runs(), label="p2")["results"]["total_cost_kc"] == 58_834.0

    def test_unknown_label_none(self):
        assert select_run(self._runs(), label="P9") is None

    def test_stamp_and_label_combine(self):
        runs = self._runs() + [
            _rec("2026-08-10T20:20:00", "CB", "2026-08-10",
                 "2026-08-10_1959_P2", lines=17, cost=58_868.0)]
        assert select_run(runs, stamp="1959", label="P2")["results"]["total_cost_kc"] \
            == 58_868.0

    def test_label_ignores_runs_without_label(self):
        runs = self._runs() + [
            _rec("2026-08-12T15:00:00", "CB", "2026-08-10", "2026-08-10", lines=16)]
        assert rec_label(select_run(runs, label="P2")) == "P2"

    def test_side_carries_label(self):
        c = build_comparison(self._runs()[0], self._runs()[1], PROFILES)
        assert c["prediction"]["label"] == "P1"
        assert c["real"]["label"] == "P2"


class TestAggregateAndDelta:
    def test_aggregate_small_large(self):
        agg = aggregate_mix({"Type B": 12, "Type E": 2, "Type C": 1}, PROFILES)
        assert agg == {"mala": 12, "velka": 3}

    def test_unknown_type_counts_as_large(self):
        assert aggregate_mix({"Neznamy": 1}, PROFILES)["velka"] == 1

    def test_delta_is_prediction_minus_real(self):
        pred = _rec("p", "CB", "2026-07-15", "2026-07-15_1811",
                    lines=16, mix={"Type B": 14, "Type E": 2}, orders=201, cost=60000)
        real = _rec("r", "CB", "2026-07-15", "2026-07-15",
                    lines=16, mix={"Type B": 15, "Type E": 1}, orders=175, cost=55000)
        c = build_comparison(pred, real, PROFILES)
        assert c["delta"]["lines"] == 0
        assert c["delta"]["mala"] == -1
        assert c["delta"]["velka"] == 1
        assert c["delta"]["orders"] == 26
        assert c["delta"]["cost_kc"] == 5000
        assert c["delta"]["vehicle_types"] == {"Type B": -1, "Type E": 1}
        assert c["prediction"]["stamp"] == "1811"

    def test_type_only_in_real_appears_negative(self):
        pred = _rec("p", "CB", "2026-07-15", "2026-07-15_1811", mix={"Type B": 10})
        real = _rec("r", "CB", "2026-07-15", "2026-07-15",
                    mix={"Type B": 9, "Type C": 1})
        c = build_comparison(pred, real, PROFILES)
        assert c["delta"]["vehicle_types"]["Type C"] == -1


class TestUpsert:
    def test_replaces_same_zone_date_keeps_others(self, tmp_path):
        path = tmp_path / "comparison.jsonl"
        old_kept = {"zone": "HK", "date": "2026-07-15", "delta": {"lines": 3}}
        old_replaced = {"zone": "CB", "date": "2026-07-15", "delta": {"lines": 9}}
        path.write_text(
            json.dumps(old_kept) + "\n" + json.dumps(old_replaced) + "\n",
            encoding="utf-8")
        upsert_comparisons(path, [{"zone": "CB", "date": "2026-07-15",
                                   "delta": {"lines": 0}}])
        records = load_run_log(path)
        assert len(records) == 2
        cb = next(r for r in records if r["zone"] == "CB")
        assert cb["delta"]["lines"] == 0

    def test_creates_file_and_parents(self, tmp_path):
        path = tmp_path / "sub" / "comparison.jsonl"
        upsert_comparisons(path, [{"zone": "CB", "date": "2026-07-15"}])
        assert len(load_run_log(path)) == 1


class TestExcludedFor:
    def _rec_with_orders_file(self, orders_file, zone="CB", date="2026-07-15"):
        return {
            "input": {"zone": zone, "delivery_date": date,
                      "orders_file": str(orders_file)},
            "results": {"output_dir": f"data/results/{zone}/{date}"},
        }

    def test_reads_prepare_stats_json(self, tmp_path):
        from compare_prediction import excluded_for
        orders = tmp_path / "orders_CB_2026-07-15.csv"
        orders.write_text("", encoding="utf-8")
        (tmp_path / "prepare_stats_CB_2026-07-15.json").write_text(
            json.dumps({"excluded_total": 8}), encoding="utf-8")
        assert excluded_for(self._rec_with_orders_file(orders)) == 8

    def test_fallback_missing_locs_count(self, tmp_path):
        from compare_prediction import excluded_for
        orders = tmp_path / "orders_CB_2026-07-15.csv"
        orders.write_text("", encoding="utf-8")
        (tmp_path / "missing_locs_CB_2026-07-15.txt").write_text(
            "# hlavicka\ncode-a\ncode-b\n", encoding="utf-8")
        assert excluded_for(self._rec_with_orders_file(orders)) == 2

    def test_nothing_available_none(self, tmp_path):
        from compare_prediction import excluded_for
        orders = tmp_path / "orders_CB_2026-07-15.csv"
        assert excluded_for(self._rec_with_orders_file(orders)) is None

    def test_no_orders_file_none(self):
        from compare_prediction import excluded_for
        assert excluded_for({"input": {"zone": "CB",
                                       "delivery_date": "2026-07-15"},
                             "results": {}}) is None


class TestLabelledOutputDirs:
    """predict_day --label přidá k názvu složky příponu; párování predikce
    s realitou na tom nesmí spadnout (jinak by označený běh nešel porovnat)."""

    def _rec(self, leaf):
        return {"results": {"output_dir": f"data/prediction/results/CB/{leaf}"}}

    def test_label_does_not_break_date(self):
        assert cp.rec_date(self._rec("2026-08-05_1430_s-koeficientem")) == "2026-08-05"

    def test_label_keeps_stamp_readable(self):
        assert cp.rec_stamp(self._rec("2026-08-05_1430_s-koeficientem")) == "1430"

    def test_plain_and_stamped_still_work(self):
        assert cp.rec_date(self._rec("2026-08-05")) == "2026-08-05"
        assert cp.rec_stamp(self._rec("2026-08-05")) is None
        assert cp.rec_stamp(self._rec("2026-08-05_1430")) == "1430"

    def test_suffix_without_stamp(self):
        # ostré porovnávací běhy (2026-08-03_s-buffery) stamp nemají
        assert cp.rec_date(self._rec("2026-08-03_s-buffery")) == "2026-08-03"
        assert cp.rec_stamp(self._rec("2026-08-03_s-buffery")) is None
