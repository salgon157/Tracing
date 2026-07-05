"""
update_osrm.py — Stáhne čerstvá OSM data z Geofabriku a připraví je pro routing
================================================================================

Cílová instance: C:\\osrm_current  (paralelní k stabilní C:\\osrm, která se NIKDY
neaktualizuje touto cestou).

Workflow:
  1. HEAD request na Geofabrik → zjisti Last-Modified vzdáleného PBF
  2. Pokud máme stejný PBF (per metadata) → "Data jsou aktuální, z [datum]" a end
  3. Jinak stáhni .osm.pbf + .md5, ověř MD5
  4. Spusť osrm-extract / osrm-partition / osrm-customize přes Docker
  5. Pokud chybí ors-config.yml, zkopíruj z C:\\osrm
  6. Vypiš upozornění o restartu ors-current kontejneru

Stavový soubor C:\\osrm_current\\update_state.json udržuje fázi, takže po crashi
lze pokračovat tam, kde jsme skončili (download → extract → partition → customize).

Použití:
  python update_osrm.py             # plná aktualizace pokud potřeba
  python update_osrm.py --check     # jen vypis aktualnost, exit 0
  python update_osrm.py --skip-osrm # jen download + MD5, bez Dockeru (debug)
  python update_osrm.py --force     # přeprocesuje i když "ready"
  python update_osrm.py --data-dir D:\\test  # override C:\\osrm_current
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

# UTF-8 stdout na Windows konzoli
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# ── Konfigurace ──────────────────────────────────────────────────────────────

GEOFABRIK_BASE = "https://download.geofabrik.de/europe"
PBF_NAME       = "czech-republic-latest.osm.pbf"
OSRM_NAME      = "czech-republic-latest.osrm"   # výstup z osrm-extract

# Override pro testy (např. malý liechtenstein PBF)
GEOFABRIK_PBF_URL = os.environ.get(
    "GEOFABRIK_PBF_URL", f"{GEOFABRIK_BASE}/{PBF_NAME}"
)
GEOFABRIK_MD5_URL = GEOFABRIK_PBF_URL + ".md5"

DEFAULT_DATA_DIR = r"C:\osrm_current"
STABLE_DATA_DIR  = r"C:\osrm"             # NIKDY tam nezasahovat — jen čteme ors-config.yml

DOCKER_IMG       = "osrm/osrm-backend"
OSRM_PROFILE_LUA = "/opt/car.lua"

STATE_FILE_NAME  = "update_state.json"
ORS_CONFIG_NAME  = "ors-config.yml"

# Maximální stáří lokálních OSM dat před stažením nových (ve dnech)
MAX_DATA_AGE_DAYS = 7

# Fáze v stavovém souboru
PHASE_INIT        = "init"
PHASE_DOWNLOADING = "downloading"
PHASE_DOWNLOADED  = "downloaded"
PHASE_EXTRACTED   = "extracted"
PHASE_PARTITIONED = "partitioned"
PHASE_READY       = "ready"


# ── Stavový soubor ───────────────────────────────────────────────────────────

def load_state(data_dir: Path) -> dict:
    state_path = data_dir / STATE_FILE_NAME
    if not state_path.exists():
        return {"phase": PHASE_INIT}
    try:
        with open(state_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print(f"[WARN] Poškozený {state_path} — startuji od nuly.")
        return {"phase": PHASE_INIT}


def save_state(data_dir: Path, state: dict) -> None:
    state_path = data_dir / STATE_FILE_NAME
    tmp = state_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path)


def update_phase(data_dir: Path, state: dict, new_phase: str, **extra) -> dict:
    state["phase"] = new_phase
    state.update(extra)
    save_state(data_dir, state)
    return state


# ── Geofabrik metadata ───────────────────────────────────────────────────────

def head_remote_pbf() -> dict:
    """Vrátí dict s 'last_modified', 'content_length', 'date_short' (YYYY-MM-DD)."""
    r = requests.head(GEOFABRIK_PBF_URL, allow_redirects=True, timeout=15)
    if r.status_code != 200:
        raise SystemExit(
            f"\n[CHYBA] Geofabrik vrátil HTTP {r.status_code} pro {GEOFABRIK_PBF_URL}\n"
            f"        Zkus později nebo zkontroluj URL."
        )
    last_modified = r.headers.get("Last-Modified", "")
    content_length = int(r.headers.get("Content-Length", "0"))
    date_short = ""
    if last_modified:
        try:
            dt = parsedate_to_datetime(last_modified)
            date_short = dt.strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass
    return {
        "last_modified": last_modified,
        "content_length": content_length,
        "date_short": date_short,
    }


def fmt_http_date(http_date: str) -> str:
    """'Fri, 24 Apr 2026 03:14:22 GMT' → '2026-04-24 (Fri 03:14 UTC)'."""
    if not http_date:
        return "(neznámé)"
    try:
        dt = parsedate_to_datetime(http_date)
        return dt.strftime("%Y-%m-%d (%a %H:%M UTC)")
    except (TypeError, ValueError):
        return http_date


# ── Download ─────────────────────────────────────────────────────────────────

def download_pbf(data_dir: Path, expected_size: int = 0) -> Path:
    """Stáhne PBF s progress barem. Vrátí cestu k finálnímu souboru."""
    final = data_dir / PBF_NAME
    partial = data_dir / (PBF_NAME + ".partial")
    if partial.exists():
        partial.unlink()  # nedůvěřujeme částečnému downloadu

    r = requests.get(GEOFABRIK_PBF_URL, stream=True, timeout=60)
    r.raise_for_status()
    total = int(r.headers.get("Content-Length", expected_size or 0))
    total_mb = total / (1024 * 1024)

    chunk_size = 1024 * 1024  # 1 MB
    downloaded = 0
    last_print = 0.0
    t_start = time.time()

    with open(partial, "wb") as f:
        for chunk in r.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)

            now = time.time()
            if now - last_print >= 0.5 or downloaded == total:
                mb = downloaded / (1024 * 1024)
                pct = (downloaded / total * 100) if total else 0
                speed = downloaded / max(0.001, now - t_start) / (1024 * 1024)
                if total:
                    print(f"\r  Stahuji: {mb:6.0f} / {total_mb:6.0f} MB "
                          f"({pct:5.1f}%) — {speed:5.1f} MB/s",
                          end="", flush=True)
                else:
                    print(f"\r  Stahuji: {mb:6.0f} MB — {speed:5.1f} MB/s",
                          end="", flush=True)
                last_print = now

    print()  # newline po progress baru
    os.replace(partial, final)
    return final


def download_md5(data_dir: Path) -> str | None:
    """Stáhne .md5 soubor; vrátí hex hash nebo None pokud chybí."""
    try:
        r = requests.get(GEOFABRIK_MD5_URL, timeout=15)
        if r.status_code != 200:
            print(f"  [WARN] MD5 soubor vrátil HTTP {r.status_code} — přeskakuji ověření.")
            return None
    except requests.exceptions.RequestException as e:
        print(f"  [WARN] MD5 download selhal ({e}) — přeskakuji ověření.")
        return None

    md5_path = data_dir / (PBF_NAME + ".md5")
    md5_path.write_text(r.text, encoding="ascii")
    # Formát: '<hex>  filename'
    parts = r.text.strip().split()
    return parts[0] if parts else None


def verify_md5(pbf_path: Path, expected_hex: str) -> bool:
    print("  Verifikuji MD5 (může chvíli trvat — soubor je ~1 GB)...")
    h = hashlib.md5()
    with open(pbf_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    got = h.hexdigest()
    if got.lower() != expected_hex.lower():
        print(f"  [CHYBA] MD5 nesedí — očekávám {expected_hex}, mám {got}")
        return False
    print("  MD5 OK")
    return True


# ── Docker ───────────────────────────────────────────────────────────────────

def _docker_volume_arg(host_dir: Path) -> str:
    """Sestaví -v argument pro Docker Desktop na Windows.
    Použijeme str(host_dir) což zachová C:\\osrm_current formát.
    """
    return f"{str(host_dir)}:/data"


def docker_run(data_dir: Path, *cmd: str) -> None:
    """Wrapper na 'docker run --rm -v DATA:/data IMAGE CMD...'."""
    full_cmd = [
        "docker", "run", "--rm",
        "-v", _docker_volume_arg(data_dir),
        DOCKER_IMG,
        *cmd,
    ]
    print(f"  $ {' '.join(full_cmd)}")
    res = subprocess.run(full_cmd)
    if res.returncode != 0:
        raise SystemExit(
            f"\n[CHYBA] Docker příkaz selhal (returncode={res.returncode}).\n"
            f"        Zkontroluj že běží Docker Desktop a image '{DOCKER_IMG}' existuje:\n"
            f"        docker pull {DOCKER_IMG}"
        )


def osrm_extract(data_dir: Path) -> None:
    print("[1/3] OSRM extract...")
    docker_run(data_dir, "osrm-extract", "-p", OSRM_PROFILE_LUA, f"/data/{PBF_NAME}")


def osrm_partition(data_dir: Path) -> None:
    print("[2/3] OSRM partition...")
    docker_run(data_dir, "osrm-partition", f"/data/{OSRM_NAME}")


def osrm_customize(data_dir: Path) -> None:
    print("[3/3] OSRM customize...")
    docker_run(data_dir, "osrm-customize", f"/data/{OSRM_NAME}")


# ── ORS bootstrap ────────────────────────────────────────────────────────────

def _ensure_elevation_disabled(config_path: Path) -> bool:
    """
    Zajisti že ors-config.yml má elevation vypnutou (idempotentní).

    Důvod: GraphHopper jinak při graph buildu stahuje SRTM výškové dlaždice
    z internetu (`MultiSourceElevationProvider`). Když je server pomalý nebo
    blokovaný firewallem, ORS thread visí na `SSLSocketInputRecord.read`
    klidně hodiny. Pro current instanci výšku nepotřebujeme — VRP solver
    používá time_multiplier per vozidlo, ne nadmořskou výšku.

    Politika:
      - Pokud uživatel JAKKOLIV explicitně nastavil `elevation:` → respektujeme
      - Pokud klíč neexistuje → přidáme `elevation: false` pod sekci `build:`

    Vrací True pokud byla provedena změna v souboru.
    """
    content = config_path.read_text(encoding="utf-8")
    # Respektuj uživatelovu explicitní volbu
    if re.search(r'^\s*elevation:\s*\w+', content, re.MULTILINE):
        return False
    # Přidej `elevation: false` jako první řádek pod `build:`
    new_content, n = re.subn(
        r'^(\s*)build:\s*\n',
        lambda m: f"{m.group(0)}{m.group(1)}  elevation: false\n",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 0:
        print(f"  [WARN] {config_path.name} nemá sekci 'build:' — "
              f"elevation neuspěla automaticky. Přidej ručně 'elevation: false'.")
        return False
    config_path.write_text(new_content, encoding="utf-8")
    print(f"  ors-config: přidán 'elevation: false' (zabráníme stahování SRTM tiles)")
    return True


def seed_ors_config(data_dir: Path) -> bool:
    """
    Zajisti že ors-config.yml v C:\\osrm_current existuje a má vhodné nastavení.

      - Pokud chybí, zkopíruj z C:\\osrm
      - Vždy idempotentně enforce `elevation: false` (current nemá persistent
        elevation cache, GraphHopper by jinak stahoval SRTM tiles z internetu)

    Vrací True pokud byla provedena změna (kopírování nebo úprava).
    """
    target = data_dir / ORS_CONFIG_NAME
    changed = False
    if not target.exists():
        source = Path(STABLE_DATA_DIR) / ORS_CONFIG_NAME
        if not source.exists():
            print(f"  [WARN] {source} neexistuje — ors-config.yml musíš dodat ručně.")
            return False
        shutil.copy2(source, target)
        print(f"  ors-config.yml zkopírován z {source}")
        changed = True
    if _ensure_elevation_disabled(target):
        changed = True
    return changed


# ── PBF aliasy ───────────────────────────────────────────────────────────────

def ensure_pbf_aliases(data_dir: Path) -> None:
    """
    Zajisti že všechny PBF aliasy odkazované v ors-config.yml ukazují na aktuální PBF.

    Po každém stažení nového PBF přes os.replace() se NTFS hardlink (např. osm_file.pbf)
    odpojí od nových dat — musíme ho smazat a vytvořit znovu.

    Parsuje ors-config.yml textově (bez yaml dependency), hledá 'source_file:' řádky.
    Pro každý alias-název (≠ PBF_NAME) smaže stávající soubor a vytvoří nový hardlink.
    Fallback na shutil.copy2 pokud hardlink není podporován (cross-device nebo jiný FS).
    """
    config_path = data_dir / ORS_CONFIG_NAME
    pbf_source  = data_dir / PBF_NAME
    if not config_path.exists() or not pbf_source.exists():
        return
    with open(config_path, encoding="utf-8") as f:
        content = f.read()
    for m in re.finditer(r'source_file:\s*(\S+)', content):
        ref = m.group(1).strip()
        alias_name = Path(ref).name
        if alias_name == PBF_NAME:
            continue  # stejný název, alias nepotřeba
        alias_path = data_dir / alias_name
        if alias_path.exists() or alias_path.is_symlink():
            try:
                alias_path.unlink()
            except OSError as e:
                print(f"  [WARN] Nepodařilo se smazat starý alias {alias_name}: {e}")
                continue
        try:
            os.link(pbf_source, alias_path)
            print(f"  PBF alias (hardlink): {alias_name} → {PBF_NAME}")
        except OSError:
            try:
                shutil.copy2(pbf_source, alias_path)
                print(f"  PBF alias (kopie): {alias_name} → {PBF_NAME}")
            except OSError as e:
                print(f"  [WARN] Nepodařilo se vytvořit alias {alias_name}: {e}")


# ── Bezpečnostní guard ───────────────────────────────────────────────────────

def assert_not_stable_dir(data_dir: Path) -> None:
    """Zajisti že nikdy nepíšeme do C:\\osrm. Hard rule."""
    try:
        resolved = data_dir.resolve()
        stable = Path(STABLE_DATA_DIR).resolve()
        if resolved == stable:
            raise SystemExit(
                f"\n[CHYBA] Cílová složka je stabilní instance ({stable}).\n"
                f"        Tento skript NIKDY nepíše do C:\\osrm.\n"
                f"        Použij --data-dir s jinou cestou."
            )
    except (OSError, RuntimeError):
        pass  # nemůžeme resolvovat → composability check failed, ale to je v pořádku


# ── Hlavní pipeline ──────────────────────────────────────────────────────────

def _pbf_age_days(local_lm: str) -> int | None:
    """Vrátí věk lokálních OSM dat ve dnech, nebo None pokud nelze parsovat."""
    if not local_lm:
        return None
    try:
        dt = parsedate_to_datetime(local_lm)
        return (datetime.now(timezone.utc) - dt).days
    except (TypeError, ValueError):
        return None


def needs_update(state: dict, remote_meta: dict, force: bool) -> bool:
    """
    Vrátí True pokud je potřeba stáhnout nová data.

    Podmínky pro stažení:
      - force=True (vždy)
      - phase != ready (nedokončené zpracování)
      - lokální OSM data jsou starší než MAX_DATA_AGE_DAYS dní
      - datum lokálních dat nelze určit
    """
    if force:
        return True
    if state.get("phase") != PHASE_READY:
        return True  # nedokončené zpracování → musíme dokončit
    local_lm = state.get("pbf_last_modified_http", "")
    age = _pbf_age_days(local_lm)
    if age is None:
        return True  # nelze určit věk → bezpečně aktualizuj
    return age >= MAX_DATA_AGE_DAYS


def run_pipeline(
    data_dir: Path,
    *,
    check: bool = False,
    skip_osrm: bool = False,
    force: bool = False,
    print_post_hint: bool = True,
) -> dict:
    """
    Programmatic entry point — volá CLI `main()` i osrm_orchestrator.

    Vrací dict:
      {
        "updated": bool,         # True pokud se reálně stahovalo / processovalo
        "data_changed": bool,    # True pokud byl PBF přepsán novou verzí
        "config_changed": bool,  # True pokud byl ors-config.yml upraven
        "date_short": str,       # YYYY-MM-DD pro reporting (lokální nebo nový)
        "phase": str,            # poslední fáze v stavovém souboru
        "remote_last_modified": str,
      }
    """
    print("=" * 62)
    print(" OSM update — czech-republic-latest")
    print("=" * 62)

    data_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(data_dir)
    prev_pbf_lm = state.get("pbf_last_modified_http", "")

    # ── 1. Zjisti co je vzdáleně ────────────────────────────────────
    print("Kontroluji Geofabrik...")
    remote = head_remote_pbf()
    age = _pbf_age_days(prev_pbf_lm)
    age_str = f"  ({age} dní)" if age is not None else ""
    print(f"  Lokální PBF:    {fmt_http_date(prev_pbf_lm)}{age_str}")
    print(f"  Vzdálené PBF:   {fmt_http_date(remote['last_modified'])}")

    result = {
        "updated": False,
        "data_changed": False,
        "config_changed": False,
        "date_short": remote["date_short"],
        "phase": state.get("phase", PHASE_INIT),
        "remote_last_modified": remote["last_modified"],
    }

    # ── --check mód ─────────────────────────────────────────────────
    if check:
        if state.get("phase") == PHASE_READY:
            if age is not None and age < MAX_DATA_AGE_DAYS:
                print(f"Data jsou aktuální ({age} dní < {MAX_DATA_AGE_DAYS}), z {fmt_http_date(prev_pbf_lm)}")
            else:
                days_info = f"{age} dní" if age is not None else "věk neznámý"
                print(f"Data jsou stará ({days_info} ≥ {MAX_DATA_AGE_DAYS}) — "
                      f"spusť bez --check pro aktualizaci")
        else:
            print(f"K dispozici novější data z {remote['date_short'] or '?'} "
                  f"(spusť bez --check pro aktualizaci)")
        return result

    # ── 2. Rozhodni jestli pokračovat ───────────────────────────────
    if not needs_update(state, remote, force):
        print(f"Data jsou aktuální ({age} dní < {MAX_DATA_AGE_DAYS}), z {fmt_http_date(prev_pbf_lm)}")
        # I když nestahujeme, zajisti konzistenci konfigurace (idempotentní)
        if seed_ors_config(data_dir):
            result["config_changed"] = True
        ensure_pbf_aliases(data_dir)
        return result

    # ── 3. Stáhni PBF (pokud potřeba) ───────────────────────────────
    pbf_path = data_dir / PBF_NAME
    pbf_already_current = (
        prev_pbf_lm == remote["last_modified"]
        and pbf_path.exists()
        and state.get("phase") in (PHASE_DOWNLOADED, PHASE_EXTRACTED,
                                   PHASE_PARTITIONED, PHASE_READY)
    )
    if pbf_already_current and not force:
        print(f"PBF je již stažen, pokračuji od přerušené fáze ({state.get('phase')}).")
    else:
        print(f"Stažena nová data z {remote['date_short'] or '?'}")
        update_phase(data_dir, state, PHASE_DOWNLOADING,
                     pbf_last_modified_http=remote["last_modified"])

        download_pbf(data_dir, expected_size=remote["content_length"])

        expected_md5 = download_md5(data_dir)
        if expected_md5:
            if not verify_md5(pbf_path, expected_md5):
                pbf_path.unlink(missing_ok=True)
                raise SystemExit("[CHYBA] MD5 nesedí — soubor smazán, zkus znovu.")
            state["pbf_md5"] = expected_md5

        update_phase(data_dir, state, PHASE_DOWNLOADED,
                     pbf_size_bytes=pbf_path.stat().st_size,
                     pbf_downloaded_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))
        result["data_changed"] = True

    # ── --skip-osrm mód ─────────────────────────────────────────────
    if skip_osrm:
        print("\n[--skip-osrm] Docker zpracování přeskočeno.")
        print(f"PBF připraven: {pbf_path}")
        result["updated"] = True
        result["phase"] = state.get("phase", PHASE_DOWNLOADED)
        return result

    # ── 4. OSRM zpracování (resume podle fáze) ──────────────────────
    phase = state.get("phase")
    if phase in (PHASE_DOWNLOADED, PHASE_INIT) or force:
        osrm_extract(data_dir)
        update_phase(data_dir, state, PHASE_EXTRACTED)
        phase = PHASE_EXTRACTED

    if phase == PHASE_EXTRACTED:
        osrm_partition(data_dir)
        update_phase(data_dir, state, PHASE_PARTITIONED)
        phase = PHASE_PARTITIONED

    if phase == PHASE_PARTITIONED:
        osrm_customize(data_dir)
        update_phase(data_dir, state, PHASE_READY,
                     osrm_built_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))

    # ── 5. ORS bootstrap ────────────────────────────────────────────
    if seed_ors_config(data_dir):
        result["config_changed"] = True
    ensure_pbf_aliases(data_dir)

    # ── 6. Hotovo ───────────────────────────────────────────────────
    print()
    print(f"Hotovo. {data_dir} je připraven.")

    if print_post_hint:
        print()
        print(f"[OZN] PBF byl aktualizován. Pro plnou aktualizaci ORS smaž")
        print(f"      {data_dir}\\graphs\\ a restartuj ORS kontejner:")
        print(f"      scripts\\stop_osrm_current.bat")
        print(f"      rmdir /S /Q {data_dir}\\graphs")
        print(f"      scripts\\start_osrm_current.bat")

    result["updated"] = True
    result["phase"] = PHASE_READY
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stáhne čerstvá Geofabrik OSM data a připraví C:\\osrm_current",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Příklady:\n"
            "  python update_osrm.py             # plná aktualizace pokud potřeba\n"
            "  python update_osrm.py --check     # jen vypiš stav, neměň nic\n"
            "  python update_osrm.py --skip-osrm # download bez Docker zpracování\n"
            "  python update_osrm.py --force     # přeprocesuj i když ready\n"
        ),
    )
    parser.add_argument("--check", action="store_true",
                        help="Vypiš lokální vs vzdálený stav, nestahuj nic.")
    parser.add_argument("--skip-osrm", action="store_true",
                        help="Stáhni a verifikuj PBF, ale přeskoč Docker (debug).")
    parser.add_argument("--force", action="store_true",
                        help="Přeprocesuj i když data jsou aktuální.")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help=f"Cílová složka (default: {DEFAULT_DATA_DIR})")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_dir = Path(args.data_dir)
    assert_not_stable_dir(data_dir)
    try:
        run_pipeline(
            data_dir,
            check=args.check,
            skip_osrm=args.skip_osrm,
            force=args.force,
        )
    except KeyboardInterrupt:
        print("\n[INTR] Přerušeno uživatelem. Stav uložen — při dalším běhu se naváže.")
        sys.exit(130)


if __name__ == "__main__":
    main()
