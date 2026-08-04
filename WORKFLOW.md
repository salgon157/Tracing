# Workflow — VRP plánovač tras

Praktický návod na denní běh. Depot kódy: **CB** (České Budějovice),
**HK** (Hradec Králové), **MO** (Morava), **PR** (Praha).

> Všechny příkazy se spouští z kořene projektu:
> `C:\VSCode_MyCode\Tracing_ALL\Tracing_MAIN\vrp_benchmark`

---

## 1. Denní workflow (jedno depo)

```powershell
# 1) Vlož PRÁVĚ JEDEN RiRo soubor do aktivni/ složky depa:
#    data/input/CB/aktivni/riro-YYYYMMDD-CB.csv

# 2) Priprav objednavky (RiRo -> orders CSV + validace)
python prepare_inputs_v6.py CB
#    -> data/prepared/CB/orders_CB_YYYY-MM-DD.csv
#    -> data/prepared/CB/prepare_stats_CB_YYYY-MM-DD.json  (bilance zpracování)

# 3) Spust solver
python vrp_solver_lines_v6.py --orders-file data/prepared/CB/orders_CB_YYYY-MM-DD.csv
#    -> data/results/CB/YYYY-MM-DD/  (lines_summary.csv, lines_stops.csv,
#                                     lines_plan.xlsx, zone_summary.json,
#                                     eso_export_CB_YYYY-MM-DD.csv)

# 4) Vizualizace (HTML mapa)
python visualize_routes.py data/results/CB/YYYY-MM-DD/ --open
```

Pro ostatní depa vyměň `CB` → `HK` / `MO` / `PR`.


**Run pro porovnání s predict**
python -m pytest tests -q --ignore tests/test_ors_hgv_integration.py
if ($?) {
  $env:SKIP_STARTUP_TESTS = "1"
  foreach ($d in "CB","HK","MO","PR") {
    Write-Host "`n=== $d ===" -ForegroundColor Cyan
    python prepare_inputs_v6.py $d
    if (-not $?) { Write-Host "prepare $d selhalo - preskakuji"; continue }
    python vrp_solver_lines_v6.py --orders-file "data/prepared/$d/orders_${d}_2026-07-31.csv" --budget-min 5
  }
  Remove-Item Env:\SKIP_STARTUP_TESTS
}

**Pravidla vstupu:**
- V `data/input/{DEPOT}/aktivni/` musí být **právě jeden** CSV. Víc/míň → chyba.
- Datum se bere z názvu souboru (`riro-YYYYMMDD-...`), depo z CLI argumentu.
- Výstupní složka se **auto-detekuje** z názvu orders souboru
  (`orders_CB_2026-04-29.csv` → `data/results/CB/2026-04-29/`).

### Formát RiRo (od 28. 7. 2026)

RiRo z ESO9 je **jediný zdroj pravdy** — 31 sloupců, středníkem, bez hlavičky:

| sloupec | obsah |
|---|---|
| **L / M** (11/12) | závozové okno od–do (sekundy od půlnoci) |
| **R / S** (17/18) | **lon / lat** — GPS (dřív rezerva s `-1000`) |
| **Y** (24) | **datum ROZVOZU** (YYYYMMDD) — musí sedět na datum závozu |
| **AA** (26) | `KG:51.475#SEC:261` — váha + **kompletní čas zastávky v sekundách** |
| **AE** (30) | kg z minulého závozu; `-1000` = minule bez závozu |

- **`SEC` je celý čas zastávky** — solver ho použije tak, jak je (`ceil` na minuty).
  Žádný vzorec za váhu se nepřipočítává.
- `data/static/locations_*.csv` už **NEJSOU potřeba** — GPS chodí v riro.
  (`build_static_data.py` a `convert_to_riro.py` jsou legacy, jen se nemažou.)
- **Starší formáty** jsou odmítnuty jasnou chybou: 30 sloupců bez SEC (do 16. 7.),
  32 sloupců s GPS na konci (16. 7.), 30 sloupců bez AE (17.–23. 7.). Archiv: `data/input/{DEPOT}/archiv_stary_format/`.
- Historické `orders_*.csv` z dubna/července **nejde spustit** — nemají `service_sec`.
  Výsledky benchmarků z nich už máme; nová data jedou jen na předpočítaném čase.

### Přísný režim prepare

