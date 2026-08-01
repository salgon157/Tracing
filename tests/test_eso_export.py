"""
test_eso_export.py — export plánu pro import do ESO

Formát podle vzoru od ESO (srpen 2026): středníky, cp1250, časy v SEKUNDÁCH
od půlnoci, jeden řádek na zastávku (sklad se nevypisuje). Depo časy:
odjezd depo = výjezd na trasu, příjezd Depo = výjezd − nakládka (40 min),
čas konec linky = návrat do skladu.
"""
import csv

import pytest

from vrp_solver_lines_v6 import CONFIG, ESO_EXPORT_HEADER, save_eso_export


def _stop(order_id, arrival, departure, loc="loc x"):
    return {"stop": f"Z {order_id}", "id": order_id, "location_code": loc,
            "arrival": arrival, "departure": departure, "kg": 100}


def _route(stops, depot_departure="07:20", depot_return="12:00",
           type_code="TYPE_02"):
    return {
        "vehicle_id": "V1",
        "vehicle_type": "Dodávka 1.35t",
        "type_code": type_code,
        "stops": ([{"stop": "Sklad Štoky", "arrival": depot_departure, "kg": 0}]
                  + stops
                  + [{"stop": "Sklad Štoky (návrat)", "arrival": depot_return,
                      "kg": 0}]),
    }


@pytest.fixture
def vehicle_types_csv(tmp_path):
    p = tmp_path / "vehicle_types.csv"
    p.write_text("type_code,type_name,max_kg\n"
                 "TYPE_02,Dodávka 1.35t,1350\n"
                 "TYPE_04,Kamion,3000\n", encoding="utf-8")
    return str(p)


def _read_export(path):
    with open(path, encoding="cp1250", newline="") as f:
        return list(csv.reader(f, delimiter=";"))


class TestEsoExport:
    def test_header_matches_eso_sample_exactly(self, tmp_path, vehicle_types_csv):
        # včetně překlepů „objendávky" a „odjedz" — ESO parsuje podle názvů
        path = save_eso_export([_route([_stop("O1", "08:00", "08:10")])],
                               tmp_path, [], "CB",
                               vehicle_types_file=vehicle_types_csv)
        rows = _read_export(path)
        assert rows[0] == ESO_EXPORT_HEADER
        assert rows[0][0] == "č. objendávky"
        assert rows[0][9] == "plán odjedz depo"

    def test_times_in_seconds_and_loading_shift(self, tmp_path, vehicle_types_csv):
        route = _route([_stop("O1", "08:00", "08:10"),
                        _stop("O2", "09:30", "09:45")],
                       depot_departure="07:20", depot_return="12:00")
        orders = [{"id": "O1", "block_id": "CB"}, {"id": "O2", "block_id": "CB"}]
        path = save_eso_export([route], tmp_path, orders, "CB",
                               delivery_date="2026-08-01",
                               vehicle_types_file=vehicle_types_csv)
        rows = _read_export(path)
        r1 = rows[1]
        assert r1[0] == "O1"
        assert r1[1] == "loc x"
        assert r1[2] == "CB"
        assert r1[3:6] == ["1", "1", "2"]          # linka 1, zastávka 1 z 2
        assert r1[6] == str(8 * 3600)              # příjezd 08:00
        assert r1[7] == str(8 * 3600 + 600)        # odjezd 08:10
        assert r1[8] == str(7 * 3600 + 20 * 60 - 40 * 60)   # nakládka od 06:40
        assert r1[9] == str(7 * 3600 + 20 * 60)    # výjezd 07:20
        assert r1[10] == str(12 * 3600)            # návrat 12:00
        assert r1[11] == "2"                       # TYPE_02 -> 2
        assert r1[12] == "1350.0"
        r2 = rows[2]
        assert r2[0] == "O2" and r2[4] == "2"
        # depo časy jsou stejné na všech řádcích linky
        assert r2[8:11] == r1[8:11]

    def test_depot_stops_not_exported(self, tmp_path, vehicle_types_csv):
        path = save_eso_export([_route([_stop("O1", "08:00", "08:10")])],
                               tmp_path, [], "CB",
                               vehicle_types_file=vehicle_types_csv)
        rows = _read_export(path)
        assert len(rows) == 2                      # hlavička + 1 objednávka
        assert not any("Sklad" in r[0] for r in rows[1:])

    def test_line_numbers_and_counts(self, tmp_path, vehicle_types_csv):
        routes = [
            _route([_stop("O1", "08:00", "08:10")]),
            _route([_stop("O2", "08:00", "08:10"),
                    _stop("O3", "09:00", "09:15"),
                    _stop("O4", "10:00", "10:05")], type_code="TYPE_04"),
        ]
        path = save_eso_export(routes, tmp_path, [], "MO",
                               vehicle_types_file=vehicle_types_csv)
        rows = _read_export(path)
        assert [r[3] for r in rows[1:]] == ["1", "2", "2", "2"]
        assert [r[4] for r in rows[1:]] == ["1", "1", "2", "3"]
        assert [r[5] for r in rows[1:]] == ["1", "3", "3", "3"]
        assert rows[2][11] == "4" and rows[2][12] == "3000.0"

    def test_loading_clamped_at_midnight(self, tmp_path, vehicle_types_csv):
        # výjezd 00:20 -> nakládka by byla „−20 min", ořízne se na 0
        route = _route([_stop("O1", "01:00", "01:10")], depot_departure="00:20")
        path = save_eso_export([route], tmp_path, [], "CB",
                               vehicle_types_file=vehicle_types_csv)
        rows = _read_export(path)
        assert rows[1][8] == "0"
        assert rows[1][9] == str(20 * 60)

    def test_default_loading_is_40_min(self):
        assert CONFIG["depot_loading_min"] == 40

    def test_filename_contains_zone_and_date(self, tmp_path, vehicle_types_csv):
        path = save_eso_export([_route([_stop("O1", "08:00", "08:10")])],
                               tmp_path, [], "PR", delivery_date="2026-08-01",
                               vehicle_types_file=vehicle_types_csv)
        assert path.name == "eso_export_PR_2026-08-01.csv"
