"""
closures_utils.py — detekce a aplikace uzavírek na matice
=========================================================
Importováno solverem (v6) po fázi OSRM/ORS.

Architektura uzavírek (v3 — exact ORS):
  1. Najdi kandidátní O→D páry, jejichž přímka prochází blízko uzavírky.
  2. Pro KAŽDÝ kandidátní pár spočítej přesný overhead přes ORS
     avoid_polygon (baseline vs detour — 2 volání per pár, paralelně).
  3. Aktualizuj ČAS i VZDÁLENOST v matici.

  Žádné probe-body, žádné směrové odhady, žádný near/far split.
  Solver dostane přesné realistické časy a vzdálenosti objízdky.
"""

import json
import math
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import numpy as np
import requests

CLOSURES_FILE = Path("data/static/closures.json")

OSRM_URL_DEFAULT = "http://localhost:5000"
ORS_URL_DEFAULT = "http://localhost:8080"
ORS_PROFILE_DEFAULT = "driving-hgv"
ROUTE_CONFIRM_WORKERS = 8
DEPOT_NEAR_KM = 4.0
ENDPOINT_NEAR_MIN_KM = 0.35
ROUTE_TIMEOUT_SEC = 20
DEPOT_LAT = 49.5061806
DEPOT_LON = 15.5950131
DEPOT_MATCH_KM = 0.2
DEPOT_FALLBACK_PROBE_KM = (2.0, 4.0)
DEPOT_ANCHOR_TARGET_KM = (0.5, 0.9, 1.4, 2.2, 3.2)
OSRM_ROUTE_PROFILES = {"driving"}
DEFAULT_CLOSURE_ROUTE_PROFILES = {
    "driving": "driving-hgv",
    "driving-hgv": "driving-hgv",
}
CLOSURE_BUFFER_M    = 80     # šířka avoid_polygon pásma (metry na každou stranu)
ORS_WORKERS         = 8      # paralelní ORS vlákna

# Zachováno pro closure_map_editor.py (vizualizace bypass-tras)
PROBE_DIST_KM = 4.0
PROBE_PAIRS   = [(0, 180), (45, 225), (90, 270), (135, 315)]


# ============================================================
#  NAČTENÍ UZAVÍREK
# ============================================================

def load_active_closures(path=None) -> list:
    """Vrátí seznam uzavírek aktivních pro dnešní datum."""
    p = Path(path) if path else CLOSURES_FILE
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    today = str(date.today())
    result = []
    for c in data.get("closures", []):
        if not c.get("active"):
            continue
        if c.get("valid_from") and c["valid_from"] > today:
            continue
        if c.get("valid_to") and c["valid_to"] < today:
            continue
        result.append(c)
    return result


# ============================================================
#  GEOMETRICKÉ POMOCNÉ FUNKCE
# ============================================================

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Vzdálenost dvou GPS bodů v km (Haversine)."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(min(1.0, a)))


def point_to_segment_km(plat: float, plon: float,
                         alat: float, alon: float,
                         blat: float, blon: float) -> float:
    """Nejkratší vzdálenost bodu P od úsečky AB v km."""
    dx = blon - alon
    dy = blat - alat
    if dx == 0 and dy == 0:
        return haversine_km(plat, plon, alat, alon)
    t = max(0.0, min(1.0,
        ((plon - alon) * dx + (plat - alat) * dy) / (dx * dx + dy * dy)
    ))
    cx = alon + t * dx
    cy = alat + t * dy
    return haversine_km(plat, plon, cy, cx)


def segment_to_segment_min_km(olat: float, olon: float,
                               dlat: float, dlon: float,
                               alat: float, alon: float,
                               blat: float, blon: float) -> float:
    """
    Minimální vzdálenost mezi úsečkou O→D a úsečkou A→B (uzavírka) v km.

    Postup:
      1. Test průsečíku (2D algebraicky v lat/lon) → vrátí 0 pokud se kříží.
      2. Jinak minimum ze 4 vzdáleností bod→úsečka (obě kombinace koncových bodů).

    Dostatečně přesné pro pre-filter; lat/lon se chovají jako Kartézské
    souřadnice na malých vzdálenostech (< 200 km).
    """
    rx, ry = dlat - olat, dlon - olon   # vektor O→D
    sx, sy = blat - alat, blon - alon   # vektor A→B
    rxs = rx * sy - ry * sx             # cross product (2D skalár)
    qpx, qpy = alat - olat, alon - olon

    if abs(rxs) > 1e-15:                # segmenty nejsou souběžné
        t = (qpx * sy - qpy * sx) / rxs
        u = (qpx * ry - qpy * rx) / rxs
        if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
            return 0.0                  # segmenty se protínají → vzdálenost 0

    return min(
        point_to_segment_km(alat, alon, olat, olon, dlat, dlon),
        point_to_segment_km(blat, blon, olat, olon, dlat, dlon),
        point_to_segment_km(olat, olon, alat, alon, blat, blon),
        point_to_segment_km(dlat, dlon, alat, alon, blat, blon),
    )


