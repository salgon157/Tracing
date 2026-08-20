"""
test_merge_rescue.py — nouzový plán při pádu PR: výběr volných velkých aut
ze stavu dne, filtry a řazení párů, jednoautový re-solve s okny/pauzami,
schvalovací smyčka (povolit/další/konec), účetnictví flotily, report.
Syntetická data — žádná síť, žádný solver subprocess.
"""
from pathlib import Path

import pytest

import merge_rescue as mr
from merge_rescue import (
    apply_merge_to_counts,
    build_report,
    find_pairs,
    format_proposal,
    free_big_types,
    load_zone_lines,
    pick_big_for,
    rescue_loop,
    solve_pair,
)

@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch):
    """GLS jinak běží vždy do limitu — pro bránu stačí zlomek (úlohy ~6 uzlů)."""
    monkeypatch.setitem(mr.CONFIG, "solve_limit_s", 0.15)


FLEET = [
    {"type_code": "TYPE_01", "max_kg": "1200", "available_count": "2"},
    {"type_code": "TYPE_02", "max_kg": "1350", "available_count": "49"},
    {"type_code": "TYPE_03", "max_kg": "3200", "available_count": "2"},
    {"type_code": "TYPE_04", "max_kg": "8000", "available_count": "1"},
    {"type_code": "TYPE_05", "max_kg": "2000", "available_count": "1"},
]


# ─────────────────────────────────────────────────────────────────────────────
#  Volná velká auta ze stavu dne
# ─────────────────────────────────────────────────────────────────────────────

class TestFreeBig:
    def test_only_nonsmall_with_positive_remaining(self):
        rem = {"TYPE_01": 1, "TYPE_02": 6, "TYPE_03": 1, "TYPE_04": 0, "TYPE_05": 0}
        big = free_big_types(rem, FLEET)
        assert [b["type_code"] for b in big] == ["TYPE_03"]

    def test_sorted_smallest_first(self):
        rem = {"TYPE_03": 1, "TYPE_04": 1, "TYPE_05": 1}
        assert [b["type_code"] for b in free_big_types(rem, FLEET)] == \
            ["TYPE_05", "TYPE_03", "TYPE_04"]

    def test_unknown_type_in_state_ignored(self):
        assert free_big_types({"TYPE_99": 3}, FLEET) == []

    def test_pick_smallest_adequate(self):
        big = free_big_types({"TYPE_03": 1, "TYPE_04": 1, "TYPE_05": 1}, FLEET)
        assert pick_big_for(1900, big)["type_code"] == "TYPE_05"
        assert pick_big_for(2500, big)["type_code"] == "TYPE_03"
        assert pick_big_for(5000, big)["type_code"] == "TYPE_04"
        assert pick_big_for(9000, big) is None

    def test_pick_skips_exhausted(self):
        big = free_big_types({"TYPE_03": 1, "TYPE_04": 1}, FLEET)
        big[0]["count"] = 0
        assert pick_big_for(2500, big)["type_code"] == "TYPE_04"

    def test_counts_math(self):
        rem = {"TYPE_02": 6, "TYPE_03": 1}
        out = apply_merge_to_counts(rem, "TYPE_03", ["TYPE_02", "TYPE_01"])
        assert out == {"TYPE_02": 7, "TYPE_03": 0, "TYPE_01": 1}
        assert rem == {"TYPE_02": 6, "TYPE_03": 1}          # vstup nezměněn


# ─────────────────────────────────────────────────────────────────────────────
#  Jednoautový re-solve — syntetická matice (minuty)
# ─────────────────────────────────────────────────────────────────────────────

def _stop(ws, we, service=5, loc="x", **kw):
    return {"ws": ws, "we": we, "service": service, "loc": loc,
            "lat": 0.0, "lon": 0.0, "order_id": "O1", **kw}


def _mat(n, t=30):
    return [[0 if i == j else t for j in range(n)] for i in range(n)]


