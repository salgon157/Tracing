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

### Formát RiRo (od 13. 8. 2026)

RiRo z ESO9 je **jediný zdroj pravdy** — **19 sloupců**, středníkem, bez hlavičky
(`record_type` string se při změně layoutu nezměnil, formát se pozná podle
počtu sloupců):

| sloupec | obsah |
|---|---|
| **A** (0) | record_type `RIRO_INPUT_LOCATIONSANDORDERS_V3.00` |
| **B** (1) | kód lokace (klíč pro los z historie) |
| **C** (2) | název zákazníka |
| **D / E / F / G** (3–6) | ulice, PSČ, město, země — **průchozí do prepared** (plánovač je nečte) |
| **H** (7) | interní ID z ESO, stálé per lokace (`eso_col7`; význam nepotvrzen) |
| **I / J** (8/9) | závozové okno od–do (sekundy od půlnoci) |
| **K / L** (10/11) | **lon / lat** — GPS |
| **M** (12) | číslo objednávky `O126…` |
| **N** (13) | interní ID z ESO, unikátní per řádek (`eso_col13`; význam nepotvrzen) |
| **O** (14) | **datum ROZVOZU** (YYYYMMDD) — musí sedět na datum závozu |
| **P** (15) | poznámka |
| **Q** (16) | `KG:51.475#SEC:261` — váha + **kompletní čas zastávky v sekundách** |
| **R** (17) | kg z minulého závozu; `-1000` = minule bez závozu |
| **S** (18) | **rampa**: `1` má, `0` nemá — přísně validováno; podle ní vybírá L3 (kamion předem) |

- **`SEC` je celý čas zastávky** — solver ho použije tak, jak je (`ceil` na minuty).
  Žádný vzorec za váhu se nepřipočítává.
- **Prepared CSV** nese navíc průchozí sloupce `street, zip, country, eso_col7,
  eso_col13, ramp` (na konci hlavičky; prvních 13 sloupců drží staré pořadí).
  Mrtvé sloupce `code_a` a `riro_vehicle_type_code` zanikly. `ramp` teče
  i do výstupů solveru (`lines_stops.csv`, sloupec `Rampa` v XLSX);
  `prepare_stats` hlásí `ramp_orders`.
- Proti starému formátu **zanikly**: telefon, e-mail a textová poznámka o rampě.
- `data/static/locations_*.csv` už **NEJSOU potřeba** — GPS chodí v riro.
  (`build_static_data.py` a `convert_to_riro.py` jsou legacy, jen se nemažou.)
- **Starší formáty** (30/31/32 sloupců, do 12. 8. 2026) jsou odmítnuty jasnou
  chybou. Archiv: `data/input/{DEPOT}/archiv_stary_format/`.
- Historické `orders_*.csv` z dubna/července **nejde spustit** — nemají `service_sec`.
  Výsledky benchmarků z nich už máme; nová data jedou jen na předpočítaném čase.

### Přísný režim prepare

Když **jakýkoliv** řádek neprojde validací (vadná GPS / chybějící SEC / vadné okno),
prepare vypíše konkrétní řádky s důvodem a **skončí chybou — nic neuloží**.
Správně je jen když projdou všechny řádky z ESO9.

```powershell
python prepare_inputs_v6.py CB --allow-drops   # vědomě pokračovat i s vadnými řádky
```

Navíc: **objednávka s jiným datem rozvozu (sloupec O) než datum závozu je vada
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
2. **solver: `validate_orders_servable`** — před solvem: servis < nejzazší
   návrat (chytí i staré prepared soubory), objednávka dosažitelná ze
   skladu tam i zpět alespoň v jednom profilu, **vejde se do největšího
   auta** a **okno je stihnutelné** (od 17. 8.); s `--driver-breaks` navíc
   tam a zpět ≤ denní limit jízdy. Vše exit 2 se jménem objednávky.
   Zároveň `load_orders_day` už **nikdy tiše nepřeskočí vadný řádek**
   prepared souboru — exit 2 se soupisem.