Když **jakýkoliv** řádek neprojde validací (vadná GPS / chybějící SEC / vadné okno),
prepare vypíše konkrétní řádky s důvodem a **skončí chybou — nic neuloží**.
Správně je jen když projdou všechny řádky z ESO9.

```powershell
python prepare_inputs_v6.py CB --allow-drops   # vědomě pokračovat i s vadnými řádky
```

Navíc: **objednávka s jiným datem rozvozu (sloupec Y) než datum závozu je vada
exportu** → fatální chyba, nejde obejít `--allow-drops` (ten by objednávku
zahodil a ona by se nerozvezla). Správná reakce je opravit export z ESO9.

### Pojistky proti tiché ztrátě objednávek

31\. 7. 2026 poslalo ESO9 vadné SEC (až 96 742 s = **26,9 h** vykládky).
Objednávka se servisem nad strop trasy (23,5 h) je neobsloužitelná, OR-Tools
prohlásil celý cluster za neřešitelný a jeho objednávky — **49 z 91 v ostrém
plánu PR** — tiše zmizely z výstupu. Od té doby jsou v pipeline čtyři závory;
**poloviční plán se už nikdy neuloží**:

1. **prepare: `SERVICE_SEC_MAX` (2 h)** — legitimní SEC nikdy nepřekročil
   ~1,5 h; řádek nad limit = vadný payload → přísný režim odmítne celý soubor.
2. **solver: `validate_orders_servable`** — před solvem: servis < strop trasy
   (chytí i staré prepared soubory) a objednávka dosažitelná ze skladu tam
   i zpět alespoň v jednom profilu.
3. **phase C: záchranný re-solve** — nevyřešený cluster vítězného seedu dostane
   druhý pokus s 3× časem a náhradní strategií; když neuspěje, běh **spadne
   s diagnostikou** (dřív objednávky clusteru tiše zmizely).
4. **finální invariant `verify_plan_complete`** — před uložením: každá vstupní
   objednávka je v plánu právě jednou, jinak se neuloží nic a vypíše se seznam.

### Export plánu do ESO (`eso_export_{DEPO}_{DATUM}.csv`)

Vzniká automaticky při každém uložení výsledků. Formát podle vzoru z ESO
(srpen 2026): středníky, **cp1250**, hlavička doslova jako vzor (včetně jejich
překlepů — parsují podle názvů sloupců). Jeden řádek na zastávku, sklad se
nevypisuje. Časy v **sekundách od půlnoci**:

- `plán příjezd/odjezd lokace` — příjezd a odjezd na zastávce
- `plán odjedz depo` — výjezd na trasu; `plán příjezd Depo` — výjezd −
  **nakládka** (`CONFIG depot_loading_min`, teď 40 min; jen v exportu,
  plánování tras neovlivňuje); `čas konec linky` — návrat do skladu
- `typ vozidla` — číslo z type_code (TYPE_02 → 2); `nosnost vozu` — max_kg
  z `vehicle_types.csv` (papírová nosnost, bez plánovací rezervy)

### Predikční režim (`--prediction`)

Přidává ho `predict_day.py`, ručně ho nepotřebuješ. Mění dvě věci:

1. **dřívější datum rozvozu = dopredikovaná objednávka** (v ostrém běhu chyba)
2. **los podle šance z historie** — každá dopredikovaná objednávka se do plánu
   dostane **celá, nebo vůbec**. Kilogramy se neškálují.

**Šance = zavezené dny / způsobilé dny stejného dne v týdnu** (počítá
`order_history.py` z `data/historie_objednavky/*.xlsx`, sloupce `Datum`
a `Zkratka` = location_code):

| pravidlo | jak to funguje |
|---|---|
| **okno** | od prvního závozu, nejvýš **rok** zpět; končí posledním dnem, který historie pokrývá — dny za koncem dat nejsou „nezavezeno", ale „nevíme" |
| **pauza** | mezera mezi závozy **delší než 2 měsíce** (61 dní) zahodí historii před ní — bereme to, jako bychom zákazníkovi začali vozit až po ní |
| **svátky** | státní svátky se vynechávají z čitatele **i** jmenovatele; závoz uskutečněný ve svátek se nepočítá (10 pondělí, 2 svátky, jel oba + 7 z 8 zbylých → **7/8**) |
| **bez historie** | nová adresa nebo žádný způsobilý den v okně → **100 %** (radši auto navíc než nerozvezený zákazník) |
| **los** | deterministický ze `(datum závozu, adresa)` — stejný běh dá stejný plán; všechny objednávky jedné adresy sdílejí jeden los |

