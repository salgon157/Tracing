"""
Testy tří nových tabů: Predikce, Flotila, Prostředí.
Čtecí endpointy nad syntetickými soubory (monkeypatch cest v config),
buildery příkazů, a že se joby sestaví (dry) bez spuštění.
"""
import json

import pytest

from webui.app import api_fleet, api_prediction, commands, config


# ── Flotila ──────────────────────────────────────────────────────────────────

class TestFleet:
    def _write(self, path, rows):
        # nový formát: středníky + valid_for_date (od 6. 8. 2026)
        header = ("type_code;type_name;max_kg;cost_per_km;start_cost_kc;"
                  "available_count;profiles;osrm_profile;"
                  "cost_per_km_source;available_count_source;valid_for_date\n")
        path.write_text(header + "".join(rows), encoding="utf-8")

    def test_reads_and_summarizes(self, tmp_path, monkeypatch):
        p = tmp_path / "vehicle_types-20260806.csv"
        self._write(p, [
            "T1;Dodávka;1350;11.0;1000;50;Malé auto;driving;src_c;src_n;20260805\n",
            "T2;Kamion;8000;35.0;1000;2;Velké auto;driving-hgv;src_c;src_n;20260805\n",
        ])
        monkeypatch.setattr(config, "VEHICLE_TYPES_DIR", tmp_path)
        d = api_fleet.fleet()
        assert d["summary"]["types"] == 2
        assert d["summary"]["vehicles_total"] == 52
        assert d["summary"]["small_count"] == 50
        assert d["summary"]["large_count"] == 2
        assert d["summary"]["cost_source"] == "src_c"
        assert len(d["rows"]) == 2
        assert d["source_file"] == "vehicle_types-20260806.csv"

    def test_picks_newest_by_date_in_name(self, tmp_path, monkeypatch):
        self._write(tmp_path / "vehicle_types-20260806.csv",
                    ["T1;Nova;1350;11.0;1000;50;Malé auto;driving;c;n;20260805\n"])
        self._write(tmp_path / "vehicle_types-20260701.csv",
                    ["T1;Stara;1350;11.0;1000;9;Malé auto;driving;c;n;20260630\n"])
        monkeypatch.setattr(config, "VEHICLE_TYPES_DIR", tmp_path)
        d = api_fleet.fleet()
        assert d["source_file"] == "vehicle_types-20260806.csv"
        assert d["summary"]["vehicles_total"] == 50

    def test_missing_file_tolerant(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "VEHICLE_TYPES_DIR", tmp_path)
        d = api_fleet.fleet()
        assert d["rows"] == []
        assert "error" in d

    def test_archive_lists_csvs_newest_first(self, tmp_path, monkeypatch):
        arc = tmp_path / "archiv"
        arc.mkdir()
        (arc / "vehicle_types_2026-07-19.csv").write_text("x", encoding="utf-8")
        (arc / "vehicle_types_2026-06-01.csv").write_text("y", encoding="utf-8")
        (arc / "poznamka.txt").write_text("z", encoding="utf-8")
        monkeypatch.setattr(config, "VEHICLE_TYPES_ARCHIV", arc)
        names = [a["name"] for a in api_fleet.archive()]
        assert names == ["vehicle_types_2026-07-19.csv", "vehicle_types_2026-06-01.csv"]

    def test_archive_missing_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "VEHICLE_TYPES_ARCHIV", tmp_path / "neni")
        assert api_fleet.archive() == []


# ── Predikce: čtení ──────────────────────────────────────────────────────────

class TestPredictionReads:
    def test_runs_from_jsonl_newest_first(self, tmp_path, monkeypatch):
        log = tmp_path / "run_log.jsonl"
        recs = [
            {"run_id": "A", "input": {"zone": "CB", "delivery_date": "2026-07-22",
                                      "orders_count": 245},
             "results": {"lines_count": 20, "total_cost_kc": 70000,
                         "total_km": 4500, "vehicle_type_mix": {"Dodávka": 20},
                         "output_dir": "data/prediction/results/CB/2026-07-22_1811"}},
            {"run_id": "B", "input": {"zone": "HK", "delivery_date": "2026-07-22",
                                      "orders_count": 150},
             "results": {"lines_count": 12, "total_cost_kc": 50000, "total_km": 3000,
                         "vehicle_type_mix": {}, "output_dir": ".../HK/2026-07-22_1811"}},
        ]
        log.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
        monkeypatch.setattr(config, "PREDICTION_RUN_LOG", log)
        out = api_prediction.prediction_runs()
        assert [r["run_id"] for r in out] == ["B", "A"]      # reversed = newest first
        assert out[1]["stamp"] == "1811"
        assert out[1]["cost_kc"] == 70000

    def test_runs_missing_log_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PREDICTION_RUN_LOG", tmp_path / "neni.jsonl")
        assert api_prediction.prediction_runs() == []

    def test_comparison_filter_by_date(self, tmp_path, monkeypatch):
        cmp = tmp_path / "comparison.jsonl"
        cmp.write_text("\n".join(json.dumps(r) for r in [
            {"date": "2026-07-22", "zone": "CB"},
            {"date": "2026-07-15", "zone": "CB"},
        ]), encoding="utf-8")
        monkeypatch.setattr(config, "PREDICTION_COMPARISON", cmp)
        assert len(api_prediction.comparison()) == 2
        assert [r["zone"] for r in api_prediction.comparison(date="2026-07-15")] == ["CB"]

    def test_comparison_missing_tolerant(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PREDICTION_COMPARISON", tmp_path / "neni.jsonl")
        assert api_prediction.comparison() == []


# ── Buildery příkazů ─────────────────────────────────────────────────────────

class TestPredictionCommands:
    def test_build_predict_day_basic(self):
        argv = commands.build_predict_day(depots=["CB", "MO"], budget_min=5,
                                          osm_source="current")
        assert argv[1] == "predict_day.py"
        assert "CB" in argv and "MO" in argv
        assert argv[argv.index("--budget") + 1] == "5"
        assert argv[argv.index("--osm-source") + 1] == "current"

    def test_build_predict_day_stable_and_no_vis(self):
        argv = commands.build_predict_day(osm_source="stable", no_visualize=True)
        assert argv[argv.index("--osm-source") + 1] == "stable"
        assert "--no-visualize" in argv

    def test_build_compare(self):
        argv = commands.build_compare_prediction(date="2026-07-22", pred_stamp="1811")
        assert argv[1] == "compare_prediction.py"
        assert argv[argv.index("--date") + 1] == "2026-07-22"
        assert argv[argv.index("--pred-stamp") + 1] == "1811"


class TestPredictionRunDry:
    def test_run_prediction_dry_builds_job(self):
        req = api_prediction.PredictRequest(depots=["CB"], budget_min=5, dry=True)
        job = api_prediction.run_prediction(req)
        assert job["type"] == "prediction"
        assert any("predict_day.py" in s["cmdline"] for s in job["steps"])

    def test_run_prediction_rejects_bad_depot(self):
        req = api_prediction.PredictRequest(depots=["XX"], dry=True)
        with pytest.raises(Exception):
            api_prediction.run_prediction(req)

    def test_compare_dry_builds_job(self):
        req = api_prediction.CompareRequest(date="2026-07-22", dry=True)
        job = api_prediction.run_compare(req)
        assert job["type"] == "compare_prediction"
        assert any("compare_prediction.py" in s["cmdline"] for s in job["steps"])
