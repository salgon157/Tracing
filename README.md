# Tracing – VRP plánovač / benchmark

Nástroje pro plánování svozových tras (Vehicle Routing Problem) a benchmarking solveru.
Routing přes ORS/OSRM (Docker), řešení tras a export plánů.

## Struktura

- `vrp_solver_lines_v6.py` – hlavní solver (jedno depo)
- `vrp_solver_lines_all_depots_v6.py` – varianta pro více depot
- `prepare_inputs_v6.py` – příprava a validace vstupů
- `benchmark/` – běhové a benchmark skripty
- `benchmark_all_depots_solver_v6.py` – benchmark přes všechna depa
- `visualize_routes.py` – vizualizace tras (HTML mapy)
- `osrm_orchestrator.py`, `update_osrm.py` – správa routovacích kontejnerů (Docker)
- `data/`
  - `input/`, `prepared/` – **NEverzováno** (osobní údaje zákazníků – GDPR)
  - `static/` – jen config bez PII je verzován: `vehicle_types.csv`, `closures.json`.
    `locations_*.csv` a `vehicle_registry.csv` jsou **NEverzováno** (adresy, jména řidičů).
  - `results/` – výstupy běhů (NEverzováno, viz `.gitignore`)
- `tests/` – testy (pytest)

## Poznámka k verzování

Do gitu se **NEdávají osobní údaje** (GDPR): RiRo vstupy, připravené objednávky,
`locations_*.csv` (adresy zákazníků) ani `vehicle_registry.csv` (jména řidičů, SPZ).
Blokuje je `.gitignore`. Verzuje se pouze kód a config bez PII
(`vehicle_types.csv`, `closures.json`).

Výsledky běhů (`data/results/`), logy a routovací grafy (`routing/`) se do gitu
nedávají také – jsou velké a generované.