3. **phase C: záchranný re-solve** — nevyřešený cluster vítězného seedu dostane
   druhý pokus (3× čas, ale nejvýš zbytek budgetu; obě strategie paralelně,
   všechny nevyřešené clustery najednou); když neuspěje, běh **spadne
   s diagnostikou a exit 3** (dřív objednávky clusteru tiše zmizely).
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

Přidává ho `predict_day.py`, ručně ho nepotřebuješ. Mění tři věci:

1. **dřívější datum rozvozu = dopredikovaná objednávka** (v ostrém běhu chyba)
2. **los podle šance z historie** — každá dopredikovaná objednávka se do plánu
   dostane **celá, nebo vůbec**
3. **koeficient kg** — vahám těch, které losem prošly, se přenásobí poměrem
   `suma(dnes) / suma(minule)` (viz níže)

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

Tabulka „která objednávka prošla a proč" je v konzoli, strojově pak
v `prepare_stats_*.json` (blok `prediction`, včetně čitatele, jmenovatele,
hozeného čísla a okna u každé objednávky).

**Koeficient kg — druhý krok po losu.** Váha dopredikované objednávky je
z minulého týdne; koeficient ji převede na dnešek:

- počítá se jako `suma(kg dnes) / suma(kg minule)` ze **spárovaných**
  objednávek — těch, které mají ve sloupci **AE** kg z minulého závozu
  (v praxi reálné objednávky na dnešek; dopredikované řádky v AE nic nemají)
- ořez **0,5–2,0**, minimálně **10 párů** (jinak se nepoužije, tedy ×1,0)
- aplikuje se **jen na dopredikované, které prošly losem**; reálné objednávky
  mají skutečnou váhu a nikdo se jich nedotkne
- **čas zastávky (SEC) se nemění nikdy** — neznáme vzorec, kterým ho ESO9 počítá

Souhrn („z čeho spočítán, kolik kg to přidalo") je v konzole pod tabulkou losu
a v `prepare_stats_*.json` pod `prediction.kg_coefficient`.

> **Varování na vadné AE.** Legitimní hodnoty jsou kladné číslo, `-1000`
> (minule bez závozu) a prázdno. Cokoli jiného — typicky `XII.00`, což je
> číslo, které Excel přeformátoval na datum — prepare vypíše jako varování
> se seznamem řádků. Plán to nezastaví (řádek se jen nezapočítá), ale
> koeficient pak stojí na menším vzorku. Seznam je i ve stats
> (`prediction.kg_coefficient.suspect_rows`).

**Porovnávací predikční běhy:**

```powershell
python predict_day.py --label s-koeficientem          # odliší výstupní složku
python predict_day.py --input-date 20260803           # přepočítá starší den
```

`--label` přilepí příponu k názvu výstupní složky
(`results/CB/2026-08-05_1430_s-koeficientem/`), takže jde pustit dvě verze
predikce vedle sebe a porovnat je. `--input-date` vezme
`riro-YYYYMMDD-*.csv` ze složky depa místo jediného souboru z `aktivni/` —
ta zůstane netknutá pro běžný běh.

Roční exporty do `data/historie_objednavky/` dodáváš ručně; složka je
v `.gitignore` (GDPR). Načtení obou souborů trvá ~17 s na depo.

### Predikcí řízené plánování dne (`plan_day.py`) — příprava na server

```powershell
python plan_day.py predict               # celá predikční fáze (~8 solver běhů)
python plan_day.py predict --budget 5    # budget na jeden běh (default 5 min)
```

Odpovídá na otázky „kam dát velká auta" a „jaká porušení večer povolit".
Vše běží na **L0** (100 % nosnosti, okna −5/+25); solver se nemění — flotila
se omezuje generovanými `vehicle_types` soubory:

1. **P1** — každé depo zvlášť s **celým skladem** → „přání" (přetečení
   se pozná samo: jeden kamion a tři zájemci = tři přání na jeden kus)
