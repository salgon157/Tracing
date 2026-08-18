# Tracing — VRP plánovač svozových tras

Plánovač rozvozových tras (Vehicle Routing Problem) pro firmu s jedním skladem
(**Štoky**) a čtyřmi objednávkovými regiony. Optimalizuje **peníze**
(Kč/km + fixní náklad za výjezd auta) při dodržení závozových oken a kapacit.

Jádro je **CLI pipeline** (Python skripty). Nad ní je tenká **webová vrstva**
(`webui/`), která spouští tytéž skripty přes subprocess — žádnou logiku
neduplikuje.

> Podrobný provozní návod: **[WORKFLOW.md](WORKFLOW.md)**

---

## Rychlý start

```powershell
# 1) závislosti (Python 3.14, vyvíjeno na Windows)
pip install -r requirements.txt

# 2) routing kontejnery (Docker) — provozní běhy jedou na 'current'
docker start osrm-current ors-current      # porty 5001 / 8081

# 3) testy (bez integračních, ty potřebují běžící routing)
python -m pytest tests webui/tests -q --ignore tests/test_ors_hgv_integration.py

# 4) denní běh jednoho depa
python prepare_inputs_v6.py CB
python vrp_solver_lines_v6.py --orders-file data/prepared/CB/orders_CB_2026-07-23.csv

# 5) webové rozhraní (volitelné) → http://127.0.0.1:8777
python -m uvicorn webui.app.main:app --host 127.0.0.1 --port 8777
```

**Všechny příkazy se spouští z kořene repa** (skripty používají relativní cesty).

---

## Jak to funguje

```
RiRo CSV (z ESO9)          prepare_inputs_v6.py        vrp_solver_lines_v6.py
data/input/{DEPO}/aktivni/  ───────────────────►  data/prepared/  ──────────►  data/results/{DEPO}/{datum}/
                            validace, GPS, SEC       OR-Tools + OSRM/ORS        CSV, XLSX, zone_summary.json
```

1. **RiRo soubor** (export z firemního ESO9) padne do `data/input/{DEPO}/aktivni/` —
   právě jeden. Nese GPS i předpočítaný čas zastávky, je to **jediný zdroj pravdy**.
2. **`prepare_inputs_v6.py`** ho zvaliduje a přeloží na solver-ready CSV.
   Přísný režim: jakýkoli vadný řádek → vypíše který a proč a **skončí chybou**.
3. **`vrp_solver_lines_v6.py`** spočítá matice vzdáleností (OSRM pro dodávky,
   ORS pro kamiony), naplánuje trasy (OR-Tools, fáze A/C/E) a zapíše výstupy
   + řádek do `data/results/run_log.jsonl`.
4. **`visualize_routes.py`** volitelně vykreslí HTML mapu.

**Depa CB/HK/MO/PR nejsou sklady** — všechna auta vyjíždějí ze Štok. Jsou to
regiony zakázek s různými časy uzávěrky (kvůli postupnému chystání ve skladu),
proto se plánují odděleně.

---

## Klíčové skripty

