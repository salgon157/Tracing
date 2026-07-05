"""
export_prepare.py - build an offline ZIP package for route visualization.

The script keeps the current project untouched and creates a new export package:
  - index.html with embedded route/stop data
  - local Leaflet JS/CSS assets
  - local tile cache for a configurable zoom range
  - package_info.json + README.txt

Typical usage:
  python export_prepare.py data/results/CB/2026-04-10/
  python export_prepare.py data/results/CB/2026-04-10/ --max-zoom 13

Output:
  data/results/CB/2026-04-10/routes_map_offline_bundle.zip

Notes:
  - The export machine downloads tiles and Leaflet assets once during package
    creation. The recipient only needs to unzip the package and open index.html.
  - Verify that your tile provider allows caching / redistribution before using
    the default tile source for broader external sharing.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import visualize_routes as vr

try:
    from closures_utils import load_active_closures

    CLOSURES_AVAILABLE = True
except ImportError:
    CLOSURES_AVAILABLE = False


DEFAULT_TILE_URL = "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png"
DEFAULT_TILE_ATTRIBUTION = "(c) OpenStreetMap (c) CARTO"
DEFAULT_TILE_SUBDOMAINS = "abcd"
DEFAULT_LEAFLET_VERSION = "1.9.4"
DEFAULT_TIMEOUT_SEC = 20
DEFAULT_TILE_WORKERS = 16
DEFAULT_MIN_ZOOM = 7
DEFAULT_MAX_ZOOM = 14
DEFAULT_TILE_MARGIN = 1
# Pro zoom >= tohoto prahu se stahují jen dlaždice podél reálné geometrie tras
# místo celého bounding boxu. Výrazně méně dlaždic na vysokých zoomech.
SPARSE_ZOOM_THRESHOLD = 13
DEFAULT_ZIP_NAME = "routes_map_offline_bundle.zip"
USER_AGENT = "Tracing_01 offline exporter/1.0"

LEAFLET_FILES = (
    "leaflet.js",
    "leaflet.css",
    "images/layers.png",
    "images/layers-2x.png",
    "images/marker-icon.png",
    "images/marker-icon-2x.png",
    "images/marker-shadow.png",
)

# 1x1 transparent PNG. Browsers render it fine even if the requested tile is
# larger. Used only as a fallback when a tile download fails.
BLANK_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
    b"\xff?\x00\x05\xfe\x02\xfeA\xd9\x8f\xa7\x00\x00\x00\x00IEND\xaeB`\x82"
)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offline mapa tras - {title}</title>
<link rel="stylesheet" href="leaflet/leaflet.css"/>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: "Segoe UI", Arial, sans-serif;
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #1a1a2e;
  color: #eee;
}}
#header {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  background: #16213e;
  border-bottom: 2px solid #0f3460;
  min-height: 52px;
  flex-shrink: 0;
}}
.nav-btn {{
  background: #0f3460;
  border: none;
  color: #e94560;
  font-size: 20px;
  width: 36px;
  height: 36px;
  cursor: pointer;
  border-radius: 6px;
  transition: background 0.2s;
}}
.nav-btn:hover {{ background: #e94560; color: white; }}
#route-badge {{
  font-size: 15px;
  font-weight: bold;
  color: white;
  background: #0f3460;
  padding: 4px 12px;
  border-radius: 20px;
  border-left: 4px solid #e94560;
  white-space: nowrap;
}}
#route-meta {{ font-size: 13px; color: #a8b2d8; flex: 1; }}
#route-meta b {{ color: white; }}
#route-counter {{ font-size: 12px; color: #57689e; margin-left: auto; white-space: nowrap; }}
#mode-badge {{
  font-size: 11px;
  color: white;
  background: #e94560;
  padding: 4px 8px;
  border-radius: 999px;
  white-space: nowrap;
}}
#content {{ display: flex; flex: 1; overflow: hidden; }}
#sidebar {{
  width: 320px;
  min-width: 220px;
  max-width: 420px;
  overflow-y: auto;
  background: #16213e;
  border-right: 1px solid #0f3460;
  flex-shrink: 0;
}}
#sidebar::-webkit-scrollbar {{ width: 6px; }}
#sidebar::-webkit-scrollbar-track {{ background: #16213e; }}
#sidebar::-webkit-scrollbar-thumb {{ background: #0f3460; border-radius: 3px; }}
.stop-item {{
  padding: 8px 12px;
  border-bottom: 1px solid #0f3460;
  cursor: pointer;
  transition: background 0.15s;
}}
.stop-item:hover {{ background: #0f3460; }}
.stop-depot {{ background: #1a1040; border-left: 3px solid #e94560; }}
.stop-depot:hover {{ background: #280f58; }}
.stop-number {{
  display: inline-block;
  width: 20px;
  height: 20px;
  line-height: 20px;
  text-align: center;
  border-radius: 50%;
  font-size: 10px;
  font-weight: bold;
  margin-right: 6px;
  vertical-align: middle;
  flex-shrink: 0;
}}
.stop-row-top {{ display: flex; align-items: center; }}
.stop-name {{ font-size: 13px; color: #eee; flex: 1; }}
.stop-arrival {{ font-size: 11px; color: #7f8fc7; margin-left: 4px; white-space: nowrap; }}
.stop-details {{ font-size: 11px; color: #57689e; margin-top: 2px; padding-left: 26px; }}
.stop-highlight {{ background: #0f3460 !important; outline: 2px solid #e94560; }}
#map {{ flex: 1; }}
.leaflet-popup-content-wrapper {{
  background: #16213e;
  color: #eee;
  border: 1px solid #0f3460;
}}
.leaflet-popup-tip {{ background: #16213e; }}
.popup-title {{
  font-weight: bold;
  font-size: 13px;
  margin-bottom: 4px;
  color: white;
}}
.popup-row {{ font-size: 12px; color: #a8b2d8; }}
</style>
</head>
<body>

<div id="header">
  <button class="nav-btn" id="prev-btn" title="Predchozi trasa">&#9664;</button>
  <button class="nav-btn" id="next-btn" title="Dalsi trasa">&#9654;</button>
  <div id="route-badge">LINE_01</div>
  <div id="route-meta"></div>
  <div id="route-counter"></div>
  <div id="mode-badge">OFFLINE ZIP</div>
</div>

<div id="content">
  <div id="sidebar"></div>
  <div id="map"></div>
</div>

<script src="leaflet/leaflet.js"></script>
<script>
const ROUTES = ROUTES_DATA_PLACEHOLDER;
const CLOSURES = CLOSURES_DATA_PLACEHOLDER;
const TILE_TEMPLATE = TILE_TEMPLATE_PLACEHOLDER;
const TILE_ATTRIBUTION = TILE_ATTRIBUTION_PLACEHOLDER;
const TILE_MIN_ZOOM = TILE_MIN_ZOOM_PLACEHOLDER;
const TILE_MAX_ZOOM = TILE_MAX_ZOOM_PLACEHOLDER;

const map = L.map('map', {{
  zoomControl: true,
  minZoom: TILE_MIN_ZOOM,
  maxZoom: TILE_MAX_ZOOM
}});

L.tileLayer(TILE_TEMPLATE, {{
  attribution: TILE_ATTRIBUTION,
  minZoom: TILE_MIN_ZOOM,
  maxZoom: TILE_MAX_ZOOM,
  noWrap: true
}}).addTo(map);

let currentIdx = 0;
let currentLayers = [];
let currentMarkers = [];

function makeNumberIcon(num, color, isDepot) {{
  if (isDepot) {{
    return L.divIcon({{
      html: '<div style="background:#e94560;width:16px;height:16px;border-radius:50%;border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,.5)"></div>',
      iconSize: [20, 20],
      iconAnchor: [10, 10],
      className: ''
    }});
  }}

  return L.divIcon({{
    html: `<div style="background:${{color}};color:white;width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:bold;border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,.5)">${{num}}</div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
    className: ''
  }});
}}

function renderRoute(idx) {{
  currentLayers.forEach(layer => {{
    try {{ map.removeLayer(layer); }} catch (err) {{}}
  }});
  currentLayers = [];
  currentMarkers = [];

  const route = ROUTES[idx];
  const color = route.color;

  document.getElementById('route-badge').textContent = route.line_id;
  document.getElementById('route-badge').style.borderColor = color;
  document.getElementById('route-meta').innerHTML =
    `<b>${{route.vehicle_type}}</b>${{route.vehicle_id ? ' | ' + route.vehicle_id : ''}} | ` +
    `<b>${{route.total_km.toFixed(1)}}</b> km | ` +
    `<b>${{route.duration_h.toFixed(1)}}</b> h | ` +
    `<b>${{route.total_kg.toFixed(0)}}</b> kg | ` +
    `<b>${{Math.round(route.total_cost_kc).toLocaleString('cs-CZ')}}</b> Kc`;
  document.getElementById('route-counter').textContent = `${{idx + 1}} / ${{ROUTES.length}}`;

  const validStops = route.stops.filter(stop => stop.lat != null && stop.lon != null);
  if (validStops.length === 0) {{
    document.getElementById('sidebar').innerHTML = '<div style="padding:16px;color:#57689e">Zadne GPS souradnice.</div>';
    return;
  }}

  const latlngs = route.geometry ? route.geometry : validStops.map(stop => [stop.lat, stop.lon]);
  const poly = L.polyline(latlngs, {{ color, weight: 3.5, opacity: 0.85 }}).addTo(map);
  currentLayers.push(poly);

  let stopCounter = 0;
  route.stops.forEach((stop, index) => {{
    if (stop.lat == null || stop.lon == null) return;
    const isDepot = index === 0 || index === route.stops.length - 1;
    if (!isDepot) stopCounter += 1;
    const displayNum = isDepot ? 'D' : stopCounter;
    const marker = L.marker([stop.lat, stop.lon], {{
      icon: makeNumberIcon(displayNum, color, isDepot),
      zIndexOffset: isDepot ? 1000 : 0
    }});

    const kgStr = stop.kg > 0 ? `${{stop.kg.toFixed(0)}} kg` : '';
    const legStr = stop.leg_km != null && stop.leg_km > 0 ? `+${{stop.leg_km.toFixed(1)}} km` : '';
    const winStr = stop.window ? `<span style="color:#7f8fc7">[${{stop.window}}]</span>` : '';
    const noteStr = stop.note ? `<br><i style="color:#a8b2d8">${{stop.note}}</i>` : '';

    marker.bindPopup(
      `<div class="popup-title">${{stop.place}}</div>` +
      `<div class="popup-row">${{stop.arrival}} ${{winStr}}</div>` +
      ((kgStr || legStr) ? `<div class="popup-row">${{[kgStr, legStr].filter(Boolean).join(' | ')}}</div>` : '') +
      noteStr,
      {{ maxWidth: 260 }}
    );

    marker.addTo(map);
    currentLayers.push(marker);
    currentMarkers.push({{ marker, stopIdx: index }});
  }});

  try {{
    map.fitBounds(poly.getBounds(), {{ padding: [30, 30] }});
  }} catch (err) {{}}

  const sidebar = document.getElementById('sidebar');
  stopCounter = 0;
  sidebar.innerHTML = route.stops.map((stop, index) => {{
    const isDepot = index === 0 || index === route.stops.length - 1;
    if (!isDepot) stopCounter += 1;
    const num = isDepot ? '' : stopCounter;
    const numEl = num !== ''
      ? `<span class="stop-number" style="background:${{color}}">${{num}}</span>`
      : '<span class="stop-number" style="background:#e94560">D</span>';
    const legStr = stop.leg_km != null && stop.leg_km > 0 ? ` +${{stop.leg_km.toFixed(1)}} km` : '';
    const kgStr = stop.kg > 0 ? ` · ${{stop.kg.toFixed(0)}} kg` : '';
    const winStr = stop.window ? ` · ${{stop.window}}` : '';
    const noteStr = stop.note ? ` · ${{stop.note}}` : '';
    return `<div class="stop-item ${{isDepot ? 'stop-depot' : ''}}" id="stop-${{index}}" onclick="focusStop(${{idx}}, ${{index}})">
      <div class="stop-row-top">
        ${{numEl}}
        <span class="stop-name">${{stop.place}}</span>
        <span class="stop-arrival">${{stop.arrival}}${{legStr}}</span>
      </div>
      ${{(kgStr || winStr || noteStr) ? `<div class="stop-details">${{(kgStr + winStr + noteStr).replace(/^ · /, '')}}</div>` : ''}}
    </div>`;
  }}).join('');
}}

function focusStop(routeIdx, stopIdx) {{
  const stop = ROUTES[routeIdx].stops[stopIdx];
  if (stop.lat == null || stop.lon == null) return;

  map.setView([stop.lat, stop.lon], Math.min(TILE_MAX_ZOOM, 15), {{ animate: true }});
  document.querySelectorAll('.stop-highlight').forEach(el => el.classList.remove('stop-highlight'));
  const el = document.getElementById(`stop-${{stopIdx}}`);
  if (el) {{
    el.classList.add('stop-highlight');
    el.scrollIntoView({{ block: 'nearest' }});
  }}
  const found = currentMarkers.find(item => item.stopIdx === stopIdx);
  if (found) found.marker.openPopup();
}}

function goNext() {{
  currentIdx = (currentIdx + 1) % ROUTES.length;
  renderRoute(currentIdx);
}}

function goPrev() {{
  currentIdx = (currentIdx - 1 + ROUTES.length) % ROUTES.length;
  renderRoute(currentIdx);
}}

document.getElementById('next-btn').onclick = goNext;
document.getElementById('prev-btn').onclick = goPrev;
document.addEventListener('keydown', event => {{
  if (event.key === 'ArrowRight') goNext();
  if (event.key === 'ArrowLeft') goPrev();
}});

function renderClosures() {{
  CLOSURES.forEach(closure => {{
    const from = closure.segment.from;
    const to = closure.segment.to;
    if (from.lat == null || to.lat == null) return;

    const line = L.polyline([[from.lat, from.lon], [to.lat, to.lon]], {{
      color: '#e53935',
      weight: 4,
      opacity: 0.9,
      dashArray: '8 5'
    }}).addTo(map);

    line.bindTooltip(`Closure: ${{closure.id}} - ${{closure.name}}`, {{ sticky: true }});

    [from, to].forEach(point => {{
      L.circleMarker([point.lat, point.lon], {{
        radius: 5,
        color: '#e53935',
        fillColor: '#e53935',
        fillOpacity: 1,
        weight: 2
      }}).addTo(map);
    }});
  }});
}}

renderRoute(0);
if (CLOSURES.length > 0) {{
  renderClosures();
}}
</script>
</body>
</html>
"""


