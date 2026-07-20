# Experiment: nejrychlejší vs. nejkratší trasy

## Otázka

Solver minimalizuje **peníze = km × Kč/km + fix**. Čas je jen omezení (okna, délka dne).
Jenže kilometry, které sčítáme, přicházejí z OSRM, a ten svým výchozím profilem
hledá **nejrychlejší** cestu — vrací tedy *vzdálenost nejrychlejší trasy*, ne
nejkratší možnou.

Přitom platíme za km a čas nás skoro nic nestojí (řidič placený za den, nejdelší
trasa využívá 49 % časového stropu). **Kdyby OSRM hledal nejkratší cestu, mohli
bychom ušetřit — za cenu pomalejší jízdy.**

Km tvoří ~68 % ceny plánu (49 632 z 73 358 Kč na CB). Už **3 % úspory = ~1 500 Kč/den
na jednom depu** — víc než celý náš náskok před Rinkayem (1 100 Kč).

## Železné pravidlo: produkce se NESMÍ dotknout

| co | pravidlo |
|---|---|
| kód projektu | **žádná změna** — experiment produkční skripty jen importuje |
| `C:\osrm` (stable) | **jen čteme** PBF, nikdy nezapisujeme |
| `C:\osrm_current` | vůbec se nedotýkáme |
| kontejnery `osrm-stable`, `ors-stable`, `*-current` | vůbec se nedotýkáme |
| porty 5000/5001/8080/8081 | vůbec se nedotýkáme |
| `data/results/`, `data/prediction/` | nezapisujeme (výstupy jdou do `results/` zde) |

Experiment si staví **vlastní** instanci: složka `C:\osrm_shortest`, kontejner
`osrm-shortest`, port **5002**.

## Jablka s jablky

Jediná proměnná, která se smí lišit, je **způsob hledání trasy** (nejrychlejší vs.
nejkratší). Vše ostatní musí být identické:

- **stejná OSM data** — build skript PBF **kopíruje z `C:\osrm`** (8. 4. 2026),
  nestahuje nový z Geofabriku
- **stejný algoritmus** — MLD (`osrm-partition` + `osrm-customize`), jako stable
- **stejný image** — `osrm/osrm-backend`
- **stejné objednávky** — tentýž prepared CSV
- **stejný solver** — importuje se produkční `vrp_solver_lines_v6.py`, ne kopie

## Proč flotila bez kamionů

`fleet/vehicle_types_no_hgv.csv` = produkční flotila **minus** TYPE_06/TYPE_07
(jediné dva vozy s profilem `driving-hgv`).

Důvod: kamiony routuje ORS, ne OSRM — museli bychom stavět i druhou ORS instanci.
Přitom náš vítězný plán kamiony **vůbec nepoužil** a dodávky (profil `driving`)
nesou 68 % ceny. Takže je vynecháme a izolujeme otázku tam, kde jsou peníze.

## Postup

### 1× příprava (trvá desítky minut, hlavně `osrm-extract`)

```powershell
cd experiments\shortest_vs_fastest
.\build_shortest_osrm.ps1
```

Postaví `C:\osrm_shortest` ze stejného PBF s profilem `weight_name = 'distance'`
a nastartuje kontejner `osrm-shortest` na portu 5002.

Ověření, že běží obě instance:
```powershell
(Invoke-RestMethod "http://localhost:5000/route/v1/driving/15.595,49.506;14.259,48.810?overview=false").routes[0] | Select-Object distance, duration, weight_name
(Invoke-RestMethod "http://localhost:5002/route/v1/driving/15.595,49.506;14.259,48.810?overview=false").routes[0] | Select-Object distance, duration, weight_name
```
Nejkratší (5002) musí mít **menší `distance` a větší `duration`** než 5000
a hlásit `weight_name = distance` (5000 hlásí `routability`).

⚠️ Nepoužívej `curl` — v PowerShellu je to alias pro `Invoke-WebRequest`, který
parsuje HTML a vyhodí bezpečnostní dotaz. `Invoke-RestMethod` vrací rovnou JSON.

### FÁZE 1 — měření matic (bez solveru, rychlé, rozhodující)

```powershell
python compare_matrices.py --orders-file ..\..\data\prepared\CB\orders_CB_2026-07-17.csv --plan-dir ..\..\data\results\CB\2026-07-17_ceny
```

Spočítá tytéž body dvěma maticemi a odpoví na to hlavní:
**„kdybychom jeli STEJNÉ trasy, ale nejkratšími cestami, kolik km ušetříme?"**
Plus kolik času to stojí. Solver se vůbec nespouští.

**Rozhodovací pravidlo:** úspora < 1 % → zavřít; 1–3 % → hraniční; > 3 % → jít do fáze 2.

### FÁZE 2 — plný běh solveru (jen když fáze 1 dá smysl)

```powershell
python run_variant.py --orders-file ..\..\data\prepared\CB\orders_CB_2026-07-17.csv --variant fastest --budget-min 5
python run_variant.py --orders-file ..\..\data\prepared\CB\orders_CB_2026-07-17.csv --variant shortest --budget-min 5
```

Pustí **produkční solver** (import, ne kopie) proti 5000 resp. 5002, výstupy do
`results/`. Pak porovnej `zone_summary.json` obou.

### Úklid po experimentu

```powershell
docker stop osrm-shortest; docker rm osrm-shortest
Remove-Item -Recurse -Force C:\osrm_shortest    # ~5 GB
```

## Jak run_variant.py obchází změnu kódu

Solver bere URL z `OSM_PRESETS` v `osm_routing.py` (preset `stable`/`current`).
`run_variant.py` **v paměti svého procesu** přepíše preset `current` na port 5002
a zavolá solver s `--fresh-osm`. Na disku se nemění nic — produkční `osm_routing.py`
zůstává netknutý a běžné spuštění solveru z kořene repa funguje beze změny.

## Na co si dát pozor při interpretaci

- **Nejkratší ≠ lepší.** Vede přes vesnice a úzké silnice: pomalejší, horší pro
  řidiče, u velkých aut často neprůjezdné. Proto to měříme jen pro dodávky.
- **Delší den** znamená větší riziko u závozových oken. Fáze 1 proto hlásí
  i časovou daň, ne jen úsporu km.
- Případné reálné nasazení by znamenalo přestavbu produkčního OSRM grafu —
  to je samostatné rozhodnutí až podle výsledků.
