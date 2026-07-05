"""
benchmark/configs.py — 15 konfigurací pro hledání optimálního nastavení solveru
================================================================================

Tři osy testování + follow-up fáze:
  Osa 1 — Phase allocation (C/D/E budget split)      — 5 konfigurací
  Osa 2 — Počet clusterů                             — 2 konfigurace
  Osa 3 — LNS destroy range + stagnation             — 3 konfigurace
  Kombinace nejzajímavějších os                       — 2 konfigurace
  Phase 2 — Symetrická mřížka cluster × D            — 2 konfigurace
  Phase 3 — Fine-tuning vítěze                       — 1 konfigurace

Winner (stav 2026-04): 06_2clusters (+1.7 % vs 01_baseline průměr přes 9 datasetů,
+2.1 % na cross-validačních dnech Apr 16+17, CB/HK/MO × 04-07/10/16/17).
Tyto hodnoty jsou nyní produkční default v solver.CONFIG.

Každá konfigurace je dict obsahující:
  name          — krátký identifikátor
  description   — co testujeme a proč
  overrides     — dict hodnot pro monkey-patch do solver.CONFIG
                  (pouze položky které se liší od výchozí konfigurace)

Pozor: 01_baseline má hodnoty historického produkčního defaultu (před benchmarkem),
NE aktuálního. Zachování je nutné pro platnost porovnání v experiment_log.jsonl.
"""

