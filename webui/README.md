# webui — webové rozhraní nad VRP plánovačem

Tenká **API-first** vrstva (FastAPI + uvicorn), která spouští **stejné příkazy**
jako ruční CLI workflow (viz kořenové `WORKFLOW.md`) přes subprocess a přidává
prohlížení výsledků, benchmarky a sledování běhů. Frontend je statické
HTML/JS/CSS bez build stepu.

> **CLI workflow zůstává nedotčený.** webui nic nepřepisuje v existujícím kódu —
> jen volá `prepare_inputs_v6.py`, `vrp_solver_lines_v6.py`,
> `vrp_solver_lines_all_depots_v6.py`, `benchmark_all_depots_solver_v6.py`
> a `visualize_routes.py` přesně tak, jak bys je spustil ručně.

## Instalace

```bash
pip install -r webui/requirements.txt
```

Závislosti **solveru** (numpy, pandas, ortools, requests, scikit-learn, openpyxl,
pytest) jsou samostatné a musí být nainstalované zvlášť — webui je nepřidává.

## Spuštění

Z kořene repa (nebo přes launcher):

```bash
# Windows
webui\start_webui.bat

# Linux / macOS
webui/start_webui.sh

# ručně
python -m uvicorn webui.app.main:app --host 127.0.0.1 --port 8777
```

Pak otevři **http://127.0.0.1:8777**.

- **Port 8777** (volný vedle 8765 closure editor, 5000/5001 OSRM, 8080/8081 ORS).
  Přepíšeš přes env `VRP_WEBUI_PORT`.
- Server běží na `127.0.0.1` (jen lokálně).
- **Nespouštěj s `--reload`** — reloader by při změně souboru zabil frontu úloh
  a osiřel běžící solver.

## Nutná infrastruktura pro reálné běhy

Solver při startu pinguje **OSRM/ORS** a bez nich spadne. Před reálným během
spusť Docker routing:

```bash
scripts\start_osrm_stable.bat      # stable (5000/8080)
```

Pro `--fresh-osm` běhy se o kontejnery (5001/8081) postará orchestrátor sám.

## Taby

- **Denní běh** — vybereš depo, nahraješ RiRo do `aktivni/`, spustíš
  prepare → solve → visualize. „Zobrazit příkazy" ukáže přesné příkazy (dry-run).
- **Benchmarky** — all-depots solver a multi-variant benchmark; prohlížeč sessionů.
- **Výsledky** — seznam běhů, souhrn, download (xlsx/csv/json), mapa v iframe,
  historie z `run_log.jsonl` s porovnáním dvou běhů.
- **Úlohy** — tabulka úloh + testovací úloha (selftest, ověří UTF-8 a cancel).

## Kde žijí data úloh

`webui/jobs/{job_id}/` — `job.json` (stav) + `job.log` (společný log všech kroků,
oddělené `===== STEP … =====`). Složka je **gitignored** (runtime artefakty).
Živý log = polling `GET /api/jobs/{id}/log?offset=<bajty>` (offset v bajtech kvůli
diakritice). Po restartu serveru se nedokončené úlohy označí `interrupted`.

## Souběh a bezpečnost

- **Jedna úloha současně** (solver saturuje CPU; `aktivni/` snese jeden soubor).
  Další úloha čeká ve frontě.
- Upload do depa s běžící úlohou je zablokován (423).
- Cancel zabíjí **celý strom procesů** (i multiprocessing děti solveru).
- Cesty k výsledkům jsou validované — žádný path traversal ven z `data/results`.

## Poznámky k budoucímu Linux serveru

- Vše používá `pathlib` / `sys.executable`, žádný Windows-specifický kód.
  `.bat` je jen launcher, `.sh` je jeho protějšek.
- Subprocess: POSIX `start_new_session=True` + `killpg`, Windows
  `CREATE_NEW_PROCESS_GROUP` + `taskkill /T`.
- Cesty v API jsou vždy s dopřednými lomítky.
- Nikdy se nepředává `--open` do `visualize_routes.py` (headless-safe).

## Testy

```bash
python -m pytest webui/tests -q
```

Drženy odděleně od startup brány solveru (`pytest tests/ -q`) — nemíchat.