class TestSolvePair:
    def test_compatible_windows_zero_violations(self):
        # 4 zastávky, okna široká -> vše v oknech
        meta = [_stop(6 * 60, 22 * 60) for _ in range(4)]
        r = solve_pair(_mat(5, 20), list(range(5)), meta)
        assert r is not None and r["viol_n"] == 0 and r["time_ok"]
        assert len(r["visits"]) == 4

    def test_conflicting_windows_report_violation(self):
        # dvě zastávky chtějí totéž úzké okno, cesta 60 min -> jedna to nestihne
        meta = [_stop(600, 610), _stop(600, 610)]
        r = solve_pair(_mat(3, 60), [0, 1, 2], meta)
        assert r is not None and r["viol_n"] >= 1
        assert r["viol_min"] >= 50
        v = [x for x in r["visits"] if x["viol"] > 0][0]
        assert v["arrival"] > v["we"] or v["early"]

    def test_return_by_2330_is_hard_in_model(self):
        # okna u půlnoci: model NESMÍ vrátit auto po 23:30, radši přijede
        # před oknem (porušení "před"), než aby přetáhl návrat
        meta = [_stop(22 * 60 + 30, 23 * 60 + 30), _stop(22 * 60 + 30, 23 * 60 + 30)]
        r = solve_pair(_mat(3, 45), [0, 1, 2], meta)
        assert r is not None
        assert r["end"] <= mr.CONFIG["latest_return_min"] or not r["time_ok"]
        early = [v for v in r["visits"] if v["viol"] > 0 and v["early"]]
        assert early                                       # někdo jel před oknem

    def test_break_shift_applied_after_4_5h(self):
        # řetěz 5 zastávek po 65 min jízdy -> elapsed přes 4,5 h -> 1 pauza
        meta = [_stop(0, 24 * 60, service=10) for _ in range(5)]
        r = solve_pair(_mat(6, 65), list(range(6)), meta)
        assert r is not None and r["breaks"] >= 1
        assert r["end"] >= r["dep"] + 5 * 65 + 4 * 10 + 45

    def test_drive_over_9h_flagged(self):
        meta = [_stop(0, 24 * 60) for _ in range(5)]
        r = solve_pair(_mat(6, 100), list(range(6)), meta)  # 6×100 min jízdy
        assert r is not None and r["time_ok"] is False


# ─────────────────────────────────────────────────────────────────────────────
#  Filtry a řazení párů
# ─────────────────────────────────────────────────────────────────────────────

def _line(zone, lid, vid, kg, stops, idx):
    return {"zone": zone, "line_id": lid, "vehicle_id": vid,
            "type_code": vid.rsplit("_", 1)[0], "kg": kg, "km": 100.0,
            "stops": stops, "idx": idx}


class TestFindPairs:
    def _setup(self):
        # matice 7×7 (0=sklad, 1..6 zastávky), 20 min všude
        dur = _mat(7, 20)
        big = [{"type_code": "TYPE_03", "max_kg": 3200.0, "count": 1}]
        s = lambda: [_stop(6 * 60, 22 * 60)]
        L1 = _line("CB", "LINE_01", "TYPE_02_01", 1000, s() * 2, [1, 2])
        L2 = _line("CB", "LINE_02", "TYPE_02_02", 1100, s() * 2, [3, 4])
        L3 = _line("CB", "LINE_03", "TYPE_02_03", 1200, s() * 2, [5, 6])
        return dur, big, [L1, L2, L3]

    def test_pairs_found_and_sorted(self):
        dur, big, lines = self._setup()
        pairs = find_pairs(lines, {"CB": dur}, big)
        assert len(pairs) == 3                       # všechny kombinace vyhoví
        assert all(p["viol_n"] == 0 for p in pairs)

    def test_kg_over_biggest_free_excluded(self):
        dur, big, lines = self._setup()
        lines[0]["kg"] = 2200                        # 2200+1100 > 3200; 2200+1200 > 3200
        pairs = find_pairs(lines, {"CB": dur}, big)
        assert {(p["A"]["line_id"], p["B"]["line_id"]) for p in pairs} == \
            {("LINE_02", "LINE_03")}

    def test_stop_cap_excluded(self, monkeypatch):
        dur, big, lines = self._setup()
        monkeypatch.setitem(mr.CONFIG, "max_stops", 3)
        assert find_pairs(lines, {"CB": dur}, big) == []

    def test_hgv_unreachable_excluded(self):
        dur, big, lines = self._setup()
        dur[0][5] = 999_999                          # zastávka linky 3 pro hgv nedostupná
        pairs = find_pairs(lines, {"CB": dur}, big)
        assert all("LINE_03" not in (p["A"]["line_id"], p["B"]["line_id"])
                   for p in pairs)

    def test_different_zones_not_paired(self):
        dur, big, lines = self._setup()
        lines[2]["zone"] = "MO"
        pairs = find_pairs(lines, {"CB": dur, "MO": dur}, big)
        assert {(p["A"]["line_id"], p["B"]["line_id"]) for p in pairs} == \
            {("LINE_01", "LINE_02")}

    def test_violation_over_tolerance_excluded(self):
        dur, big, lines = self._setup()
        # linka 3: dvě neslučitelná úzká okna -> porušení >> 60 min
        lines[2]["stops"] = [_stop(600, 605), _stop(600, 605)]
        pairs = find_pairs(lines, {"CB": [[0 if i == j else 90 for j in range(7)]
                                          for i in range(7)]}, big)
        assert all(p["viol_max"] <= mr.CONFIG["win_tol_min"] for p in pairs)


