# Výsledky: nejrychlejší vs. nejkratší trasy (experiment 20. 7. 2026)

**Status: změřeno, NENASAZENO.** Kandidát na snížení nákladů ~3 % ceny dopravy.
Leží záměrně mimo hlavní flow projektu — produkce jede beze změny na `routability`.

## Otázka a motivace

Solver minimalizuje **Kč = km × sazba + fix za auto**. Čas je jen omezení (závozová
okna, délka dne). Kilometry ale dodává OSRM, který defaultně hledá trasu podle
**`routability`** (≈ nejrychlejší po kvalitních silnicích) — vrací tedy vzdálenost
*rychlé* trasy, ne nejkratší možné. Přitom platíme za km a čas řidiče je placený
za den. Hypotéza: routing podle **`distance`** (nejkratší cesta) ušetří peníze.

## Metodika (jablka s jablky)

- Izolovaná OSRM instance `osrm-shortest` (port 5002, `C:\osrm_shortest`),
  postavená ze **stejného PBF jako produkce** (kopie z `C:\osrm`, data 8. 4. 2026),
  stejný algoritmus MLD, stejný image, stejný `--max-table-size 1000`.
  Jediný rozdíl: `car.lua` s `weight_name = 'distance'`.
- **Produkční solver importován, ne kopírován** (`run_variant.py` přepíše routing
  URL jen v paměti procesu). Stejné objednávky (17. 7., finální RiRo, 245+171+162+177),
  stejná flotila bez kamionů (60 vozů — kamiony jedou přes ORS, který se neměnil),
  stejný budget 5 min, ORS pro objízdky uzavírek konstantní v obou variantách.
- Fáze 1 = porovnání matic bez solveru (izoluje efekt silnic od efektu solveru).
  Fáze 2 = plné běhy solveru, 4 depa × 2 varianty.

## Výsledky — firma celkem (1 den, 17. 7. 2026)

| | rychlá (routability) | krátká (distance) | delta |
|---|---:|---:|---:|
| **cena** | 300 959 Kč | **291 278 Kč** | **−9 681 Kč (−3,2 %)** |
| km | 21 323 | 20 301 | −1 022 (−4,8 %) |
| hodiny řízení | 462,7 | 539,6 | **+76,9 (+16,6 %)** |
| aut | 64 | 66 | +2 |

Per depo (cena Kč): CB 71 712→69 704 (−2 008) · HK 57 669→56 562 (−1 107, +1 auto)
· MO 75 150→73 691 (−1 459, +1 auto) · **PR 96 428→91 321 (−5 107, stejná auta)**.

**Proveditelnost:** všech 755 objednávek obslouženo v obou variantách, 0 porušení
oken mimo toleranci (−5/+25 min) kdekoliv; krátká varianta má na všech 4 depech
dokonce MÉNĚ pozdních příjezdů (kompaktnější trasy). Roční extrapolace
~2,4–2,9 mil. Kč — ale z JEDNOHO dne dat.

## Kde se úspora bere (klíčové zjištění)

**V dálkových nájezdech ze skladu, ne v rozvozu.** Top úseky (stejné A→B v obou
variantách): Štoky→Mohelnicko **−58 km** (D1 přes Brno 188 km vs. přímo přes
Vysočinu 130 km), Štoky→Tymákov −26 km (D1-Praha-D5 vs. přímo po s. 18),
Štoky→Rakovník −13 km. Vzorec: rychlá jede dálniční sítí „přes roh", krátká řeže
úhlopříčku po silnicích II./III. třídy. Lokální skoky mezi zákazníky šetří po
1–3 km a jsou bez rizika (stejné okresky). Ze 211 společných úseků: 88× krátká
kratší, 122× shodné, 1× delší (o 0,1 km).

Vizualizace top 3: `results/top3_routes_map.html` (negenerováno v gitu — vytvoří
skript, viz níže).

## Rozklad úspory: silnice vs. solver

Přecenění tras rychlé varianty (CB) krátkou maticí: **75 % úspory je čistý efekt
silnic** (deterministický, −147,7 km na identických trasách), 25 % přidalo
přeskládání plánů solverem (může být i šum lokálních optim — viz abnormality).

## Abnormality a poučení

1. **`routability` ≠ jen „nejrychlejší"** — default OSRM profil optimalizuje čas
   A ZÁROVEŇ preferuje kvalitní silnice (penalizuje obytné zóny, nízké třídy).
   Přepnutím na `distance` se ztrácí obojí — proto extrémy typu +45 % času.
