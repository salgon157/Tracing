"""
test_buffer_overrides.py — CLI override plánovacích bufferů

Default (CONFIG): nosnost ×1.02, okno -5/+25 min. Přepínače dovolí spočítat
plán natvrdo: nosnost 100 % a okna přesně jak je poslalo ESO9.

Klíčové: bez přepínačů se NESMÍ změnit nic — jsou to volitelné override,
ne nové výchozí chování.
"""
import argparse
import copy

import pytest

from vrp_solver_lines_v6 import CONFIG, apply_buffer_overrides


def _args(**kw):
    base = {"no_buffers": False, "capacity_multiplier": None,
            "tw_expand_before": None, "tw_expand_after": None}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def cfg():
    """Kopie CONFIG — testy nesmí ušpinit globální stav pro další testy."""
    return copy.deepcopy(CONFIG)


class TestDefaults:
    def test_no_flags_changes_nothing(self, cfg):
        before = copy.deepcopy(cfg)
        changes = apply_buffer_overrides(_args(), cfg)
        assert changes == []
        assert cfg == before

    def test_config_defaults_unchanged(self):
        # ostrý default zůstává 102 % a -5/+25 min
        assert CONFIG["vehicle_capacity_multiplier"] == 1.02
        assert CONFIG["tw_expand_before_min"] == 5
        assert CONFIG["tw_expand_after_min"] == 25


class TestNoBuffers:
    def test_sets_all_three(self, cfg):
        changes = apply_buffer_overrides(_args(no_buffers=True), cfg)
        assert cfg["vehicle_capacity_multiplier"] == 1.0
        assert cfg["tw_expand_before_min"] == 0
        assert cfg["tw_expand_after_min"] == 0
        assert len(changes) == 3

    def test_change_log_is_readable(self, cfg):
        changes = apply_buffer_overrides(_args(no_buffers=True), cfg)
        joined = " | ".join(changes)
        assert "102% → 100%" in joined
        assert "5 min → 0 min" in joined
        assert "25 min → 0 min" in joined


class TestGranular:
    def test_capacity_only(self, cfg):
        apply_buffer_overrides(_args(capacity_multiplier=1.0), cfg)
        assert cfg["vehicle_capacity_multiplier"] == 1.0
        assert cfg["tw_expand_before_min"] == 5      # okna beze změny
        assert cfg["tw_expand_after_min"] == 25

    def test_windows_only(self, cfg):
        apply_buffer_overrides(_args(tw_expand_before=0, tw_expand_after=0), cfg)
        assert cfg["tw_expand_before_min"] == 0
        assert cfg["tw_expand_after_min"] == 0
        assert cfg["vehicle_capacity_multiplier"] == 1.02   # nosnost beze změny

    def test_granular_wins_over_no_buffers(self, cfg):
        # „tvrdý režim, ale nech +10 min na konci okna"
        apply_buffer_overrides(_args(no_buffers=True, tw_expand_after=10), cfg)
        assert cfg["vehicle_capacity_multiplier"] == 1.0
        assert cfg["tw_expand_before_min"] == 0
        assert cfg["tw_expand_after_min"] == 10

    def test_zero_is_honored_not_treated_as_missing(self, cfg):
        # 0 je platná hodnota, nesmí propadnout na „nezadáno"
        changes = apply_buffer_overrides(_args(tw_expand_before=0), cfg)
        assert cfg["tw_expand_before_min"] == 0
        assert len(changes) == 1

    def test_setting_same_value_is_not_a_change(self, cfg):
        changes = apply_buffer_overrides(_args(capacity_multiplier=1.02), cfg)
        assert changes == []


class TestEffectOnPlanning:
    def test_capacity_multiplier_reaches_vehicles(self, tmp_path, cfg):
        # ověř skutečný dopad: násobič se propíše do max_kg vozidel
        from vrp_solver_lines_v6 import load_vehicle_types_db
        p = tmp_path / "vt.csv"
        p.write_text(
            "type_code,type_name,max_kg,cost_per_km,start_cost_kc,"
            "available_count,total_count,active_count,profiles,"
            "cost_per_km_source,available_count_source,time_multiplier,osrm_profile\n"
            "TYPE_02,Dodávka,1000,11.0,1000,1,1,1,Malé auto,x,y,1.0,driving\n",
            encoding="utf-8")

        original = CONFIG["vehicle_capacity_multiplier"]
        try:
            CONFIG["vehicle_capacity_multiplier"] = 1.02
            assert load_vehicle_types_db(str(p))[0]["max_kg"] == pytest.approx(1020)
            apply_buffer_overrides(_args(no_buffers=True))
            assert load_vehicle_types_db(str(p))[0]["max_kg"] == pytest.approx(1000)
        finally:
            CONFIG["vehicle_capacity_multiplier"] = original

    def test_windows_reach_data_model(self, cfg):
        from vrp_solver_lines_v6 import build_data_model
        import numpy as np

        order = {"lat": 50.0, "lon": 14.0, "time_from": "08:00", "time_to": "16:00",
                 "weight_kg": 100.0, "service_sec": 600, "order_number": "O1"}
        vehicle = {"vehicle_id": "V1", "id": "V1", "type_code": "TYPE_02",
                   "max_kg": 1400, "cost_per_km": 10.0, "start_cost": 0,
                   "max_duration_h": 10, "time_multiplier": 1.0,
                   "osrm_profile": "driving"}
        m = np.zeros((2, 2))

        before = (CONFIG["tw_expand_before_min"], CONFIG["tw_expand_after_min"])
        try:
            CONFIG["tw_expand_before_min"], CONFIG["tw_expand_after_min"] = 5, 25
            data = build_data_model([order], [vehicle], m, [m])
            assert data["time_windows"][1] == (8 * 60 - 5, 16 * 60 + 25)

            apply_buffer_overrides(_args(no_buffers=True))
            data = build_data_model([order], [vehicle], m, [m])
            assert data["time_windows"][1] == (8 * 60, 16 * 60)
        finally:
            CONFIG["tw_expand_before_min"], CONFIG["tw_expand_after_min"] = before