# ─────────────────────────────────────────────────────────────────────────────
#  Schvalovací smyčka
# ─────────────────────────────────────────────────────────────────────────────

def _pair(zone="CB", a="LINE_01", b="LINE_02", kg=2100, viol_n=1, viol_min=10):
    s = _stop(6 * 60, 22 * 60)
    return {"zone": zone, "kg": kg, "stops_n": 4, "drive": 300, "dep": 400,
            "end": 800, "breaks": 0, "viol_n": viol_n, "viol_min": viol_min,
            "viol_max": viol_min, "time_ok": True,
            "visits": [{**s, "arrival": 700, "viol": viol_min, "early": False}],
            "A": _line(zone, a, "TYPE_02_01", 1000, [s] * 2, [1, 2]),
            "B": _line(zone, b, "TYPE_02_02", 1100, [s] * 2, [3, 4])}


BIG1 = [{"type_code": "TYPE_03", "max_kg": 3200.0, "count": 1}]
REM = {"TYPE_02": 6, "TYPE_03": 1}


class TestRescueLoop:
    def test_approve_and_pr_succeeds(self):
        calls = []
        def run_pr(counts):
            calls.append(dict(counts))
            return 0, {"status": "ok", "lines_count": 13}
        r = rescue_loop([_pair()], BIG1, REM, run_pr, ask=lambda _: "a",
                        echo=lambda *_: None)
        assert r["status"] == "ok" and len(r["merges"]) == 1
        assert calls == [{"TYPE_02": 8, "TYPE_03": 0}]     # +2 malá, −1 velké
        assert r["pr"]["lines_count"] == 13

    def test_refuse_ends_without_solver(self):
        def run_pr(counts):                                 # noqa: ARG001
            raise AssertionError("solver nesmí běžet bez povolení")
        r = rescue_loop([_pair()], BIG1, REM, run_pr, ask=lambda _: "n",
                        echo=lambda *_: None)
        assert r["status"] == "refused" and r["merges"] == []

    def test_next_proposal_on_d(self):
        seen = []
        def ask(prompt):
            seen.append(prompt)
            return "d" if len(seen) == 1 else "a"
        p1, p2 = _pair(), _pair(a="LINE_03", b="LINE_04", viol_min=20)
        p2["A"]["vehicle_id"] = "TYPE_02_03"; p2["B"]["vehicle_id"] = "TYPE_02_04"
        r = rescue_loop([p1, p2], BIG1, REM, lambda c: (0, {"status": "ok"}),
                        ask=ask, echo=lambda *_: None)
        assert r["status"] == "ok"
        assert r["merges"][0]["A"]["line_id"] == "LINE_03"  # první přeskočen

    def test_iterates_while_pr_fails_and_big_available(self):
        big2 = [{"type_code": "TYPE_03", "max_kg": 3200.0, "count": 2}]
        rcs = iter([3, 0])
        r = rescue_loop([_pair(), _pair(a="LINE_03", b="LINE_04")], big2, REM,
                        lambda c: (next(rcs), {"status": "x"}),
                        ask=lambda _: "a", echo=lambda *_: None)
        assert r["status"] == "ok" and len(r["merges"]) == 2
        assert r["remaining"]["TYPE_02"] == 10             # 6 + 2×2

    def test_no_more_big_cars_gives_noway(self):
        r = rescue_loop([_pair(), _pair(a="LINE_03", b="LINE_04")], BIG1, REM,
                        lambda c: (3, None), ask=lambda _: "a",
                        echo=lambda *_: None)
        assert r["status"] == "noway" and len(r["merges"]) == 1

    def test_line_not_merged_twice(self):
        big2 = [{"type_code": "TYPE_03", "max_kg": 3200.0, "count": 2}]
        # druhý pár sdílí LINE_01 -> nesmí se nabídnout po prvním sloučení
        r = rescue_loop([_pair(), _pair(a="LINE_01", b="LINE_04")], big2, REM,
                        lambda c: (3, None), ask=lambda _: "a",
                        echo=lambda *_: None)
        assert len(r["merges"]) == 1

    def test_empty_pairs_noway(self):
        r = rescue_loop([], BIG1, REM, lambda c: (0, None),
                        ask=lambda _: "a", echo=lambda *_: None)
        assert r["status"] == "noway" and r["merges"] == []

    def test_pr_data_error_stops(self):
        r = rescue_loop([_pair()], BIG1, REM, lambda c: (2, None),
                        ask=lambda _: "a", echo=lambda *_: None)
        assert r["status"] == "error" and r["rc"] == 2


