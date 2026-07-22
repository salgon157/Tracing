"""
refresh_osm.py — přestavba ČERSTVÉ routing instance (typicky 1× týdně)

Proč samostatný skript: běhy solveru routing data ZÁMĚRNĚ nestahují ani
nepřestavují. Kdyby to dělaly, jednou za čas by se 30–60minutový rebuild
spustil uprostřed plánování před uzávěrkou. Přestavba proto patří do klidného
okna (neděle večer), ne do provozního běhu.

Co dělá (přes osrm_orchestrator.ensure_fresh_routing_ready):
  1. zkontroluje stáří OSM dat v C:\\osrm_current
  2. je-li starší než 7 dní, stáhne nová z Geofabriku (~880 MB)
  3. přestaví OSRM graf (osrm-extract / partition / customize)
  4. restartuje kontejnery osrm-current (5001) a ors-current (8081)
     a počká, až začnou odpovídat (ORS rebuild grafu i ~30 min)

Stabilní instance (C:\\osrm, porty 5000/8080) se NIKDY nedotkne — to je
zamrzlá mapa pro porovnatelná měření (benchmarky).

Použití:
  python refresh_osm.py                # plná přestavba (týdenní rutina)
  python refresh_osm.py --check        # jen zjisti, jestli jsou data zastaralá
  python refresh_osm.py --skip-update  # jen nastartuj/oprav kontejnery, nestahuj
  python refresh_osm.py --force        # přestav i když jsou data čerstvá

Na serveru: naplánovaná úloha, např. neděle 22:00.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Přestavba čerstvé routing instance (C:\\osrm_current). "
                    "Stable instance zůstává nedotčená.")
    parser.add_argument("--check", action="store_true",
                        help="Jen ověř stáří dat, nic nestahuj ani nepřestavuj")
    parser.add_argument("--skip-update", action="store_true",
                        help="Přeskoč stahování/přestavbu, jen zajisti běžící kontejnery")
    parser.add_argument("--force", action="store_true",
                        help="Přestav i když data ještě nejsou starší než 7 dní")
    args = parser.parse_args()

    t0 = datetime.now()
    print("=" * 64)
    print(f"REFRESH ROUTING (current)  —  start {t0:%Y-%m-%d %H:%M:%S}")
    print("=" * 64)

    from pathlib import Path

    from update_osrm import DEFAULT_DATA_DIR, run_pipeline
    data_dir = Path(DEFAULT_DATA_DIR)

    try:
        if args.check:
            # run_pipeline(check=True) jen ohlásí stáří dat, nic nestahuje
            result = run_pipeline(data_dir, check=True)
            print("\nNic se nestahovalo ani nepřestavovalo (--check).")
        elif args.force:
            # Vynucená přestavba + restart kontejnerů (ensure_* force nezná)
            from osrm_orchestrator import ensure_fresh_routing_ready
            result = run_pipeline(data_dir, force=True)
            print("\nData přestavěna, restartuji kontejnery...")
            result = ensure_fresh_routing_ready(skip_update=True)
        else:
            from osrm_orchestrator import ensure_fresh_routing_ready
            result = ensure_fresh_routing_ready(skip_update=args.skip_update)
    except KeyboardInterrupt:
        sys.exit("\n[INTR] Přerušeno. Stav je uložen — příští běh naváže.")

    elapsed = (datetime.now() - t0).total_seconds() / 60
    print("\n" + "=" * 64)
    print(f"HOTOVO za {elapsed:.1f} min")
    if isinstance(result, dict):
        for key in ("phase", "data_updated", "containers_restarted"):
            if key in result:
                print(f"  {key}: {result[key]}")
    print("\nProvozní běhy (solver, predict_day) teď jedou na čerstvé mapě.")
    print("Benchmarky zůstávají na stable (zamrzlá mapa) — to je záměr.")


if __name__ == "__main__":
    main()
