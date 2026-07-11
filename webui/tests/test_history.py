"""
Parsování run_log.jsonl a benchmark session — syntetická data přes tmp_path.

Spouštět:  python -m pytest webui/tests -q
"""

import json

from webui.app import history


def _write_jsonl(path, records):
    lines = []
    for r in records:
        lines.append(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rec(run_id, zone, budget_sec, lines):
    return {
        "run_id": run_id,
        "input":  {"zone": zone, "delivery_date": "2026-04-29", "orders_count": 10},
        "config": {"total_time_budget_sec": budget_sec},
        "results": {"lines_count": lines, "total_cost_kc": 1000.0, "total_km": 50.0,
                    "total_hours": 5.0, "elapsed_min": 2.0, "output_dir": "x"},
    }


def test_skips_bad_and_empty_lines(tmp_path):
    p = tmp_path / "run_log.jsonl"
    _write_jsonl(p, [
        _rec("1", "CB", 300, 3),
        "TOHLE NENI JSON",
        "",
        "{ neuzavreny",
        _rec("2", "HK", 1800, 4),
    ])
    recs = history.read_runlog(path=p)
    assert len(recs) == 2                       # 2 vadné/prázdné přeskočeny
    assert recs[0]["run_id"] == "2"             # nejnovější první
    assert recs[0]["budget_min"] == 30.0        # 1800/60
    assert recs[1]["budget_min"] == 5.0         # 300/60


def test_zone_filter(tmp_path):
    p = tmp_path / "run_log.jsonl"
    _write_jsonl(p, [_rec("1", "CB", 300, 3), _rec("2", "HK", 300, 4),
                     _rec("3", "CB", 600, 5)])
    cb = history.read_runlog(zone="CB", path=p)
    assert [r["run_id"] for r in cb] == ["3", "1"]
    assert all(r["zone"] == "CB" for r in cb)


def test_limit(tmp_path):
    p = tmp_path / "run_log.jsonl"
    _write_jsonl(p, [_rec(str(i), "CB", 300, i) for i in range(10)])
    assert len(history.read_runlog(limit=3, path=p)) == 3


def test_missing_file_returns_empty(tmp_path):
    assert history.read_runlog(path=tmp_path / "neexistuje.jsonl") == []


def test_raw_record_preserved(tmp_path):
    p = tmp_path / "run_log.jsonl"
    _write_jsonl(p, [_rec("1", "CB", 300, 3)])
    recs = history.read_runlog(path=p)
    assert recs[0]["raw"]["input"]["orders_count"] == 10


def test_benchmark_session_parsing(tmp_path, monkeypatch):
    from webui.app import config
    # session dir uvnitř falešného RESULTS_ROOT, aby relative_to fungovalo
    monkeypatch.setattr(config, "RESULTS_ROOT", tmp_path)
    sess = tmp_path / "ALL_BENCHMARK" / "session_test"
    sess.mkdir(parents=True)
    (sess / "benchmark_plan.json").write_text(
        json.dumps({"date": "2026-04-29", "budget_min": 30, "variants": ["a", "b"]}),
        encoding="utf-8")
    (sess / "benchmark_runs.csv").write_text(
        "variant_id,status,total_cost_kc\n"
        "a,success,1000\n"
        "b,failed,\n", encoding="utf-8")
    out = history.read_benchmark_session(sess)
    assert out["name"] == "session_test"
    assert out["plan"]["budget_min"] == 30
    assert len(out["runs"]) == 2
    assert out["runs"][0]["variant_id"] == "a"
    assert out["runs"][0]["status"] == "success"


def test_benchmark_session_csv_fallback_to_jsonl(tmp_path, monkeypatch):
    from webui.app import config
    monkeypatch.setattr(config, "RESULTS_ROOT", tmp_path)
    sess = tmp_path / "ALL_BENCHMARK" / "session_j"
    sess.mkdir(parents=True)
    (sess / "benchmark_runs.jsonl").write_text(
        json.dumps({"variant_id": "x", "status": "success"}) + "\n", encoding="utf-8")
    out = history.read_benchmark_session(sess)
    assert out["plan"] is None                  # plán chybí → tolerováno
    assert out["runs"][0]["variant_id"] == "x"