def detect_tile_extension(tile_url_template: str) -> str:
    path = urlsplit(tile_url_template).path
    suffix = Path(path).suffix.lower()
    return suffix if suffix else ".png"


def clamp_lat(lat: float) -> float:
    return max(min(lat, 85.05112878), -85.05112878)


def lon_to_tile_x(lon: float, zoom: int) -> int:
    count = 2**zoom
    value = int(math.floor((lon + 180.0) / 360.0 * count))
    return min(max(value, 0), count - 1)


def lat_to_tile_y(lat: float, zoom: int) -> int:
    count = 2**zoom
    lat_rad = math.radians(clamp_lat(lat))
    value = int(
        math.floor((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * count)
    )
    return min(max(value, 0), count - 1)


def iter_tile_jobs(points: list[tuple[float, float]], min_zoom: int, max_zoom: int, margin: int):
    if not points:
        return {}, []

    lats = [lat for lat, _lon in points]
    lons = [lon for _lat, lon in points]
    min_lat = min(lats)
    max_lat = max(lats)
    min_lon = min(lons)
    max_lon = max(lons)

    ranges: dict[int, dict[str, int]] = {}
    jobs: list[tuple[int, int, int]] = []

    for zoom in range(min_zoom, max_zoom + 1):
        count = 2**zoom

        if zoom < SPARSE_ZOOM_THRESHOLD:
            # Nízký zoom — stáhni celý bounding box (dlaždic je málo)
            x_min = max(0, lon_to_tile_x(min_lon, zoom) - margin)
            x_max = min(count - 1, lon_to_tile_x(max_lon, zoom) + margin)
            y_min = max(0, lat_to_tile_y(max_lat, zoom) - margin)
            y_max = min(count - 1, lat_to_tile_y(min_lat, zoom) + margin)
            zoom_tiles = set(
                (x, y)
                for x in range(x_min, x_max + 1)
                for y in range(y_min, y_max + 1)
            )
        else:
            # Vysoký zoom — jen dlaždice podél reálné geometrie tras + margin
            # Výrazně méně dlaždic než plný bbox, protože trasy nezaplňují celý obdélník.
            zoom_tiles: set[tuple[int, int]] = set()
            for lat, lon in points:
                tx = lon_to_tile_x(lon, zoom)
                ty = lat_to_tile_y(lat, zoom)
                for dx in range(-margin, margin + 1):
                    for dy in range(-margin, margin + 1):
                        nx = min(max(tx + dx, 0), count - 1)
                        ny = min(max(ty + dy, 0), count - 1)
                        zoom_tiles.add((nx, ny))
            x_min = min(x for x, _ in zoom_tiles)
            x_max = max(x for x, _ in zoom_tiles)
            y_min = min(y for _, y in zoom_tiles)
            y_max = max(y for _, y in zoom_tiles)

        ranges[zoom] = {
            "x_min": x_min,
            "x_max": x_max,
            "y_min": y_min,
            "y_max": y_max,
            "count": len(zoom_tiles),
        }

        for x, y in zoom_tiles:
            jobs.append((zoom, x, y))

    return ranges, jobs


def collect_bounds_points(route_list: list[dict], closures: list[dict]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []

    for route in route_list:
        geometry = route.get("geometry") or []
        if geometry:
            for lat, lon in geometry:
                points.append((lat, lon))
        else:
            for stop in route.get("stops", []):
                lat = stop.get("lat")
                lon = stop.get("lon")
                if lat is not None and lon is not None:
                    points.append((lat, lon))

    for closure in closures:
        segment = closure.get("segment", {})
        for side in ("from", "to"):
            point = segment.get(side, {})
            lat = point.get("lat")
            lon = point.get("lon")
            if lat is not None and lon is not None:
                points.append((lat, lon))

    return points


def build_tile_url(template: str, subdomains: str, zoom: int, x: int, y: int) -> str:
    url = template
    if "{s}" in url:
        if not subdomains:
            raise ValueError("Tile template requires {s}, but no subdomains were supplied.")
        subdomain = subdomains[(x + y) % len(subdomains)]
        url = url.replace("{s}", subdomain)

    return (
        url.replace("{z}", str(zoom))
        .replace("{x}", str(x))
        .replace("{y}", str(y))
        .replace("{r}", "")
    )


def download_file(url: str, target: Path, timeout_sec: int) -> None:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout_sec) as response:
        data = response.read()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def download_leaflet_assets(build_root: Path, version: str, timeout_sec: int) -> None:
    leaflet_root = build_root / "leaflet"
    dist_base = f"https://unpkg.com/leaflet@{version}/dist"

    print(f"Downloading Leaflet {version} assets...")
    for relative_path in LEAFLET_FILES:
        url = f"{dist_base}/{relative_path}"
        target = leaflet_root / relative_path
        download_file(url, target, timeout_sec)


def download_tiles(
    build_root: Path,
    tile_url: str,
    subdomains: str,
    tile_ext: str,
    jobs: list[tuple[int, int, int]],
    timeout_sec: int,
    workers: int,
) -> tuple[int, int]:
    tile_root = build_root / "tiles"
    total = len(jobs)
    completed = 0
    success = 0
    failed = 0
    last_report = 0

    def worker(job: tuple[int, int, int]) -> tuple[bool, tuple[int, int, int], str | None]:
        zoom, x, y = job
        url = build_tile_url(tile_url, subdomains, zoom, x, y)
        target = tile_root / str(zoom) / str(x) / f"{y}{tile_ext}"

        try:
            download_file(url, target, timeout_sec)
            return True, job, None
        except Exception as exc:  # pragma: no cover - network failure path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(BLANK_PNG)
            return False, job, str(exc)

    print(f"Downloading {total} tiles...")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(worker, job) for job in jobs]
        for future in as_completed(futures):
            ok, _job, error = future.result()
            completed += 1
            if ok:
                success += 1
            else:
                failed += 1

            if completed == total or completed - last_report >= 50:
                print(f"  Tiles: {completed}/{total} (ok={success}, failed={failed})")
                last_report = completed
                if error and failed <= 3:
                    print(f"    Tile download fallback used: {error}")

    return success, failed