def might_cross_closure(loc_from: tuple, loc_to: tuple,
                         closure: dict, pre_factor: float = 6.0) -> bool:
    """
    Rychlá předběžná kontrola (bez HTTP):
    leží přímá úsečka O→D blízko uzavírky?

    Používá přesnou min-vzdálenost dvou úseček — žádné vzorkování, žádné
    vynechání kvůli hrubé mřížce. Minimální buffer 0.2 km.
    """
    seg = closure["segment"]
    buf = max(closure.get("buffer_km", 0.15) * pre_factor, 0.2)
    alat, alon = seg["from"]["lat"], seg["from"]["lon"]
    blat, blon = seg["to"]["lat"],   seg["to"]["lon"]

    return segment_to_segment_min_km(
        loc_from[0], loc_from[1], loc_to[0], loc_to[1],
        alat, alon, blat, blon
    ) < buf


def geometry_crosses_closure(geometry: list, closure: dict,
                              detection_buf_km: float | None = None) -> bool:
    """
    Vrátí True pokud alespoň jeden bod geometrie leží
    do vzdálenosti buf od uzavřeného úseku.

    detection_buf_km: explicitní buffer pro detekci. Pokud None, použije se
    max(closure.buffer_km, 0.06) — min 60m, aby OSRM geometrie (body á 30–80 m)
    spolehlivě zachytila i krátké uzavírky (CLO_004 = 15m segment).
    """
    seg = closure["segment"]
    buf = detection_buf_km if detection_buf_km is not None else max(closure.get("buffer_km", 0.15), 0.06)
    alat, alon = seg["from"]["lat"], seg["from"]["lon"]
    blat, blon = seg["to"]["lat"],   seg["to"]["lon"]

    for lat, lon in geometry:
        if point_to_segment_km(lat, lon, alat, alon, blat, blon) <= buf:
            return True
    return False


def endpoint_near_closure(loc: tuple, closure: dict,
                          factor: float = 6.0,
                          min_km: float = ENDPOINT_NEAR_MIN_KM) -> bool:
    """Conservative endpoint-near-closure check used in broad candidate capture."""
    seg = closure["segment"]
    alat, alon = seg["from"]["lat"], seg["from"]["lon"]
    blat, blon = seg["to"]["lat"], seg["to"]["lon"]
    near_km = max(closure.get("buffer_km", 0.15) * factor, min_km)
    return point_to_segment_km(loc[0], loc[1], alat, alon, blat, blon) <= near_km


def closure_near_location(loc: tuple, closure: dict,
                          near_km: float = DEPOT_NEAR_KM) -> bool:
    """Returns True when a closure lies near a reference location such as the depot."""
    seg = closure["segment"]
    alat, alon = seg["from"]["lat"], seg["from"]["lon"]
    blat, blon = seg["to"]["lat"], seg["to"]["lon"]
    return point_to_segment_km(loc[0], loc[1], alat, alon, blat, blon) <= near_km


def route_geometry_hits_closures(geometry: list, closures: list) -> list:
    """Returns closure ids intersected by the supplied route geometry."""
    hit_ids = []
    for closure in closures:
        if geometry_crosses_closure(geometry, closure):
            hit_ids.append(closure["id"])
    return hit_ids


def _is_depot_point(latlon: tuple) -> bool:
    return haversine_km(latlon[0], latlon[1], DEPOT_LAT, DEPOT_LON) <= DEPOT_MATCH_KM


def _geometry_length_km(geometry: list) -> float:
    if len(geometry) < 2:
        return 0.0
    total = 0.0
    for idx in range(1, len(geometry)):
        total += haversine_km(
            geometry[idx - 1][0], geometry[idx - 1][1],
            geometry[idx][0], geometry[idx][1],
        )
    return total


def _slice_route(route: dict, end_index: int) -> dict | None:
    geometry = route.get("geometry", [])
    if not geometry or end_index <= 0:
        return None
    end_index = min(end_index, len(geometry) - 1)
    sub_geometry = geometry[:end_index + 1]
    geom_total = _geometry_length_km(geometry)
    geom_sub = _geometry_length_km(sub_geometry)
    if geom_total <= 0:
        ratio = 1.0
    else:
        ratio = max(0.0, min(1.0, geom_sub / geom_total))
    return _route_dict(
        route["duration_min"] * ratio,
        route["distance_km"] * ratio,
        sub_geometry,
    )


def _slice_route_from(route: dict, start_index: int) -> dict | None:
    geometry = route.get("geometry", [])
    if not geometry or start_index >= len(geometry) - 1:
        return None
    start_index = max(0, start_index)
    sub_geometry = geometry[start_index:]
    geom_total = _geometry_length_km(geometry)
    geom_sub = _geometry_length_km(sub_geometry)
    if geom_total <= 0:
        ratio = 1.0
    else:
        ratio = max(0.0, min(1.0, geom_sub / geom_total))
    return _route_dict(
        route["duration_min"] * ratio,
        route["distance_km"] * ratio,
        sub_geometry,
    )


