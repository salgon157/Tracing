"""
/api/env — READ-ONLY stav routing prostředí (OSRM/ORS instance).

Pingne stable i current instanci a řekne, která odpovídá. URL bere z
produkčního osm_routing.OSM_PRESETS (jediný zdroj pravdy), takže se nikdy
nerozejde se solverem. Nic nespouští ani nerestartuje.

Motivace: běhy teď defaultně jedou na 'current' (5001/8081). Tenhle panel
ukáže PŘED spuštěním, jestli jsou správné kontejnery nahoře — ušetří "proč
to spadlo v 5:00 ráno".
"""

from __future__ import annotations

import urllib.error
import urllib.request

from fastapi import APIRouter

import sys

from . import config

router = APIRouter(prefix="/api/env")

# osm_routing žije v kořeni repa — zajisti, že je na sys.path (server běží
# z kořene, ale testy nemusí).
if str(config.REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(config.REPO_ROOT))
try:
    from osm_routing import DEFAULT_OSM_SOURCE, OSM_PRESETS
except Exception:                       # noqa: BLE001 — když by osm_routing chyběl
    OSM_PRESETS = {}
    DEFAULT_OSM_SOURCE = "current"


def _probe(url: str, timeout: float = 2.0) -> bool:
    """True když endpoint odpovídá (i HTTP 4xx = server žije, jen jiný request)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError:
        return True                      # server odpověděl (byť 4xx) → žije
    except Exception:                    # noqa: BLE001 — connection refused, timeout…
        return False


def _ping_osrm(base: str) -> bool:
    # OSRM: /route/v1/driving/... (na tenhle endpoint umí odpovědět)
    return _probe(f"{base}/route/v1/driving/14.4,50.0;14.5,50.1?overview=false")


def _ping_ors(base: str) -> bool:
    # ORS obsluhuje /ors/v2/*, NE /route/v1 — vlastní health endpoint
    return _probe(f"{base}/ors/v2/health")


@router.get("")
def env() -> dict:
    """Stav obou instancí + která je default pro provozní běhy."""
    instances = []
    for source, preset in OSM_PRESETS.items():
        osrm = preset.get("osrm_urls", {}).get("driving", "")
        ors = preset.get("osrm_urls", {}).get("driving-hgv", "")
        osrm_ok = _ping_osrm(osrm) if osrm else False
        ors_ok = _ping_ors(ors) if ors else False
        instances.append({
            "source":       source,
            "is_default":   source == DEFAULT_OSM_SOURCE,
            "osrm_url":     osrm,
            "ors_url":      ors,
            "osrm_ok":      osrm_ok,
            "ors_ok":       ors_ok,
            "ready":        osrm_ok and ors_ok,
            "start_hint":   preset.get("start_hint", ""),
        })
    default = next((i for i in instances if i["is_default"]), None)
    return {
        "default_source": DEFAULT_OSM_SOURCE,
        "default_ready":  bool(default and default["ready"]),
        "instances":      instances,
    }