def closures_to_js(closures: list[dict]) -> list[dict]:
    result = []
    for closure in closures:
        segment = closure.get("segment", {})
        result.append(
            {
                "id": closure.get("id", ""),
                "name": closure.get("name", ""),
                "segment": {
                    "from": {
                        "lat": segment.get("from", {}).get("lat"),
                        "lon": segment.get("from", {}).get("lon"),
                    },
                    "to": {
                        "lat": segment.get("to", {}).get("lat"),
                        "lon": segment.get("to", {}).get("lon"),
                    },
                },
            }
        )
    return result


def generate_html(
    route_list: list[dict],
    title: str,
    closures: list[dict],
    tile_ext: str,
    min_zoom: int,
    max_zoom: int,
    tile_attribution: str,
) -> str:
    template = HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    return (
        template.replace(
            "ROUTES_DATA_PLACEHOLDER",
            json.dumps(route_list, ensure_ascii=False, separators=(",", ":")),
        )
        .replace(
            "CLOSURES_DATA_PLACEHOLDER",
            json.dumps(closures_to_js(closures), ensure_ascii=False, separators=(",", ":")),
        )
        .replace("TILE_TEMPLATE_PLACEHOLDER", json.dumps(f"tiles/{{z}}/{{x}}/{{y}}{tile_ext}"))
        .replace("TILE_ATTRIBUTION_PLACEHOLDER", json.dumps(tile_attribution))
        .replace("TILE_MIN_ZOOM_PLACEHOLDER", str(min_zoom))
        .replace("TILE_MAX_ZOOM_PLACEHOLDER", str(max_zoom))
        .replace("{title}", title)
    )