def _reverse_route(route: dict) -> dict:
    return _route_dict(
        route["duration_min"],
        route["distance_km"],
        list(reversed(route.get("geometry", []))),
    )


def _concat_routes(first: dict, second: dict) -> dict:
    geom_a = list(first.get("geometry", []))
    geom_b = list(second.get("geometry", []))
    if geom_a and geom_b and geom_a[-1] == geom_b[0]:
        geom = geom_a + geom_b[1:]
    else:
        geom = geom_a + geom_b
    return _route_dict(
        first["duration_min"] + second["duration_min"],
        first["distance_km"] + second["distance_km"],
        geom,
    )


def _segment_hits_closure(p1: tuple, p2: tuple, closure: dict) -> bool:
    """Segment-to-segment hit check — uses same min 60m detection buffer as geometry check."""
    seg = closure["segment"]
    buf = max(closure.get("buffer_km", 0.15), 0.06)
    return segment_to_segment_min_km(
        p1[0], p1[1], p2[0], p2[1],
        seg["from"]["lat"], seg["from"]["lon"],
        seg["to"]["lat"], seg["to"]["lon"],
    ) <= buf


def _first_hit_segment_index(route: dict, closures: list) -> int | None:
    geometry = route.get("geometry", [])
    for idx in range(len(geometry) - 1):
        p1 = tuple(geometry[idx])
        p2 = tuple(geometry[idx + 1])
        if any(_segment_hits_closure(p1, p2, closure) for closure in closures):
            return idx
    return None


