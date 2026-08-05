"""
test_vehicle_types_format.py — datovaný vozový park se středníky

Od 6. 8. 2026 chodí aktivní vozový park jako `vehicle_types-YYYYMMDD.csv`:
středníky místo čárek + sloupec `valid_for_date` navíc. Program si sám bere
soubor s nejvyšším datem v NÁZVU; starší se ručně přesouvají do
`data/static/vehicle_types_archiv/`.

Starý čárkový formát se odmítá jasnou chybou — tichý fallback by znamenal
plánování s prázdnou flotilou nebo na neaktuálních počtech aut.
"""
import pytest

from vrp_solver_lines_v6 import (
    CONFIG,
    _load_raw_max_kg_by_type,
    find_latest_vehicle_types,
    load_vehicle_types_db,
)

HEADER = ("type_code;type_name;max_kg;cost_per_km;start_cost_kc;"
          "available_count;total_count;active_count;profiles;"
          "cost_per_km_source;available_count_source;time_multiplier;"
          "osrm_profile;valid_for_date\n")

OLD_HEADER = ("type_code,type_name,max_kg,cost_per_km,start_cost_kc,"
              "available_count,total_count,active_count,profiles,"
              "cost_per_km_source,available_count_source,time_multiplier,"
              "osrm_profile\n")


def _write(path, rows, header=HEADER):
    path.write_text(header + "".join(rows), encoding="utf-8")
    return path


def _row(code="TYPE_02", name="Dodávka", max_kg=1350, count=5,
         profile="driving", valid="20260805"):
    return (f"{code};{name};{max_kg};11.0;1000;{count};{count};{count};"
            f"Malé auto;src_c;src_n;1.0;{profile};{valid}\n")


# ═════════════════════════════════════════════════════════════════════════════
#  Výběr nejnovějšího souboru podle data v názvu
# ═════════════════════════════════════════════════════════════════════════════