2. **Solver má obrovský rozptyl lokálních optim:** dva běhy nad stejnými daty
   (jen jiná matice) sdílí pouze 26 % přejezdů a 0/20 linek má stejnou množinu
   objednávek. Srovnávání jednotlivých běhů je proto šum ±stovky Kč — tvrdá
   čísla dává jen maticové porovnání (fáze 1).
3. **`--max-table-size`:** default OSRM je 100 lokací; produkce běží s 1000.
   Bez toho /table na 246 bodů vrací HTTP 400. Zapracováno do build skriptu.
4. **ORS (kamiony) pokryt není** — Matrix API ORS nemá parametr `preference`,
   nejkratší routing pro `driving-hgv` by chtěl zásah do konfigurace ORS.
   Pro tento experiment nevadí (vítězné plány kamiony nepoužívají).
5. **HK nejdelší trasa 14,8 h** (rychlá měla 10,2). Dnes v limitu (strop 23,5 h
   je fikce), ale při zavedení reálného stropu směny by tahle trasa neprošla.
6. **+2 auta (HK, MO):** ekonomika stojí na fixu 1000 Kč/auto. Při reálném
   nákladu na řidiče X Kč/den klesá úspora o 2×(X−1000); nula až při X≈5 800.
7. Startup integrační test ORS↔OSRM porovnává shodu vzdáleností — s `distance`
   profilem z principu neprojde (jiné vzdálenosti jsou smysl experimentu),
   v `run_variant.py` se přeskakuje s vysvětlením.

## Návrhy implementace (od nejmenšího rizika)

1. **Hybridní profil (doporučený další krok):** `car.lua` s `weight_name =
   'distance'`, ale ponechanými/zvýšenými `speed`→`rate` penalizacemi pro
   `tertiary` a nižší třídy (v sekci profilu jde nastavit per-highway-type
   `weight` násobky). Cíl: brát zkratky po slušných dvojkách (s.18, s.353, stará
   s.6), ale nepouštět auta na III. třídy a obytné ulice. Očekávání: většina
   z −4,8 % km bez extrémů času. Postup: zkopírovat `build_shortest_osrm.ps1`,
   upravit patch profilu, přeměřit fází 1 (bez solveru, minuty).
2. **Distance jen pro nájezdy:** úspora je koncentrovaná v radiálách ze skladu.
   Šlo by kombinovat: matice pro první/poslední úsek z `distance` instance,
   lokální rozvoz z `routability`. Výrazně složitější (dvě matice v solveru),
   zvažovat až kdyby hybrid nestačil.
3. **Plné nasazení `distance`:** nejjednodušší (přestavět produkční graf +
   upravit `update_osrm.py`, aby při aktualizaci OSM používal patchnutý profil),
   ale nese extrémy — 14,8h směny, okresky na dálkových nájezdech.

## Co MUSÍ předcházet rozhodnutí

- [ ] Zopakovat na 2–3 dalších dnech dat (stačí fáze 1 + jeden solver den).
- [ ] Ukázat provozu mapy top nájezdů („pojedou řidiči Štoky→Morava 130 km po
  okresce místo 188 po D1?") — rozhoduje o polovině úspory.
- [ ] Zjistit reálný náklad na řidiče-den (kalibrace fixu 1000 Kč).
- [ ] Postavit a přeměřit hybridní profil (návrh 1).

## Reprodukce

```powershell
# 1. postavit izolovanou instanci (30+ min, ~6 GB, port 5002)
.\build_shortest_osrm.ps1
# 2. fáze 1 — matice, bez solveru (minuty)
python compare_matrices.py --orders-file "../../data/prepared/CB/orders_CB_2026-07-17.csv" --plan-dir "../../data/results/CB/2026-07-17_ceny"
# 3. fáze 2 — plné běhy (~6 min / běh)
python run_variant.py --orders-file "../../data/prepared/CB/orders_CB_2026-07-17.csv" --variant fastest  --budget-min 5
python run_variant.py --orders-file "../../data/prepared/CB/orders_CB_2026-07-17.csv" --variant shortest --budget-min 5
# úklid (vrací stroj do původního stavu, produkce nedotčena)
docker stop osrm-shortest; docker rm osrm-shortest
Remove-Item -Recurse -Force C:\osrm_shortest
```

Výstupy běhů (results/) jsou v .gitignore — obsahují jména/GPS zákazníků (PII)
a jsou plně regenerovatelné. Čísla v tomto dokumentu jsou z běhů 20. 7. 2026.
