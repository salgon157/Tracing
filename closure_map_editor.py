"""
closure_map_editor.py — interaktivní mapový editor uzavírek
============================================================

Spuštění:
  python closure_map_editor.py

Otevře prohlížeč na http://localhost:8765
Klikáš na mapu, server zapíše přímo do data/static/closures.json

Ovládání:
  Krok 1 — klikni na začátek uzavírky (červený bod)
  Krok 2 — klikni na konec uzavírky   (červená čára)
  Krok 3 — vyplň název a klikni Uložit

Objízdka se počítá automaticky přes ORS avoid_polygon při spuštění solveru.
Na mapě je zobrazena vizualizace objízdky (zelená čára) pokud ORS běží.
"""

import json
import math
import re
import webbrowser
import threading
import requests as _requests
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import date

CLOSURES_FILE    = Path("data/static/closures.json")
PORT             = 8765
ORS_URL          = "http://localhost:8080"
ORS_PROFILE      = "driving-hgv"
PROBE_DIST_KM    = 4.0
CLOSURE_BUFFER_M = 80

# Sklad — souřadnice z vrp_solver_lines_v6.py::DEPOT
DEPOT_LAT      = 49.5061806
DEPOT_LON      = 15.5950131
DEPOT_NEAR_KM  = 4.0   # uzavírky do 4 km od skladu dostanou depot trasy

DEPOT_DIRECTIONS = [
    (0,   "→ S"),
    (45,  "→ SV"),
    (90,  "→ V"),
    (135, "→ JV"),
    (180, "→ J"),
    (225, "→ JZ"),
    (270, "→ Z"),
    (315, "→ SZ"),
]


# ============================================================
#  DATA
# ============================================================

def load_data() -> dict:
    if not CLOSURES_FILE.exists():
        return {"version": 1, "closures": []}
    with open(CLOSURES_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict):
    CLOSURES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CLOSURES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def next_id(closures: list) -> str:
    nums = [int(m.group(1)) for c in closures
            if (m := re.match(r"CLO_(\d+)", c["id"]))]
    return f"CLO_{(max(nums) + 1) if nums else 1:03d}"


def append_closure(body: dict) -> dict:
    data  = load_data()
    new_c = {
        "id":        next_id(data["closures"]),
        "name":      body.get("name", "Uzavírka"),
        "active":    True,
        "created":   str(date.today()),
        "valid_from": body.get("valid_from") or str(date.today()),
        "valid_to":   body.get("valid_to") or None,
        "segment": {
            "from": body["segment"]["from"],
            "to":   body["segment"]["to"],
        },
        "buffer_km": float(body.get("buffer_km", 0.05)),
        "notes":     body.get("notes", ""),
    }
    data["closures"].append(new_c)
    save_data(data)
    return new_c


def migrate_remove_detour_options():
    """Odstraní zastaralé detour_options z closures.json."""
    data = load_data()
    changed = False
    for c in data["closures"]:
        if "detour_options" in c:
            del c["detour_options"]
            changed = True
    if changed:
        save_data(data)
        print("  Migrace: detour_options odstraněny z closures.json (nyní ORS overhead)")


# ============================================================
#  ORS BYPASS VIZUALIZACE
# ============================================================

def _offset_point(lat, lon, bearing, dist_km):
    R = 6371.0
    b = math.radians(bearing)
    d = dist_km / R
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(math.sin(lat1) * math.cos(d)
                     + math.cos(lat1) * math.sin(d) * math.cos(b))
    lon2 = lon1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def _segment_to_polygon(a_lat, a_lon, b_lat, b_lon, buffer_m):
    mid_lat = (a_lat + b_lat) / 2
    buf_lat = buffer_m / 111_111
    buf_lon = buffer_m / (111_111 * math.cos(math.radians(mid_lat)))
    dlat = b_lat - a_lat
    dlon = b_lon - a_lon
    length = math.sqrt(dlat ** 2 + dlon ** 2)
    if length < 1e-10:
        corners = [
            [a_lon - buf_lon, a_lat - buf_lat],
            [a_lon + buf_lon, a_lat - buf_lat],
            [a_lon + buf_lon, a_lat + buf_lat],
            [a_lon - buf_lon, a_lat + buf_lat],
        ]
    else:
        perp_lat = -dlon / length
        perp_lon =  dlat / length
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
    corners.append(corners[0])
    return {"type": "Polygon", "coordinates": [corners]}


PROBE_LABELS = [
    (0,   180, "S → J"),
    (45,  225, "SV → JZ"),
    (90,  270, "V → Z"),
    (135, 315, "JV → SZ"),
]