def write_readme(build_root: Path, zip_name: str) -> None:
    readme = f"""Offline route package
=====================

How to use:
1. Unzip {zip_name}
2. Open index.html in a browser

Package contents:
- index.html ........ offline route viewer
- leaflet/ .......... local JS/CSS assets
- tiles/ ............ local tile cache
- package_info.json . export metadata

No additional downloads are required on the recipient PC.
"""
    (build_root / "README.txt").write_text(readme, encoding="utf-8")


def write_package_info(
    build_root: Path,
    result_dir: Path,
    args: argparse.Namespace,
    route_list: list[dict],
    closures: list[dict],
    tile_ranges: dict[int, dict[str, int]],
    tile_success: int,
    tile_failed: int,
    zip_name: str,
) -> None:
    payload = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_result_dir": str(result_dir.resolve()),
        "zip_name": zip_name,
        "route_count": len(route_list),
        "closure_count": len(closures),
        "tile_source_template": args.tile_url,
        "tile_subdomains": args.tile_subdomains,
        "tile_attribution": args.tile_attribution,
        "min_zoom": args.min_zoom,
        "max_zoom": args.max_zoom,
        "tile_margin": args.tile_margin,
        "tile_ranges": tile_ranges,
        "tile_success": tile_success,
        "tile_failed": tile_failed,
        "leaflet_version": args.leaflet_version,
        "osrm_url": None if args.no_osrm else args.osrm_url,
    }
    (build_root / "package_info.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _check_osrm(url: str) -> None:
    """Ověří dostupnost OSRM. Při chybě vypíše jasnou hlášku a ukončí program."""
    import sys
    import requests as _req
    try:
        _req.get(f"{url}/health", timeout=3)
    except Exception:
        print(f"\n[CHYBA] OSRM není dostupný na {url}")
        print("  Export vyžaduje OSRM pro trasování po silnicích.")
        print("  Spusť Docker a zkus znovu, nebo použij --no-osrm pro vzdušné čáry.")
        sys.exit(1)


def zip_directory(source_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source_dir.rglob("*")):
            archive.write(path, arcname=path.relative_to(source_dir.parent))


def resolve_result_dir(arg_value: str | None) -> Path:
    if arg_value:
        return Path(arg_value)

    latest = vr.find_latest_result_dir()
    if latest is None:
        raise FileNotFoundError("No result directory with lines_stops.csv was found in data/results.")
    return latest


def load_closures() -> list[dict]:
    if not CLOSURES_AVAILABLE:
        print("Closures: skipping, closures_utils is not available.")
        return []

    closures = load_active_closures()
    if closures:
        print(f"Closures: {len(closures)} active")
    else:
        print("Closures: none active")
    return closures


def _extract_json_array_from_html(html: str, marker: str) -> list[dict]:
    marker_index = html.find(marker)
    if marker_index < 0:
        raise ValueError(f"Marker not found: {marker}")

    start = html.find("[", marker_index)
    if start < 0:
        raise ValueError(f"JSON array start not found after marker: {marker}")

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(html)):
        ch = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return json.loads(html[start : index + 1])

    raise ValueError(f"JSON array end not found for marker: {marker}")


