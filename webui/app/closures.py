"""
Read-only přístup k uzavírkám (data/static/closures.json) + náhledová mapa.

Veškeré MUTACE (toggle/remove/create/test) jdou přes existující nástroje
(manage_closures.py subprocess, closure_map_editor.py) — tady se logika uzavírek
NIKDY nemění ani neduplikuje, jen se čte pro zobrazení.
"""

from __future__ import annotations

import json

from . import config

CLOSURES_JSON = config.DATA_ROOT / "static" / "closures.json"


def list_closures() -> dict:
    """Načte closures.json (tolerantně). Vrátí {version, closures, path[, error]}."""
    if not CLOSURES_JSON.exists():
        return {"version": None, "closures": [], "path": CLOSURES_JSON.as_posix()}
    try:
        data = json.loads(CLOSURES_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": None, "closures": [], "path": CLOSURES_JSON.as_posix(),
                "error": "closures.json nelze přečíst"}
    if not isinstance(data, dict):
        return {"version": None, "closures": [], "path": CLOSURES_JSON.as_posix()}
    return {
        "version":  data.get("version"),
        "closures": data.get("closures", []),
        "path":     CLOSURES_JSON.as_posix(),
    }


def _segments() -> list[dict]:
    out = []
    for c in list_closures()["closures"]:
        seg = c.get("segment") or {}
        f, t = seg.get("from") or {}, seg.get("to") or {}
        if all(k in f for k in ("lat", "lon")) and all(k in t for k in ("lat", "lon")):
            out.append({
                "id": c.get("id", ""), "name": c.get("name", ""),
                "active": bool(c.get("active")),
                "from": [f["lat"], f["lon"]], "to": [t["lat"], t["lon"]],
            })
    return out


_MAP_TEMPLATE = """<!DOCTYPE html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0}</style></head>
<body><div id="map"></div><script>
const segs = __SEGMENTS__;
const map = L.map('map');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19, attribution:'© OpenStreetMap'}).addTo(map);
const bounds = [];
segs.forEach(s => {
  const color = s.active ? '#dc2626' : '#9aa3ad';
  L.polyline([s.from, s.to], {color, weight:5, opacity:0.85})
    .bindPopup(`<b>${s.id}</b> ${s.name||''}<br>${s.active?'aktivní':'neaktivní'}`)
    .addTo(map);
  L.circleMarker(s.from, {radius:5, color}).addTo(map);
  bounds.push(s.from, s.to);
});
if (bounds.length) map.fitBounds(bounds, {padding:[40,40]});
else map.setView([49.8, 15.5], 7);   // střed ČR když nejsou uzavírky
</script></body></html>"""


def closures_map_html() -> str:
    """Self-contained Leaflet HTML se segmenty uzavírek (červená=aktivní)."""
    return _MAP_TEMPLATE.replace("__SEGMENTS__", json.dumps(_segments(), ensure_ascii=False))
