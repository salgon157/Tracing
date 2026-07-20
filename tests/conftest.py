"""
conftest.py — sdílené fixtures pro celý test suite.
Automaticky přidává vrp_benchmark/ na sys.path, aby importy fungovaly
bez ohledu na to, odkud je pytest spuštěn.
"""
import json
import sys
from pathlib import Path

# Přidáme vrp_benchmark/ (rodičovský adresář tests/) na cestu
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest


# ── Shared closure constants ──────────────────────────────────────────────────

SAMPLE_CLOSURE = {
    "id": "CLO_TEST",
    "active": True,
    "valid_from": "2020-01-01",
    "valid_to": "2099-12-31",
    "buffer_km": 0.15,
    "segment": {
        "from": {"lat": 50.0000, "lon": 14.4000},
        "to":   {"lat": 50.0100, "lon": 14.4100},
    },
}

EXPIRED_CLOSURE = {
    "id": "CLO_EXPIRED",
    "active": True,
    "valid_from": "2000-01-01",
    "valid_to": "2001-01-01",   # expired
    "buffer_km": 0.15,
    "segment": {
        "from": {"lat": 50.0, "lon": 14.4},
        "to":   {"lat": 50.0, "lon": 14.4},
    },
}

FUTURE_CLOSURE = {
    "id": "CLO_FUTURE",
    "active": True,
    "valid_from": "2099-01-01",   # not yet valid
    "valid_to": "2099-12-31",
    "buffer_km": 0.15,
    "segment": {
        "from": {"lat": 50.0, "lon": 14.4},
        "to":   {"lat": 50.0, "lon": 14.4},
    },
}

INACTIVE_CLOSURE = {
    "id": "CLO_INACTIVE",
    "active": False,
    "valid_from": "2020-01-01",
    "valid_to": "2099-12-31",
    "buffer_km": 0.15,
    "segment": {
        "from": {"lat": 50.0, "lon": 14.4},
        "to":   {"lat": 50.0, "lon": 14.4},
    },
}


@pytest.fixture
def closure_json(tmp_path):
    """Zapíše closures.json do tmp_path, vrátí cestu jako string."""
    data = {"closures": [SAMPLE_CLOSURE]}
    p = tmp_path / "closures.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


@pytest.fixture
def multi_closure_json(tmp_path):
    """Zapíše closures.json se 4 uzavírkami (aktivní, expirovaná, budoucí, neaktivní)."""
    data = {"closures": [SAMPLE_CLOSURE, EXPIRED_CLOSURE, FUTURE_CLOSURE, INACTIVE_CLOSURE]}
    p = tmp_path / "closures.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


@pytest.fixture
def sample_orders():
    """Dvě typické objednávky pro testy solver modulu."""
    return [
        {
            "order_number": "O1",
            "lat": 50.05, "lon": 14.45,
            "time_from": "08:00", "time_to": "12:00",
            "weight_kg": 300.0, "service_sec": 900,
            "block_id": "CB",
        },
        {
            "order_number": "O2",
            "lat": 50.10, "lon": 14.50,
            "time_from": "10:00", "time_to": "16:00",
            "weight_kg": 600.0, "service_sec": 1200,
            "block_id": "CB",
        },
    ]


@pytest.fixture
def sample_vehicles():
    """Jedno typické vozidlo pro testy build_data_model."""
    return [
        {
            "vehicle_id": "V1",
            "type_code": "TYPE_02",
            "max_kg": 1400,
            "cost_per_km": 10.0,
            "start_cost": 0,
            "max_duration_h": 10,
            "time_multiplier": 1.0,
            "osrm_profile": "driving",
        },
    ]