def load_routes_from_visualize_html(result_dir: Path) -> tuple[list[dict], list[dict]] | None:
    html_path = result_dir / "routes_map.html"
    if not html_path.exists():
        return None

    html = html_path.read_text(encoding="utf-8")
    routes = _extract_json_array_from_html(html, "const ROUTES")
    closures = _extract_json_array_from_html(html, "const CLOSURES")

    geometry_count = sum(1 for route in routes if route.get("geometry"))
    if geometry_count == 0:
        return None

    print(
        "Route geometry source: existing routes_map.html "
        f"({geometry_count}/{len(routes)} routes with geometry)"
    )
    return routes, closures


def build_routes(result_dir: Path, no_osrm: bool, osrm_url: str) -> tuple[list[dict], list[dict]]:
    stops_csv = result_dir / "lines_stops.csv"
    summary_csv = result_dir / "lines_summary.csv"
    if not stops_csv.exists():
        raise FileNotFoundError(f"Missing file: {stops_csv}")
    if not summary_csv.exists():
        raise FileNotFoundError(f"Missing file: {summary_csv}")

    print(f"Loading routes from: {stops_csv}")
    routes_stops = vr.load_stops(stops_csv)
    summary = vr.load_summary(summary_csv)

    if not no_osrm:
        try:
            loaded = load_routes_from_visualize_html(result_dir)
            if loaded is not None:
                return loaded
            print("Existing routes_map.html found, but it does not contain road geometry. Falling back.")
        except Exception as exc:
            print(f"Existing routes_map.html could not be reused ({exc}). Falling back.")

    closures = load_closures()

    geometry_enabled = not no_osrm and getattr(vr, "_CLOSURES_AVAILABLE", False)
    if geometry_enabled:
        print(f"Route geometry source: OSRM via {osrm_url}")
    elif no_osrm:
        print("Route geometry: skipped via --no-osrm, using straight lines.")
    else:
        print("Route geometry: closures-aware helper unavailable, using straight lines.")

    route_list = vr.build_route_objects(
        routes_stops,
        summary,
        osrm_url=osrm_url if geometry_enabled else None,
        closures=closures if geometry_enabled else None,
    )
    return route_list, closures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare an offline ZIP export for route maps.")
    parser.add_argument(
        "result_dir",
        nargs="?",
        default=None,
        help="Result directory, e.g. data/results/CB/2026-04-10/",
    )
    parser.add_argument(
        "--zip-name",
        default=DEFAULT_ZIP_NAME,
        help=f"Output zip file name (default: {DEFAULT_ZIP_NAME})",
    )
    parser.add_argument(
        "--tile-url",
        default=DEFAULT_TILE_URL,
        help="Tile URL template used during export time.",
    )
    parser.add_argument(
        "--tile-subdomains",
        default=DEFAULT_TILE_SUBDOMAINS,
        help="Subdomains used for {s} in the tile template (default: abcd).",
    )
    parser.add_argument(
        "--tile-attribution",
        default=DEFAULT_TILE_ATTRIBUTION,
        help="Attribution text shown in the offline map.",
    )
    parser.add_argument(
        "--min-zoom",
        type=int,
        default=DEFAULT_MIN_ZOOM,
        help=f"Minimum cached zoom (default: {DEFAULT_MIN_ZOOM})",
    )
    parser.add_argument(
        "--max-zoom",
        type=int,
        default=DEFAULT_MAX_ZOOM,
        help=f"Maximum cached zoom (default: {DEFAULT_MAX_ZOOM})",
    )
    parser.add_argument(
        "--tile-margin",
        type=int,
        default=DEFAULT_TILE_MARGIN,
        help=f"Extra tile margin around data bounds (default: {DEFAULT_TILE_MARGIN})",
    )
    parser.add_argument(
        "--tile-workers",
        type=int,
        default=DEFAULT_TILE_WORKERS,
        help=f"Parallel tile downloads (default: {DEFAULT_TILE_WORKERS})",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"Download timeout in seconds (default: {DEFAULT_TIMEOUT_SEC})",
    )
    parser.add_argument(
        "--leaflet-version",
        default=DEFAULT_LEAFLET_VERSION,
        help=f"Leaflet version to vendor into the package (default: {DEFAULT_LEAFLET_VERSION})",
    )
    parser.add_argument(
        "--no-osrm",
        action="store_true",
        help="Skip road geometry and export straight stop-to-stop lines.",
    )
    parser.add_argument(
        "--osrm-url",
        default="http://localhost:5000",
        help="OSRM base URL used when fetching route geometry.",
    )
    args = parser.parse_args()

    if args.min_zoom < 0 or args.max_zoom < 0:
        parser.error("Zoom values must be non-negative.")
    if args.min_zoom > args.max_zoom:
        parser.error("--min-zoom must be <= --max-zoom.")
    if args.tile_margin < 0:
        parser.error("--tile-margin must be >= 0.")
    if args.tile_workers < 1:
        parser.error("--tile-workers must be >= 1.")
    if args.timeout_sec < 1:
        parser.error("--timeout-sec must be >= 1.")
    return args