def _build_combined_polygon(closures: list) -> dict:
    """
    Sestaví MultiPolygon ze všech aktivních uzavírek — stejná logika jako solver.
    ORS se pak vyhýbá všem uzavírkám najednou, takže objízdka jedné nevede
    přes druhou.
    """
    polygons = []
    for c in closures:
        seg   = c["segment"]
        a_lat, a_lon = seg["from"]["lat"], seg["from"]["lon"]
        b_lat, b_lon = seg["to"]["lat"],   seg["to"]["lon"]
        buf_m = max(CLOSURE_BUFFER_M, c.get("buffer_km", 0.05) * 1000)
        poly  = _segment_to_polygon(a_lat, a_lon, b_lat, b_lon, buf_m)
        polygons.append(poly["coordinates"])
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def get_all_bypasses(cid: str) -> list:
    """
    Zavolá ORS s kombinovaným avoid_polygon (všechny aktivní uzavírky) pro
    všechny 4 osy kolem dané uzavírky a vrátí seznam tras:
    [{"label": "V → Z", "coords": [[lat, lon], ...], "dur_min": X}, ...]

    Kombinovaný polygon zajistí, že zobrazená objízdka neprochází jinými
    uzavírkami — stejné chování jako solver.
    """
    data      = load_data()
    closure   = next((c for c in data["closures"] if c["id"] == cid), None)
    if not closure:
        return []

    # Kombinovaný polygon — všechny aktivní uzavírky najednou
    active     = [c for c in data["closures"] if c.get("active")]
    avoid_poly = _build_combined_polygon(active)

    seg   = closure["segment"]
    a_lat, a_lon = seg["from"]["lat"], seg["from"]["lon"]
    b_lat, b_lon = seg["to"]["lat"],   seg["to"]["lon"]
    mid_lat = (a_lat + b_lat) / 2
    mid_lon = (a_lon + b_lon) / 2

    url    = f"{ORS_URL}/ors/v2/directions/{ORS_PROFILE}/geojson"
    routes = []

    for bear_from, bear_to, label in PROBE_LABELS:
        pt_from = _offset_point(mid_lat, mid_lon, bear_from, PROBE_DIST_KM)
        pt_to   = _offset_point(mid_lat, mid_lon, bear_to,   PROBE_DIST_KM)
        body = {
            "coordinates": [
                [pt_from[1], pt_from[0]],
                [pt_to[1],   pt_to[0]],
            ],
            "options": {"avoid_polygons": avoid_poly},
        }
        try:
            r = _requests.post(url, json=body, timeout=10)
            if r.status_code == 200:
                feat   = r.json()["features"][0]
                coords = feat["geometry"]["coordinates"]
                dur_s  = feat["properties"]["summary"]["duration"]
                routes.append({
                    "label":   label,
                    "coords":  [[lat, lon] for lon, lat in coords],
                    "dur_min": round(dur_s / 60, 1),
                })
        except Exception:
            pass

    return routes


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(min(1.0, a)))


def get_depot_bypasses(cid: str) -> list | None:
    """
    Pro uzavírky do DEPOT_NEAR_KM od skladu vrátí 8 tras
    Sklad → probe bod v každém směru od uzavírky (avoid všech uzavírek).
    Vrátí None pokud uzavírka není blízko skladu.
    """
    data    = load_data()
    closure = next((c for c in data["closures"] if c["id"] == cid), None)
    if not closure:
        return None

    seg     = closure["segment"]
    a_lat, a_lon = seg["from"]["lat"], seg["from"]["lon"]
    b_lat, b_lon = seg["to"]["lat"],   seg["to"]["lon"]
    mid_lat = (a_lat + b_lat) / 2
    mid_lon = (a_lon + b_lon) / 2

    if _haversine_km(DEPOT_LAT, DEPOT_LON, mid_lat, mid_lon) > DEPOT_NEAR_KM:
        return None   # uzavírka je daleko od skladu — depot trasy nedávají smysl

    active     = [c for c in data["closures"] if c.get("active")]
    avoid_poly = _build_combined_polygon(active)
    url        = f"{ORS_URL}/ors/v2/directions/{ORS_PROFILE}/geojson"
    routes     = []

    for bearing, label in DEPOT_DIRECTIONS:
        pt_to = _offset_point(mid_lat, mid_lon, bearing, PROBE_DIST_KM)
        body  = {
            "coordinates": [
                [DEPOT_LON, DEPOT_LAT],          # start: sklad
                [pt_to[1],  pt_to[0]],           # cíl: probe bod
            ],
            "options": {"avoid_polygons": avoid_poly},
        }
        try:
            r = _requests.post(url, json=body, timeout=10)
            if r.status_code == 200:
                feat   = r.json()["features"][0]
                coords = feat["geometry"]["coordinates"]
                dur_s  = feat["properties"]["summary"]["duration"]
                routes.append({
                    "label":   f"Sklad {label}",
                    "coords":  [[lat, lon] for lon, lat in coords],
                    "dur_min": round(dur_s / 60, 1),
                    "bearing": bearing,
                })
        except Exception:
            pass

    return routes


