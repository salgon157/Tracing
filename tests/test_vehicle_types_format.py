"""
test_vehicle_types_format.py — vozový park se středníky

Aktivní vozový park je `vehicle_types-*.csv` (středníky místo čárek +
sloupec `valid_for_date`) a v `data/static` smí být PRÁVĚ JEDEN. Program
sám nevybírá — víc souborů je vada, kterou nahlásí; který soubor tam bude,
řeší vrstva nad ním. Co neplatí, patří do `vehicle_types_archiv/`.

Starý čárkový formát se odmítá jasnou chybou — tichý fallback by znamenal
plánování s prázdnou flotilou nebo na neaktuálních počtech aut.
"""
import pytest

from vrp_solver_lines_v6 import (
    CONFIG,
    _load_raw_max_kg_by_type,
    find_vehicle_types_file,
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
#  Výběr souboru: musí být právě jeden
# ═════════════════════════════════════════════════════════════════════════════

class TestFindVehicleTypes:
    def test_single_file_found(self, tmp_path):
        _write(tmp_path / "vehicle_types-20260806.csv", [_row()])
        assert find_vehicle_types_file(tmp_path).name == "vehicle_types-20260806.csv"

    def test_name_does_not_have_to_be_dated(self, tmp_path):
        # datum v názvu už nic neurčuje — vybírá se podle toho, že je jediný
        _write(tmp_path / "vehicle_types-aktualni.csv", [_row()])
        assert find_vehicle_types_file(tmp_path).name == "vehicle_types-aktualni.csv"

    def test_two_files_are_an_error(self, tmp_path):
        _write(tmp_path / "vehicle_types-20260701.csv", [_row()])
        _write(tmp_path / "vehicle_types-20260806.csv", [_row()])
        with pytest.raises(ValueError) as e:
            find_vehicle_types_file(tmp_path)
        msg = str(e.value)
        assert "PRÁVĚ JEDEN" in msg
        assert "vehicle_types-20260701.csv" in msg
        assert "vehicle_types-20260806.csv" in msg

    def test_never_picks_silently(self, tmp_path):
        # dřív se bral nejnovější podle data — to už NESMÍ projít
        _write(tmp_path / "vehicle_types-20260101.csv", [_row()])
        _write(tmp_path / "vehicle_types-20261231.csv", [_row()])
        with pytest.raises(ValueError):
            find_vehicle_types_file(tmp_path)

    def test_ignores_other_files(self, tmp_path):
        _write(tmp_path / "vehicle_types-20260806.csv", [_row()])
        _write(tmp_path / "vehicle_types_old.csv", [_row()])       # podtržítko
        _write(tmp_path / "vehicle_types.csv", [_row()])            # bez pomlčky
        (tmp_path / "poznamka.txt").write_text("x", encoding="utf-8")
        assert find_vehicle_types_file(tmp_path).name == "vehicle_types-20260806.csv"

    def test_empty_dir_explains_expected_name(self, tmp_path):
        with pytest.raises(FileNotFoundError) as e:
            find_vehicle_types_file(tmp_path)
        assert "vehicle_types-*.csv" in str(e.value)

    def test_missing_dir(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            find_vehicle_types_file(tmp_path / "neni")


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

    def test_auto_finds_single_file_without_path(self, tmp_path, monkeypatch):
        _write(tmp_path / "vehicle_types-20260806.csv", [_row(count=4)])
        monkeypatch.setattr("vrp_solver_lines_v6.VEHICLE_TYPES_DIR", tmp_path)
        assert len(load_vehicle_types_db()) == 4

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
        path = find_vehicle_types_file()
        vehicles = load_vehicle_types_db(str(path))
        assert vehicles, "vozový park je prázdný"
        assert all(v["max_kg"] > 0 for v in vehicles)
        assert all(v["cost_per_km"] > 0 for v in vehicles)

    def test_config_default_is_empty_means_auto(self):
        # prázdná cesta v CONFIG = najdi jediný soubor; kdyby tam byl natvrdo
        # starý název, program by po výměně souboru tiše spadl
        assert CONFIG["vehicle_types_file"] in ("", None) or \
            "vehicle_types-" in CONFIG["vehicle_types_file"]


# ═════════════════════════════════════════════════════════════════════════════
#  Vadné číslo v povinném sloupci = fatální (ne tiché přeskočení)
#
#  Vzniklo z reálného rizika: cost_per_km 17.4 zobrazí český Excel jako
#  17. duben a při uložení to zapíše natvrdo. Dřív takový řádek jen zmizel
#  a plánovalo se s menší flotilou, aniž by si toho někdo všiml.
# ═════════════════════════════════════════════════════════════════════════════

class TestBrokenRowsFatal:
    def test_excel_date_in_cost_stops_run(self, tmp_path):
        p = _write(tmp_path / "vehicle_types-20260806.csv", [
            _row(code="TYPE_02", count=53),
            "TYPE_03;Nákladní 2t;2000;17.04.2026;1000;1;1;1;"
            "Velké auto;c;n;1.0;driving;20260805\n",
        ])
        with pytest.raises(ValueError) as e:
            load_vehicle_types_db(str(p))
        msg = str(e.value)
        assert "TYPE_03" in msg
        assert "17.04.2026" in msg
        assert "DATUM" in msg and "Excel" in msg

    def test_report_names_line_and_column(self, tmp_path):
        p = _write(tmp_path / "vehicle_types-20260806.csv", [
            _row(), _row(), "TYPE_04;Kamion;NENI_CISLO;19.5;1000;1;1;1;"
                            "Velké auto;c;n;1.0;driving;20260805\n",
        ])
        with pytest.raises(ValueError) as e:
            load_vehicle_types_db(str(p))
        msg = str(e.value)
        assert "řádek   4" in msg          # hlavička = 1, pak dva _row()
        assert "max_kg" in msg and "NENI_CISLO" in msg

    def test_broken_count_also_fatal(self, tmp_path):
        p = _write(tmp_path / "vehicle_types-20260806.csv", [
            "TYPE_02;Dodávka;1350;11.0;1000;X;5;5;"
            "Malé auto;c;n;1.0;driving;20260805\n",
        ])
        with pytest.raises(ValueError, match="VADNÝCH ŘÁDKŮ"):
            load_vehicle_types_db(str(p))

    def test_all_broken_rows_listed_at_once(self, tmp_path):
        # ať uživatel nemusí opravovat po jednom a spouštět znovu
        p = _write(tmp_path / "vehicle_types-20260806.csv", [
            "TYPE_02;Dodávka;1350;17.04.2026;1000;5;5;5;M;c;n;1.0;driving;20260805\n",
            "TYPE_03;Nákladní;2000;19.05.2026;1000;1;1;1;V;c;n;1.0;driving;20260805\n",
        ])
        with pytest.raises(ValueError) as e:
            load_vehicle_types_db(str(p))
        msg = str(e.value)
        assert "2 VADNÝCH" in msg
        assert "TYPE_02" in msg and "TYPE_03" in msg

    def test_zero_count_is_not_an_error(self, tmp_path):
        # typ, který dnes není k dispozici, je legitimní — jen se nepoužije
        p = _write(tmp_path / "vehicle_types-20260806.csv", [
            _row(code="TYPE_02", count=5),
            _row(code="TYPE_06", count=0),
        ])
        vehicles = load_vehicle_types_db(str(p))
        assert len(vehicles) == 5
        assert all(v["type_code"] == "TYPE_02" for v in vehicles)

    def test_comment_row_is_not_an_error(self, tmp_path):
        p = _write(tmp_path / "vehicle_types-20260806.csv", [
            "#TYPE_09;poznamka;;;;;;;;;;;;\n",
            _row(count=2),
        ])
        assert len(load_vehicle_types_db(str(p))) == 2

    def test_healthy_file_unaffected(self, tmp_path):
        # závora nesmí nic měnit na správných datech
        p = _write(tmp_path / "vehicle_types-20260806.csv",
                   [_row(count=3), _row(code="TYPE_06", count=1)])
        assert len(load_vehicle_types_db(str(p))) == 4