# ─────────────────────────────────────────────────────────────────────────────
#  Prezentace a report
# ─────────────────────────────────────────────────────────────────────────────

class TestOutput:
    def test_proposal_lists_each_violated_window(self):
        p = _pair(viol_min=53)
        p["visits"][0]["loc"] = "kotva u lipy"
        txt = format_proposal(p, BIG1[0], 2)
        assert "LINE_01 + LINE_02" in txt and "TYPE_03" in txt
        assert "kotva u lipy" in txt and "o 53 min" in txt
        assert "+2 malých aut" in txt

    def test_proposal_zero_violations_says_so(self):
        p = _pair(viol_n=0, viol_min=0)
        p["visits"][0]["viol"] = 0
        assert "VŠECHNA DODRŽENA" in format_proposal(p, BIG1[0], 2)

    def test_report_contains_changes_and_route(self):
        res = {"status": "ok",
               "merges": [{**_pair(), "big": BIG1[0]}],
               "remaining": {}, "pr": {"status": "ok"}}
        txt = build_report("2026-08-20", res, Path("data/results/PR/2026-08-20_rescue"))
        assert "zrušit LINE_01 a LINE_02" in txt
        assert "TYPE_02_01" in txt and "2026-08-20_rescue" in txt
        assert "ručně v ESO" in txt


# ─────────────────────────────────────────────────────────────────────────────
#  Načtení linek z výsledků (malé, ne dvojlinky)
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadZoneLines:
    def test_small_only_no_double_runs(self, tmp_path):
        (tmp_path / "lines_summary.csv").write_text(
            "line_id,vehicle_id,total_km,total_kg,double_run\n"
            "L1,TYPE_02_01,120,900,\n"
            "L2,TYPE_03_01,80,2000,\n"          # střední -> ven
            "L3,TYPE_02_02,60,800,1\n"          # dvojlinka -> ven
            "L4,TYPE_02_03,90,700,\n"
            ",,,,\n", encoding="utf-8")
        (tmp_path / "lines_stops.csv").write_text(
            "line_id,order_id,location_code,arrival,window,service_min,lat,lon\n"
            "L1,,SKLAD,05:00,,0,49.5,15.6\n"
            "L1,O1,adr a,08:00,07:00–09:00,5,49.1,15.1\n"
            "L2,O2,adr b,09:00,08:00–10:00,5,49.2,15.2\n"
            "L3,O3,adr c,10:00,09:00–11:00,5,49.3,15.3\n"
            "L4,O4,adr d,11:00,10:00–12:00,5,49.4,15.4\n", encoding="utf-8")
        lines = load_zone_lines(tmp_path, "CB", {"TYPE_01", "TYPE_02"})
        assert [l["line_id"] for l in lines] == ["L1", "L4"]
        assert lines[0]["stops"][0]["ws"] == 7 * 60
        assert lines[0]["stops"][0]["loc"] == "adr a"