# ============================================================
#  HTML
# ============================================================

HTML = r"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Editor uzavírek — VRP</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', Arial, sans-serif; display: flex; height: 100vh; background: #1a1a2e; color: #eee; }

#sidebar {
  width: 340px; min-width: 280px; background: #16213e;
  border-right: 2px solid #0f3460; display: flex; flex-direction: column;
  overflow-y: auto; flex-shrink: 0;
}
#sidebar::-webkit-scrollbar { width: 6px; }
#sidebar::-webkit-scrollbar-thumb { background: #0f3460; border-radius: 3px; }
#map { flex: 1; }

h2 { font-size: 15px; color: white; padding: 14px 16px 10px; border-bottom: 1px solid #0f3460; }

.steps { padding: 12px 16px; border-bottom: 1px solid #0f3460; }
.step {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 8px 0; opacity: 0.4; transition: opacity .2s;
}
.step.active { opacity: 1; }
.step.done   { opacity: 0.6; }
.step-num {
  width: 24px; height: 24px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: bold; margin-top: 1px;
}
.step-1 .step-num, .step-2 .step-num { background: #e94560; color: white; }
.step-3 .step-num { background: #2196F3; color: white; }
.step-text { font-size: 13px; line-height: 1.4; }
.step-text b { color: white; font-size: 12px; display: block; margin-bottom: 2px; }
.step-text span { color: #7f8fc7; font-size: 12px; }

.form-section { padding: 12px 16px; border-bottom: 1px solid #0f3460; }
.form-section label { font-size: 12px; color: #a8b2d8; display: block; margin-bottom: 4px; margin-top: 10px; }
.form-section label:first-child { margin-top: 0; }
.form-section input, .form-section textarea {
  width: 100%; background: #0f3460; border: 1px solid #1a3a6e;
  color: white; padding: 7px 10px; border-radius: 4px; font-size: 13px; outline: none;
}
.form-section input:focus { border-color: #2196F3; }
.form-row { display: flex; gap: 8px; }
.form-row > div { flex: 1; }

.coord-item {
  display: flex; align-items: center; gap: 6px;
  background: #0f3460; border-radius: 4px; padding: 5px 8px; margin-bottom: 4px;
  font-size: 12px; color: #a8b2d8;
}
.coord-label { font-weight: bold; flex: 1; }
.coord-val   { color: #7f8fc7; font-size: 11px; }

.btn {
  width: 100%; padding: 10px; border: none; border-radius: 6px;
  font-size: 14px; font-weight: bold; cursor: pointer; transition: background .2s;
}
.btn-save  { background: #e94560; color: white; margin-top: 4px; }
.btn-save:hover    { background: #c62a47; }
.btn-save:disabled { background: #444; cursor: not-allowed; }
.btn-reset { background: #333; color: #aaa; margin-top: 6px; }
.btn-reset:hover { background: #555; color: white; }
.actions { padding: 12px 16px; }

#status {
  padding: 8px 16px; font-size: 12px; min-height: 36px;
  border-top: 1px solid #0f3460; color: #7f8fc7;
}
#status.ok  { color: #4CAF50; }
#status.err { color: #e94560; }

.existing-section { padding: 0 16px 16px; border-top: 1px solid #0f3460; }
.existing-title { font-size: 12px; color: #57689e; padding: 10px 0 6px; }

.closure-item {
  background: #0f3460; border-radius: 6px; padding: 8px 10px;
  margin-bottom: 6px; font-size: 12px;
}
.closure-item-header {
  display: flex; align-items: center; gap: 6px; margin-bottom: 4px;
}
.closure-id   { font-weight: bold; color: white; font-size: 13px; }
.closure-name { color: #a8b2d8; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.closure-active { font-size: 11px; font-weight: bold; }
.closure-active.on  { color: #4CAF50; }
.closure-active.off { color: #e94560; }
.closure-meta { color: #57689e; font-size: 11px; margin-bottom: 6px; }
.closure-actions { display: flex; gap: 6px; }
.closure-btn {
  flex: 1; padding: 4px 0; font-size: 11px; font-weight: bold;
  border: none; border-radius: 4px; cursor: pointer; transition: background .2s;
}
.btn-toggle  { background: #1a3a6e; color: #a8b2d8; }
.btn-toggle:hover  { background: #2196F3; color: white; }
.btn-bypass  { background: #1a3a6e; color: #4CAF50; }
.btn-bypass:hover  { background: #1b5e20; color: white; }
.btn-bypass.loaded { background: #1b5e20; color: #81C784; }
.btn-delete  { background: #1a3a6e; color: #e94560; }
.btn-delete:hover  { background: #7f0000; color: white; }
.btn-locate  { background: #1a3a6e; color: #FFC107; }
.btn-locate:hover  { background: #e65100; color: white; }
.btn-depot   { background: #1a3a6e; color: #F06292; }
.btn-depot:hover   { background: #880e4f; color: white; }
.btn-depot.loaded  { background: #880e4f; color: #F48FB1; }
</style>
</head>
<body>

<div id="sidebar">
  <h2>🚧 Editor uzavírek</h2>

  <div class="steps">
    <div class="step step-1 active" id="step1">
      <div class="step-num">1</div>
      <div class="step-text"><b>Začátek uzavírky</b><span>Klikni na mapu — červený bod</span></div>
    </div>
    <div class="step step-2" id="step2">
      <div class="step-num">2</div>
      <div class="step-text"><b>Konec uzavírky</b><span>Klikni na mapu — červená čára</span></div>
    </div>
    <div class="step step-3" id="step3">
      <div class="step-num">3</div>
      <div class="step-text"><b>Název a uložení</b><span>Vyplň formulář a ulož</span></div>
    </div>
  </div>

  <div class="form-section">
    <label>Název uzavírky *</label>
    <input type="text" id="name" placeholder="např. Most Golčův Jeníkov">

    <label>Uzavřený úsek</label>
    <div id="closure-coords">
      <div class="coord-item" id="pt-start" style="opacity:.4">
        <span class="coord-label" style="color:#e94560">▶ Start</span>
        <span class="coord-val" id="start-val">—</span>
      </div>
      <div class="coord-item" id="pt-end" style="opacity:.4">
        <span class="coord-label" style="color:#e94560">■ Konec</span>
        <span class="coord-val" id="end-val">—</span>
      </div>
    </div>

    <label>Buffer detekce (km)</label>
    <input type="number" id="buffer" value="0.05" step="0.05" min="0.02" max="2.0">

    <div class="form-row" style="margin-top:10px">
      <div>
        <label>Platí od</label>
        <input type="date" id="valid-from">
      </div>
      <div>
        <label>Platí do (prázdné = ∞)</label>
        <input type="date" id="valid-to">
      </div>
    </div>

    <label>Poznámka</label>
    <input type="text" id="notes" placeholder="volitelné">
  </div>

  <div class="actions">
    <button class="btn btn-save" id="btn-save" onclick="saveClosure()" disabled>
      💾 Uložit uzavírku
    </button>
    <button class="btn btn-reset" onclick="resetNew()">↺ Reset</button>
  </div>

  <div id="status">Klikni na mapu pro zadání začátku uzavírky.</div>

  <div class="existing-section" id="existing-section" style="display:none">
    <div style="display:flex;align-items:center;gap:8px;padding:10px 0 6px">
      <span class="existing-title" style="padding:0;flex:1">Uložené uzavírky</span>
      <button class="closure-btn btn-bypass" id="btn-show-all"
              style="flex:none;padding:4px 10px;font-size:11px"
              onclick="showAllBypasses()">↺ Zobraz vše</button>
    </div>
    <div id="existing-list"></div>
  </div>
</div>

<div id="map"></div>

<script>
// Sklad
const DEPOT_LAT     = 49.5061806;
const DEPOT_LON     = 15.5950131;
const DEPOT_NEAR_KM = 4.0;

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371, toRad = x => x * Math.PI / 180;
  const dLat = toRad(lat2 - lat1), dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat/2)**2
          + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon/2)**2;
  return R * 2 * Math.asin(Math.sqrt(Math.min(1, a)));
}

const map = L.map('map').setView([49.8, 15.5], 8);
L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
  attribution: '© OpenStreetMap © CARTO', subdomains: 'abcd', maxZoom: 19
}).addTo(map);

// ── State pro novou uzavírku ───────────────────────────────
let state = { step: 0, start: null, end: null };
let markerStart = null, markerEnd = null, newLine = null;

// ── Vrstvy pro existující uzavírky ────────────────────────
const closureLayers = {};   // id → {segment, markers, bypassLine}

const iconClosure = L.divIcon({
  html: '<div style="background:#e94560;width:12px;height:12px;border-radius:50%;border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,.6)"></div>',
  iconSize:[16,16], iconAnchor:[8,8], className:''
});

document.getElementById('valid-from').value = new Date().toISOString().slice(0,10);

// ── Klik na mapu ──────────────────────────────────────────
map.on('click', function(e) {
  const { lat, lng } = e.latlng;

  if (state.step === 0) {
    if (markerStart) map.removeLayer(markerStart);
    markerStart = L.marker([lat, lng], {icon: iconClosure}).addTo(map);
    state.start = [lat, lng];
    state.step = 1;
    document.getElementById('start-val').textContent = `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
    document.getElementById('pt-start').style.opacity = '1';
    setStep(2);
    setStatus('Klikni na konec uzavřeného úseku.');

  } else if (state.step === 1) {
    if (markerEnd)  map.removeLayer(markerEnd);
    if (newLine)    map.removeLayer(newLine);
    markerEnd = L.marker([lat, lng], {icon: iconClosure}).addTo(map);
    state.end = [lat, lng];
    state.step = 2;
    newLine = L.polyline([state.start, [lat, lng]],
      {color: '#e94560', weight: 4, dashArray: '8 4'}).addTo(map);
    document.getElementById('end-val').textContent = `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
    document.getElementById('pt-end').style.opacity = '1';
    document.getElementById('btn-save').disabled = false;
    setStep(3);
    setStatus('Uzavřený úsek nastaven. Vyplň název a ulož.');
  }
});

function setStep(active) {
  for (let i = 1; i <= 3; i++) {
    const el = document.getElementById(`step${i}`);
    el.classList.remove('active', 'done');
    if (i < active)  el.classList.add('done');
    if (i === active) el.classList.add('active');
  }
}

function setStatus(msg, cls='') {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.className = cls;
}

// ── Uložení ───────────────────────────────────────────────
async function saveClosure() {
  const name = document.getElementById('name').value.trim();
  if (!name) { setStatus('Vyplň název uzavírky!', 'err'); return; }
  if (state.step < 2) { setStatus('Nastav uzavřený úsek na mapě.', 'err'); return; }

  const payload = {
    name,
    segment: {
      from: { lat: state.start[0], lon: state.start[1] },
      to:   { lat: state.end[0],   lon: state.end[1]   },
    },
    buffer_km:  parseFloat(document.getElementById('buffer').value) || 0.05,
    valid_from: document.getElementById('valid-from').value || null,
    valid_to:   document.getElementById('valid-to').value   || null,
    notes:      document.getElementById('notes').value.trim(),
  };

  try {
    const resp = await fetch('/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const result = await resp.json();
    setStatus(`✓ Uzavírka ${result.id} uložena`, 'ok');
    loadExisting();
    setTimeout(resetNew, 2000);
  } catch(e) {
    setStatus('Chyba při ukládání: ' + e.message, 'err');
  }
}

// ── Reset formuláře ───────────────────────────────────────
function resetNew() {
  if (markerStart) map.removeLayer(markerStart);
  if (markerEnd)   map.removeLayer(markerEnd);
  if (newLine)     map.removeLayer(newLine);
  markerStart = markerEnd = newLine = null;
  state = { step: 0, start: null, end: null };
  document.getElementById('name').value    = '';
  document.getElementById('notes').value   = '';
  document.getElementById('valid-to').value = '';
  document.getElementById('start-val').textContent = '—';
  document.getElementById('end-val').textContent   = '—';
  document.getElementById('pt-start').style.opacity = '0.4';
  document.getElementById('pt-end').style.opacity   = '0.4';
  document.getElementById('btn-save').disabled = true;
  setStep(1);
  setStatus('Klikni na mapu pro zadání začátku uzavírky.');
}

// ── Existující uzavírky — načtení + mapa ─────────────────
async function loadExisting() {
  try {
    const resp = await fetch('/closures');
    const data = await resp.json();
    const cl = data.closures || [];

    // Odstraň staré vrstvy z mapy
    Object.values(closureLayers).forEach(l => {
      if (l.segment) map.removeLayer(l.segment);
      if (l.mStart)  map.removeLayer(l.mStart);
      if (l.mEnd)    map.removeLayer(l.mEnd);
      (l.bypassLines || []).forEach(bl => map.removeLayer(bl));
      (l.depotLines  || []).forEach(dl => map.removeLayer(dl));
    });
    Object.keys(closureLayers).forEach(k => delete closureLayers[k]);

    const section = document.getElementById('existing-section');
    const list    = document.getElementById('existing-list');

    if (cl.length === 0) { section.style.display = 'none'; return; }
    section.style.display = 'block';

    list.innerHTML = cl.map(c => {
      const activeClass = c.active ? 'on' : 'off';
      const activeLabel = c.active ? '● aktivní' : '○ neaktivní';
      const valid = [c.valid_from, c.valid_to ? `do ${c.valid_to}` : '∞'].filter(Boolean).join(' — ');
      const midLat = (c.segment.from.lat + c.segment.to.lat) / 2;
      const midLon = (c.segment.from.lon + c.segment.to.lon) / 2;
      const nearDepot = haversineKm(DEPOT_LAT, DEPOT_LON, midLat, midLon) <= DEPOT_NEAR_KM;
      const depotBtn  = nearDepot
        ? `<button class="closure-btn btn-depot" id="depot-btn-${c.id}" onclick="showDepotBypass('${c.id}')">🏭 Odjezd</button>`
        : '';
      return `
        <div class="closure-item" id="item-${c.id}">
          <div class="closure-item-header">
            <span class="closure-id">${c.id}</span>
            <span class="closure-name">${c.name}</span>
            <span class="closure-active ${activeClass}">${activeLabel}</span>
          </div>
          <div class="closure-meta">${valid}</div>
          <div class="closure-actions">
            <button class="closure-btn btn-locate" onclick="locateClosure('${c.id}')">⊕ Najít</button>
            <button class="closure-btn btn-toggle" onclick="toggleClosure('${c.id}')">⏯ Toggle</button>
            <button class="closure-btn btn-bypass" id="bypass-btn-${c.id}" onclick="showBypass('${c.id}')">↺ Objížďka</button>
            ${depotBtn}
            <button class="closure-btn btn-delete" onclick="deleteClosure('${c.id}', '${c.name.replace(/'/g,"\\'")}')">🗑</button>
          </div>
        </div>`;
    }).join('');

    // Nakresli uzavírky na mapu
    cl.forEach(c => {
      const seg   = c.segment;
      const from  = [seg.from.lat, seg.from.lon];
      const to    = [seg.to.lat,   seg.to.lon];
      const color = c.active ? '#e94560' : '#666';
      const line  = L.polyline([from, to],
        {color, weight: 5, dashArray: '8 5', opacity: c.active ? 1 : 0.5}
      ).addTo(map);
      line.bindPopup(`<b>${c.id}</b><br>${c.name}<br>${c.active ? 'aktivní' : 'neaktivní'}`);

      const mS = L.circleMarker(from, {radius:5, color:'white', fillColor:color, fillOpacity:1, weight:2}).addTo(map);
      const mE = L.circleMarker(to,   {radius:5, color:'white', fillColor:color, fillOpacity:1, weight:2}).addTo(map);

      closureLayers[c.id] = { segment: line, mStart: mS, mEnd: mE,
                               bypassLines: [], depotLines: [] };
    });

  } catch(e) {}
}

// ── Zoom na uzavírku ──────────────────────────────────────
function locateClosure(id) {
  const layer = closureLayers[id];
  if (!layer || !layer.segment) return;
  map.fitBounds(layer.segment.getBounds(), { padding: [80, 80], maxZoom: 16 });
  layer.segment.openPopup();
}

// ── Zobraz ORS objížďky (všechny osy) ────────────────────
const BYPASS_COLORS = ['#4CAF50', '#2196F3', '#FF9800', '#E040FB'];

async function showBypass(id) {
  const btn   = document.getElementById(`bypass-btn-${id}`);
  const layer = closureLayers[id];
  if (!layer) return;

  // Pokud už jsou zobrazené — skryj
  if (layer.bypassLines.length > 0) {
    layer.bypassLines.forEach(bl => map.removeLayer(bl));
    layer.bypassLines = [];
    btn.textContent = '↺ Objížďky';
    btn.classList.remove('loaded');
    return;
  }

  btn.textContent = '…';
  btn.disabled = true;

  try {
    const resp = await fetch(`/bypass?id=${id}`);
    const data = await resp.json();
    if (data.routes && data.routes.length > 0) {
      const bounds = [];
      data.routes.forEach((route, i) => {
        const color = BYPASS_COLORS[i % BYPASS_COLORS.length];
        const line  = L.polyline(route.coords, {
          color, weight: 4, opacity: 0.85
        }).addTo(map);
        line.bindPopup(
          `<b>${id} — ${route.label}</b><br>` +
          `<span style="color:${color}">●</span> ORS avoid_polygon<br>` +
          `Doba jízdy: <b>${route.dur_min} min</b>`
        );
        layer.bypassLines.push(line);
        route.coords.forEach(c => bounds.push(c));
      });
      btn.textContent = `✓ Skrýt (${data.routes.length})`;
      btn.classList.add('loaded');
      if (bounds.length) map.fitBounds(L.latLngBounds(bounds), { padding: [60, 60] });
    } else {
      btn.textContent = '✗ ORS offline';
    }
  } catch(e) {
    btn.textContent = '✗ Chyba';
  }
  btn.disabled = false;
}

// ── Zobraz všechny objížďky najednou ─────────────────────
let showAllActive = false;

async function showAllBypasses() {
  const btn = document.getElementById('btn-show-all');

  // Pokud jsou zobrazené — skryj vše
  if (showAllActive) {
    Object.keys(closureLayers).forEach(id => {
      const layer = closureLayers[id];
      if (!layer) return;
      layer.bypassLines.forEach(bl => map.removeLayer(bl));
      layer.bypassLines = [];
      const b = document.getElementById(`bypass-btn-${id}`);
      if (b) { b.textContent = '↺ Objížďky'; b.classList.remove('loaded'); }
    });
    btn.textContent = '↺ Zobraz vše';
    btn.classList.remove('loaded');
    showAllActive = false;
    return;
  }

  btn.textContent = '…';
  btn.disabled = true;

  try {
    const resp = await fetch('/bypass/all');
    const data = await resp.json();   // {CLO_001: [routes], CLO_003: [routes]}
    const bounds = [];
    let anyLoaded = false;

    // Barvy per uzavírka (opakují se pokud víc než 4)
    const CID_COLORS = ['#4CAF50','#2196F3','#FF9800','#E040FB',
                        '#00BCD4','#FFEB3B','#FF5722','#9E9E9E'];
    const cidList = Object.keys(data);

    cidList.forEach((cid, cidIdx) => {
      const routes = data[cid];
      const layer  = closureLayers[cid];
      if (!layer || !routes.length) return;

      // Skryj případné staré bypass linie
      layer.bypassLines.forEach(bl => map.removeLayer(bl));
      layer.bypassLines = [];

      // Barvy pro osy téže uzavírky = různé odstíny, ale jasně patří k jedné
      // Base color per closure, alpha variace pro různé osy
      const baseColor = CID_COLORS[cidIdx % CID_COLORS.length];

      routes.forEach((route, i) => {
        const line = L.polyline(route.coords, {
          color: baseColor, weight: 4,
          opacity: 1 - i * 0.15,   // osy téže uzavírky se lehce liší průhledností
          dashArray: i === 0 ? null : '6 3',
        }).addTo(map);
        line.bindPopup(
          `<b>${cid} — ${route.label}</b><br>` +
          `<span style="color:${baseColor}">●</span> kombinovaný avoid_polygon<br>` +
          `Doba: <b>${route.dur_min} min</b>`
        );
        layer.bypassLines.push(line);
        route.coords.forEach(c => bounds.push(c));
        anyLoaded = true;
      });

      const b = document.getElementById(`bypass-btn-${cid}`);
      if (b) { b.textContent = `✓ Skrýt (${routes.length})`; b.classList.add('loaded'); }
    });

    if (anyLoaded) {
      btn.textContent = '✓ Skrýt vše';
      btn.classList.add('loaded');
      showAllActive = true;
      if (bounds.length) map.fitBounds(L.latLngBounds(bounds), { padding: [40, 40] });
    } else {
      btn.textContent = '✗ ORS offline';
    }
  } catch(e) {
    btn.textContent = '✗ Chyba';
  }
  btn.disabled = false;
}

// ── Depot odjezdové trasy ─────────────────────────────────
async function showDepotBypass(id) {
  const btn   = document.getElementById(`depot-btn-${id}`);
  const layer = closureLayers[id];
  if (!layer) return;

  // Skryj pokud jsou zobrazené
  if (layer.depotLines.length > 0) {
    layer.depotLines.forEach(l => map.removeLayer(l));
    layer.depotLines = [];
    btn.textContent = '🏭 Odjezd';
    btn.classList.remove('loaded');
    return;
  }

  btn.textContent = '…';
  btn.disabled = true;

  try {
    const resp = await fetch(`/bypass/depot?id=${id}`);
    const data = await resp.json();

    if (data.routes === null) {
      btn.textContent = '— mimo sklad';
      btn.disabled = false;
      return;
    }

    if (!data.routes || data.routes.length === 0) {
      btn.textContent = '✗ ORS offline';
      btn.disabled = false;
      return;
    }

    const bounds = [[DEPOT_LAT, DEPOT_LON]];

    // Depot marker
    const depotMarker = L.circleMarker([DEPOT_LAT, DEPOT_LON], {
      radius: 8, color: 'white', fillColor: '#F06292', fillOpacity: 1, weight: 2
    }).addTo(map);
    depotMarker.bindPopup('<b>🏭 Sklad</b>');
    layer.depotLines.push(depotMarker);

    data.routes.forEach(route => {
      const line = L.polyline(route.coords, {
        color: '#F06292', weight: 3,
        dashArray: '10 5', opacity: 0.9,
      }).addTo(map);
      line.bindPopup(
        `<b>${id} — ${route.label}</b><br>` +
        `<span style="color:#F06292">●</span> Odjezd ze skladu (avoid_polygon)<br>` +
        `Doba: <b>${route.dur_min} min</b>`
      );
      layer.depotLines.push(line);
      route.coords.forEach(c => bounds.push(c));
    });

    btn.textContent = `✓ Skrýt (${data.routes.length})`;
    btn.classList.add('loaded');
    map.fitBounds(L.latLngBounds(bounds), { padding: [60, 60] });

  } catch(e) {
    btn.textContent = '✗ Chyba';
  }
  btn.disabled = false;
}

// ── Toggle aktivní ────────────────────────────────────────
async function toggleClosure(id) {
  try {
    const resp = await fetch('/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id}),
    });
    const result = await resp.json();
    if (result.ok) loadExisting();
    else setStatus(`Chyba: ${result.error}`, 'err');
  } catch(e) {
    setStatus('Chyba při toggle: ' + e.message, 'err');
  }
}

// ── Smazat ────────────────────────────────────────────────
async function deleteClosure(id, name) {
  if (!confirm(`Smazat uzavírku ${id} — ${name}?`)) return;
  try {
    const resp = await fetch('/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id}),
    });
    const result = await resp.json();
    if (result.ok) {
      setStatus(`✓ Uzavírka ${id} smazána.`, 'ok');
      loadExisting();
    } else {
      setStatus(`Chyba: ${result.error}`, 'err');
    }
  } catch(e) {
    setStatus('Chyba při mazání: ' + e.message, 'err');
  }
}

loadExisting();
</script>
</body>
</html>
"""


# ============================================================
#  HTTP SERVER
# ============================================================

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._respond(200, "text/html; charset=utf-8", HTML.encode("utf-8"))

        elif self.path == "/closures":
            data = load_data()
            self._respond(200, "application/json", json.dumps(data).encode())

        elif self.path == "/bypass/all":
            # Všechny uzavírky najednou — vrátí {cid: [routes]}
            data   = load_data()
            result = {}
            for c in data["closures"]:
                if c.get("active"):
                    result[c["id"]] = get_all_bypasses(c["id"])
            self._respond(200, "application/json", json.dumps(result).encode())

        elif self.path.startswith("/bypass/depot"):
            from urllib.parse import urlparse, parse_qs
            cid    = parse_qs(urlparse(self.path).query).get("id", [None])[0]
            routes = get_depot_bypasses(cid) if cid else None
            self._respond(200, "application/json",
                          json.dumps({"routes": routes}).encode())

        elif self.path.startswith("/bypass"):
            from urllib.parse import urlparse, parse_qs
            cid    = parse_qs(urlparse(self.path).query).get("id", [None])[0]
            routes = get_all_bypasses(cid) if cid else []
            self._respond(200, "application/json",
                          json.dumps({"routes": routes}).encode())

        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length))

        if self.path == "/save":
            new_c = append_closure(body)
            print(f"  ✓ Uložena uzavírka {new_c['id']}: {new_c['name']}")
            self._respond(200, "application/json", json.dumps(new_c).encode())

        elif self.path == "/toggle":
            cid  = body.get("id", "")
            data = load_data()
            for c in data["closures"]:
                if c["id"] == cid:
                    c["active"] = not c["active"]
                    save_data(data)
                    state_str = "aktivní" if c["active"] else "neaktivní"
                    print(f"  ✓ Uzavírka {cid} → {state_str}")
                    self._respond(200, "application/json",
                                  json.dumps({"ok": True, "active": c["active"]}).encode())
                    return
            self._respond(200, "application/json",
                          json.dumps({"ok": False, "error": f"{cid} nenalezena"}).encode())

        elif self.path == "/delete":
            cid    = body.get("id", "")
            data   = load_data()
            before = len(data["closures"])
            data["closures"] = [c for c in data["closures"] if c["id"] != cid]
            if len(data["closures"]) < before:
                save_data(data)
                print(f"  ✓ Smazána uzavírka {cid}")
                self._respond(200, "application/json", json.dumps({"ok": True}).encode())
            else:
                self._respond(200, "application/json",
                              json.dumps({"ok": False, "error": f"{cid} nenalezena"}).encode())

        else:
            self._respond(404, "text/plain", b"Not found")

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


# ============================================================
#  MAIN
# ============================================================

def main():
    migrate_remove_detour_options()

    server = HTTPServer(("localhost", PORT), Handler)
    url    = f"http://localhost:{PORT}"

    print("═" * 50)
    print("  Editor uzavírek — VRP solver")
    print("═" * 50)
    print(f"  Server: {url}")
    print(f"  Soubor: {CLOSURES_FILE}")
    print(f"  ORS:    {ORS_URL}  (pro vizualizaci objížděk)")
    print(f"  Ukonči: Ctrl+C")
    print("─" * 50)

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer zastaven.")


if __name__ == "__main__":
    main()
