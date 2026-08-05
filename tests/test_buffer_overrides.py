"""
test_buffer_overrides.py — CLI override plánovacích bufferů

Default je v CONFIG (nosnost ×1.0x, okno -5/+25 min). Přepínače dovolí
spočítat plán natvrdo: nosnost 100 % a okna přesně jak je poslalo ESO9.

Klíčové: bez přepínačů se NESMÍ změnit nic — jsou to volitelné override,
ne nové výchozí chování.

Testy ZÁMĚRNĚ nekontrolují konkrétní hodnoty v CONFIG — ty jsou provozní
nastavení, které se legitimně ladí (1.02 → 1.03 …). Kdyby je test přibil,
blokoval by běh po každé takové změně. Testuje se chování override, ne čísla.
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

    def test_config_keys_exist_and_are_sane(self):
        # konkrétní hodnoty se ladí za provozu — hlídá se jen typ a rozsah,
        # ať překlep (10.3 místo 1.03) nebo chybějící klíč neprojde tiše
        mult = CONFIG["vehicle_capacity_multiplier"]
        assert isinstance(mult, (int, float)) and 1.0 <= mult <= 1.2
        for key in ("tw_expand_before_min", "tw_expand_after_min"):
            assert isinstance(CONFIG[key], int) and 0 <= CONFIG[key] <= 120


class TestNoBuffers:
    def test_sets_hard_values(self, cfg):
        # podstatný je VÝSLEDNÝ stav, ne kolik položek se cestou změnilo
        # (když už je config na tvrdých hodnotách, není co měnit)
        apply_buffer_overrides(_args(no_buffers=True), cfg)
        assert cfg["vehicle_capacity_multiplier"] == 1.0
        assert cfg["tw_expand_before_min"] == 0
        assert cfg["tw_expand_after_min"] == 0

    def test_change_log_shows_old_and_new_value(self, cfg):
        # log musí říct, z čeho na co se šlo — ať je z konzole vidět,
        # v jakém režimu plán vznikl. Kontrolují se jen položky, které
        # se opravdu měnily; hodnota už na tvrdém defaultu se neloguje.
        old_mult   = cfg["vehicle_capacity_multiplier"]
        old_before = cfg["tw_expand_before_min"]
        old_after  = cfg["tw_expand_after_min"]

        joined = " | ".join(apply_buffer_overrides(_args(no_buffers=True), cfg))

        expected = []
        if old_mult != 1.0:
            expected.append(f"nosnost vozidel: {old_mult:.0%} → 100%")
        if old_before != 0:
            expected.append(f"okno před: {old_before} min → 0 min")
        if old_after != 0:
            expected.append(f"okno po: {old_after} min → 0 min")

        for line in expected:
            assert line in joined
        assert joined.count("→") == len(expected)   # nic navíc se neloguje


class TestGranular:
    def test_capacity_only(self, cfg):
        windows_before = (cfg["tw_expand_before_min"], cfg["tw_expand_after_min"])
        apply_buffer_overrides(_args(capacity_multiplier=1.0), cfg)
        assert cfg["vehicle_capacity_multiplier"] == 1.0
        # okna beze změny — ať je default v CONFIG jakýkoli
        assert (cfg["tw_expand_before_min"], cfg["tw_expand_after_min"]) == windows_before

    def test_windows_only(self, cfg):
        capacity_before = cfg["vehicle_capacity_multiplier"]
        apply_buffer_overrides(_args(tw_expand_before=0, tw_expand_after=0), cfg)
        assert cfg["tw_expand_before_min"] == 0
        assert cfg["tw_expand_after_min"] == 0
        assert cfg["vehicle_capacity_multiplier"] == capacity_before

    def test_granular_wins_over_no_buffers(self, cfg):
        # „tvrdý režim, ale nech +10 min na konci okna"
        apply_buffer_overrides(_args(no_buffers=True, tw_expand_after=10), cfg)
        assert cfg["vehicle_capacity_multiplier"] == 1.0
        assert cfg["tw_expand_before_min"] == 0
        assert cfg["tw_expand_after_min"] == 10

    def test_zero_is_honored_not_treated_as_missing(self, cfg):
        # 0 je platná hodnota, nesmí propadnout na „nezadáno" (falsy != None).
        # Výchozí stav si test nastaví sám, ať nezávisí na hodnotě v CONFIG.
        cfg["tw_expand_before_min"] = 7
        changes = apply_buffer_overrides(_args(tw_expand_before=0), cfg)
        assert cfg["tw_expand_before_min"] == 0
        assert len(changes) == 1

    def test_setting_same_value_is_not_a_change(self, cfg):
        same = cfg["vehicle_capacity_multiplier"]
        changes = apply_buffer_overrides(_args(capacity_multiplier=same), cfg)
        assert changes == []


class TestEffectOnPlanning:
    def test_capacity_multiplier_reaches_vehicles(self, tmp_path, cfg):
        # ověř skutečný dopad: násobič se propíše do max_kg vozidel
        from vrp_solver_lines_v6 import load_vehicle_types_db
        p = tmp_path / "vehicle_types-20260806.csv"
        p.write_text(
            "type_code;type_name;max_kg;cost_per_km;start_cost_kc;"
            "available_count;total_count;active_count;profiles;"
            "cost_per_km_source;available_count_source;time_multiplier;"
            "osrm_profile;valid_for_date\n"
            "TYPE_02;Dodávka;1000;11.0;1000;1;1;1;Malé auto;x;y;1.0;driving;20260805\n",
            encoding="utf-8")

        original = CONFIG["vehicle_capacity_multiplier"]
        try:
            # 1.05 je čistě testovací hodnota (ne produkční default), ať je
            # vidět, že se propisuje cokoli, co je zrovna v CONFIG
            CONFIG["vehicle_capacity_multiplier"] = 1.05
            assert load_vehicle_types_db(str(p))[0]["max_kg"] == pytest.approx(1050)
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
