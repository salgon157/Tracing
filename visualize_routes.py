"""
visualize_routes.py — interaktivní mapa tras z výstupu solveru
==============================================================

Použití:
  python visualize_routes.py data/results/CB/2026-04-10/
  python visualize_routes.py data/results/CB/2026-04-10/ --open

Výstup:
  data/results/CB/2026-04-10/routes_map.html

Navigace v prohlížeči:
  ◀ ▶  tlačítka nebo šipky ← →  na klávesnici
  Klik na zastávku v seznamu → vycentruje mapu
  Klik na marker → popup s detaily
"""

import csv
import json
import argparse
import sys
import webbrowser
from pathlib import Path

try:
    from closures_utils import (
        build_closure_aware_route_for_pair,
        load_active_closures,
    )
    _CLOSURES_AVAILABLE = True
except ImportError:
    _CLOSURES_AVAILABLE = False

ROUTE_COLORS = [
    "#2196F3", "#E91E63", "#4CAF50", "#FF9800", "#9C27B0",
    "#00BCD4", "#F44336", "#8BC34A", "#FF5722", "#3F51B5",
    "#009688", "#FFC107", "#673AB7", "#795548", "#607D8B",
]


def load_stops(stops_csv: Path) -> dict:
    """Načte lines_stops.csv, vrátí dict {line_id: [stops]}."""
    routes = {}
    with open(stops_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            line_id = row["line_id"]
            if line_id not in routes:
                routes[line_id] = []
            stop = {
                "place":    row.get("place", ""),
                "order_id": row.get("order_id", ""),
                "arrival":  row.get("arrival", ""),
                "leg_km":   _float_or_none(row.get("leg_km")),
                "kg":       _float_or_none(row.get("kg")),
                "window":   row.get("window", ""),
                "note":     row.get("note", ""),
                "lat":      _float_or_none(row.get("lat")),
                "lon":      _float_or_none(row.get("lon")),
            }
            routes[line_id].append(stop)
    return routes


def load_summary(summary_csv: Path) -> dict:
    """Načte lines_summary.csv, vrátí dict {line_id: summary_row}."""
    summary = {}
    with open(summary_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            summary[row["line_id"]] = row
    return summary


def _float_or_none(val):
    try:
        return float(val) if val not in (None, "", "nan") else None
    except (ValueError, TypeError):
        return None


def _check_osrm(url: str) -> None:
    """Ověří dostupnost OSRM. Při chybě vypíše jasnou hlášku a ukončí program."""
    import requests as _req
    try:
        _req.get(f"{url}/health", timeout=3)
    except Exception:
        print(f"\n[CHYBA] OSRM není dostupný na {url}")
        print("  Vizualizace vyžaduje OSRM pro trasování po silnicích.")
        print("  Spusť Docker a zkus znovu, nebo použij --no-osrm pro vzdušné čáry.")
        sys.exit(1)


def fetch_route_geometry(stops: list, osrm_url: str = "http://localhost:5000",
                         closures: list | None = None) -> list | None:
    """
    Fetches per-leg geometry through the shared closure-aware helper so the map
    matches the solver's closure logic.
    """
    valid = [(s["lat"], s["lon"]) for s in stops
             if s.get("lat") is not None and s.get("lon") is not None]
    if len(valid) < 2:
        return None

    route_cache: dict = {}
    geometry: list = []
    for idx in range(len(valid) - 1):
        route = build_closure_aware_route_for_pair(
            valid[idx],
            valid[idx + 1],
            closures=closures or [],
            matrix_profile="driving",
            osrm_url=osrm_url,
            closure_route_profile="driving-hgv",
            cache=route_cache,
        )
        if not route or not route.get("geometry"):
            return None
        leg_geometry = route["geometry"]
        if geometry:
            geometry.extend(leg_geometry[1:])
        else:
            geometry.extend(leg_geometry)
    return geometry


def build_route_objects(routes_stops: dict, summary: dict, osrm_url: str | None = None,
                        closures: list | None = None) -> list:
    route_list = []
    n = len(routes_stops)
    for i, (line_id, stops) in enumerate(sorted(routes_stops.items())):
        s = summary.get(line_id, {})

        geometry = None
        if osrm_url:
            print(f"  [{i+1}/{n}] {line_id} — stahuji closure-aware geometrii...", end="\r")
            geometry = fetch_route_geometry(stops, osrm_url, closures=closures)

        route_list.append({
            "line_id":       line_id,
            "vehicle_id":    s.get("vehicle_id", ""),
            "vehicle_type":  s.get("vehicle_type", ""),
            "total_km":      _float_or_none(s.get("total_km")) or 0,
            "duration_h":    _float_or_none(s.get("duration_h")) or 0,
            "total_kg":      _float_or_none(s.get("total_kg")) or 0,
            "total_cost_kc": _float_or_none(s.get("total_cost_kc")) or 0,
            "color":         ROUTE_COLORS[i % len(ROUTE_COLORS)],
            "stops":         stops,
            "geometry":      geometry,   # [[lat,lon], ...] po silnicích, nebo None = vzdušná čára
        })
    if osrm_url:
        fetched = sum(1 for r in route_list if r["geometry"])
        print(f"  Geometrie stažena pro {fetched}/{n} tras.          ")
    return route_list


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Mapa tras — {title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', Arial, sans-serif; display: flex; flex-direction: column; height: 100vh; background: #1a1a2e; color: #eee; }

#header {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 16px; background: #16213e; border-bottom: 2px solid #0f3460;
  min-height: 52px; flex-shrink: 0;
}
.nav-btn {
  background: #0f3460; border: none; color: #e94560;
  font-size: 20px; width: 36px; height: 36px; cursor: pointer;
  border-radius: 6px; transition: background 0.2s;
}
.nav-btn:hover { background: #e94560; color: white; }

#route-badge {
  font-size: 15px; font-weight: bold; color: white;
  background: #0f3460; padding: 4px 12px; border-radius: 20px;
  border-left: 4px solid #e94560; white-space: nowrap;
}
#route-meta { font-size: 13px; color: #a8b2d8; flex: 1; }
#route-meta b { color: white; }
#route-counter { font-size: 12px; color: #57689e; margin-left: auto; white-space: nowrap; }
#kbd-hint { font-size: 11px; color: #3a4a7a; white-space: nowrap; }

#content { display: flex; flex: 1; overflow: hidden; }

#sidebar {
  width: 300px; min-width: 220px; max-width: 380px;
  overflow-y: auto; background: #16213e;
  border-right: 1px solid #0f3460; flex-shrink: 0;
}
#sidebar::-webkit-scrollbar { width: 6px; }
#sidebar::-webkit-scrollbar-track { background: #16213e; }
#sidebar::-webkit-scrollbar-thumb { background: #0f3460; border-radius: 3px; }

.stop-item {
  padding: 8px 12px; border-bottom: 1px solid #0f3460;
  cursor: pointer; transition: background 0.15s;
}
.stop-item:hover { background: #0f3460; }
.stop-depot { background: #1a1040; border-left: 3px solid #e94560; }
.stop-depot:hover { background: #280f58; }
.stop-number {
  display: inline-block; width: 20px; height: 20px; line-height: 20px;
  text-align: center; border-radius: 50%; font-size: 10px; font-weight: bold;
  margin-right: 6px; vertical-align: middle; flex-shrink: 0;
}
.stop-row-top { display: flex; align-items: center; }
.stop-name { font-size: 13px; color: #eee; flex: 1; }
.stop-arrival { font-size: 11px; color: #7f8fc7; margin-left: 4px; white-space: nowrap; }
.stop-details { font-size: 11px; color: #57689e; margin-top: 2px; padding-left: 26px; }
.stop-highlight { background: #0f3460 !important; outline: 2px solid #e94560; }

#map { flex: 1; }

.leaflet-popup-content-wrapper { background: #16213e; color: #eee; border: 1px solid #0f3460; }
.leaflet-popup-tip { background: #16213e; }
.popup-title { font-weight: bold; font-size: 13px; margin-bottom: 4px; color: white; }
.popup-row { font-size: 12px; color: #a8b2d8; }

.depot-icon {
  background: #e94560; border-radius: 50%; border: 2px solid white;
  width: 14px; height: 14px; margin: 3px;
}
</style>
</head>
<body>

<div id="header">
  <button class="nav-btn" id="prev-btn" title="Předchozí trasa (←)">&#9664;</button>
  <button class="nav-btn" id="next-btn" title="Další trasa (→)">&#9654;</button>
  <div id="route-badge">LINE_01</div>
  <div id="route-meta"></div>
  <div id="route-counter"></div>
  <div id="kbd-hint">← → klávesy</div>
</div>

<div id="content">
  <div id="sidebar"></div>
  <div id="map"></div>
</div>

<script>
const ROUTES   = ROUTES_DATA_PLACEHOLDER;
const CLOSURES = CLOSURES_DATA_PLACEHOLDER;

const map = L.map('map', { zoomControl: true });
L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> © <a href="https://carto.com/">CARTO</a>',
  subdomains: 'abcd',
  maxZoom: 19
}).addTo(map);

let currentIdx = 0;
let currentLayers = [];
let currentMarkers = [];

function makeNumberIcon(num, color, isDepot) {
  if (isDepot) {
    return L.divIcon({
      html: `<div style="background:#e94560;width:16px;height:16px;border-radius:50%;border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,.5)"></div>`,
      iconSize: [20, 20], iconAnchor: [10, 10], className: ''
    });
  }
  return L.divIcon({
    html: `<div style="background:${color};color:white;width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:bold;border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,.5)">${num}</div>`,
    iconSize: [26, 26], iconAnchor: [13, 13], className: ''
  });
}

function renderRoute(idx) {
  currentLayers.forEach(l => { try { map.removeLayer(l); } catch(e){} });
  currentLayers = [];
  currentMarkers = [];

  const r = ROUTES[idx];
  const color = r.color;

  // Header
  document.getElementById('route-badge').textContent = r.line_id;
  document.getElementById('route-badge').style.borderColor = color;
  document.getElementById('route-meta').innerHTML =
    `<b>${r.vehicle_type}</b>${r.vehicle_id ? ' &nbsp;|&nbsp; ' + r.vehicle_id : ''} &nbsp;|&nbsp; ` +
    `<b>${r.total_km.toFixed(1)}</b> km &nbsp;|&nbsp; ` +
    `<b>${r.duration_h.toFixed(1)}</b> h &nbsp;|&nbsp; ` +
    `<b>${r.total_kg.toFixed(0)}</b> kg &nbsp;|&nbsp; ` +
    `<b>${Math.round(r.total_cost_kc).toLocaleString('cs-CZ')}</b> Kč`;
  document.getElementById('route-counter').textContent = `${idx + 1} / ${ROUTES.length}`;

  // Collect valid GPS stops
  const validStops = r.stops.filter(s => s.lat != null && s.lon != null);
  if (validStops.length === 0) {
    document.getElementById('sidebar').innerHTML = '<div style="padding:16px;color:#57689e">Žádné GPS souřadnice.</div>';
    return;
  }

  // Polyline — reálné silnice (geometry z OSRM) nebo fallback vzdušná čára
  const latlngs = r.geometry ? r.geometry : validStops.map(s => [s.lat, s.lon]);
  const poly = L.polyline(latlngs, { color, weight: 3.5, opacity: 0.85 }).addTo(map);
  currentLayers.push(poly);

  // Markers
  let stopCounter = 0;
  r.stops.forEach((s, i) => {
    if (s.lat == null || s.lon == null) return;
    const isDepot = i === 0 || i === r.stops.length - 1;
    if (!isDepot) stopCounter++;
    const displayNum = isDepot ? '●' : stopCounter;
    const marker = L.marker([s.lat, s.lon], {
      icon: makeNumberIcon(displayNum, color, isDepot),
      zIndexOffset: isDepot ? 1000 : 0
    });

    const kgStr = s.kg > 0 ? `${s.kg.toFixed(0)} kg` : '';
    const legStr = s.leg_km != null && s.leg_km > 0 ? `+${s.leg_km.toFixed(1)} km` : '';
    const winStr = s.window ? `<span style="color:#7f8fc7">[${s.window}]</span>` : '';
    const noteStr = s.note ? `<br><i style="color:#a8b2d8">${s.note}</i>` : '';
    marker.bindPopup(
      `<div class="popup-title">${s.place}</div>` +
      `<div class="popup-row">${s.arrival} ${winStr}</div>` +
      (kgStr || legStr ? `<div class="popup-row">${[kgStr, legStr].filter(Boolean).join(' &nbsp;|&nbsp; ')}</div>` : '') +
      noteStr,
      { maxWidth: 260 }
    );
    marker.addTo(map);
    currentLayers.push(marker);
    currentMarkers.push({ marker, stopIdx: i });
  });

  // Fit bounds
  try { map.fitBounds(poly.getBounds(), { padding: [30, 30] }); } catch(e) {}

  // Sidebar
  const sidebar = document.getElementById('sidebar');
  stopCounter = 0;
  sidebar.innerHTML = r.stops.map((s, i) => {
    const isDepot = i === 0 || i === r.stops.length - 1;
    if (!isDepot) stopCounter++;
    const num = isDepot ? '' : stopCounter;
    const numEl = num !== '' ? `<span class="stop-number" style="background:${color}">${num}</span>` : `<span class="stop-number" style="background:#e94560">⌂</span>`;
    const legStr = s.leg_km != null && s.leg_km > 0 ? ` +${s.leg_km.toFixed(1)} km` : '';
    const kgStr = s.kg > 0 ? ` · ${s.kg.toFixed(0)} kg` : '';
    const winStr = s.window ? ` · ${s.window}` : '';
    const noteStr = s.note ? ` · ${s.note}` : '';
    return `<div class="stop-item ${isDepot ? 'stop-depot' : ''}" id="stop-${i}" onclick="focusStop(${idx},${i})">
      <div class="stop-row-top">
        ${numEl}
        <span class="stop-name">${s.place}</span>
        <span class="stop-arrival">${s.arrival}${legStr}</span>
      </div>
      ${(kgStr || winStr || noteStr) ? `<div class="stop-details">${(kgStr + winStr + noteStr).replace(/^ · /, '')}</div>` : ''}
    </div>`;
  }).join('');
}

function focusStop(routeIdx, stopIdx) {
  const s = ROUTES[routeIdx].stops[stopIdx];
  if (s.lat != null && s.lon != null) {
    map.setView([s.lat, s.lon], 15, { animate: true });
    // Highlight in sidebar
    document.querySelectorAll('.stop-highlight').forEach(el => el.classList.remove('stop-highlight'));
    const el = document.getElementById(`stop-${stopIdx}`);
    if (el) { el.classList.add('stop-highlight'); el.scrollIntoView({ block: 'nearest' }); }
    // Open popup
    const found = currentMarkers.find(m => m.stopIdx === stopIdx);
    if (found) found.marker.openPopup();
  }
}

function goNext() { currentIdx = (currentIdx + 1) % ROUTES.length; renderRoute(currentIdx); }
function goPrev() { currentIdx = (currentIdx - 1 + ROUTES.length) % ROUTES.length; renderRoute(currentIdx); }

document.getElementById('next-btn').onclick = goNext;
document.getElementById('prev-btn').onclick = goPrev;
document.addEventListener('keydown', e => {
  if (e.key === 'ArrowRight') goNext();
  if (e.key === 'ArrowLeft')  goPrev();
});

renderRoute(0);

// ── Uzavírky — permanentní vrstva ──────────────────────────────
function renderClosures() {
  CLOSURES.forEach(c => {
    const f = c.segment.from;
    const t = c.segment.to;
    if (f.lat == null || t.lat == null) return;

    // Červená úsečka uzavřeného úseku
    const line = L.polyline([[f.lat, f.lon], [t.lat, t.lon]], {
      color: '#e53935', weight: 4, opacity: 0.9,
      dashArray: '8 5'
    }).addTo(map);
    line.bindTooltip(`🚧 ${c.id}: ${c.name}`, { sticky: true });

    // Červené koncové markery
    [f, t].forEach(pt => {
      L.circleMarker([pt.lat, pt.lon], {
        radius: 5, color: '#e53935', fillColor: '#e53935',
        fillOpacity: 1, weight: 2
      }).addTo(map);
    });

  });
}
if (CLOSURES.length > 0) renderClosures();
</script>
</body>
</html>
"""


def _closures_to_js(closures: list) -> list:
    """Převede closures na serializovatelný seznam pro JS."""
    result = []
    for c in closures:
        seg = c.get("segment", {})
        entry = {
            "id":      c.get("id", ""),
            "name":    c.get("name", ""),
            "segment": {
                "from": {"lat": seg.get("from", {}).get("lat"), "lon": seg.get("from", {}).get("lon")},
                "to":   {"lat": seg.get("to",   {}).get("lat"), "lon": seg.get("to",   {}).get("lon")},
            },
        }
        result.append(entry)
    return result


def generate_html(route_list: list, title: str, closures: list | None = None) -> str:
    routes_json   = json.dumps(route_list, ensure_ascii=False, indent=None)
    closures_json = json.dumps(_closures_to_js(closures or []), ensure_ascii=False, indent=None)
    return (HTML_TEMPLATE
            .replace("ROUTES_DATA_PLACEHOLDER",   routes_json)
            .replace("CLOSURES_DATA_PLACEHOLDER", closures_json)
            .replace("{title}", title))


def find_latest_result_dir() -> Path | None:
    """Pokusí se najít nejnovější výsledkovou složku v data/results/."""
    base = Path("data/results")
    if not base.exists():
        return None
    candidates = sorted(
        [p for p in base.glob("*/*/*") if p.is_dir()],
        key=lambda p: str(p),
        reverse=True,
    )
    for c in candidates:
        if (c / "lines_stops.csv").exists():
            return c
    return None


def main():
    parser = argparse.ArgumentParser(description="Vizualizace tras VRP solveru")
    parser.add_argument("result_dir", nargs="?", default=None,
                        help="Složka s výsledky, např. data/results/CB/2026-04-10/")
    parser.add_argument("--open", action="store_true",
                        help="Automaticky otevřít HTML v prohlížeči")
    parser.add_argument("--no-osrm", action="store_true",
                        help="Přeskočit OSRM — zobrazí vzdušné čáry místo silnic")
    parser.add_argument("--osrm-url", default="http://localhost:5000",
                        help="OSRM base URL (default: http://localhost:5000)")
    parser.add_argument("--fresh-osm", action="store_true",
                        help="Použij čerstvou routing instanci (port 5001, C:\\osrm_current). "
                             "Zkratka pro --osrm-url http://localhost:5001 — vyhrává nad ní.")
    args = parser.parse_args()

    # --fresh-osm má přednost před --osrm-url, aby uživateli stačil jeden krátký flag
    if args.fresh_osm:
        args.osrm_url = "http://localhost:5001"
        print(f"[OSM] zdroj: current (fresh) | OSRM={args.osrm_url}")
        # Self-contained: orchestrator stáhne data a nastartuje Docker kontejnery,
        # pokud neběží. Při --no-osrm nemá smysl spouštět routing — přeskoč.
        if not args.no_osrm:
            from osrm_orchestrator import ensure_fresh_routing_ready
            ensure_fresh_routing_ready()

    if args.result_dir:
        result_dir = Path(args.result_dir)
    else:
        result_dir = find_latest_result_dir()
        if result_dir is None:
            print("[CHYBA] Nenalezena žádná výsledková složka v data/results/")
            print("  Zadej cestu: python visualize_routes.py data/results/CB/2026-04-10/")
            return

    stops_csv   = result_dir / "lines_stops.csv"
    summary_csv = result_dir / "lines_summary.csv"
    output_html = result_dir / "routes_map.html"

    if not stops_csv.exists():
        print(f"[CHYBA] Nenalezen: {stops_csv}")
        return
    if not summary_csv.exists():
        print(f"[CHYBA] Nenalezen: {summary_csv}")
        return

    print(f"Načítám: {stops_csv}")
    routes_stops = load_stops(stops_csv)
    summary      = load_summary(summary_csv)

    osrm_url = None if args.no_osrm else args.osrm_url
    if osrm_url:
        _check_osrm(osrm_url)
        print(f"OSRM geometrie: {osrm_url}")
    else:
        print("OSRM přeskočeno — vzdušné čáry")

    # Načti aktivní uzavírky (pokud je modul dostupný)
    closures = []
    if _CLOSURES_AVAILABLE:
        closures = load_active_closures()
        if closures:
            print(f"Uzavírky:     {len(closures)} aktivních — trasování přes objízdky")
        else:
            print("Uzavírky:     žádné aktivní")
    else:
        print("Uzavírky:     closures_utils nedostupný — přeskočeno")

    route_list = build_route_objects(routes_stops, summary, osrm_url=osrm_url,
                                     closures=closures if osrm_url else None)

    if not route_list:
        print("[CHYBA] Žádné trasy k zobrazení.")
        return

    title = result_dir.name  # datum nebo název složky
    html  = generate_html(route_list, title, closures=closures)

    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html)

    total_stops = sum(len(r["stops"]) for r in route_list)
    print(f"\nVygenerováno: {output_html}")
    print(f"Tras:         {len(route_list)}")
    print(f"Zastávek:     {total_stops}")
    print(f"\nOtevři v prohlížeči nebo spusť s --open:")
    print(f"  python visualize_routes.py {result_dir} --open")

    if args.open:
        webbrowser.open(output_html.resolve().as_uri())
        print("Otevírám v prohlížeči...")


if __name__ == "__main__":
    main()
