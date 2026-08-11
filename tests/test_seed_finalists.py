"""
Testy pro --seed-finalists: kolik finalistů, ranking seedů, čas fáze E.

Testy jsou BEHAVIORÁLNÍ — žádné přibité hodnoty CONFIG (jsou to startup
testy: červená = žádný běh; default se musí dát měnit bez červených testů).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vrp_solver_lines_v6 as solver


# ─────────────────────────────────────────────────────────────────────────────
#  resolve_seed_finalists — auto podle stroje, explicitní se respektuje
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveSeedFinalists:
    def test_auto_dost_jader_da_max_seedu(self):
        # 19 workerů / 2 clustery → vejde se 9, ale seedy jsou jen 3
        assert solver.resolve_seed_finalists("auto", n_workers=19, n_clusters=2) == 3

    def test_auto_malo_jader_spadne_na_1(self):
        # 3 workery / 2 clustery → jen 1 finalista = dosavadní chování
        assert solver.resolve_seed_finalists("auto", n_workers=3, n_clusters=2) == 1

    def test_auto_stredni_stroj(self):
        assert solver.resolve_seed_finalists("auto", n_workers=5, n_clusters=2) == 2

    def test_auto_nikdy_pod_1(self):
        assert solver.resolve_seed_finalists("auto", n_workers=1, n_clusters=8) == 1

    def test_auto_strop_je_pocet_seedu(self):
        assert solver.resolve_seed_finalists("auto", 100, 2, n_seeds=3) == 3

    def test_explicitni_cislo_se_respektuje_i_na_malem_stroji(self):
        # explicitní přání se tiše neořezává — fáze E místo toho rozdělí čas
        assert solver.resolve_seed_finalists(3, n_workers=3, n_clusters=2) == 3

    def test_explicitni_nad_pocet_seedu_se_orizne(self):
        assert solver.resolve_seed_finalists(5, 19, 2, n_seeds=3) == 3

    def test_retezcove_cislo_z_cli_projde(self):
        assert solver.resolve_seed_finalists("2", 19, 2) == 2

    def test_nula_je_chyba(self):
        with pytest.raises(ValueError):
            solver.resolve_seed_finalists(0, 19, 2)

    def test_default_v_configu_je_platny(self):
        # konkrétní hodnotu netestujeme (ať jde měnit) — jen že projde
        v = solver.CONFIG["seed_finalists"]
        assert solver.resolve_seed_finalists(v, 19, 2) >= 1


# ─────────────────────────────────────────────────────────────────────────────
#  rank_seeds — pořadí, penalizace, determinismus
# ─────────────────────────────────────────────────────────────────────────────

def _res(cost=0.0, solved=True):
    return {"routes": [{"stops": []}] if solved else [], "cost": cost}


class TestRankSeeds:
    def test_radi_podle_penalizovane_ceny(self):
        results = {
            "a": {0: _res(100), 1: _res(100)},
            "b": {0: _res(50),  1: _res(60)},
        }
        ranked = solver.rank_seeds(results, {"a": 2, "b": 2}, penalty_kc=1000)
        assert [r["seed"] for r in ranked] == ["b", "a"]
        assert ranked[0]["raw"] == 110

    def test_nevyreseny_cluster_dostane_penaltu(self):
        results = {
            "levny_deravy":    {0: _res(10), 1: _res(solved=False)},
            "drahy_kompletni": {0: _res(500), 1: _res(500)},
        }
        ranked = solver.rank_seeds(
            results, {"levny_deravy": 2, "drahy_kompletni": 2},
            penalty_kc=1_000_000)
        assert ranked[0]["seed"] == "drahy_kompletni"
        assert ranked[0]["complete"] is True
        assert ranked[1]["complete"] is False

    def test_seed_bez_jedineho_reseni_vypadne(self):
        results = {"mrtvy": {0: _res(solved=False)}, "ok": {0: _res(5)}}
        ranked = solver.rank_seeds(results, {"mrtvy": 1, "ok": 1}, penalty_kc=10)
        assert [r["seed"] for r in ranked] == ["ok"]

    def test_chybejici_vysledek_clusteru_se_pocita_jako_nevyreseny(self):
        # cluster 1 vůbec nedoběhl (chyba workeru) → nesmí být complete
        results = {"x": {0: _res(100)}}
        ranked = solver.rank_seeds(results, {"x": 2}, penalty_kc=1000)
        assert ranked[0]["complete"] is False
        assert ranked[0]["penalized"] == 100 + 1000

    def test_remiza_je_deterministicka(self):
        results = {"b": {0: _res(100)}, "a": {0: _res(100)}}
        ranked = solver.rank_seeds(results, {"a": 1, "b": 1}, penalty_kc=10)
        assert [r["seed"] for r in ranked] == ["a", "b"]


# ─────────────────────────────────────────────────────────────────────────────
#  phase_e_time_per_task — čas dle vln, wall clock se drží
# ─────────────────────────────────────────────────────────────────────────────

class TestPhaseETimePerTask:
    def test_jedna_vlna_cely_budget(self):
        assert solver.phase_e_time_per_task(180, n_tasks=6, n_workers=19) == 180

    def test_dve_vlny_pulka_casu(self):
        assert solver.phase_e_time_per_task(180, n_tasks=6, n_workers=3) == 90

    def test_dosavadni_chovani_pro_jednoho_finalistu(self):
        # dnešní vzorec: budget / ceil(clusters / workers)
        assert solver.phase_e_time_per_task(178, n_tasks=2, n_workers=19) == 178

    def test_spodni_mez_15s(self):
        assert solver.phase_e_time_per_task(10, n_tasks=100, n_workers=1) == 15

    def test_nula_ukolu_nespadne(self):
        assert solver.phase_e_time_per_task(180, n_tasks=0, n_workers=4) == 180


# ─────────────────────────────────────────────────────────────────────────────
#  _state_from_cluster_results — sestavení stavu finalisty
# ─────────────────────────────────────────────────────────────────────────────

class TestStateFromClusterResults:
    def _scd(self, orders):
        # 2 clustery: objednávky [0,1] a [2]
        return {
            "clusters":            [[orders[0], orders[1]], [orders[2]]],
            "cluster_indices":     [[0, 1], [2]],
            "vehicle_assignments": [[], []],
        }

    def test_labels_a_naklady_sedi(self):
        orders = [{"id": f"O{i}", "weight_kg": 1} for i in range(3)]
        cluster_res = {
            0: {"routes": [{"stops": [], "total_kc": 100}], "cost": 100.0},
            1: {"routes": [{"stops": [], "total_kc": 50}],  "cost": 50.0},
        }
        st = solver._state_from_cluster_results(
            orders, self._scd(orders), cluster_res, seed_penalty=999)
        assert st.cluster_labels == [0, 0, 1]
        assert st.cluster_costs == [100.0, 50.0]
        assert st.total_cost == 150.0

    def test_nevyreseny_cluster_nese_penaltu(self):
        orders = [{"id": f"O{i}", "weight_kg": 1} for i in range(3)]
        cluster_res = {0: {"routes": [{"stops": [], "total_kc": 10}], "cost": 10.0}}
        st = solver._state_from_cluster_results(
            orders, self._scd(orders), cluster_res, seed_penalty=777)
        assert st.cluster_costs[1] == 777