class TestFindLatest:
    def test_picks_highest_date(self, tmp_path):
        _write(tmp_path / "vehicle_types-20260701.csv", [_row()])
        _write(tmp_path / "vehicle_types-20260806.csv", [_row()])
        _write(tmp_path / "vehicle_types-20260315.csv", [_row()])
        assert find_latest_vehicle_types(tmp_path).name == "vehicle_types-20260806.csv"

    def test_sorts_by_date_not_alphabetically(self, tmp_path):
        # 20261101 > 20260901 i když textově "1" < "9" na druhé pozici
        _write(tmp_path / "vehicle_types-20260901.csv", [_row()])
        _write(tmp_path / "vehicle_types-20261101.csv", [_row()])
        assert find_latest_vehicle_types(tmp_path).name == "vehicle_types-20261101.csv"

    def test_ignores_undated_and_other_files(self, tmp_path):
        _write(tmp_path / "vehicle_types-20260806.csv", [_row()])
        _write(tmp_path / "vehicle_types_old.csv", [_row()])
        _write(tmp_path / "vehicle_types.csv", [_row()])
        (tmp_path / "poznamka.txt").write_text("x", encoding="utf-8")
        assert find_latest_vehicle_types(tmp_path).name == "vehicle_types-20260806.csv"

    def test_empty_dir_explains_expected_name(self, tmp_path):
        with pytest.raises(FileNotFoundError) as e:
            find_latest_vehicle_types(tmp_path)
        assert "vehicle_types-YYYYMMDD.csv" in str(e.value)

    def test_only_undated_file_is_not_enough(self, tmp_path):
        # samotný vehicle_types.csv (starý název) se ignoruje — je to vada
        _write(tmp_path / "vehicle_types.csv", [_row()])
        with pytest.raises(FileNotFoundError):
            find_latest_vehicle_types(tmp_path)

    def test_missing_dir(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            find_latest_vehicle_types(tmp_path / "neni")


# ═════════════════════════════════════════════════════════════════════════════
#  Načtení nového formátu
# ═════════════════════════════════════════════════════════════════════════════

class TestLoadNewFormat:
    def test_semicolons_parsed(self, tmp_path):
        p = _write(tmp_path / "vehicle_types-20260806.csv",
                   [_row(count=3), _row(code="TYPE_06", name="Kamion",
                                        max_kg=8000, count=1,
                                        profile="driving-hgv")])
        vehicles = load_vehicle_types_db(str(p))
        assert len(vehicles) == 4                       # 3 + 1 expandovaných
        assert {v["type_code"] for v in vehicles} == {"TYPE_02", "TYPE_06"}
        hgv = [v for v in vehicles if v["type_code"] == "TYPE_06"][0]
        assert hgv["osrm_profile"] == "driving-hgv"

    def test_valid_for_date_does_not_break_load(self, tmp_path):
        # sloupec navíc se zatím nepoužívá, ale nesmí vadit
        p = _write(tmp_path / "vehicle_types-20260806.csv", [_row(valid="20260805")])
        assert len(load_vehicle_types_db(str(p))) == 5

    def test_auto_picks_latest_without_path(self, tmp_path, monkeypatch):
        _write(tmp_path / "vehicle_types-20260701.csv", [_row(count=9)])
        _write(tmp_path / "vehicle_types-20260806.csv", [_row(count=4)])
        monkeypatch.setattr("vrp_solver_lines_v6.VEHICLE_TYPES_DIR", tmp_path)
        assert len(load_vehicle_types_db()) == 4        # z novějšího souboru

    def test_raw_max_kg_reads_semicolons(self, tmp_path):
        # ESO export potřebuje papírovou nosnost — bez capacity multiplieru
        p = _write(tmp_path / "vehicle_types-20260806.csv",
                   [_row(max_kg=1350), _row(code="TYPE_06", max_kg=8000)])
        mapping = _load_raw_max_kg_by_type(str(p))
        assert mapping == {"TYPE_02": 1350.0, "TYPE_06": 8000.0}


# ═════════════════════════════════════════════════════════════════════════════
#  Odmítnutí starého formátu
# ═════════════════════════════════════════════════════════════════════════════

class TestOldFormatRejected:
    def test_comma_file_fails_loudly(self, tmp_path):
        p = tmp_path / "vehicle_types-20260806.csv"
        p.write_text(OLD_HEADER +
                     "TYPE_02,Dodávka,1350,11.0,1000,5,5,5,Malé auto,x,y,1.0,driving\n",
                     encoding="utf-8")
        with pytest.raises(ValueError) as e:
            load_vehicle_types_db(str(p))
        msg = str(e.value)
        assert "STARÉM formátu" in msg and "čárka" in msg

    def test_error_names_expected_filename(self, tmp_path):
        p = tmp_path / "vehicle_types-20260806.csv"
        p.write_text(OLD_HEADER, encoding="utf-8")
        with pytest.raises(ValueError) as e:
            load_vehicle_types_db(str(p))
        assert "vehicle_types-YYYYMMDD.csv" in str(e.value)

    def test_missing_columns_still_reported(self, tmp_path):
        p = tmp_path / "vehicle_types-20260806.csv"
        p.write_text("type_code;type_name\nTYPE_02;Dodávka\n", encoding="utf-8")
        with pytest.raises(ValueError) as e:
            load_vehicle_types_db(str(p))
        assert "povinné sloupce" in str(e.value)


# ═════════════════════════════════════════════════════════════════════════════
#  Skutečný produkční soubor
# ═════════════════════════════════════════════════════════════════════════════

class TestRealFleetFile:
    def test_production_file_loads(self):
        # ostrý soubor v data/static musí jít načíst — jinak nepojede nic
        path = find_latest_vehicle_types()
        vehicles = load_vehicle_types_db(str(path))
        assert vehicles, "vozový park je prázdný"
        assert all(v["max_kg"] > 0 for v in vehicles)
        assert all(v["cost_per_km"] > 0 for v in vehicles)

    def test_config_default_is_empty_means_auto(self):
        # prázdná cesta v CONFIG = ber nejnovější; kdyby tam byl natvrdo
        # starý název, program by po výměně souboru tiše spadl
        assert CONFIG["vehicle_types_file"] in ("", None) or \
            "vehicle_types-" in CONFIG["vehicle_types_file"]