def main() -> None:
    args = parse_args()
    result_dir = resolve_result_dir(args.result_dir)
    if not result_dir.exists():
        raise FileNotFoundError(f"Result directory does not exist: {result_dir}")

    if not args.no_osrm:
        _check_osrm(args.osrm_url)

    route_list, closures = build_routes(result_dir, args.no_osrm, args.osrm_url)
    if not route_list:
        raise RuntimeError("No routes available for export.")

    points = collect_bounds_points(route_list, closures)
    if not points:
        raise RuntimeError("No valid coordinates were found for the export.")

    tile_ranges, tile_jobs = iter_tile_jobs(points, args.min_zoom, args.max_zoom, args.tile_margin)
    tile_total = len(tile_jobs)
    print("Tile ranges:")
    for zoom in range(args.min_zoom, args.max_zoom + 1):
        info = tile_ranges[zoom]
        print(
            f"  z{zoom}: x={info['x_min']}..{info['x_max']} "
            f"y={info['y_min']}..{info['y_max']} -> {info['count']} tiles"
        )
    print(f"Total tiles: {tile_total}")

    if tile_total > 5000:
        print("Warning: large tile count, the ZIP may become quite big.")

    tile_ext = detect_tile_extension(args.tile_url)
    zip_name = args.zip_name
    zip_path = result_dir / zip_name

    with tempfile.TemporaryDirectory(prefix="routes_offline_") as temp_dir:
        package_root = Path(temp_dir) / Path(zip_name).stem
        package_root.mkdir(parents=True, exist_ok=True)

        download_leaflet_assets(package_root, args.leaflet_version, args.timeout_sec)
        tile_success, tile_failed = download_tiles(
            package_root,
            args.tile_url,
            args.tile_subdomains,
            tile_ext,
            tile_jobs,
            args.timeout_sec,
            args.tile_workers,
        )

        html = generate_html(
            route_list=route_list,
            title=result_dir.name,
            closures=closures,
            tile_ext=tile_ext,
            min_zoom=args.min_zoom,
            max_zoom=args.max_zoom,
            tile_attribution=args.tile_attribution,
        )
        (package_root / "index.html").write_text(html, encoding="utf-8")
        write_readme(package_root, zip_name)
        write_package_info(
            build_root=package_root,
            result_dir=result_dir,
            args=args,
            route_list=route_list,
            closures=closures,
            tile_ranges=tile_ranges,
            tile_success=tile_success,
            tile_failed=tile_failed,
            zip_name=zip_name,
        )

        if zip_path.exists():
            zip_path.unlink()
        zip_directory(package_root, zip_path)

    print("")
    print(f"Offline ZIP created: {zip_path}")
    print(f"Routes: {len(route_list)}")
    print(f"Tiles ok/failed: {tile_success}/{tile_failed}")
    print("Recipient flow: unzip -> open index.html")


if __name__ == "__main__":
    main()