2. **Rezervace** — velké typy (nosnost > 1350 kg) podle přání; přetečené
   typy ořezané žebříčkem podle naloženosti linek (kg); nerezervované
   kusy = volný pool
3. **P2** — depa sekvenčně **CB → MO → HK → PR** s budgetem: depo smí použít
   vlastní rezervaci + zbytek po odečtení rezervací dep, která ještě nebyla
   na řadě (nevyužité kusy tečou dál samy). Malá auta neomezená — jejich
   deficit se MĚŘÍ, ne maskuje.
4. **Rozhodnutí** — deficit malých proti `available − 1` (rezerva) se přes
   X_NEED nejméně naložených linek přepočte na kg:
   deficit 0 → **L0** · chybí ≤ 3 % denních kg → **L1+L2** (103 % + dvojlinky)
   · víc → navíc **L3: kamion předem** (viz níže; bez zbylého kamionu
   jen alert)

Mezi P1 a P2 navíc **zdražení výjezdu (#2)**: když z P1 chybí VÍC než 3
malá auta A střední (nosnost 1351–3999) jedou pod 50 % dostupných,
zdraží se výjezd VŠEM typům — chybí 4 → +200 Kč, každé další +100,
strop +500 (`fleet_budget.start_cost_escalation`). Solver pak
konsoliduje do větších aut místo porušování. Delta platí pro P2 i
večerní real (nese ji decision); pod každým během se vypisuje
**nenavýšená cena** (`cena − delta × počet linek`) — ceny v souborech
jsou navýšené, skutečné jsou v tomhle výpisu.

Výstup: `data/prediction/results/decision_{DATUM}.json` (level, rezervace,
start_cost blok, l3 blok, čísla deficitu — večerní běh z něj čte), plné
solver výstupy v `results/{DEPO}/{DATUM}_{HHMM}_P1|_P2/`, generované
flotily v `results/plan_day/{DATUM}_{HHMM}/`. Parametry (rezerva, práh
3 %, trigger zdražení) jsou konstanty v `fleet_budget.py`.

### Dvojlinky (`--double-runs`, porušení L2)

Malé auto (nosnost ≤ 1350) smí naložit ve skladu **2× za den**. Zapíná se
přepínačem solveru — v běžný den je vypnuto, večer ho zapne `plan_day`
podle decision:

```powershell
python vrp_solver_lines_v6.py --orders-file ... --double-runs
```

- **cena**: druhá jízda platí **plný druhý výjezd** (`start_cost_kc` typu,
  dnes 1000 Kč) + 1 Kč navíc — solver tak vždy preferuje fyzická auta
  a dvojlinku použije, až když se vyplatí (např. místo poloprázdného
  kamionu). Reálné km a časy platí normálně.
- **čas**: druhá jízda smí vyjet od `CONFIG double_run_earliest` (10:00)
  a po solve se **páruje na fyzické auto téhož typu**, které se vrátilo
  aspoň `depot_loading_min` (40 min) před jejím výjezdem. Jedno auto
  = max jedna dvojlinka. Když se žádné vrátivší se auto nehodí, vezme se
  **nečinné fyzické auto téhož typu z celé flotily** (jede jako svou první
  jízdu, ne dvojlinka; od 17. 8. — dřív párování vidělo jen auta, která
  jela, a spadlo, i když jinde stála). Teprve když ani to → **exit 3**
  s výpisem návratů — žádné tiché překrytí směn.
- **výstupy**: druhá jízda nese vehicle_id fyzického auta; v lines_summary
  má sloupec `double_run` = „2. jízda". Max virtuálních jízd:
  `CONFIG double_runs_max` (10).
- **rozdělení mezi clustery** (od 16. 8. 2026): fyzická auta se dělí
  podle demand score jako dřív; virtuální jízdy se pak **rozprostřou
  poměrně** podle toho, kolik objednávek clusteru jde obsloužit po
  jejich nejdřívějším výjezdu — nikdy jako souvislý blok do jednoho
  clusteru. Cluster bez fyzického auta žádnou nedostane a počet
  clusterů se stropuje počtem **fyzických** aut. Důvod: PR 17. 8.
  (poslední depo, 19 fyzických + 10 virtuálních) — všech 10 dvojlinek
  skončilo v jednom clusteru, ten měl 4 fyzická auta na 41 ranních
  objednávek → neřešitelné, depo bez plánu. Zároveň se hlídá, že
  nejtěžší objednávka každého clusteru se vejde do některého jeho
  auta (jinak výměna aut mezi clustery); report neřešitelného clusteru
  obě věci vypisuje.

### Večerní ostrý běh (`plan_day.py real`)

```powershell
python plan_day.py real                  # všechna depa podle uzávěrek
python plan_day.py real PR               # jen jedno depo (po částech)
```

Sekvence dep **CB → MO → HK → PR** nad ostrými daty, řízená decision:

- **vyžaduje `decision_{DATUM}.json`** ze stejného dne (`plan_day predict`)
- flotila = **živý budget**: po každém depu se odečtou spotřebovaná auta
  (počítáno per fyzické vozidlo — dvojlinka auto nepočítá dvakrát);
  velké typy navíc chrání rezervace dep, která ještě nebyla na řadě
- solver jede s flagy z decision (L0, nebo L1+L2 = 103 % + `--double-runs`);
  případné zdražení výjezdu (#2) a vyřazení L3 objednávek se aplikují samy
- **eskalace**: když depo nevyjde na denním levelu, zvedne se na L1+L2
  (platí od tohoto depa dál); když nevyjde ani tak → **ALERT a konec**,
  člověk rozhodne — hotová depa jsou definitivní
- **stav** (`data/results/plan_day/{DATUM}/state.json`): zbytek flotily,
  hotová depa, aktuální level — druhé spuštění naváže a hotová depa
  přeskočí; běh po částech (každé depo po své uzávěrce) je tedy přirozený
- výstupy do standardních složek `data/results/{DEPO}/{DATUM}/` (ESO
  export, mapy, run log) — `--label` přesměruje pro testovací běhy

**Default solveru je od vlny 3 L0** (100 % nosnosti; okna −5/+25 zůstávají).
„Přesně jako dřív" = `--capacity-multiplier 1.03`.

### L3 — kamion předem (`plan_day.py l3`, od 14. 8. 2026; výběr VRP od 16. 8. 2026)

Když deficit malých přeteče 3 % denních kg, jede ráno **kamion 18t**
a sebere velké **rampové** objednávky napříč depy, aby se zbytek dne
vešel do malých aut:

1. **Výběr v predikci** (`l3_planner.select_locations_vrp`, automaticky
   v `predict`): kandidáti = rampové **skutečné** objednávky (sloupec
   `predicted == 0` — dopredikované z losu NIKDY, jejich čísla večer
   neexistují), agregované per lokace. Výběr je **VRP s volitelnými
   zastávkami** v OR-Tools nad **reálnou hgv maticí** (ORS): každá lokace
   je volitelná s penále `kg × λ` (`kg_value_kc`, 6 Kč/kg), zastávka
   stojí `stop_cost_kc` (150 Kč), tvrdé podmínky jsou nosnost, **denní
   limit čisté jízdy** (`driver_max_drive_h`, 9 h — EU), pauzy a okno
   04:00–20:00, a Σ kg ≤ cíl = `missing_kg + max(10 %, 500 kg)`. Solver
   tak sám dělá obchod „100 km zajížďky = 2 800 Kč = stojí za to jen pro
   ≥ 470 kg" a vybírá **jen sjízdné smyčky** — 3 × 1 000 kg v rozích
   100km čtverce vezme radši než 20 × 70 kg v okruhu 40 km, pokud se ta
   smyčka vejde do dne. Když sjízdné lokace nedají `missing_kg`, zkusí se
   λ × 3 a × 10 (`kg_value_escalation`) a vezme se nejlepší; pod
   `missing_kg` je výběr označený `exhausted`. Kamiony = ty zbylé po P2;
   bez kamionu se L3 nekoná (jen alert). Do `decision.l3` jde výběr
   i **odhad per kamion** (km, jízda, span, pauzy) — vidět už odpoledne.
   Bez ORS padá výběr na záložní greedy (`select_locations`, kg × blízkost,
   bez času — 16. 8. 2026 tak vybral 611 km / 13 h na jeden kamion).
2. **Večer** (`real`): objednávky z `decision.l3` se při prepare vyřadí
   (`--exclude-orders-file` — podle ČÍSEL, storno = warning) a zapíšou
   solver-ready do `prepared/{DEPO}/l3_orders_*.csv`; kamiony L3 jsou
   odečtené z budgetu hned na startu (depa s nimi nepočítají).
3. **Po posledním depu**:
   ```powershell
   python plan_day.py l3
   ```
   sloučí l3_orders všech dep (block `L3`, okna lokací neplatí — pevně
   **04:00–20:00**) a **nejdřív zkontroluje sjízdnost** (stejný model
   jako výběr, ale s reálnými objednávkami a všechny povinné — odpoví za
   ~20 s). Když nevyjde, zkusí přidat kamion, který večer nikdo nepoužil
   (odečte ho ze stavu); když ani to, skončí ALERTem s přesným seznamem
   **co komu vrátit** (per depo, čísla objednávek) +
   `state/l3_unplanned_{DATUM}.json`, prázdnou výstupní složku uklidí.
   Pak pustí solver s **`--driver-breaks`** (režim řidiče EU: pauzy 45 min
   v každém úseku do 4,5 h uplynulého času + **denní limit čisté jízdy
   9 h** jako tvrdá dimenze; routing kudy-smí-kamion řeší ORS
   `driving-hgv` jako dosud). Výstupy standardně do
   `data/results/L3/{DATUM}/` vč. **ESO exportu**.
4. **Řidiči**: `driver_assignment.py` zónu L3 přibere automaticky,
   když `data/results/L3/{DATUM}/lines_summary.csv` existuje.

Parametry (okno, cíl, λ, cena zastávky, budget) v `l3_planner.L3_CONFIG`;
pravidla řidiče (`driver_break_after_h`, `driver_break_min`,
`driver_max_drive_h`) jen v CONFIG solveru — výběr si je bere odtud.

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

### Exit kódy a `run_status.json` (pro server / UI, od 17. 8. 2026)

Solver končí vždy jedním ze čtyř kódů a zapíše **`run_status.json`** do
výstupní složky (jakmile je z názvu orders souboru známá — tedy i při
pádu při načítání dat):

| exit | `status` | význam | co s tím dělá `plan_day` |
|---|---|---|---|
| **0** | `ok` | plán uložen (`lines_count`, `total_cost_kc`, `total_kg`, `output_dir`) | pokračuje |
| **1** | `error` | technická chyba (routing instance neběží, výjimka, ORS profil) | ALERT, **neeskaluje** |
| **2** | `data_error` | vadná data — validace / závory (vadný řádek prepared, servis nad strop, objednávka těžší než největší auto, nestihnutelné okno, nedosažitelná GPS) | ALERT „oprav data a spusť znovu", **neeskaluje** |
| **3** | `infeasible` | řešení neexistuje (žádný seed / záchrana nevyšla / dvojlinky se nespárovaly) | **eskalace** L0 → L1+L2; z L1+L2 už není kam → ALERT |

Soubor nese `reason` (první řádek hlášky), plnou `message`, `orders`
(čísla dotčených objednávek vytažená z hlášky), `zone`, `delivery_date`,
`run_id`, `elapsed_sec`, `finished_at`. Dřív každý nenulový kód přepnul
zbytek večera na L1+L2 — i kvůli vadnému řádku v datech (audit 1.4).

**Záchranný re-solve** nevyřešeného clusteru vítězného seedu se od 17. 8.
vejde do celkového budgetu (3× čas původního pokusu, ale nejvýš to, co
zbývá do konce budgetu; fáze E dostane zbytek), běží **paralelně** (všechny
nevyřešené clustery × obě strategie najednou). Když nevyjde → exit 3 hned.
Vědomě zkusit déle: `--rescue-extra-min N` (default 0; `plan_day real/l3`
ho předává; na serveru nechat 0 — běh drží slovo o délce).

| Přepínač | Význam |
|---|---|
| `--budget-min 5` | Časový budget solveru v minutách (default 30). Rychlé porovnávací běhy. |
| `--rescue-extra-min N` | Druhé kolo záchranného re-solve NAD budget (default 0). Viz výše. |
| `--output-dir CESTA` | Ruční výstupní složka (jinak auto-detekce). Nutné pro porovnávací běhy, ať se nepřepíšou. |
| `--force-matrix` | **Nouzový** přepínač — vypne limit nedosažitelných párů pro všechny profily. Běžně NENÍ potřeba: limity jsou per profil (`driving` 0,1 %, `driving-hgv` 5 %) a pokrývají i Prahu. |
| `--allow-profile-fallback` | Dovol tichý fallback kamionů na osobní profil když ORS selže. **DEFAULT je hard-fail** (jinak by kamiony jely po špatných trasách). Používej jen vědomě. |
| `--zone-label CB` | Popisek zóny do výstupů (jinak z dat). |
| `--no-buffers` | **Tvrdý režim bez rezerv**: nosnost 100 % a závozová okna přesně jak je poslalo ESO9 (bez posunu −5/+25 min). |
| `--capacity-multiplier 1.0` | Jen nosnost (default viz CONFIG). |
| `--tw-expand-before 0` / `--tw-expand-after 0` | Jen okna (default 5 / 25 min). |
| `--seed-finalists 1` | Vynutí jen vítěze fáze C ve fázi E = **chování před 11. 8. 2026**. Na srovnávací běhy. Default je `auto` (viz níže). |
| `--double-runs` | Dvojlinky (porušení L2) — virtuální druhé jízdy malých aut od 10:00; večer zapíná `plan_day` podle decision. Dvojlinky se dělí mezi clustery poměrně, ne jako blok. |
| `--driver-breaks` | **Režim řidiče EU** (L3 kamiony): 45 min pauza v každém úseku do 4,5 h **uplynulého času** (jízda + vykládka — tak to počítá OR-Tools; vědomě přísnější než EU „4,5 h jízdy", stojí to nejvýš jednu pauzu navíc na dlouhé trase a solver zůstává jednoduchý a rychlý) + **denní limit čisté jízdy 9 h** jako tvrdá dimenze; objednávka, jejíž cesta tam a zpět limit přesáhne, zastaví běh hned (`validate_orders_servable`). Běžné dodávkové linky nemají. |

### Finalisté fáze E (`seed_finalists`, default `auto` od 11. 8. 2026)

Fáze C zkusí tři rozdělení do clusterů (`kmeans`, `sweep`, `tw_midpoint`) a
dřív poslala do fáze E jen to nejlevnější. Jenže **pořadí po fázi C je
špatný odhad kvality po fázi E**: v A/B na 8 depo-dnech vyhrál v 7 z 24 běhů
(29 %) jiný seed, než vybrala fáze C — na PR 7. 8. dokonce ten, který v C
prohrál o 3 050 Kč.

Fáze E proto dotahuje **víc finalistů naráz** a bere nejlevnější výsledek.
Fáze E dosud používala 2 jádra z 20; teď 6, wall clock stejný.

| hodnota | co udělá |
|---|---|
| `auto` (**default**) | Kolik se vejde do jedné vlny workerů: `workery // clustery`, max 3. Na 20 jádrech → 3, na 4 jádrech → **1** (= staré chování). Wall clock se nikdy neprodlouží. |
| `1` | Jen vítěz fáze C — chování před 11. 8. 2026. |
| `2` / `3` | Vynucený počet. Když se nevejde do jedné vlny, čas na úlohu se rozdělí (wall clock drží, ale každý solve má míň času). |

Naměřeno (5min budget, 3 opakování na variantu): medián prakticky nula,
ale **nejhorší běh −5 420 Kč** za dva dny dohromady a na CB 7. 8. o **auto
míň**. Přínos je v chvostu, ne v průměru.

`plan_day predict` i `real` mají `--seed-finalists` jako **passthrough** —
bez zadání se solveru nepředává nic a jede na CONFIG. Solver na začátku
vždy vypíše `Finalisté E: N …`, takže je na slabším stroji vidět, když
`auto` spadlo na 1.

Run log nese `config.seed_finalists` (rozřešené číslo) a u běhů s víc
finalisty i `results.finalists` — cena každého seedu po C a po E. Z toho se
dá zpětně vyčíst, jak často nepostupující seed otočil výsledek.

### Plánovací buffery — co znamenají

Solver **nemění data**, jen si při plánování nechává rezervu:

| buffer | default | co dělá |
|---|---|---|
| `vehicle_capacity_multiplier` | **1.0** = 100 % (L0) | default od vlny 3; porušení L1 = `--capacity-multiplier 1.03` (řídí `plan_day` podle decision) |
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

## 6. Vozový park a náklady vozidel

**V `data/static/` smí být PRÁVĚ JEDEN `vehicle_types-*.csv`** — ten se
použije. Program **sám nevybírá**: víc souborů je vada, kterou nahlásí
a zastaví se. Který soubor tam bude, řeší vrstva nad ním; plánovat podle
souboru, o kterém nikdo nerozhodl, je horší než se zastavit.
Co už neplatí, přesuň do `data/static/vehicle_types_archiv/`.

Formát: **středníky** (`;`), kódování UTF-8, hlavička povinná.

| sloupec | význam |
|---|---|
| `cost_per_km` | sazba za km |
| `start_cost_kc` | **fixní náklad za výjezd** (Kč; mzda řidiče / amortizace). Per-type, dražší řidiče kamionů lze nastavit zvlášť. `0` = žádný |
| `available_count` | počet aut daného typu (celofiremní sdílený pool) |
| `valid_for_date` | datum platnosti (`YYYYMMDD`, jako datum rozvozu v riro) — **zatím se nepoužívá**, jen se veze |

- **Starý čárkový formát se odmítá** jasnou chybou. Tichý fallback by znamenal
  plánování s prázdnou flotilou nebo na neaktuálních počtech aut.
- **Nečitelné číslo v povinném sloupci = běh stojí.** Řádek se nepřeskočí —
  chybějící typ vozidla by tiše zmenšil flotilu a plán by počítal s auty,
  která nemáme. Hláška vypíše řádek, sloupec i hodnotu; když vypadá jako
  datum (`17.04.2026`), upozorní na Excel jako příčinu.

> **Pozor na Excel.** `cost_per_km` má hodnoty jako `17.4` — české locale je
> zobrazí jako 17. duben. Prohlížet soubor můžeš, ale **neukládej ho z Excelu**;
> uložením se to zobrazení zapíše natvrdo. Uprav ho v textovém editoru, nebo
> exportuj z ESO9 znovu. `git diff` na `data/static/` ukáže, jestli se změnil.
- Který soubor běh použil, je vidět v konzoli (`Vozový park: …`) a v run logu.
- Ruční volba jiného souboru: `--vehicle-types-file CESTA`.
- Starý `count_block_{DEPOT}` byl fikce a je odstraněn.

### Přiřazení řidičů (`driver_assignment.py`) — od 13. 8. 2026

Samostatný krok **po naplánování VŠECH dep dne** (do `plan_day` se
nezapojuje — spouštění řeší vrstva nad námi):

```powershell
python driver_assignment.py 2026-08-13              # celý den
python driver_assignment.py 2026-08-13 --label b5   # testovací běhy
```

Vstup: registr aut+řidičů z ESO — **právě jeden** `.xlsx`
v `data/ridici/aktivni/` (PII → složka je gitignored). Bere se jen
`Použít vozidlo=Ano`; typ auta se mapuje přes (Typ, Nosnost) na TYPE kód.

Jedna **celodenní** přiřazovací úloha (maďarský algoritmus): všechny
linky všech dep × řidiči — globální optimum, žádná sekvence po depech.
Řidič jede max jednu linku denně (i když má víc aut); dvojlinka = jedna
jednotka (obě jízdy týž řidič).

**HARD**: den v týdnu (`Dny použitelnosti`, lomítko dělí týden/víkend),
`Dostupnost=Ano`, `Aktivní=Ano`, správný typ auta.
**SOFT** (váhy v CONFIG na začátku skriptu):

| kritérium | váha | logika |
|---|---|---|
| plnění plánu km | 3.0 | kdo zaostává za poměrnou částí ročního plánu — **BEZ DAT, dokud ESO neplní `Aktual. km`** (do té doby neutrální + warning) |
| dojezd | 1.0 | dlouhé linky vzdáleným řidičům (pořadové párování, žádné konstanty) |
| kvalita × tightness | 1.0 | Rychlý na linky s napjatými okny; tight zastávka = rezerva do konce okna ≤ 15 min, konec linky váží 1,3× víc než začátek |
| familiarity | 1.0 | podíl zastávek, které řidič zná — **BEZ DAT, dokud historie závozů nenese řidiče** |

Výstupy: `data/results/driver_assignment/{DATUM}/driver_plan_{DATUM}.csv`
(+ `driver_plan_{DEPO}_{DATUM}.csv` vedle plánu každého depa +
`summary.json` s váhami a warningy). Málo řidičů po hard filtrech →
ALERT s výpisem nepokrytých linek a exit ≠ 0.

---

## 7. Git a osobní údaje (GDPR) — DŮLEŽITÉ

**Do gitu NIKDY nejdou osobní údaje.** Blokuje je `.gitignore`:
- `data/input/`, `data/prepared/`, `data/prediction/` (jména, adresy, GPS, váhy zákazníků)
- `data/static/locations_*.csv` (adresy zákazníků — pipeline je už nepoužívá, ale
  soubory na disku zůstávají a do gitu nesmí)
- `data/static/vehicle_registry.csv` (jména řidičů, SPZ)
- `data/ridici/` (registr aut+řidičů z ESO — jména, telefony, SPZ)

Verzuje se pouze **kód + config bez PII** (`vehicle_types.csv`, `closures.json`).
Data existují jen lokálně na disku. Repo je **Private**.

Commitujeme **při milnících** (dokončená feature / opravený bug / funkční stav),
ne po každé drobnosti.

---

## 8. Testy

Solver i `prepare_inputs` spouští **startup unit testy** automaticky před během
(celá `tests/` mimo integrační; aktuální počet viz README). Přeskočit:
`SKIP_STARTUP_TESTS=1`. Ručně:
```powershell
python -m pytest tests/ -q
```
Integrační routing testy (`test_ors_hgv_integration.py`) běží automaticky po
nastartování routing instance (ověří ORS vs OSRM).