def _last_hit_segment_index(route: dict, closures: list) -> int | None:
    geometry = route.get("geometry", [])
    for idx in range(len(geometry) - 2, -1, -1):
        p1 = tuple(geometry[idx])
        p2 = tuple(geometry[idx + 1])
        if any(_segment_hits_closure(p1, p2, closure) for closure in closures):
            return idx
    return None


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Kompasový azimut od (lat1,lon1) k (lat2,lon2) ve stupních [0, 360)."""
    dlon  = math.radians(lon2 - lon1)
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = (math.cos(lat1r) * math.sin(lat2r)
         - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def nearest_sector(b: float) -> int:
    return int(round(b / 45) * 45) % 360


def _pair_cache_key(prefix: str, profile: str,
                    from_latlon: tuple, to_latlon: tuple) -> tuple:
    return (
        prefix,
        profile,
        round(float(from_latlon[0]), 6),
        round(float(from_latlon[1]), 6),
        round(float(to_latlon[0]), 6),
        round(float(to_latlon[1]), 6),
    )


def _route_dict(duration_min: float, distance_km: float,
                geometry: list | None = None) -> dict:
    return {
        "duration_min": float(duration_min),
        "distance_km": float(distance_km),
        "geometry": geometry or [],
    }


def _resolve_closure_route_profile(matrix_profile: str,
                                   closure_route_profile: str | None) -> str:
    if closure_route_profile:
        return closure_route_profile
    return DEFAULT_CLOSURE_ROUTE_PROFILES.get(matrix_profile, ORS_PROFILE_DEFAULT)


# ============================================================
#  OSRM ROUTE GEOMETRIE — zachováno pro manage_closures.py
# ============================================================

def _osrm_route(from_latlon: tuple, to_latlon: tuple,
                osrm_url: str, profile: str = "driving") -> dict | None:
    url = (
        f"{osrm_url}/route/v1/{profile}/"
        f"{from_latlon[1]},{from_latlon[0]};{to_latlon[1]},{to_latlon[0]}"
        "?overview=full&geometries=geojson"
    )
    try:
        resp = requests.get(url, timeout=ROUTE_TIMEOUT_SEC)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("routes"):
            return None
        route = data["routes"][0]
        geometry = [[lat, lon] for lon, lat in route["geometry"]["coordinates"]]
        return _route_dict(route["duration"] / 60.0, route["distance"] / 1000.0, geometry)
    except Exception:
        return None


def osrm_route_geometry(lat1: float, lon1: float,
                        lat2: float, lon2: float,
                        osrm_url: str = OSRM_URL_DEFAULT) -> list | None:
    route = _osrm_route((lat1, lon1), (lat2, lon2), osrm_url, "driving")
    return route["geometry"] if route else None


# ============================================================
#  ORS AVOID_POLYGON — overhead přepočet
# ============================================================

def _offset_point(lat: float, lon: float,
                   bearing: float, dist_km: float) -> tuple[float, float]:
    """Vrátí (lat, lon) bodu vzdáleného dist_km od (lat, lon) v daném azimutu."""
    R = 6371.0
    b = math.radians(bearing)
    d = dist_km / R
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(d)
        + math.cos(lat1) * math.sin(d) * math.cos(b)
    )
    lon2 = lon1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def _segment_to_polygon(a_lat: float, a_lon: float,
                         b_lat: float, b_lon: float,
                         buffer_m: float) -> dict:
    """
    Vrátí GeoJSON Polygon dict pásma kolem úsečky AB s šířkou buffer_m metrů.
    """
    mid_lat = (a_lat + b_lat) / 2
    buf_lat = buffer_m / 111_111
    buf_lon = buffer_m / (111_111 * math.cos(math.radians(mid_lat)))

    dlat   = b_lat - a_lat
    dlon   = b_lon - a_lon
    length = math.sqrt(dlat ** 2 + dlon ** 2)

    if length < 1e-10:
        corners = [
            [a_lon - buf_lon, a_lat - buf_lat],
            [a_lon + buf_lon, a_lat - buf_lat],
            [a_lon + buf_lon, a_lat + buf_lat],
            [a_lon - buf_lon, a_lat + buf_lat],
        ]
    else:
        # Kolmý jednotkový vektor
        perp_lat =  -dlon / length
        perp_lon =   dlat / length
        # Prodloužení podél segmentu (o buffer) pro překrytí koncových bodů
        ext_lat  = (dlat / length) * buf_lat
        ext_lon  = (dlon / length) * buf_lon
        corners = [
            [a_lon + perp_lon * buf_lon - ext_lon,
             a_lat + perp_lat * buf_lat - ext_lat],
            [b_lon + perp_lon * buf_lon + ext_lon,
             b_lat + perp_lat * buf_lat + ext_lat],
            [b_lon - perp_lon * buf_lon + ext_lon,
             b_lat - perp_lat * buf_lat + ext_lat],
            [a_lon - perp_lon * buf_lon - ext_lon,
             a_lat - perp_lat * buf_lat - ext_lat],
        ]

    corners.append(corners[0])   # uzavřít polygon
    return {"type": "Polygon", "coordinates": [corners]}


def _combined_avoid_polygon(closures: list) -> dict:
    """
    Sestaví jeden GeoJSON MultiPolygon ze všech aktivních uzavírek.

    ORS pak najednou vyhýbá všem uzavírkám — pokud objízdka kolem uzavírky A
    prochází uzavírkou B, ORS automaticky najde trasu obcházející obě.
    Pro jedinou uzavírku vrátí prostý Polygon (ORS to přijme stejně).
    """
    polygons = []
    for c in closures:
        seg   = c["segment"]
        a_lat, a_lon = seg["from"]["lat"], seg["from"]["lon"]
        b_lat, b_lon = seg["to"]["lat"],   seg["to"]["lon"]
        buf_m = max(CLOSURE_BUFFER_M, c.get("buffer_km", 0.05) * 1_000)
        poly  = _segment_to_polygon(a_lat, a_lon, b_lat, b_lon, buf_m)
        polygons.append(poly["coordinates"])

    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def _ors_route(from_latlon: tuple, to_latlon: tuple,
               ors_url: str, profile: str,
               avoid_poly: dict | None = None) -> dict | None:
    """ORS directions endpoint -> route dict or None."""
    url  = f"{ors_url}/ors/v2/directions/{profile}/geojson"
    body: dict = {
        "coordinates": [
            [from_latlon[1], from_latlon[0]],   # ORS: [lon, lat]
            [to_latlon[1],   to_latlon[0]],
        ]
    }
    if avoid_poly is not None:
        body["options"] = {"avoid_polygons": avoid_poly}
    try:
        r = requests.post(url, json=body, timeout=ROUTE_TIMEOUT_SEC)
        if r.status_code != 200:
            return None
        feat = r.json()["features"][0]
        summary = feat["properties"]["summary"]
        geometry = [[lat, lon] for lon, lat in feat["geometry"]["coordinates"]]
        return _route_dict(summary["duration"] / 60.0, summary["distance"] / 1000.0, geometry)
    except Exception:
        return None


# Zpětně kompatibilní wrapper (používá closure_map_editor)
def _ors_duration(from_latlon, to_latlon, ors_url, profile, avoid_poly=None):
    r = _ors_route(from_latlon, to_latlon, ors_url, profile, avoid_poly)
    return r["duration_min"] if r else None


def fetch_baseline_route(from_latlon: tuple, to_latlon: tuple, *,
                         matrix_profile: str,
                         osrm_url: str = OSRM_URL_DEFAULT,
                         ors_url: str = ORS_URL_DEFAULT,
                         closure_route_profile: str | None = None,
                         cache: dict | None = None) -> dict | None:
    """Fetch the real baseline route for the given matrix profile."""
    cache = cache if cache is not None else {}
    key = _pair_cache_key("baseline", matrix_profile, from_latlon, to_latlon)
    if key in cache:
        return cache[key]

    if matrix_profile in OSRM_ROUTE_PROFILES:
        route = _osrm_route(from_latlon, to_latlon, osrm_url, matrix_profile)
    else:
        ors_profile = _resolve_closure_route_profile(matrix_profile, closure_route_profile)
        route = _ors_route(from_latlon, to_latlon, ors_url, ors_profile)

    cache[key] = route
    return route


def fetch_avoid_route(from_latlon: tuple, to_latlon: tuple, *,
                      matrix_profile: str,
                      avoid_poly: dict,
                      ors_url: str = ORS_URL_DEFAULT,
                      closure_route_profile: str | None = None,
                      cache: dict | None = None) -> dict | None:
    """Fetch the exact ORS avoid route used to replace matrix cells."""
    cache = cache if cache is not None else {}
    ors_profile = _resolve_closure_route_profile(matrix_profile, closure_route_profile)
    key = _pair_cache_key("avoid", ors_profile, from_latlon, to_latlon)
    if key in cache:
        return cache[key]

    route = _ors_route(from_latlon, to_latlon, ors_url, ors_profile, avoid_poly)
    cache[key] = route
    return route


def build_closure_candidate_sets(locations: list, closures: list,
                                 depot_index: int = 0) -> tuple[set, dict]:
    """
    Broad candidate capture:
      - straight-line hit near closure
      - either endpoint near closure
      - all depot pairs when the closure is near the depot
    """
    all_candidates: set = set()
    per_closure: dict = {}
    depot_loc = locations[depot_index] if 0 <= depot_index < len(locations) else None

    for closure in closures:
        pairs = set()
        depot_near = depot_loc is not None and closure_near_location(depot_loc, closure)
        for i, loc_from in enumerate(locations):
            near_from = endpoint_near_closure(loc_from, closure)
            for j, loc_to in enumerate(locations):
                if i == j:
                    continue
                if (
                    might_cross_closure(loc_from, loc_to, closure)
                    or near_from
                    or endpoint_near_closure(loc_to, closure)
                    or (depot_near and (i == depot_index or j == depot_index))
                ):
                    pairs.add((i, j))
        per_closure[closure["id"]] = pairs
        all_candidates.update(pairs)

    return all_candidates, per_closure


def confirm_closure_candidates(candidates: list, locations: list, closures: list, *,
                               matrix_profile: str,
                               osrm_url: str = OSRM_URL_DEFAULT,
                               ors_url: str = ORS_URL_DEFAULT,
                               closure_route_profile: str | None = None,
                               cache: dict | None = None,
                               workers: int = ROUTE_CONFIRM_WORKERS) -> tuple[dict, dict]:
    """
    Confirm candidate pairs against the real baseline route geometry.

    Returns:
      confirmed: {(i, j): {"route": baseline_route, "hit_ids": [...]}}
      per_closure: {closure_id: {(i, j), ...}}
    """
    cache = cache if cache is not None else {}
    confirmed: dict = {}
    per_closure = {closure["id"]: set() for closure in closures}

    def _confirm(pair):
        i, j = pair
        route = fetch_baseline_route(
            locations[i], locations[j],
            matrix_profile=matrix_profile,
            osrm_url=osrm_url,
            ors_url=ors_url,
            closure_route_profile=closure_route_profile,
            cache=cache,
        )
        if not route or not route.get("geometry"):
            return None
        hit_ids = route_geometry_hits_closures(route["geometry"], closures)
        if not hit_ids:
            return None
        return i, j, route, hit_ids

    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(_confirm, pair): pair for pair in candidates}
        for fut in as_completed(futures):
            result = fut.result()
            if not result:
                continue
            i, j, route, hit_ids = result
            confirmed[(i, j)] = {"route": route, "hit_ids": hit_ids}
            for closure_id in hit_ids:
                per_closure[closure_id].add((i, j))

    return confirmed, per_closure


def _build_depot_anchor_candidates(depot_latlon: tuple,
                                   near_depot_closures: list,
                                   avoid_poly: dict,
                                   *,
                                   matrix_profile: str,
                                   ors_url: str,
                                   closure_route_profile: str | None,
                                   cache: dict) -> list:
    key = (
        "depot-anchor-candidates",
        round(depot_latlon[0], 6),
        round(depot_latlon[1], 6),
        matrix_profile,
        tuple(sorted(c["id"] for c in near_depot_closures)),
    )
    if key in cache:
        return cache[key]

    candidates = []
    seen_points = set()
    for probe_km in DEPOT_FALLBACK_PROBE_KM:
        for bearing in range(0, 360, 45):
            probe = _offset_point(depot_latlon[0], depot_latlon[1], bearing, probe_km)
            route = fetch_avoid_route(
                depot_latlon,
                probe,
                matrix_profile=matrix_profile,
                avoid_poly=avoid_poly,
                ors_url=ors_url,
                closure_route_profile=closure_route_profile,
                cache=cache,
            )
            if not route or route_geometry_hits_closures(route.get("geometry", []), near_depot_closures):
                continue

            geometry = route.get("geometry", [])
            if len(geometry) < 2:
                continue

            traveled = 0.0
            next_targets = list(DEPOT_ANCHOR_TARGET_KM)
            for idx in range(1, len(geometry)):
                prev = geometry[idx - 1]
                curr = geometry[idx]
                traveled += haversine_km(prev[0], prev[1], curr[0], curr[1])
                while next_targets and traveled >= next_targets[0]:
                    point = tuple(curr)
                    next_targets.pop(0)
                    if any(endpoint_near_closure(point, closure, factor=2.0, min_km=0.08)
                           for closure in near_depot_closures):
                        continue
                    dedupe = (round(point[0], 5), round(point[1], 5))
                    if dedupe in seen_points:
                        continue
                    seen_points.add(dedupe)
                    depot_to_anchor = _slice_route(route, idx)
                    if not depot_to_anchor:
                        continue
                    candidates.append({
                        "point": point,
                        "from_depot": depot_to_anchor,
                        "to_depot": _reverse_route(depot_to_anchor),
                        "bearing": bearing,
                        "probe_km": probe_km,
                    })
                    break

    cache[key] = candidates
    return candidates


def _try_depot_splice(problem_route: dict,
                      from_latlon: tuple, to_latlon: tuple,
                      near_depot_closures: list,
                      all_closures: list,
                      avoid_poly: dict,
                      *,
                      matrix_profile: str,
                      ors_url: str,
                      closure_route_profile: str | None,
                      cache: dict) -> dict | None:
    """
    Depot-near robust fallback:
    keep the clean remote part of the route and replace only the depot-adjacent
    head/tail with a separately computed avoid route.
    """
    geometry = problem_route.get("geometry", [])
    if len(geometry) < 3:
        return None

    best = None

    if _is_depot_point(to_latlon) and not _is_depot_point(from_latlon):
        hit_idx = _first_hit_segment_index(problem_route, near_depot_closures)
        if hit_idx is None:
            return None

        tried = set()
        for backoff in (1, 2, 3, 5, 8, 13, 21, 34):
            anchor_idx = max(1, hit_idx - backoff)
            if anchor_idx in tried:
                continue
            tried.add(anchor_idx)

            prefix = _slice_route(problem_route, anchor_idx)
            if not prefix or len(prefix.get("geometry", [])) < 2:
                continue
            if route_geometry_hits_closures(prefix.get("geometry", []), all_closures):
                continue

            anchor = tuple(prefix["geometry"][-1])
            if _is_depot_point(anchor):
                continue

            safe_link = fetch_avoid_route(
                to_latlon,
                anchor,
                matrix_profile=matrix_profile,
                avoid_poly=avoid_poly,
                ors_url=ors_url,
                closure_route_profile=closure_route_profile,
                cache=cache,
            )
            if not safe_link or route_geometry_hits_closures(safe_link.get("geometry", []), all_closures):
                continue

            candidate = _concat_routes(prefix, _reverse_route(safe_link))
            if route_geometry_hits_closures(candidate.get("geometry", []), all_closures):
                continue
            if best is None or candidate["duration_min"] < best["duration_min"]:
                best = candidate

    elif _is_depot_point(from_latlon) and not _is_depot_point(to_latlon):
        hit_idx = _last_hit_segment_index(problem_route, near_depot_closures)
        if hit_idx is None:
            return None

        tried = set()
        for advance in (2, 3, 5, 8, 13, 21, 34):
            anchor_idx = min(len(geometry) - 2, hit_idx + advance)
            if anchor_idx in tried:
                continue
            tried.add(anchor_idx)

            suffix = _slice_route_from(problem_route, anchor_idx)
            if not suffix or len(suffix.get("geometry", [])) < 2:
                continue
            if route_geometry_hits_closures(suffix.get("geometry", []), all_closures):
                continue

            anchor = tuple(suffix["geometry"][0])
            if _is_depot_point(anchor):
                continue

            safe_link = fetch_avoid_route(
                from_latlon,
                anchor,
                matrix_profile=matrix_profile,
                avoid_poly=avoid_poly,
                ors_url=ors_url,
                closure_route_profile=closure_route_profile,
                cache=cache,
            )
            if not safe_link or route_geometry_hits_closures(safe_link.get("geometry", []), all_closures):
                continue

            candidate = _concat_routes(safe_link, suffix)
            if route_geometry_hits_closures(candidate.get("geometry", []), all_closures):
                continue
            if best is None or candidate["duration_min"] < best["duration_min"]:
                best = candidate

    return best


def _try_depot_fallback(from_latlon: tuple, to_latlon: tuple,
                        near_depot_closures: list,
                        all_closures: list,
                        avoid_poly: dict,
                        *,
                        matrix_profile: str,
                        ors_url: str,
                        closure_route_profile: str | None,
                        cache: dict) -> dict | None:
    if _is_depot_point(from_latlon):
        depot_latlon = from_latlon
        remote = to_latlon
        mode = "outbound"
    elif _is_depot_point(to_latlon):
        depot_latlon = to_latlon
        remote = from_latlon
        mode = "inbound"
    else:
        return None

    anchors = _build_depot_anchor_candidates(
        depot_latlon,
        near_depot_closures,
        avoid_poly,
        matrix_profile=matrix_profile,
        ors_url=ors_url,
        closure_route_profile=closure_route_profile,
        cache=cache,
    )
    if not anchors:
        return None

    best = None
    for anchor in anchors:
        if mode == "outbound":
            remote_leg = fetch_avoid_route(
                anchor["point"],
                remote,
                matrix_profile=matrix_profile,
                avoid_poly=avoid_poly,
                ors_url=ors_url,
                closure_route_profile=closure_route_profile,
                cache=cache,
            )
            if not remote_leg or route_geometry_hits_closures(remote_leg.get("geometry", []), all_closures):
                continue
            candidate = _concat_routes(anchor["from_depot"], remote_leg)
        else:
            remote_leg = fetch_avoid_route(
                remote,
                anchor["point"],
                matrix_profile=matrix_profile,
                avoid_poly=avoid_poly,
                ors_url=ors_url,
                closure_route_profile=closure_route_profile,
                cache=cache,
            )
            if not remote_leg or route_geometry_hits_closures(remote_leg.get("geometry", []), all_closures):
                continue
            candidate = _concat_routes(remote_leg, anchor["to_depot"])

        if route_geometry_hits_closures(candidate.get("geometry", []), all_closures):
            continue
        if best is None or candidate["duration_min"] < best["duration_min"]:
            best = candidate

    return best


def build_closure_aware_route_for_pair(from_latlon: tuple, to_latlon: tuple, *,
                                       closures: list | None = None,
                                       matrix_profile: str = "driving",
                                       osrm_url: str = OSRM_URL_DEFAULT,
                                       ors_url: str = ORS_URL_DEFAULT,
                                       closure_route_profile: str | None = None,
                                       cache: dict | None = None,
                                       avoid_poly: dict | None = None) -> dict | None:
    """
    Shared route fetch for visualization and diagnostics.

    Returns the baseline route unless its geometry hits an active closure.
    """
    cache = cache if cache is not None else {}
    closures = closures or []
    baseline = fetch_baseline_route(
        from_latlon, to_latlon,
        matrix_profile=matrix_profile,
        osrm_url=osrm_url,
        ors_url=ors_url,
        closure_route_profile=closure_route_profile,
        cache=cache,
    )
    if not baseline or not closures:
        return baseline

    baseline_hits = route_geometry_hits_closures(baseline.get("geometry", []), closures)
    if not baseline_hits:
        return baseline

    avoid_poly = avoid_poly or _combined_avoid_polygon(closures)
    avoid_route = fetch_avoid_route(
        from_latlon, to_latlon,
        matrix_profile=matrix_profile,
        avoid_poly=avoid_poly,
        ors_url=ors_url,
        closure_route_profile=closure_route_profile,
        cache=cache,
    )
    avoid_hits = route_geometry_hits_closures(avoid_route.get("geometry", []), closures) if avoid_route else []
    if avoid_route and not avoid_hits:
        return avoid_route

    if _is_depot_point(from_latlon) or _is_depot_point(to_latlon):
        problem_routes = []
        if avoid_route and avoid_hits:
            problem_routes.append((avoid_route, avoid_hits))
        problem_routes.append((baseline, baseline_hits))

        for route_seed, hit_ids in problem_routes:
            near_depot_closures = [
                closure for closure in closures
                if closure["id"] in hit_ids and closure_near_location((DEPOT_LAT, DEPOT_LON), closure)
            ]
            if not near_depot_closures:
                continue

            splice = _try_depot_splice(
                route_seed,
                from_latlon,
                to_latlon,
                near_depot_closures,
                closures,
                avoid_poly,
                matrix_profile=matrix_profile,
                ors_url=ors_url,
                closure_route_profile=closure_route_profile,
                cache=cache,
            )
            if splice:
                return splice

            fallback = _try_depot_fallback(
                from_latlon,
                to_latlon,
                near_depot_closures,
                closures,
                avoid_poly,
                matrix_profile=matrix_profile,
                ors_url=ors_url,
                closure_route_profile=closure_route_profile,
                cache=cache,
            )
            if fallback:
                return fallback

    return avoid_route or baseline


def compute_closure_overheads(closure: dict, avoid_poly: dict,
                               ors_url: str = ORS_URL_DEFAULT,
                               ors_profile: str = ORS_PROFILE_DEFAULT) -> dict:
    """
    Probe-based overhead per směr (8 sektorů).
    Používáno POUZE v closure_map_editor pro vizualizaci bypass-tras.
    Solver používá přesný per-pair výpočet (viz apply_closures_to_matrix).
    """
    seg     = closure["segment"]
    mid_lat = (seg["from"]["lat"] + seg["to"]["lat"]) / 2
    mid_lon = (seg["from"]["lon"] + seg["to"]["lon"]) / 2
    overheads: dict = {}

    for bear_from, bear_to in PROBE_PAIRS:
        pt_from = _offset_point(mid_lat, mid_lon, bear_from, PROBE_DIST_KM)
        pt_to   = _offset_point(mid_lat, mid_lon, bear_to,   PROBE_DIST_KM)
        baseline = _ors_duration(pt_from, pt_to, ors_url, ors_profile)
        detour   = _ors_duration(pt_from, pt_to, ors_url, ors_profile, avoid_poly)
        if baseline is None or detour is None:
            continue
        overhead = max(0.0, detour - baseline)
        overheads[bear_to]   = overhead
        overheads[bear_from] = overhead

    return overheads


def _fetch_exact_avoid_routes(pairs: list, locations: list, closures: list, avoid_poly: dict, *,
                              matrix_profile: str,
                              ors_url: str,
                              closure_route_profile: str | None,
                              cache: dict | None = None) -> dict:
    """Fetch exact avoid routes for all confirmed pairs."""
    cache = cache if cache is not None else {}

    def _call(pair):
        i, j = pair
        route = build_closure_aware_route_for_pair(
            locations[i], locations[j],
            closures=closures,
            matrix_profile=matrix_profile,
            avoid_poly=avoid_poly,
            ors_url=ors_url,
            closure_route_profile=closure_route_profile,
            cache=cache,
        )
        if not route:
            return None
        return i, j, route

    results = {}
    with ThreadPoolExecutor(max_workers=ORS_WORKERS) as exe:
        futures = {exe.submit(_call, pair): pair for pair in pairs}
        for fut in as_completed(futures):
            result = fut.result()
            if not result:
                continue
            i, j, route = result
            results[(i, j)] = route
    return results


# ============================================================
#  HLAVNÍ FUNKCE — volá solver
# ============================================================

def apply_closures_to_matrix(
    durations_min: np.ndarray,
    distances_km: np.ndarray,
    locations: list,
    *,
    matrix_profile: str,
    osrm_url: str = OSRM_URL_DEFAULT,
    ors_url: str = ORS_URL_DEFAULT,
    closures_path=None,
    closure_route_profile: str | None = None,
    debug_label: str | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Apply active closures to a matrix using authoritative confirmation and
    exact ORS avoid-route replacement.
    """
    closures = load_active_closures(closures_path)
    if not closures:
        return durations_min, distances_km

    label = debug_label or matrix_profile
    prefix = f"  [uzavírky][{label}]"
    print(f"\n{prefix} {len(closures)} aktivnich uzavirek")

    combined_poly = _combined_avoid_polygon(closures)
    route_cache: dict = {}

    candidates, per_closure_candidates = build_closure_candidate_sets(locations, closures)
    for closure in closures:
        closure_candidates = per_closure_candidates.get(closure["id"], set())
        depot_near = closure_near_location(locations[0], closure) if locations else False
        depot_note = " | depot-near" if depot_near else ""
        print(
            f"{prefix} {closure['id']}: {closure['name']} -> "
            f"{len(closure_candidates)} broad pairu{depot_note}"
        )

    if not candidates:
        print(f"{prefix} zadne broad kandidaty - matice beze zmeny")
        return durations_min, distances_km

    print(f"{prefix} potvrzuji {len(candidates)} kandidatu pres realnou baseline geometrii...")
    t_confirm = _time.time()
    confirmed, per_closure_confirmed = confirm_closure_candidates(
        sorted(candidates),
        locations,
        closures,
        matrix_profile=matrix_profile,
        osrm_url=osrm_url,
        ors_url=ors_url,
        closure_route_profile=closure_route_profile,
        cache=route_cache,
    )
    confirm_elapsed = _time.time() - t_confirm

    for closure in closures:
        print(
            f"{prefix} {closure['id']}: potvrzeno "
            f"{len(per_closure_confirmed.get(closure['id'], set()))} paru"
        )

    if not confirmed:
        print(f"{prefix} baseline nepotvrdila zadny zasah ({confirm_elapsed:.1f} s)")
        return durations_min, distances_km

    print(f"{prefix} baseline hotovo za {confirm_elapsed:.1f} s, "
          f"potvrzeno {len(confirmed)} paru")

    t_avoid = _time.time()
    exact_routes = _fetch_exact_avoid_routes(
        sorted(confirmed.keys()),
        locations,
        closures,
        combined_poly,
        matrix_profile=matrix_profile,
        ors_url=ors_url,
        closure_route_profile=closure_route_profile,
        cache=route_cache,
    )
    avoid_elapsed = _time.time() - t_avoid

    dur_out = durations_min.astype(float).copy()
    dist_out = distances_km.astype(float).copy()
    updated = 0
    max_delta_min = 0.0
    max_delta_km = 0.0

    for (i, j), route in exact_routes.items():
        prev_min = float(dur_out[i][j])
        prev_km = float(dist_out[i][j])
        dur_out[i][j] = route["duration_min"]
        dist_out[i][j] = route["distance_km"]
        updated += 1
        max_delta_min = max(max_delta_min, route["duration_min"] - prev_min)
        max_delta_km = max(max_delta_km, route["distance_km"] - prev_km)

    print(
        f"{prefix} avoid hotovo za {avoid_elapsed:.1f} s "
        f"({len(exact_routes)}/{len(confirmed)} exact route)"
    )
    print(
        f"{prefix} aktualizovano {updated} paru "
        f"(max delta +{max_delta_min:.1f} min / +{max_delta_km:.1f} km)\n"
    )

    return dur_out, dist_out