Čas zastávky (SEC) ani váhy se **nemění** — objednávka jde do plánu tak, jak
přišla z ESO9. Tabulka „která objednávka prošla a proč" je v konzoli, strojově
pak v `prepare_stats_*.json` (blok `prediction`, včetně čitatele, jmenovatele,
hozeného čísla a okna u každé objednávky).

> Koeficient kg (dřívější metoda, sloupec AE) je **vypnutý** — `compute_kg_coefficient`
> v kódu zůstává, ale `main()` ho už nevolá.

Roční exporty do `data/historie_objednavky/` dodáváš ručně; složka je
v `.gitignore` (GDPR). Načtení obou souborů trvá ~17 s na depo.

---

## 2. Routing instance (Docker) — current vs stable

Existují DVĚ mapy. Provozní běhy jedou na čerstvé, benchmarky na zamrzlé.

| preset | složka | porty | kontejnery | kdo ho používá |
|---|---|---|---|---|
| **current** (default) | `C:\osrm_current` | 5001 / 8081 | `osrm-current`, `ors-current` | denní běh, predikce, vizualizace |
| **stable** | `C:\osrm` | 5000 / 8080 | `osrm-server`, `ors-hgv` | benchmarky (měření výkonnosti algoritmu) |

**Běh routing data NIKDY nestahuje ani nepřestavuje.** Jen ověří, že instance
odpovídá — jinak by se 30–60minutový rebuild spustil uprostřed plánování před
uzávěrkou. Přestavba je samostatný krok (viz níže).

```powershell
python vrp_solver_lines_v6.py --orders-file ...                      # current (default)
python vrp_solver_lines_v6.py --orders-file ... --osm-source stable  # zamrzlá mapa
```

`--fresh-osm` zůstává jako zastaralý alias pro `--osm-source current`.

### Přestavba čerstvé mapy — `refresh_osm.py` (typicky 1× týdně)

```powershell
python refresh_osm.py                # stáhne nová data (jsou-li >7 dní) + přestaví + restartuje kontejnery
python refresh_osm.py --check        # jen zjistí stáří dat
python refresh_osm.py --skip-update  # jen nastartuje/opraví kontejnery
python refresh_osm.py --force        # přestaví i čerstvá data
```

Pusť v klidném okně (neděle večer). Na serveru naplánovaná úloha.
Stabilní instance (`C:\osrm`) se **nikdy nedotkne** — je to zamrzlá mapa,
díky které jsou benchmarky porovnatelné napříč časem.

### Proč zrovna takhle
- **denní plán i predikce = current** → počítají podle aktuální mapy a jsou
  navzájem porovnatelné (predikce vs. realita v rámci téhož dne)
- **benchmark = stable** → měříš algoritmus, ne změny v mapě

---

## 3. Užitečné přepínače solveru

| Přepínač | Význam |
|---|---|
| `--budget-min 5` | Časový budget solveru v minutách (default 30). Rychlé porovnávací běhy. |
| `--output-dir CESTA` | Ruční výstupní složka (jinak auto-detekce). Nutné pro porovnávací běhy, ať se nepřepíšou. |
| `--force-matrix` | **Nouzový** přepínač — vypne limit nedosažitelných párů pro všechny profily. Běžně NENÍ potřeba: limity jsou per profil (`driving` 0,1 %, `driving-hgv` 5 %) a pokrývají i Prahu. |
| `--allow-profile-fallback` | Dovol tichý fallback kamionů na osobní profil když ORS selže. **DEFAULT je hard-fail** (jinak by kamiony jely po špatných trasách). Používej jen vědomě. |
| `--zone-label CB` | Popisek zóny do výstupů (jinak z dat). |
| `--no-buffers` | **Tvrdý režim bez rezerv**: nosnost 100 % a závozová okna přesně jak je poslalo ESO9 (bez posunu −5/+25 min). |
| `--capacity-multiplier 1.0` | Jen nosnost (default viz CONFIG). |
| `--tw-expand-before 0` / `--tw-expand-after 0` | Jen okna (default 5 / 25 min). |

### Plánovací buffery — co znamenají

Solver **nemění data**, jen si při plánování nechává rezervu:

| buffer | default | co dělá |
|---|---|---|
| `vehicle_capacity_multiplier` | **1.03** = 103 % | plánuje na vyšší nosnost, než je papírová (slack při balení, vzdušné mezery) |
| `tw_expand_before_min` | **5 min** | řidič smí přijet 5 min před otevřením okna |
| `tw_expand_after_min` | **25 min** | řidič smí přijet 25 min po zavření okna |

Přepínače výše je přepíšou jen pro jeden běh, config zůstává. Granulární
přepínač přebije `--no-buffers`, takže jde i „tvrdý režim, ale nech +10 min":

```bash
python vrp_solver_lines_v6.py --orders-file data/prepared/CB/orders_CB_2026-07-28.csv --no-buffers --tw-expand-after 10
```

Kolik to stojí, se liší den od dne — na CB 28. 7. (oba běhy 3 min budget)
vyšel tvrdý režim na stejný počet linek a +679 Kč. Chová se to tak, jak má:
přísnější podmínky nikdy nevyjdou levněji. Stejné přepínače má
i `vrp_solver_lines_all_depots_v6.py`.

**Příklad — reprodukce staršího výsledku na zamrzlé mapě:**
```powershell
python vrp_solver_lines_v6.py --osm-source stable --orders-file data/prepared/PR/orders_PR_2026-04-29.csv
```

**Příklad — porovnání 5 vs 30 min (bez přepsání):**
```powershell
python vrp_solver_lines_v6.py --budget-min 5  --output-dir data/results/CB/2026-04-29_b5  --orders-file data/prepared/CB/orders_CB_2026-04-29.csv
python vrp_solver_lines_v6.py --budget-min 30 --output-dir data/results/CB/2026-04-29_b30 --orders-file data/prepared/CB/orders_CB_2026-04-29.csv
```
Solver na konci **automaticky porovná** s předchozím během stejné zóny+data
(z `data/results/run_log.jsonl`) — vypíše rozdíl ceny, linek, km, hodin.

---

## 4. Všechna depa najednou (sdílený sklad)

```powershell
python vrp_solver_lines_all_depots_v6.py --date 2026-04-29 --budget-min 5
python vrp_solver_lines_all_depots_v6.py --dry-run          # jen ověří vstupy
```
Přepínače: `--depots CB,MO,HK,PR`, `--budget-ratios 0.35,0.25,0.40`,
`--force-matrix`, `--osm-source current|stable`, `--clusters auto`, `--workers N`.

---

## 5. Uzavírky (objízdky)

```powershell
python closure_map_editor.py     # klikací mapa v prohlížeci -> zapisuje closures.json
python manage_closures.py        # CLI sprava
```
Aktivní uzavírky (`data/static/closures.json`) solver i vizualizér berou
automaticky. Config bez PII → **je verzován**.

---

## 6. Náklady vozidel

`data/static/vehicle_types.csv`:
- `cost_per_km` — sazba za km.
- `start_cost_kc` — **fixní náklad za výjezd vozidla** (Kč, absolutně; modeluje
  mzdu řidiče / amortizaci). Per-type, takže dražší řidiče kamionů lze nastavit
  zvlášť. `0` = žádný fixní náklad.
- `available_count` — počet aut daného typu (celofiremní sdílený pool).
  Starý `count_block_{DEPOT}` byl fikce a je odstraněn.
- Předchozí verze souboru: `data/static/vehicle_types_archiv/`.

---

## 7. Git a osobní údaje (GDPR) — DŮLEŽITÉ

**Do gitu NIKDY nejdou osobní údaje.** Blokuje je `.gitignore`:
- `data/input/`, `data/prepared/`, `data/prediction/` (jména, adresy, GPS, váhy zákazníků)
- `data/static/locations_*.csv` (adresy zákazníků — pipeline je už nepoužívá, ale
  soubory na disku zůstávají a do gitu nesmí)
- `data/static/vehicle_registry.csv` (jména řidičů, SPZ)

Verzuje se pouze **kód + config bez PII** (`vehicle_types.csv`, `closures.json`).
Data existují jen lokálně na disku. Repo je **Private**.

Commitujeme **při milnících** (dokončená feature / opravený bug / funkční stav),
ne po každé drobnosti.

---

## 8. Testy

Solver i `prepare_inputs` spouští **startup unit testy** automaticky před během
(180 testů). Přeskočit: `SKIP_STARTUP_TESTS=1`. Ručně:
```powershell
python -m pytest tests/ -q
```
Integrační routing testy (`test_ors_hgv_integration.py`) běží automaticky po
nastartování routing instance (ověří ORS vs OSRM).
