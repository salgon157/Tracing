"""
test_osm_routing.py — Testy pro centrální URL preset modul.

Cíl: zachytit náhodný drift portů nebo presetů. Běží v rámci startup test suite,
takže každý solver run ověří, že stable=5000/8080 a current=5001/8081 jsou
stále správně.
"""

import sys
from pathlib import Path

# Umožnit import osm_routing.py z parent dir (vrp_benchmark/)
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from osm_routing import OSM_PRESETS, apply_osm_source, add_osm_args


class TestApplyOsmSource:
    def test_stable_sets_5000_8080(self):
        cfg = {"osrm_url": "WRONG", "osrm_urls": {"old": "junk"}}
        apply_osm_source(cfg, "stable")
        assert cfg["osrm_url"] == "http://localhost:5000"
        assert cfg["osrm_urls"]["driving"] == "http://localhost:5000"
        assert cfg["osrm_urls"]["driving-hgv"] == "http://localhost:8080"

    def test_current_sets_5001_8081(self):
        cfg = {"osrm_url": "WRONG", "osrm_urls": {"old": "junk"}}
        apply_osm_source(cfg, "current")
        assert cfg["osrm_url"] == "http://localhost:5001"
        assert cfg["osrm_urls"]["driving"] == "http://localhost:5001"
        assert cfg["osrm_urls"]["driving-hgv"] == "http://localhost:8081"

    def test_unknown_source_raises(self):
        cfg = {"osrm_url": "", "osrm_urls": {}}
        with pytest.raises(KeyError):
            apply_osm_source(cfg, "fresh")     # překlep
        with pytest.raises(KeyError):
            apply_osm_source(cfg, "")
        with pytest.raises(KeyError):
            apply_osm_source(cfg, "STABLE")    # case-sensitive

    def test_isolation_between_calls(self):
        """Mutace osrm_urls v jednom configu nesmí ovlivnit jiný config."""
        a = {"osrm_url": "", "osrm_urls": {}}
        b = {"osrm_url": "", "osrm_urls": {}}
        apply_osm_source(a, "stable")
        apply_osm_source(b, "current")
        a["osrm_urls"]["driving"] = "MUTATED"
        assert b["osrm_urls"]["driving"] == "http://localhost:5001"

    def test_repeated_apply_overwrites(self):
        """Druhé volání s jiným source musí přepsat vše ze stable na current."""
        cfg = {"osrm_url": "", "osrm_urls": {}}
        apply_osm_source(cfg, "stable")
        apply_osm_source(cfg, "current")
        assert cfg["osrm_url"] == "http://localhost:5001"
        assert cfg["osrm_urls"]["driving-hgv"] == "http://localhost:8081"


class TestPresets:
    def test_both_presets_exist(self):
        assert "stable" in OSM_PRESETS
        assert "current" in OSM_PRESETS

    def test_preset_keys_consistent(self):
        """Oba presety mají stejnou strukturu (driving + driving-hgv)."""
        for name, preset in OSM_PRESETS.items():
            assert "osrm_url" in preset, f"{name} chybí osrm_url"
            assert "osrm_urls" in preset, f"{name} chybí osrm_urls"
            assert "driving" in preset["osrm_urls"], f"{name} chybí driving"
            assert "driving-hgv" in preset["osrm_urls"], f"{name} chybí driving-hgv"

    def test_stable_and_current_use_different_ports(self):
        """Pokud by někdo nastavil oba presety na stejné porty, jeden by se nemohl
        startovat → chytíme to tady, ne až při běhu Dockeru."""
        s = OSM_PRESETS["stable"]["osrm_urls"]
        c = OSM_PRESETS["current"]["osrm_urls"]
        assert s["driving"]     != c["driving"],     "OSRM porty se musí lišit"
        assert s["driving-hgv"] != c["driving-hgv"], "ORS porty se musí lišit"


class TestAddOsmArgs:
    def test_flag_default_false(self):
        import argparse
        parser = argparse.ArgumentParser()
        add_osm_args(parser)
        args = parser.parse_args([])
        assert args.fresh_osm is False

    def test_flag_set_true(self):
        import argparse
        parser = argparse.ArgumentParser()
        add_osm_args(parser)
        args = parser.parse_args(["--fresh-osm"])
        assert args.fresh_osm is True
