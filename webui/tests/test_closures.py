"""
Read-only čtení uzavírek + generátor mapy — syntetická closures.json (tmp_path).

Spouštět:  python -m pytest webui/tests -q
"""

import json

from webui.app import closures


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_list_reads_closures(tmp_path, monkeypatch):
    p = tmp_path / "closures.json"
    _write(p, {"version": 1, "closures": [
        {"id": "CLO_001", "name": "Test", "active": True,
         "segment": {"from": {"lat": 49.5, "lon": 15.5},
                     "to": {"lat": 49.6, "lon": 15.6}}}]})
    monkeypatch.setattr(closures, "CLOSURES_JSON", p)
    out = closures.list_closures()
    assert out["version"] == 1
    assert len(out["closures"]) == 1
    assert out["closures"][0]["id"] == "CLO_001"


def test_list_missing_file_tolerant(tmp_path, monkeypatch):
    monkeypatch.setattr(closures, "CLOSURES_JSON", tmp_path / "neexistuje.json")
    out = closures.list_closures()
    assert out["closures"] == []


def test_list_corrupt_file_tolerant(tmp_path, monkeypatch):
    p = tmp_path / "closures.json"
    p.write_text("{ tohle neni json", encoding="utf-8")
    monkeypatch.setattr(closures, "CLOSURES_JSON", p)
    out = closures.list_closures()
    assert out["closures"] == []
    assert "error" in out


def test_map_html_has_segments_and_leaflet(tmp_path, monkeypatch):
    p = tmp_path / "closures.json"
    _write(p, {"version": 1, "closures": [
        {"id": "CLO_009", "name": "Uzavirka", "active": True,
         "segment": {"from": {"lat": 49.51, "lon": 15.59},
                     "to": {"lat": 49.52, "lon": 15.61}}}]})
    monkeypatch.setattr(closures, "CLOSURES_JSON", p)
    html = closures.closures_map_html()
    assert "leaflet" in html.lower()
    assert "CLO_009" in html
    assert "49.51" in html and "15.61" in html   # souřadnice segmentu injektované


def test_map_html_empty_ok(tmp_path, monkeypatch):
    p = tmp_path / "closures.json"
    _write(p, {"version": 1, "closures": []})
    monkeypatch.setattr(closures, "CLOSURES_JSON", p)
    html = closures.closures_map_html()
    assert "__SEGMENTS__" not in html          # placeholder nahrazen
    assert "[]" in html                         # prázdné pole segmentů
