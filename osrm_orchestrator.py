r"""
osrm_orchestrator.py — Self-contained orchestrace pro `--fresh-osm`
====================================================================

Cíl: jediné volání `ensure_fresh_routing_ready()` zařídí všechno potřebné
k tomu, aby běžela current routing instance (`C:\osrm_current`) s aktuálními
daty. Volá se ze solveru / runneru / vizualizéru po `apply_osm_source(..., "current")`.

Workflow:
  1. Zavolá `update_osrm.run_pipeline()` — stáhne nová data pokud potřeba,
     spustí osrm-extract/partition/customize.
  2. Zjistí stav Docker kontejnerů (`osrm-current`, `ors-current`):
       - Pokud byla data aktualizována  → stop + rm + restart + smazat ORS graphs
       - Pokud kontejnery neběží        → start
       - Jinak                          → nech být
  3. Čeká na HTTP endpoint OSRM (~60 s) a ORS (~30 min při rebuild).

Žádné automatické zásahy do stable instance (`C:\osrm`, porty 5000/8080).

Použití (v solveru):
    from osrm_orchestrator import ensure_fresh_routing_ready
    if args.fresh_osm:
        ensure_fresh_routing_ready()
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

from update_osrm import (
    DEFAULT_DATA_DIR,
    PHASE_READY,
    assert_not_stable_dir,
    run_pipeline,
)


# ── Konfigurace ──────────────────────────────────────────────────────────────

OSRM_CONTAINER = "osrm-current"
ORS_CONTAINER  = "ors-current"

OSRM_HOST_PORT = 5001
ORS_HOST_PORT  = 8081

# Vnitřní (container-side) porty — `docker run -p HOST:CONTAINER`.
# OSRM osrm-routed default = 5000.
# ORS (openrouteservice/openrouteservice:latest, current verze) Spring Boot
# Tomcat default = 8082 (potvrzeno přes `ss -tln` uvnitř kontejneru).
# Před nějakou dobou bylo 8080 — pokud image image změní default znovu,
# uprav tady. Případně lze auto-detekovat z `docker exec ss -tln` ale
# komplikuje to flow.
OSRM_CONTAINER_PORT = 5000
ORS_CONTAINER_PORT  = 8082

# OSRM `--max-table-size` — default 100 je málo na reálné VRP datasety
# (klidně 200-500 zastávek). Stable instance používá 1000, my dáme 10000
# pro bezpečnou rezervu. Hodnota je RAM-bound (matice N×N), ale na
# moderních strojích 10k×10k = 100M cells = ~800 MB → ok.
OSRM_MAX_TABLE_SIZE = 10000

OSRM_IMAGE = "osrm/osrm-backend"
ORS_IMAGE  = "openrouteservice/openrouteservice:latest"

OSRM_HEALTH_URL = f"http://localhost:{OSRM_HOST_PORT}/route/v1/driving/14.4,50.0;14.5,50.1?overview=false"
ORS_HEALTH_URL  = f"http://localhost:{ORS_HOST_PORT}/ors/v2/health"

OSRM_DATA_FILE = "czech-republic-latest.osrm"  # výstup z osrm-extract

# Docker named volume pro persistent ORS graph cache.
# Důvod: Windows bind-mount na NTFS přes WSL2 je 18× pomalejší než interní
# FS. Named volume žije uvnitř WSL2 (linux FS) → rychlost interního FS,
# ale persistuje napříč restarty kontejneru.
ORS_GRAPHS_VOLUME = "ors-current-graphs"


# ── Docker helpers ───────────────────────────────────────────────────────────

def _docker_available() -> bool:
    """True pokud `docker` CLI funguje a daemon běží."""
    try:
        res = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=10,
        )
        return res.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _container_state(name: str) -> str:
    """
    Vrátí stav kontejneru: 'running', 'exited', 'created', 'paused', 'absent'.
    """
    res = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", name],
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        return "absent"
    return (res.stdout.strip() or "absent")


def _container_has_mount(name: str, container_path: str) -> bool:
    """
    Vrátí True pokud kontejner má volume mount s daným destination path.

    Používáme k detekci "starých" kontejnerů spuštěných před přidáním nového
    mountu (např. graphs/ pro persistent ORS cache). Když chybí očekávaný
    mount → kontejner je třeba recreate s aktuální konfigurací.
    """
    res = subprocess.run(
        ["docker", "inspect", "-f", "{{json .Mounts}}", name],
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        return False
    try:
        mounts = json.loads(res.stdout.strip() or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    for m in mounts:
        if isinstance(m, dict) and m.get("Destination") == container_path:
            return True
    return False


def _container_has_cmd_arg(name: str, arg: str) -> bool:
    """
    Vrátí True pokud kontejner má daný flag v Config.Cmd (entrypoint args).

    Použití: detekce že běžící osrm-current byl spuštěn s `--max-table-size`,
    bez kterého OSRM má default limit 100 → table requesty s 200+ zastávkami
    selžou s HTTP 400.
    """
    res = subprocess.run(
        ["docker", "inspect", "-f", "{{json .Config.Cmd}}", name],
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        return False
    try:
        cmd_list = json.loads(res.stdout.strip() or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    return arg in (cmd_list or [])


def _container_has_port_mapping(name: str, host_port: int, container_port: int) -> bool:
    """
    Vrátí True pokud kontejner má `-p host_port:container_port` mapping.

    Detekuje "starou" konfiguraci s jinou container_port hodnotou (např. když
    se default port v image změnil mezi verzemi). Pokud kontejner běží se
    starým mapováním → musí být recreate s aktualizovaným.
    """
    res = subprocess.run(
        ["docker", "inspect", "-f", "{{json .NetworkSettings.Ports}}", name],
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        return False
    try:
        ports = json.loads(res.stdout.strip() or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    key = f"{container_port}/tcp"
    bindings = ports.get(key) if isinstance(ports, dict) else None
    if not bindings:
        return False
    for b in bindings:
        if isinstance(b, dict) and str(b.get("HostPort")) == str(host_port):
            return True
    return False


def _container_has_named_volume(name: str, container_path: str, volume_name: str) -> bool:
    """
    Vrátí True pokud kontejner má NAMED VOLUME (ne bind mount) s daným
    destination a jménem volume.

    Detekuje "starou" konfiguraci s bind mountem na hostu — ta byla 18× pomalejší
    kvůli Windows ↔ WSL2 I/O overheadu. Pokud existuje mount na container_path,
    ale není to očekávaný named volume → kontejner je třeba recreate.
    """
    res = subprocess.run(
        ["docker", "inspect", "-f", "{{json .Mounts}}", name],
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        return False
    try:
        mounts = json.loads(res.stdout.strip() or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    for m in mounts:
        if not isinstance(m, dict):
            continue
        if m.get("Destination") != container_path:
            continue
        # Named volume má Type="volume" a Name=jméno volume
        if m.get("Type") == "volume" and m.get("Name") == volume_name:
            return True
    return False


def _stop_and_remove(name: str) -> None:
    """`docker stop && docker rm`. Tichá past pokud kontejner neexistuje."""
    subprocess.run(["docker", "stop", name],
                   capture_output=True, text=True, timeout=60)
    subprocess.run(["docker", "rm", name],
                   capture_output=True, text=True, timeout=30)


def _container_holding_port(port: int) -> str | None:
    """
    Fix #3: vrátí jméno kontejneru obsazujícího daný HOST port
    (kromě našich vlastních OSRM_CONTAINER / ORS_CONTAINER), nebo None.
    """
    res = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"publish={port}", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        return None
    for name in res.stdout.splitlines():
        name = name.strip()
        if name and name not in (OSRM_CONTAINER, ORS_CONTAINER):
            return name
    return None


def _start_osrm_container(data_dir: Path) -> None:
    """`docker run -d --name osrm-current -p 5001:5000 ...`"""
    osrm_file = data_dir / OSRM_DATA_FILE
    if not osrm_file.exists():
        raise SystemExit(
            f"\n[CHYBA] {osrm_file} neexistuje — nelze spustit OSRM kontejner.\n"
            f"        Spusť: python update_osrm.py"
        )
    # Fix #3: detekce port konfliktu před docker run
    conflict = _container_holding_port(OSRM_HOST_PORT)
    if conflict:
        raise SystemExit(
            f"\n[CHYBA] Port {OSRM_HOST_PORT} je obsazen kontejnerem '{conflict}'.\n"
            f"        Zastav ho a zkus znovu:\n"
            f"          docker stop {conflict} && docker rm {conflict}"
        )
    cmd = [
        "docker", "run", "-d",
        "--name", OSRM_CONTAINER,
        "-p", f"{OSRM_HOST_PORT}:{OSRM_CONTAINER_PORT}",
        "-v", f"{data_dir}:/data",
        OSRM_IMAGE,
        "osrm-routed", "--algorithm", "mld",
        "--max-table-size", str(OSRM_MAX_TABLE_SIZE),
        f"/data/{OSRM_DATA_FILE}",
    ]
    print(f"  $ {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise SystemExit(
            f"\n[CHYBA] Spuštění {OSRM_CONTAINER} selhalo:\n"
            f"        STDERR: {res.stderr.strip()}"
        )


def _start_ors_container(data_dir: Path) -> None:
    """`docker run -d --name ors-current -p 8081:8080 ...`"""
    # Fix #3: detekce port konfliktu před docker run
    conflict = _container_holding_port(ORS_HOST_PORT)
    if conflict:
        raise SystemExit(
            f"\n[CHYBA] Port {ORS_HOST_PORT} je obsazen kontejnerem '{conflict}'.\n"
            f"        Zastav ho a zkus znovu:\n"
            f"          docker stop {conflict} && docker rm {conflict}"
        )
    # Fix #4 (v2): persistent graph cache přes Docker named volume.
    # Host bind mount byl 18× pomalejší kvůli NTFS↔WSL2 overheadu; named
    # volume žije uvnitř WSL2 (linux FS) → rychlost interního FS.
    # Docker volume create je idempotentní — pokud už existuje, nic se nestane.
    subprocess.run(
        ["docker", "volume", "create", ORS_GRAPHS_VOLUME],
        capture_output=True, text=True, timeout=10,
    )

    config_path = data_dir / "ors-config.yml"
    if not config_path.exists():
        print(f"  [WARN] {config_path} neexistuje — ORS poběží bez vlastní konfigurace.")
        cmd = [
            "docker", "run", "-d",
            "--name", ORS_CONTAINER,
            "-p", f"{ORS_HOST_PORT}:{ORS_CONTAINER_PORT}",
            "-v", f"{data_dir}:/home/ors/files",
            "-v", f"{ORS_GRAPHS_VOLUME}:/home/ors/graphs",
            ORS_IMAGE,
        ]
    else:
        cmd = [
            "docker", "run", "-d",
            "--name", ORS_CONTAINER,
            "-p", f"{ORS_HOST_PORT}:{ORS_CONTAINER_PORT}",
            "-v", f"{data_dir}:/home/ors/files",
            "-v", f"{config_path}:/home/ors/config/ors-config.yml",
            "-v", f"{ORS_GRAPHS_VOLUME}:/home/ors/graphs",
            ORS_IMAGE,
        ]
    print(f"  $ {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise SystemExit(
            f"\n[CHYBA] Spuštění {ORS_CONTAINER} selhalo:\n"
            f"        STDERR: {res.stderr.strip()}"
        )


def _wipe_ors_graphs(data_dir: Path) -> None:
    """
    Smaž ORS graph cache (Docker named volume), aby se po novém PBF přestavil graf.

    Fix #4 (v2): cache žije v named volume (rychlé, uvnitř WSL2 FS).
    `docker volume rm` musí být zavolán až po odstranění kontejneru
    (caller dělá _stop_and_remove před tímto voláním).

    Bonus: cleanup legacy host-side `graphs/` složky pokud existuje
    z dřívější bind-mount éry — jen zabírá místo na NTFS.
    """
    res = subprocess.run(
        ["docker", "volume", "rm", ORS_GRAPHS_VOLUME],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode == 0:
        print(f"  ORS graph cache (named volume) smazána: {ORS_GRAPHS_VOLUME}")
    else:
        stderr = (res.stderr or "").lower()
        if "no such volume" in stderr or "not found" in stderr:
            pass  # neexistovalo, OK
        else:
            print(f"  [WARN] Nepodařilo se smazat volume {ORS_GRAPHS_VOLUME}: "
                  f"{res.stderr.strip()}")

    # Legacy cleanup — odstraň host-side graphs/ z dřívější bind-mount verze
    legacy = data_dir / "graphs"
    if legacy.exists():
        try:
            shutil.rmtree(legacy)
            print(f"  Smazána stará host-side graphs složka: {legacy}")
        except OSError as e:
            print(f"  [WARN] Nepodařilo se smazat starou {legacy}: {e}")


# ── Endpoint readiness ───────────────────────────────────────────────────────

def _wait_http_ok(url: str, timeout_s: float, label: str,
                  poll_s: float = 2.0,
                  container_name: str = "") -> None:
    """
    Čeká až URL vrátí HTTP < 500. Reportuje progress každých ~5 vteřin.

    Fix #1: pokud je zadán container_name, každých ~5 s zkontroluje
    že kontejner stále běží. Pokud selhal (exited/absent), okamžitě
    vyhodí SystemExit se posledními 30 řádky logů — žádné 30minutové čekání.

    SystemExit pokud timeout nebo kontejner padl.
    """
    print(f"  Čekám na {label} ({url}, max {timeout_s/60:.1f} min)...")
    t_start = time.time()
    last_print = 0.0
    last_err = ""
    while True:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code < 500:
                elapsed = time.time() - t_start
                print(f"  {label} odpovídá ({elapsed:.0f} s, HTTP {r.status_code})")
                return
            last_err = f"HTTP {r.status_code}"
        except requests.exceptions.RequestException as e:
            last_err = type(e).__name__

        elapsed = time.time() - t_start
        if elapsed > timeout_s:
            raise SystemExit(
                f"\n[CHYBA] {label} se nespustil do {timeout_s/60:.1f} min.\n"
                f"        Poslední chyba: {last_err}\n"
                f"        Zkontroluj logy: docker logs --tail 50 {container_name or label}"
            )
        if elapsed - last_print >= 5.0:
            # Fix #1: detekce pádu kontejneru
            if container_name:
                state = _container_state(container_name)
                if state not in ("running",):
                    logs = subprocess.run(
                        ["docker", "logs", "--tail", "30", container_name],
                        capture_output=True, text=True, timeout=10,
                    )
                    log_output = (logs.stdout or "") + (logs.stderr or "")
                    raise SystemExit(
                        f"\n[CHYBA] Kontejner '{container_name}' se zastavil (stav: {state}).\n"
                        f"--- Posledních 30 řádků logů ---\n"
                        f"{log_output.strip()}\n"
                        f"--------------------------------\n"
                        f"Oprav konfiguraci (ors-config.yml, PBF alias) a zkus znovu."
                    )
            print(f"    ...{elapsed:.0f} s ({last_err})", flush=True)
            last_print = elapsed
        time.sleep(poll_s)


# ── Hlavní vstupní bod ───────────────────────────────────────────────────────

def ensure_fresh_routing_ready(
    data_dir: str | Path = DEFAULT_DATA_DIR,
    *,
    skip_update: bool = False,
    osrm_timeout_s: float = 60,
    ors_timeout_min: float = 30,
) -> dict:
    """
    Zajisti že current routing instance běží s aktuálními daty.

    Parametry:
      data_dir       — typicky C:\\osrm_current
      skip_update    — pokud True, neběží `update_osrm.run_pipeline` (jen kontejnery)
      osrm_timeout_s — timeout čekání na OSRM endpoint (default 60 s)
      ors_timeout_min— timeout čekání na ORS endpoint (default 30 min, kvůli rebuildu grafu)

    Vrací dict z `run_pipeline` rozšířený o `containers_restarted: bool`.
    """
    data_dir = Path(data_dir)
    assert_not_stable_dir(data_dir)

    if not _docker_available():
        raise SystemExit(
            "\n[CHYBA] Docker není dostupný (`docker version` selhalo).\n"
            "        Zkontroluj že běží Docker Desktop."
        )

    # ── 1. Update dat (pokud potřeba) ────────────────────────────────
    if skip_update:
        print("[orchestrator] --skip-update: přeskakuji kontrolu dat.")
        pipeline_result = {
            "updated": False, "data_changed": False, "config_changed": False,
            "date_short": "", "phase": PHASE_READY,
            "remote_last_modified": "",
        }
    else:
        pipeline_result = run_pipeline(data_dir, print_post_hint=False)

    data_changed   = pipeline_result.get("data_changed", False)
    config_changed = pipeline_result.get("config_changed", False)

    # ── 2. Kontejnery: rozhodnutí ────────────────────────────────────
    print()
    print("─" * 62)
    print(" Docker kontejnery (current instance)")
    print("─" * 62)

    osrm_state = _container_state(OSRM_CONTAINER)
    ors_state  = _container_state(ORS_CONTAINER)
    print(f"  {OSRM_CONTAINER}: {osrm_state}")
    print(f"  {ORS_CONTAINER}:  {ors_state}")

    # Detekce "starých" kontejnerů bez očekávané konfigurace:
    #  - žádný mount na /home/ors/graphs (úplně původní verze kódu)
    #  - má bind mount na /home/ors/graphs (přechodná verze, 18× pomalejší)
    #  - port mapování míří na špatný container port (8080 místo 8082)
    ors_volume_ok = _container_has_named_volume(ORS_CONTAINER, "/home/ors/graphs",
                                                ORS_GRAPHS_VOLUME)
    ors_port_ok = _container_has_port_mapping(ORS_CONTAINER, ORS_HOST_PORT,
                                              ORS_CONTAINER_PORT)
    ors_stale = ors_state == "running" and (not ors_volume_ok or not ors_port_ok)
    if ors_stale:
        if not ors_port_ok:
            print(f"  [orchestrator] {ORS_CONTAINER} běží se špatným port mapping "
                  f"(očekáván {ORS_HOST_PORT}:{ORS_CONTAINER_PORT}) → recreate.")
        elif _container_has_mount(ORS_CONTAINER, "/home/ors/graphs"):
            print(f"  [orchestrator] {ORS_CONTAINER} běží se starým bind-mount grafem "
                  f"(pomalý) → recreate s named volume.")
        else:
            print(f"  [orchestrator] {ORS_CONTAINER} běží bez persistent graph cache "
                  f"→ recreate s named volume.")

    # OSRM detekce stale konfigurace:
    #  - port mapping (8080 → 8082 v image historii)
    #  - chybí --max-table-size flag (default 100, my potřebujeme tisíce)
    osrm_port_ok = _container_has_port_mapping(OSRM_CONTAINER, OSRM_HOST_PORT,
                                               OSRM_CONTAINER_PORT)
    osrm_table_size_ok = _container_has_cmd_arg(OSRM_CONTAINER, "--max-table-size")
    osrm_stale = osrm_state == "running" and (not osrm_port_ok or not osrm_table_size_ok)
    if osrm_stale:
        if not osrm_port_ok:
            print(f"  [orchestrator] {OSRM_CONTAINER} běží se špatným port mapping "
                  f"(očekáván {OSRM_HOST_PORT}:{OSRM_CONTAINER_PORT}) → recreate.")
        elif not osrm_table_size_ok:
            print(f"  [orchestrator] {OSRM_CONTAINER} běží bez --max-table-size "
                  f"(default 100 je málo na VRP) → recreate.")

    containers_restarted = False
    expected_ors_rebuild = False

    must_recreate = data_changed or ors_stale or config_changed
    # KRITICKÉ: wipe graph cache jen když je důvod tu cache zahodit:
    #   - nový PBF (data_changed) → graf z starých dat je nesmysl
    #   - změněný config (config_changed) → musí se rebuildovat s novými parametry
    # NE pro ors_stale (port/mount mismatch), tam stačí recreate kontejneru,
    # graph zůstává platný a načte se instantně.
    must_wipe_graphs = data_changed or config_changed

    if must_recreate:
        print()
        if data_changed:
            print("[orchestrator] Nová data → restartuji kontejnery a mažu ORS graph cache.")
        elif config_changed:
            print("[orchestrator] ors-config.yml změněn → recreate ORS kontejneru "
                  "a wipe graph cache (nový config se promítne do nového grafu).")
        else:
            print("[orchestrator] Stará konfigurace kontejneru → recreate "
                  "(graph cache zůstává, načte se instantně).")
        if osrm_state != "absent":
            _stop_and_remove(OSRM_CONTAINER)
        if ors_state != "absent":
            _stop_and_remove(ORS_CONTAINER)
        if must_wipe_graphs:
            _wipe_ors_graphs(data_dir)
        _start_osrm_container(data_dir)
        _start_ors_container(data_dir)
        containers_restarted = True
        expected_ors_rebuild = must_wipe_graphs
    else:
        # Žádný update — jen ujisti že běží se správnou konfigurací
        if osrm_state == "running" and not osrm_stale:
            pass
        elif osrm_state == "absent":
            print(f"[orchestrator] Spouštím {OSRM_CONTAINER}...")
            _start_osrm_container(data_dir)
            containers_restarted = True
        else:
            # exited / created / paused / running+stale → recreate čistě
            reason = "stale port mapping" if osrm_stale else f"stav '{osrm_state}'"
            print(f"[orchestrator] {OSRM_CONTAINER} ({reason}) → restart.")
            _stop_and_remove(OSRM_CONTAINER)
            _start_osrm_container(data_dir)
            containers_restarted = True

        # ors_stale je tady False (jinak bychom byli v `if data_changed or ors_stale ...` větvi)
        if ors_state == "running":
            pass
        elif ors_state == "absent":
            print(f"[orchestrator] Spouštím {ORS_CONTAINER}...")
            _start_ors_container(data_dir)
            containers_restarted = True
            # Pokud cache existuje (named volume nebo legacy host dir), ORS load
            # bude rychlý. Pokud ne, čekání ~5 min. Tady nemáme přímou viditelnost
            # do named volume obsahu, takže nehlásíme "rebuild".
        else:
            print(f"[orchestrator] {ORS_CONTAINER} ve stavu '{ors_state}' → restart.")
            _stop_and_remove(ORS_CONTAINER)
            _start_ors_container(data_dir)
            containers_restarted = True

    # ── 3. Čekání na endpointy ──────────────────────────────────────
    print()
    _wait_http_ok(OSRM_HEALTH_URL, osrm_timeout_s, "OSRM current",
                  container_name=OSRM_CONTAINER)

    if expected_ors_rebuild:
        print(f"  [INFO] ORS staví graph z čerstvého PBF — může trvat ~5 min.")
    _wait_http_ok(ORS_HEALTH_URL, ors_timeout_min * 60, "ORS current",
                  container_name=ORS_CONTAINER)

    print()
    print(f"[orchestrator] Hotovo. OSRM={OSRM_HEALTH_URL.rsplit('/route', 1)[0]}, "
          f"ORS=http://localhost:{ORS_HOST_PORT}")

    pipeline_result["containers_restarted"] = containers_restarted
    return pipeline_result


# ── CLI (debug) ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(
        description="Zajisti že current routing instance (C:\\osrm_current) běží s aktuálními daty.",
    )
    p.add_argument("--skip-update", action="store_true",
                   help="Přeskoč kontrolu/stažení dat — jen řiď kontejnery.")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                   help=f"Cílová složka (default: {DEFAULT_DATA_DIR})")
    p.add_argument("--ors-timeout-min", type=float, default=30.0,
                   help="Timeout čekání na ORS endpoint v minutách (default 30).")
    args = p.parse_args()

    try:
        ensure_fresh_routing_ready(
            args.data_dir,
            skip_update=args.skip_update,
            ors_timeout_min=args.ors_timeout_min,
        )
    except KeyboardInterrupt:
        print("\n[INTR] Přerušeno uživatelem.")
        sys.exit(130)
