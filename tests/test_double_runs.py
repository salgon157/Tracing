"""
test_double_runs.py — dvojlinky (--double-runs, porušení L2)

Auto smí naložit ve skladu 2× za den. Virtuální „druhá jízda" vozidla
platí PLNÝ druhý výjezd (+1 Kč preference fyzických), smí vyjet od
CONFIG double_run_earliest a po solve se párují na fyzická auta:
návrat + depot_loading_min <= výjezd druhé jízdy, jinak běh spadne.
"""
import numpy as np
import pytest

import vrp_solver_lines_v6 as solver_mod
from vrp_solver_lines_v6 import (
    CONFIG,
    build_double_run_vehicles,
    is_double_run_vehicle,
    pair_double_runs,
    solve_cluster,
    time_to_minutes,
)


def _vehicle(type_code="TYPE_02", n=1, max_kg=1350, start_cost=1000):
    return {
        "id": f"{type_code}_{n:02d}", "type_code": type_code,
        "type": "Dodávka", "driver": "",
        "max_kg": max_kg, "cost_per_km": 11.0, "start_cost": start_cost,
        "time_multiplier": 1.0, "osrm_profile": "driving",
    }


def _route(vehicle_id="TYPE_02_01", type_code="TYPE_02",
           departure="06:00", ret="09:00"):
    return {
        "vehicle_id": vehicle_id, "type_code": type_code, "driver": "Karel",
        "stops": [
            {"stop": "Sklad", "arrival": departure, "kg": 0},
            {"stop": "Zákazník", "id": "O1", "arrival": departure, "kg": 100},
            {"stop": "Sklad (návrat)", "arrival": ret, "kg": 0},
        ],
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Stavba virtuálních vozidel
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildDoubleRunVehicles:
    def test_only_small_types(self):
        fleet = [_vehicle("TYPE_02", 1), _vehicle("TYPE_06", 1, max_kg=8000)]
        virtuals = build_double_run_vehicles(fleet)
        assert all(v["type_code"] == "TYPE_02" for v in virtuals)

    def test_id_format_keeps_type_parseable(self):
        virtuals = build_double_run_vehicles([_vehicle("TYPE_02", 1)])
        assert virtuals[0]["id"] == "TYPE_02_2R01"
        # fleet_budget čte typ přes rsplit("_", 1) — nesmí se rozbít
        assert virtuals[0]["id"].rsplit("_", 1)[0] == "TYPE_02"
        assert is_double_run_vehicle(virtuals[0]["id"])
        assert not is_double_run_vehicle("TYPE_02_01")

    def test_full_second_fix_plus_preference(self):
        # plný druhý výjezd (rozhodnutí uživatele) + 1 Kč, aby solver
        # preferoval fyzická auta a druhá jízda bez první nevznikala
        virtuals = build_double_run_vehicles([_vehicle(start_cost=1000)])
        assert virtuals[0]["start_cost"] == 1001

    def test_earliest_start_from_config(self):
        virtuals = build_double_run_vehicles([_vehicle()])
        assert virtuals[0]["earliest_start_min"] == \
            time_to_minutes(CONFIG["double_run_earliest"])

    def test_capped_by_config_and_physical_count(self):
        fleet = [_vehicle("TYPE_02", i) for i in range(1, 30)]
        assert len(build_double_run_vehicles(fleet)) == \
            CONFIG["double_runs_max"]
        # 2 fyzická auta -> max 2 dvojlinky, i když config dovoluje víc
        assert len(build_double_run_vehicles(fleet[:2])) == 2

    def test_biggest_small_type_first(self):
        fleet = ([_vehicle("TYPE_01", i, max_kg=1200) for i in range(1, 3)]
                 + [_vehicle("TYPE_02", i) for i in range(1, 25)])
        virtuals = build_double_run_vehicles(fleet)
        assert virtuals[0]["type_code"] == "TYPE_02"


# ═════════════════════════════════════════════════════════════════════════════
#  Párování po solve
# ═════════════════════════════════════════════════════════════════════════════

class TestPairDoubleRuns:
    def test_no_virtuals_noop(self):
        routes = [_route()]
        assert pair_double_runs(routes) == routes

    def test_pairs_and_rewrites_vehicle(self):
        routes = [_route(ret="09:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="10:00",
                         ret="14:00")]
        out = pair_double_runs(routes)
        second = [r for r in out if r.get("double_run")]
        assert len(second) == 1
        assert second[0]["vehicle_id"] == "TYPE_02_01"   # fyzické auto
        assert second[0]["driver"] == "Karel"

    def test_reload_boundary(self):
        # návrat 09:20 + 40 min nakládka = 10:00 -> výjezd 10:00 PROJDE
        ok = [_route(ret="09:20"),
              _route(vehicle_id="TYPE_02_2R01", departure="10:00")]
        assert pair_double_runs(ok)[1]["double_run"] is True
        # návrat 09:21 -> chybí minuta -> spadne
        bad = [_route(ret="09:21"),
               _route(vehicle_id="TYPE_02_2R01", departure="10:00")]
        with pytest.raises(SystemExit, match="NEJDOU SPÁROVAT"):
            pair_double_runs(bad)

    def test_same_type_only(self):
        routes = [_route(vehicle_id="TYPE_01_01", type_code="TYPE_01",
                         ret="08:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="10:00")]
        with pytest.raises(SystemExit):
            pair_double_runs(routes)

    def test_physical_hosts_max_one_second_run(self):
        routes = [_route(ret="08:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="10:00"),
                  _route(vehicle_id="TYPE_02_2R02", departure="11:00")]
        with pytest.raises(SystemExit, match="obsazeno"):
            pair_double_runs(routes)

    def test_greedy_leaves_late_returns_for_late_departures(self):
        # brzká dvojlinka si vezme brzký návrat; pozdní zůstane pozdní
        routes = [_route(vehicle_id="TYPE_02_01", ret="09:00"),
                  _route(vehicle_id="TYPE_02_02", ret="11:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="12:00"),
                  _route(vehicle_id="TYPE_02_2R02", departure="10:00")]
        out = pair_double_runs(routes)
        by_dep = {r["stops"][0]["arrival"]: r for r in out
                  if r.get("double_run")}
        assert by_dep["10:00"]["vehicle_id"] == "TYPE_02_01"   # 09:00 návrat
        assert by_dep["12:00"]["vehicle_id"] == "TYPE_02_02"   # 11:00 návrat

    def test_second_run_without_first_fatal(self):
        # dvojlinka jede, ale žádné fyzické auto téhož typu nevyjelo
        routes = [_route(vehicle_id="TYPE_02_2R01", departure="10:00")]
        with pytest.raises(SystemExit, match="žádná"):
            pair_double_runs(routes)


# ═════════════════════════════════════════════════════════════════════════════
#  Integrace: skutečný mini-solve (OR-Tools, bez OSRM)
# ═════════════════════════════════════════════════════════════════════════════

class TestSolveWithDoubleRun:
    def _orders(self):
        # 2 objednávky po 900 kg -> jedno auto (1000 kg) je nepobere najednou
        return [
            {"order_number": "OA", "id": "OA", "name": "Ranní",
             "customer_name": "Ranní", "location_code": "ra",
             "time_from": "06:00", "time_to": "08:00",
             "weight_kg": 900.0, "lat": 50.0, "lon": 14.0,
             "service_sec": 600},
            {"order_number": "OB", "id": "OB", "name": "Odpolední",
             "customer_name": "Odpolední", "location_code": "od",
             "time_from": "12:00", "time_to": "14:00",
             "weight_kg": 900.0, "lat": 50.1, "lon": 14.1,
             "service_sec": 600},
        ]

    def test_double_run_solves_and_pairs(self):
        # 1 fyzické auto + jeho dvojlinka zvládnou den, který by jinak
        # potřeboval 2 auta
        physical = _vehicle(max_kg=1000)
        virtual = build_double_run_vehicles([physical])[0]
        vehicles = [physical, virtual]
        n = 3                                    # sklad + 2 objednávky
        dist = np.full((n, n), 5.0); np.fill_diagonal(dist, 0)
        times = np.full((n, n), 10.0); np.fill_diagonal(times, 0)

        routes, cost = solve_cluster(
            self._orders(), vehicles, dist, [times, times],
            time_limit_sec=5)
        assert len(routes) == 2, "očekávám první jízdu + dvojlinku"

        virtual_routes = [r for r in routes
                          if is_double_run_vehicle(r["vehicle_id"])]
        assert len(virtual_routes) == 1
        # dvojlinka nesměla vyjet před double_run_earliest
        dep = time_to_minutes(virtual_routes[0]["stops"][0]["arrival"])
        assert dep >= time_to_minutes(CONFIG["double_run_earliest"])

        paired = pair_double_runs(routes)
        second = [r for r in paired if r.get("double_run")][0]
        assert second["vehicle_id"] == "TYPE_02_01"

    def test_without_flag_same_day_needs_two_vehicles(self):
        # kontrolní běh: bez dvojlinky ten samý den vyžaduje 2 fyzická auta
        vehicles = [_vehicle(max_kg=1000, n=1), _vehicle(max_kg=1000, n=2)]
        n = 3
        dist = np.full((n, n), 5.0); np.fill_diagonal(dist, 0)
        times = np.full((n, n), 10.0); np.fill_diagonal(times, 0)
        routes, _ = solve_cluster(self._orders(), vehicles, dist,
                                  [times, times], time_limit_sec=5)
        assert len(routes) == 2
        assert not any(is_double_run_vehicle(r["vehicle_id"]) for r in routes)


# ═════════════════════════════════════════════════════════════════════════════
#  Alokace aut clusterům s dvojlinkami — 16. 8. 2026 (PR, poslední depo):
#  všech 10 virtuálních jízd skončilo v jednom clusteru → 4 fyzická auta na
#  41 ranních objednávek → neřešitelné ve všech seedech → depo bez plánu.
# ═════════════════════════════════════════════════════════════════════════════

from vrp_solver_lines_v6 import (       # noqa: E402  (sekce má vlastní importy)
    assign_vehicles_to_clusters,
    is_virtual_vehicle,
    _repair_heaviest_order,
    _unsolvable_cluster_report,
)


def _order(no, time_from="06:00", time_to="09:00", kg=300.0, lat=50.0, lon=14.0):
    return {"order_number": no, "id": no, "customer_name": no,
            "location_code": no.lower(), "time_from": time_from,
            "time_to": time_to, "weight_kg": kg, "lat": lat, "lon": lon,
            "service_sec": 600}


def _fleet(n_small=19, with_virtual=True):
    physical = [_vehicle("TYPE_02", i, max_kg=1350) for i in range(1, n_small + 1)]
    return physical + (build_double_run_vehicles(physical) if with_virtual else [])


def _two_clusters(n_per=40):
    # dva geograficky oddělené shluky, každý půl ranních (do 09:00) a půl
    # odpoledních (do 14:00) objednávek — obraz PR 17. 8. 2026
    def make(prefix, lat):
        out = []
        for i in range(n_per):
            early = i % 2 == 0
            out.append(_order(f"{prefix}{i:02d}",
                              time_from="06:00" if early else "10:00",
                              time_to="09:00" if early else "14:00",
                              lat=lat + i * 0.001, lon=14.0 + i * 0.001))
        return out
    return [make("A", 49.0), make("B", 51.0)]


class TestVirtualSpreadAcrossClusters:
    def test_is_virtual_by_field_or_id(self):
        assert is_virtual_vehicle({"id": "TYPE_02_2R01"})
        assert is_virtual_vehicle({"id": "X", "earliest_start_min": 600})
        assert not is_virtual_vehicle({"id": "TYPE_02_01"})

    def test_virtual_never_piles_into_one_cluster(self):
        clusters = _two_clusters()
        fleet = _fleet()
        n_virtual = sum(1 for v in fleet if is_virtual_vehicle(v))
        assert n_virtual >= 2, "test potřebuje aspoň 2 dvojlinky"
        asg = assign_vehicles_to_clusters(clusters, fleet)
        per_cluster = [sum(1 for v in a if is_virtual_vehicle(v)) for a in asg]
        # každý cluster s odpolední prací dostane díl; žádný nedostane vše
        assert all(c > 0 for c in per_cluster)
        assert max(per_cluster) < n_virtual
        # poměr odpovídá odpolední práci (tady 1:1) — rozdíl nejvýš 1
        assert abs(per_cluster[0] - per_cluster[1]) <= 1

    def test_physical_split_is_not_starved_by_virtual(self):
        # Původní chyba: cluster s virtuálními měl jen 4 fyzická auta z 19.
        clusters = _two_clusters()
        asg = assign_vehicles_to_clusters(clusters, _fleet())
        physical_counts = [sum(1 for v in a if not is_virtual_vehicle(v))
                           for a in asg]
        assert min(physical_counts) >= 8, physical_counts

    def test_all_vehicles_assigned_exactly_once(self):
        fleet = _fleet()
        asg = assign_vehicles_to_clusters(_two_clusters(), fleet)
        ids = [v["id"] for a in asg for v in a]
        assert sorted(ids) == sorted(v["id"] for v in fleet)

    def test_cluster_without_afternoon_work_gets_no_virtual(self):
        morning_only = [_order(f"M{i}", "05:00", "08:00", lat=49.0 + i * 0.001)
                        for i in range(30)]
        afternoon = [_order(f"P{i}", "10:00", "15:00", lat=51.0 + i * 0.001)
                     for i in range(30)]
        asg = assign_vehicles_to_clusters([morning_only, afternoon], _fleet())
        assert sum(1 for v in asg[0] if is_virtual_vehicle(v)) == 0
        assert sum(1 for v in asg[1] if is_virtual_vehicle(v)) > 0

    def test_without_virtual_behaviour_unchanged_shape(self):
        # bez dvojlinek: jen fyzická auta, žádné virtuální nikde
        asg = assign_vehicles_to_clusters(_two_clusters(),
                                          _fleet(with_virtual=False))
        assert all(not is_virtual_vehicle(v) for a in asg for v in a)
        assert sum(len(a) for a in asg) == 19

    def test_more_clusters_than_physical_gets_capped_upstream(self):
        # samotná alokace: 3 clustery, 2 fyzická + 2 virtuální — cluster
        # bez fyzického auta nesmí dostat virtuální
        physical = [_vehicle("TYPE_02", 1), _vehicle("TYPE_02", 2)]
        fleet = physical + build_double_run_vehicles(physical)
        clusters = [[_order("A", "10:00", "14:00", lat=49)],
                    [_order("B", "10:00", "14:00", lat=50)],
                    [_order("C", "10:00", "14:00", lat=51)]]
        asg = assign_vehicles_to_clusters(clusters, fleet)
        for a in asg:
            if a and all(is_virtual_vehicle(v) for v in a):
                pytest.fail("cluster jen s virtuálními jízdami")


class TestHeaviestOrderRepair:
    def test_swaps_in_sufficient_vehicle(self):
        heavy_cluster = [_order("H", kg=1900.0, lat=49.0),
                         _order("h2", kg=200.0, lat=49.0)]
        light_cluster = [_order("L", kg=300.0, lat=51.0)]
        big = _vehicle("TYPE_04", 1, max_kg=3200)
        smalls = [_vehicle("TYPE_02", i, max_kg=1350) for i in range(1, 4)]
        assignments = [[smalls[0], smalls[1]], [big, smalls[2]]]
        notes = _repair_heaviest_order([heavy_cluster, light_cluster], assignments)
        assert big in assignments[0]
        assert len(assignments[0]) == 2 and len(assignments[1]) == 2
        assert notes and "přesunuto" in notes[0]

    def test_no_swap_when_donor_would_break(self):
        c0 = [_order("H0", kg=1900.0, lat=49.0)]
        c1 = [_order("H1", kg=1900.0, lat=51.0)]
        big = _vehicle("TYPE_04", 1, max_kg=3200)
        small = _vehicle("TYPE_02", 1, max_kg=1350)
        assignments = [[small], [big]]
        notes = _repair_heaviest_order([c0, c1], assignments)
        assert big in assignments[1]          # dárce si auto nechá
        assert notes and "neřešitelný" in notes[0]

    def test_end_to_end_allocation_respects_heaviest(self):
        heavy_cluster = [_order("H", kg=1900.0, lat=49.0)] + \
            [_order(f"a{i}", kg=200.0, lat=49.0 + i * 0.001) for i in range(20)]
        light_cluster = [_order(f"b{i}", kg=200.0, lat=51.0 + i * 0.001)
                         for i in range(20)]
        fleet = [_vehicle("TYPE_04", 1, max_kg=3200)] + \
            [_vehicle("TYPE_02", i, max_kg=1350) for i in range(1, 12)]
        asg = assign_vehicles_to_clusters([heavy_cluster, light_cluster], fleet)
        assert max(v["max_kg"] for v in asg[0]) >= 1900


class TestUnsolvableReportNamesCulprits:
    def test_reports_virtual_ratio_and_heavy_order(self):
        orders = [_order("H", kg=1900.0)] + [_order(f"e{i}") for i in range(30)]
        physical = [_vehicle("TYPE_02", 1)]
        vehicles = physical + build_double_run_vehicles(
            [_vehicle("TYPE_02", i) for i in range(1, 6)])
        txt = _unsolvable_cluster_report("sweep", 0, orders, vehicles)
        assert "těžší než největší auto" in txt
        assert "Fyzických aut 1" in txt
        assert "málo fyzických aut" in txt.lower()
        assert "NE náklad" in txt


# ═════════════════════════════════════════════════════════════════════════════
#  Audit 2.4: párování vidí i NEČINNÁ fyzická auta z celé flotily
# ═════════════════════════════════════════════════════════════════════════════

class TestPairingUsesIdleVehicles:
    def _fleet(self, n=3, type_code="TYPE_02"):
        return [_vehicle(type_code, i) for i in range(1, n + 1)]

    def test_idle_physical_vehicle_used_before_failing(self):
        # jelo jen TYPE_02_01 (návrat 14:00 — pozdě pro výjezd 12:00);
        # TYPE_02_02 a _03 stály → dvojlinka jede jako první jízda _02
        routes = [_route(vehicle_id="TYPE_02_01", ret="14:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="12:00")]
        out = pair_double_runs(routes, self._fleet(3))
        conv = [r for r in out if r["stops"][0]["arrival"] == "12:00"][0]
        assert conv["vehicle_id"] == "TYPE_02_02"
        assert conv.get("double_run") is False           # je to jeho PRVNÍ jízda
        assert not is_double_run_vehicle(conv["vehicle_id"])

    def test_prefers_returned_vehicle_over_idle(self):
        routes = [_route(vehicle_id="TYPE_02_01", ret="10:30"),
                  _route(vehicle_id="TYPE_02_2R01", departure="12:00")]
        out = pair_double_runs(routes, self._fleet(3))
        second = [r for r in out if r["stops"][0]["arrival"] == "12:00"][0]
        assert second["vehicle_id"] == "TYPE_02_01" and second["double_run"] is True

    def test_two_virtuals_get_two_different_idle_cars(self):
        routes = [_route(vehicle_id="TYPE_02_01", ret="15:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="12:00", ret="16:00"),
                  _route(vehicle_id="TYPE_02_2R02", departure="12:30", ret="16:30")]
        out = pair_double_runs(routes, self._fleet(3))
        ids = sorted(r["vehicle_id"] for r in out)
        assert ids == ["TYPE_02_01", "TYPE_02_02", "TYPE_02_03"]

    def test_idle_of_other_type_not_used(self):
        routes = [_route(vehicle_id="TYPE_02_01", ret="15:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="12:00")]
        fleet = [_vehicle("TYPE_02", 1), _vehicle("TYPE_01", 1, max_kg=1200)]
        with pytest.raises(SystemExit) as e:
            pair_double_runs(routes, fleet)
        assert "nečinná auta typu: žádná" in str(e.value)

    def test_no_candidate_at_all_exit_3(self):
        from vrp_solver_lines_v6 import EXIT_INFEASIBLE
        routes = [_route(vehicle_id="TYPE_02_01", ret="15:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="12:00")]
        with pytest.raises(SystemExit) as e:
            pair_double_runs(routes, self._fleet(1))
        assert e.value.code == EXIT_INFEASIBLE

    def test_idle_vehicle_counted_once_in_fleet_usage(self, tmp_path):
        # integrace s fleet_budget: po přiřazení nečinného auta je spotřeba
        # = počet unikátních fyzických vozidel (auto se nepočítá dvakrát)
        import csv
        import fleet_budget as fb
        routes = [_route(vehicle_id="TYPE_02_01", ret="14:00"),
                  _route(vehicle_id="TYPE_02_2R01", departure="12:00")]
        out = pair_double_runs(routes, self._fleet(3))
        p = tmp_path / "lines_summary.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["zone", "line_id", "vehicle_id",
                                              "total_kg", "double_run"])
            w.writeheader()
            for i, r in enumerate(out, 1):
                w.writerow({"zone": "PR", "line_id": f"LINE_{i:02d}",
                            "vehicle_id": r["vehicle_id"], "total_kg": 100,
                            "double_run": "2. jízda" if r.get("double_run") else ""})
        lines = fb.parse_lines_summary(p)
        assert fb.vehicles_used_by_type(lines) == {"TYPE_02": 2}
        assert not any(l["double_run"] for l in lines)      # žádná dvojlinka