| skript | co dělá |
|---|---|
| **`vrp_solver_lines_v6.py`** | Jádro. VRP solver (OR-Tools), matice přes OSRM/ORS, cenový model, výstupy + run log. Ostatní ho importují. |
| **`prepare_inputs_v6.py`** | RiRo (19 sloupců od 13. 8. 2026) → solver-ready CSV. Validace GPS, oken, payloadu i příznaku rampy; adresa/PSČ/země/ID jako průchozí sloupce; bilance vyřazených do `prepare_stats_*.json`. |
| **`visualize_routes.py`** | HTML mapa tras (Leaflet) z výsledkové složky. |
| **`predict_day.py`** | Tenký wrapper: predikční běh nad `data/prediction/` (prepare+solve+mapy pro všechna depa). Odděleno od ostrého provozu. |
| **`order_history.py`** | Šance závozu z historie objednávek (`data/historie_objednavky/*.xlsx`): stejný den v týdnu, roční okno, pauzy, svátky. Predikce podle ní losuje, které dopredikované objednávky do plánu půjdou. |
| **`plan_day.py`** | Predikcí řízené plánování dne. `predict`: P1 (přání dep) → rezervace + zdražení výjezdu (#2) → P2 (sekvenční generálka) → rozhodnutí vč. výběru L3 → `decision_{DATUM}.json`. `real`: večerní sekvence dep s živým budgetem, eskalací a vyřazením L3 objednávek. `l3`: trasa kamionu po posledním depu (kontrola sjízdnosti → případně kamion navíc → solver s režimem řidiče EU: pauzy + 9 h jízdy; když nevyjde, seznam co komu vrátit). |
| **`_baseline_*/`** | ⛔ **DOČASNÝ archiv — NESAHAT.** git worktree se starým solverem (strana A regresního A/B); `overnight_regression.ps1` ho založí jen pro běh a na konci sám odstraní. Mimo projekt, gitignored, `pytest.ini` ho vynechává. Viz `_NESAHAT_ARCHIV.md` uvnitř. |
| **`l3_planner.py`** | Logika L3 pod plan_day: výběr rampových skutečných objednávek jako VRP s volitelnými zastávkami nad hgv maticí (penále kg×λ, cena zastávky, denní jízda 9 h, pauzy, okno; jen sjízdné smyčky), záložní greedy bez matice, kontrola sjízdnosti večer, sloučení l3_orders pro solver. |
| **`fleet_budget.py`** | Logika pod plan_day: malá/velká auta, rezervace žebříčkem kg, budget s ubíráním, caps (rezervace + volný pool), rozhodnutí o levelu (deficit → kg → L0/L1+L2/L3 alert). |
| **`driver_assignment.py`** | Přiřazení konkrétních řidičů k naplánovaným linkám — celodenní optimum (maďarský alg.) nad registrem auto+řidič z ESO (`data/ridici/aktivni/vehicles-active-*.csv`, TYPE podle `vehicle_types` dne) a historií řidič×adresa (`data/historie_ridici/`). Hard: dny, dostupnost od/do, typ auta; tier: naše auta (plán 0/0) až po smluvních; soft s váhami: plnění plánu km (rok > měsíc), dojezd, kvalita×tightness, familiarity (pořadí podle počtu závozů). Kontrola počtů aut vs vozový park dne. Samostatný krok po naplánování všech dep. |
| **`compare_prediction.py`** | Porovná predikci s realitou (Δ = predikce − realita), zapíše `comparison.jsonl`. Jediný vlastník porovnávacích vzorců. `--pred-phase P1\|P2` vybere fázi `plan_day predict`. |
| **`osm_routing.py`** | Definice routing instancí (`current` / `stable`) a jejich URL. Jediné místo, kde jsou porty. |
| **`refresh_osm.py`** | **Týdenní** přestavba čerstvé mapy (stáhne OSM data, přestaví graf, restartuje kontejnery). Běhy ji nikdy nespouštějí samy. |
| **`osrm_orchestrator.py`, `update_osrm.py`** | Vnitřek té přestavby (Docker, Geofabrik download, osrm-extract/partition/customize). |
| **`closures_utils.py`, `manage_closures.py`, `closure_map_editor.py`** | Uzavírky/objízdky: aplikace na matici, CLI správa, klikací mapový editor (port 8765). |
| **`vrp_solver_lines_all_depots_v6.py`** | Varianta plánující všechna depa jako jeden velký problém (sdílený pool aut). |
| **`benchmark_all_depots_solver_v6.py`, `benchmark/runner.py`** | Benchmark výkonnosti solveru. Jedou na **`stable`** mapě, aby byla měření porovnatelná v čase. |
| **`webui/`** | FastAPI + vanilla JS. 8 tabů (denní běh, predikce, benchmarky, výsledky, uzavírky, flotila, prostředí, úlohy). Spouští CLI skripty jako joby. |

### Legacy / nepoužívané

Zůstávají v repu kvůli historii, **v pipeline se nepoužívají**:

| skript | proč legacy |
|---|---|
| `vrp_solver_lines_invalid(DoNotUse).py` | Stará verze solveru, nespouštět. |
| `build_static_data.py` | Stavěl `locations_*.csv` z Excelů. GPS dnes chodí přímo v RiRo, soubor už pipeline nepotřebuje. |
| `convert_to_riro.py` | Jednorázová konverze výsledků do formátu konkurenčního nástroje (RiNkai) kvůli porovnání. |
| `export_prepare.py` | Jednorázový generátor offline ZIP balíčku s mapou. |
| `benchmark_configs.py` | Jednorázový experiment (hledání optimálního rozdělení solver budgetu). |
| `experiments/` | Uzavřený experiment „nejkratší vs. nejrychlejší trasy" + jeho report. Mimo hlavní flow. |

---

## Co musí běžet předem

**Docker s routing kontejnery.** Bez nich solver skončí chybou (řekne, co spustit).

| instance | porty | složka | kdo ji používá |
|---|---|---|---|
| **current** (default) | 5001 / 8081 | `C:\osrm_current` | denní běh, predikce, vizualizace |
| **stable** | 5000 / 8080 | `C:\osrm` | benchmarky (zamrzlá mapa = porovnatelná měření) |

```powershell
docker start osrm-current ors-current     # provozní
docker start osrm-server ors-hgv          # jen pro benchmarky
```

⚠️ **Běh routing data nikdy nestahuje ani nepřestavuje** — jinak by se
30–60minutový rebuild spustil uprostřed plánování před uzávěrkou. Přestavba je
samostatný krok (`python refresh_osm.py`), typicky v neděli večer.

Data samotná (`C:\osrm*`) nejsou v gitu — jsou to ~2 GB grafy, staví se skriptem.

---

## Struktura dat

```
data/
├── input/{DEPO}/aktivni/     RiRo soubory (právě jeden per depo)   [NEverzováno]
├── prepared/{DEPO}/          solver-ready CSV + prepare_stats      [NEverzováno]
├── results/{DEPO}/{datum}/   plány, mapy, run_log.jsonl            [NEverzováno]
├── prediction/               tentýž strom pro predikce             [NEverzováno]
└── static/                   vehicle_types-YYYYMMDD.csv, closures.json  [verzováno]
```

`data/prediction/` je **paralelní vesmír** k ostrému provozu — predikce nikdy
nezapíše do produkčních výsledků ani do ostré historie.

---

## Osobní údaje (GDPR) — DŮLEŽITÉ

**Do gitu nesmí osobní údaje.** Blokuje je `.gitignore`:

- `data/input/`, `data/prepared/`, `data/prediction/`, `data/results/` —
  jména, adresy, GPS a váhy zákazníků
- `data/static/locations_*.csv` — adresy zákazníků (pipeline je už nepoužívá,
  soubory na disku zůstávají)
- `data/static/vehicle_registry.csv` — jména řidičů a SPZ
- `data/ridici/` — registr aut+řidičů z ESO (jména, telefony, SPZ)
- `webui/jobs/`, `experiments/*/results/` — runtime logy a výstupy

Verzuje se **jen kód a config bez PII** (`vehicle_types-YYYYMMDD.csv`, `closures.json`).
Repo je **Private**.

---

## Testy

```powershell
python -m pytest tests webui/tests -q --ignore tests/test_ors_hgv_integration.py
```

Aktuálně **663 testů**. `prepare_inputs` i solver pouští unit testy automaticky
před během (přeskočení: `SKIP_STARTUP_TESTS=1`). Integrační routing testy
(`test_ors_hgv_integration.py`) potřebují běžící OSRM/ORS.
