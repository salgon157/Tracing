r"""
osm_routing.py — Centrální definice URL pro routing instance (OSRM/ORS)
========================================================================

Projekt používá DVĚ paralelní routing instance běžící současně na různých portech:

  ┌──────────┬──────────────────┬───────────┬──────────┬───────────────────────┐
  │ Preset   │ Folder           │ OSRM port │ ORS port │ Spravuje              │
  ├──────────┼──────────────────┼───────────┼──────────┼───────────────────────┤
  │ stable   │ C:\osrm          │   5000    │   8080   │ uživatel ručně        │
  │ current  │ C:\osrm_current  │   5001    │   8081   │ skript update_osrm.py │
  └──────────┴──────────────────┴───────────┴──────────┴───────────────────────┘

CLI vstupní body (solver, benchmark, visualizer) přidají argument `--fresh-osm`
přes `add_osm_args(parser)`. V `main()` se po parse_args() jednou zavolá
`apply_osm_source(CONFIG, "current" if args.fresh_osm else "stable")`, což
mutuje `CONFIG["osrm_url"]` a `CONFIG["osrm_urls"]`. Žádné URL se v hot-path
nerozhoduje — všichni callers (closures_utils, get_matrix, …) čtou z CONFIG.

Důvod existence: jediné místo, kde se URL definují. Při změně portu nebo
přidání třetí instance se mění pouze tento soubor.
"""

OSM_PRESETS = {
    "stable": {
        "osrm_url": "http://localhost:5000",
        "osrm_urls": {
            "driving":     "http://localhost:5000",
            "driving-hgv": "http://localhost:8080",
        },
        "start_hint": r"docker start osrm-server ors-hgv"
                      r"  (nebo scripts\start_osrm_stable.bat)",
    },
    "current": {
        "osrm_url": "http://localhost:5001",
        "osrm_urls": {
            "driving":     "http://localhost:5001",
            "driving-hgv": "http://localhost:8081",
        },
        "start_hint": "docker start osrm-current ors-current"
                      "  (data přestavíš přes: python refresh_osm.py)",
    },
}

# Výchozí zdroj pro PROVOZNÍ běhy (denní plán, predikce): čerstvá mapa.
# Benchmarky si explicitně volí "stable" — zamrzlá mapa je tam žádoucí,
# aby byla měření výkonnosti algoritmu porovnatelná napříč časem.
DEFAULT_OSM_SOURCE = "current"


def apply_osm_source(config: dict, source: str) -> None:
    """
    Mutuje config['osrm_url'] a config['osrm_urls'] podle zvoleného presetu.

    Parametry:
      config — slovník (typicky CONFIG ze solveru), který se mění in-place
      source — "stable" nebo "current"

    Vyhodí KeyError pokud source není známý preset (zachytí překlep dřív).
    """
    if source not in OSM_PRESETS:
        raise KeyError(
            f"Neznámý OSM preset: {source!r}. Dostupné: {list(OSM_PRESETS)}"
        )
    preset = OSM_PRESETS[source]
    config["osrm_url"] = preset["osrm_url"]
    config["osrm_urls"] = dict(preset["osrm_urls"])  # mělká kopie pro izolaci


def add_osm_args(parser, default: str = DEFAULT_OSM_SOURCE) -> None:
    """
    Přidá volbu routing instance.

    `default` si volí každý skript sám:
      - provozní (solver, predikce)  → "current" (čerstvá mapa)
      - benchmarky                   → "stable"  (zamrzlá, porovnatelná měření)

    Běh routing data NIKDY nestahuje ani nepřestavuje — jen použije instanci,
    která běží. Přestavba je samostatný krok: `python refresh_osm.py`.
    """
    parser.add_argument(
        "--osm-source",
        choices=sorted(OSM_PRESETS),
        default=default,
        help=f"Routing instance (default: {default}). "
             "'current' = čerstvá mapa, porty 5001/8081, C:\\osrm_current. "
             "'stable' = zamrzlá mapa, porty 5000/8080, C:\\osrm — pro "
             "porovnatelná měření a reprodukci starších výsledků.",
    )
    parser.add_argument(
        "--fresh-osm",
        action="store_true",
        help="ZASTARALÉ — alias pro --osm-source current (což je i default "
             "u provozních běhů). Ponecháno kvůli zpětné kompatibilitě.",
    )


def resolve_osm_source(args) -> str:
    """Zdroj z argumentů: --fresh-osm (zastaralý alias) přebíjí --osm-source."""
    if getattr(args, "fresh_osm", False):
        return "current"
    return getattr(args, "osm_source", DEFAULT_OSM_SOURCE)


def start_hint(source: str) -> str:
    """Jak nastartovat kontejnery dané instance (do chybových hlášek)."""
    return OSM_PRESETS.get(source, {}).get("start_hint", "spusť routing kontejnery")
