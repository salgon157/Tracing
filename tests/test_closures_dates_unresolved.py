"""
test_closures_dates_unresolved.py — audit 1.7 + 1.8

  1.7 uzavírky se filtrují podle DNE ZÁVOZU (`as_of`), ne podle dneška
  1.8 páry, pro které ORS objízdku nenašel, se hlasitě vypíšou a jsou
      ve `stats` — matice pro ně zůstává beze změny, běh pokračuje

Bez OSRM/ORS: potvrzování i avoid routy jsou nahrazené (monkeypatch).
"""
import json
from datetime import date, timedelta

import numpy as np
import pytest

import closures_utils as cu


def _closures_file(tmp_path, valid_from=None, valid_to=None, cid="CLO_T"):
    c = {"id": cid, "name": "test", "active": True, "buffer_km": 0.15,
         "segment": {"from": {"lat": 49.5, "lon": 15.5},
                     "to": {"lat": 49.6, "lon": 15.6}}}
    if valid_from:
        c["valid_from"] = valid_from
    if valid_to:
        c["valid_to"] = valid_to
    p = tmp_path / "closures.json"
    p.write_text(json.dumps({"closures": [c]}), encoding="utf-8")
    return p


class TestClosureActiveByDeliveryDate:
    def test_closure_active_on_delivery_date_not_today(self, tmp_path):
        p = _closures_file(tmp_path, "2026-08-20", "2026-08-25")
        assert len(cu.load_active_closures(p, as_of="2026-08-22")) == 1
        assert cu.load_active_closures(p, as_of="2026-08-19") == []
        assert cu.load_active_closures(p, as_of="2026-08-26") == []
        # hranice včetně
        assert len(cu.load_active_closures(p, as_of="2026-08-20")) == 1
        assert len(cu.load_active_closures(p, as_of="2026-08-25")) == 1

    def test_without_as_of_falls_back_to_today(self, tmp_path):
        today = date.today()
        p = _closures_file(tmp_path, str(today - timedelta(days=1)),
                           str(today + timedelta(days=1)))
        assert len(cu.load_active_closures(p)) == 1
        p2 = _closures_file(tmp_path, str(today + timedelta(days=1)), None)
        assert cu.load_active_closures(p2) == []
        # ...ale pro zítřejší závoz platí
        assert len(cu.load_active_closures(p2, as_of=str(today + timedelta(days=1)))) == 1

    def test_open_ended_closure_active_any_day(self, tmp_path):
        p = _closures_file(tmp_path, None, None)
        assert len(cu.load_active_closures(p, as_of="2030-01-01")) == 1

    def test_solver_passes_delivery_date(self):
        # zdrojová pojistka: solver volá apply_closures_to_matrix s as_of
        # (den závozu z názvu orders souboru) a run log nese unresolved
        import vrp_solver_lines_v6 as S
        from pathlib import Path
        src = Path(S.__file__).read_text(encoding="utf-8")
        assert "as_of=delivery_date or None" in src
        assert "closures_unresolved_pairs" in src


class TestUnresolvedPairsReported:
    def _run(self, tmp_path, monkeypatch, exact_for):
        p = _closures_file(tmp_path)
        locations = [(49.0, 15.0), (49.5, 15.5), (49.6, 15.6), (50.0, 16.0)]
        n = len(locations)
        dur = np.full((n, n), 30.0); np.fill_diagonal(dur, 0)
        dist = np.full((n, n), 20.0); np.fill_diagonal(dist, 0)
        cand = {(0, 1), (0, 2), (1, 2)}
        monkeypatch.setattr(cu, "build_closure_candidate_sets",
                            lambda locs, cls: (cand, {"CLO_T": set(cand)}))
        monkeypatch.setattr(cu, "confirm_closure_candidates",
                            lambda c, locs, cls, **kw: (
                                {pair: {"route": None, "hit_ids": ["CLO_T"]} for pair in c},
                                {"CLO_T": set(c)}))
        monkeypatch.setattr(cu, "_fetch_exact_avoid_routes",
                            lambda pairs, *a, **kw: {
                                pair: {"duration_min": 45.0, "distance_km": 30.0}
                                for pair in pairs if pair in exact_for})
        stats = {}
        d2, k2 = cu.apply_closures_to_matrix(dur, dist, locations,
                                             matrix_profile="driving",
                                             closures_path=p, stats=stats)
        return d2, k2, stats

    def test_unresolved_pairs_are_counted_and_printed(self, tmp_path, monkeypatch, capsys):
        d2, k2, stats = self._run(tmp_path, monkeypatch, exact_for={(0, 1)})
        out = capsys.readouterr().out
        assert "2 paru BEZ objizdky" in out
        assert "[  0]" in out and "[  2]" in out and "CLO_T" in out
        assert stats["unresolved_count"] == 2
        assert sorted(stats["unresolved"]) == [[0, 2], [1, 2]]
        assert stats["updated"] == 1 and stats["confirmed"] == 3
        # opravený pár má novou hodnotu, nevyřešené zůstaly beze změny
        assert d2[0][1] == 45.0 and k2[0][1] == 30.0
        assert d2[0][2] == 30.0 and d2[1][2] == 30.0

    def test_all_resolved_prints_nothing_alarming(self, tmp_path, monkeypatch, capsys):
        _, _, stats = self._run(tmp_path, monkeypatch,
                                exact_for={(0, 1), (0, 2), (1, 2)})
        out = capsys.readouterr().out
        assert "BEZ objizdky" not in out
        assert stats["unresolved_count"] == 0 and stats["updated"] == 3

    def test_stats_zero_when_no_closures(self, tmp_path):
        p = _closures_file(tmp_path, "2030-01-01", "2030-01-02")   # neaktivní k dnešku
        stats = {}
        cu.apply_closures_to_matrix(np.zeros((2, 2)), np.zeros((2, 2)),
                                    [(49.0, 15.0), (49.1, 15.1)],
                                    matrix_profile="driving", closures_path=p,
                                    stats=stats)
        assert stats["closures"] == 0 and stats["unresolved_count"] == 0