CONFIGS: list[dict] = [

    # ─── Osa 1: Phase allocation ──────────────────────────────────────────────
    # Historický baseline (pre-2026-04 benchmark) — slouží jako referenční anchor
    # pro experiment_log.jsonl, NEMĚNIT hodnoty (porovnávání napříč runy).
    # Produkční default (v solver.CONFIG) je nyní 06_2clusters (+2.1 % vs tento baseline).
    {
        "name": "01_baseline",
        "description": "Historický baseline (C=0.40 D=0.10 E=0.50, 3 clustery) — pre-benchmark default",
        "overrides": {
            "budget_phase_C_pct": 0.40,
            "budget_phase_D_pct": 0.10,
            "budget_phase_E_pct": 0.50,
            "num_clusters":       3,
        },
    },

    # Hypotéza: D nepomáhá — přesunout její budget na E
    {
        "name": "02_no_D",
        "description": "Bez phase D — celý zbytek jde do E (C=0.40 D=0.00 E=0.60)",
        "overrides": {
            "budget_phase_C_pct": 0.40,
            "budget_phase_D_pct": 0.00,
            "budget_phase_E_pct": 0.60,
            "num_clusters":       3,
        },
    },

    # Hypotéza: E dominuje — dát jí co nejvíce
    {
        "name": "03_e_dominant",
        "description": "E dominuje (C=0.25 D=0.00 E=0.75)",
        "overrides": {
            "budget_phase_C_pct": 0.25,
            "budget_phase_D_pct": 0.00,
            "budget_phase_E_pct": 0.75,
            "num_clusters":       3,
        },
    },

    # Hypotéza: silnější seed = lepší základ pro E
    {
        "name": "04_strong_seed",
        "description": "Silnější seed solve (C=0.60 D=0.00 E=0.40)",
        "overrides": {
            "budget_phase_C_pct": 0.60,
            "budget_phase_D_pct": 0.00,
            "budget_phase_E_pct": 0.40,
            "num_clusters":       3,
        },
    },

    # Rovnoměrný split C/E bez D
    {
        "name": "05_balanced_no_D",
        "description": "Rovnoměrný split bez D (C=0.33 D=0.00 E=0.67)",
        "overrides": {
            "budget_phase_C_pct": 0.33,
            "budget_phase_D_pct": 0.00,
            "budget_phase_E_pct": 0.67,
            "num_clusters":       3,
        },
    },

    # ─── Osa 2: Počet clusterů ────────────────────────────────────────────────
    # Méně, větší clustery — solver vidí větší geografické celky najednou
    {
        "name": "06_2clusters",
        "description": "Méně clusterů (2) — větší celky, baseline alokace",
        "overrides": {
            "budget_phase_C_pct": 0.40,
            "budget_phase_D_pct": 0.00,
            "budget_phase_E_pct": 0.60,
            "num_clusters":       2,
        },
    },

    # Více, menší clustery — solver řeší menší problémy rychleji
    {
        "name": "07_4clusters",
        "description": "Více clusterů (4) — menší problémy, rychlejší solve",
        "overrides": {
            "budget_phase_C_pct": 0.40,
            "budget_phase_D_pct": 0.00,
            "budget_phase_E_pct": 0.60,
            "num_clusters":       4,
        },
    },

    # ─── Osa 3: LNS parametry Phase D (D musí mít nenulový budget) ──────────────
    # Jemné lokální změny — destroy 3–12, konzervativní cross-cluster hledání
    {
        "name": "08_lns_fine",
        "description": "Jemný LNS destroy (3–12) — C=0.35 D=0.25 E=0.40",
        "overrides": {
            "budget_phase_C_pct": 0.35,
            "budget_phase_D_pct": 0.25,
            "budget_phase_E_pct": 0.40,
            "num_clusters":       3,
            "lns_destroy_min":    3,
            "lns_destroy_max":    12,
        },
    },

    # Agresivní přeskupení — destroy 10–40, větší šance uniknout z lokálního minima
    {
        "name": "09_lns_aggressive",
        "description": "Agresivní LNS destroy (10–40) — C=0.35 D=0.25 E=0.40",
        "overrides": {
            "budget_phase_C_pct": 0.35,
            "budget_phase_D_pct": 0.25,
            "budget_phase_E_pct": 0.40,
            "num_clusters":       3,
            "lns_destroy_min":    10,
            "lns_destroy_max":    40,
        },
    },

    # Rychlý restart — nízký stagnation limit nutí LNS měnit strategii dřív
    {
        "name": "10_lns_fast_restart",
        "description": "Rychlý restart LNS (destroy 5–25, stagnation=5) — C=0.35 D=0.25 E=0.40",
        "overrides": {
            "budget_phase_C_pct": 0.35,
            "budget_phase_D_pct": 0.25,
            "budget_phase_E_pct": 0.40,
            "num_clusters":       3,
            "lns_destroy_min":    5,
            "lns_destroy_max":    25,
            "lns_stagnation_limit": 5,
        },
    },

    # ─── Kombinace ────────────────────────────────────────────────────────────
    # 2 clustery (nejlepší z osy 2) + Phase D zapnutá — testuje zda D pomůže i u 2 clusterů
    {
        "name": "11_2c_with_D",
        "description": "2 clustery + Phase D (C=0.35 D=0.25 E=0.40) — kombinace os 2+3",
        "overrides": {
            "budget_phase_C_pct": 0.35,
            "budget_phase_D_pct": 0.25,
            "budget_phase_E_pct": 0.40,
            "num_clusters":       2,
            "lns_destroy_min":    5,
            "lns_destroy_max":    25,
        },
    },

    # Silný seed + Phase D s agresivním LNS — lepší základ pro cross-cluster hledání
    {
        "name": "12_strong_seed_with_D",
        "description": "Silný seed (C=0.45 D=0.25 E=0.30) + agresivní LNS (10–40)",
        "overrides": {
            "budget_phase_C_pct": 0.45,
            "budget_phase_D_pct": 0.25,
            "budget_phase_E_pct": 0.30,
            "num_clusters":       3,
            "lns_destroy_min":    10,
            "lns_destroy_max":    40,
        },
    },

    # ─── Phase 2: Symetrická mřížka cluster × D ───────────────────────────────
    # 4 clustery + Phase D — symetrický protějšek k 11_2c_with_D
    # Testuje: pomáhá D u 4 clusterů při delším budgetu (30 min)?
    {
        "name": "13_4c_with_D",
        "description": "4 clustery + Phase D (C=0.30 D=0.20 E=0.50) — testuje vliv D u 4 clust",
        "overrides": {
            "budget_phase_C_pct": 0.30,
            "budget_phase_D_pct": 0.20,
            "budget_phase_E_pct": 0.50,
            "num_clusters":       4,
            "lns_destroy_min":    5,
            "lns_destroy_max":    25,
        },
    },

    # Heavy LNS — maximální čas pro Phase D (50 % budgetu) + agresivní destroy
    # Testuje: může LNS s 15+ min běhu překonat cluster-count vítěze?
    # 2 clustery zachovány (vítěz z Phase 1), aby se izoloval efekt LNS.
    {
        "name": "14_heavy_lns",
        "description": "Heavy LNS (C=0.25 D=0.50 E=0.25) + 2 clustery + agresivní destroy (10–40)",
        "overrides": {
            "budget_phase_C_pct": 0.25,
            "budget_phase_D_pct": 0.50,
            "budget_phase_E_pct": 0.25,
            "num_clusters":       2,
            "lns_destroy_min":    10,
            "lns_destroy_max":    40,
        },
    },

    # ─── Phase 3: Fine-tuning vítěze ─────────────────────────────────────────
    # Post-diagnostika: Phase D je mrtvá → celý budget jde do C+E.
    # Otázka: vyplatí se dát E více prostoru za cenu kratšího seedování?
    # 06_2clusters má C=0.40 E=0.60 — testujeme C=0.30 E=0.70 se stejnými 2 clustery.
    {
        "name": "16_2c_e_dominant",
        "description": "2 clustery + E dominuje (C=0.30 D=0.00 E=0.70) — fine-tuning po diagnostice",
        "overrides": {
            "budget_phase_C_pct": 0.30,
            "budget_phase_D_pct": 0.00,
            "budget_phase_E_pct": 0.70,
            "num_clusters":       2,
        },
    },
]
