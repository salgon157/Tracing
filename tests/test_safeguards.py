"""
test_safeguards.py — pojistky proti tiché ztrátě objednávek

Vznik: 31. 7. 2026 poslalo ESO9 vadné SEC (až 96 742 s = 26,9 h). Objednávka
se servisem nad strop trasy je neobsloužitelná, OR-Tools prohlásil celý
cluster za neřešitelný a jeho objednávky (49 z 91 v PR) TIŠE zmizely
z uloženého plánu. Tady testujeme všechny závory, které to už nedovolí:

  1. prepare: SERVICE_SEC_MAX — vadný SEC neprojde už při přípravě dat
  2. solver: validate_orders_servable — servis vs. strop trasy + dosažitelnost
  3. solver: verify_plan_complete — finální invariant vstup == naplánováno
"""
import numpy as np
import pytest

from prepare_inputs_v6 import SERVICE_SEC_MAX, transform
from vrp_solver_lines_v6 import (
    CONFIG,
    UNREACHABLE_TIME_MIN,
    _unsolvable_cluster_report,
    validate_orders_servable,
    verify_plan_complete,
)


# ── Helpery ──────────────────────────────────────────────────────────────────

def _order(order_no="O1", service_sec=600, lat=50.0, lon=14.0, kg=300.0):
    return {
        "order_number": order_no,
        "customer_name": "Firma s.r.o.",
        "city": "Jihlava",
        "time_from": "08:00", "time_to": "16:00",
        "weight_kg": kg,
        "lat": lat, "lon": lon,
        "service_sec": service_sec,
        "id": order_no,
        "name": "Firma s.r.o.",
    }


def _raw_row(line=1, payload="KG:300#SEC:600", order_no="ORD001"):
    return {
        "_line": line,
        "delivery_date": "20260731",
        "prev_kg": "-1000",
        "location_code": "loc1",
        "customer_name": "Firma s.r.o.",
        "city": "Jihlava",
        "tw1_from_sec": "28800",
        "tw1_to_sec": "43200",
        "lon": "15.586947",
        "lat": "49.395796",
        "order_number": order_no,
        "note": "",
        "payload_raw": payload,
        "ramp": "0",
    }


def _route(order_ids):
    stops = [{"stop": "Sklad", "kg": 0}]
    stops += [{"stop": f"Z{oid}", "id": oid, "kg": 10} for oid in order_ids]
    stops += [{"stop": "Sklad (návrat)", "kg": 0}]
    return {"vehicle_id": "V1", "stops": stops}


# ═════════════════════════════════════════════════════════════════════════════
#  1. prepare: horní mez SEC
# ═════════════════════════════════════════════════════════════════════════════

class TestPrepareServiceSecMax:
    def test_absurd_sec_dropped(self):
        # reálný případ z 31. 7.: SEC=96742 s = 26,9 h vykládky
        rows = [_raw_row(line=1, payload="KG:398.9#SEC:96742", order_no="OBAD")]
        orders, dropped = transform(rows, "PR")
        assert orders == []
        assert len(dropped) == 1
        assert dropped[0]["reason"] == "vadný payload"
        assert "26.9 h" in dropped[0]["detail"]
        assert "ESO9" in dropped[0]["detail"]

    def test_limit_boundary(self):
        ok  = _raw_row(line=1, payload=f"KG:10#SEC:{SERVICE_SEC_MAX}",
                       order_no="OK")
        bad = _raw_row(line=2, payload=f"KG:10#SEC:{SERVICE_SEC_MAX + 1}",
                       order_no="BAD")
        orders, dropped = transform([ok, bad], "PR")
        assert [o["order_number"] for o in orders] == ["OK"]
        assert [d["order_number"] for d in dropped] == ["BAD"]

    def test_legit_max_observed_passes(self):
        # nejvyšší legitimní SEC z produkce (17.–28. 7.) byl 5 160 s
        rows = [_raw_row(payload="KG:585#SEC:5160")]
        orders, dropped = transform(rows, "PR")
        assert len(orders) == 1 and dropped == []


# ═════════════════════════════════════════════════════════════════════════════
#  2. solver: validate_orders_servable
# ═════════════════════════════════════════════════════════════════════════════

class TestValidateOrdersServable:
    def test_normal_orders_pass(self):
        validate_orders_servable([_order(), _order("O2", service_sec=3600)])

    def test_service_over_route_cap_fatal(self):
        max_dur_sec = int(CONFIG["max_route_duration_h"] * 3600)
        bad = _order("OBAD", service_sec=max_dur_sec + 60)
        with pytest.raises(SystemExit) as e:
            validate_orders_servable([_order(), bad])
        msg = str(e.value)
        assert "OBAD" in msg and "NEOBSLOUŽITELNÉ" in msg

    def test_real_case_96742_sec_fatal(self):
        with pytest.raises(SystemExit):
            validate_orders_servable([_order("O126109365", service_sec=96742)])

    def test_unreachable_in_all_matrices_fatal(self):
        orders = [_order("O1"), _order("O2")]
        m = np.zeros((3, 3))
        m[0][2] = UNREACHABLE_TIME_MIN     # O2 nedosažitelná tam
        m[2][0] = UNREACHABLE_TIME_MIN     # ... i zpět
        with pytest.raises(SystemExit) as e:
            validate_orders_servable(orders, {"V1": m})
        assert "O2" in str(e.value) and "nedosažiteln" in str(e.value)

    def test_reachable_in_one_matrix_passes(self):
        # kamion na objednávku nedosáhne, dodávka ano -> OK
        orders = [_order("O1")]
        truck = np.full((2, 2), float(UNREACHABLE_TIME_MIN))
        van   = np.zeros((2, 2))
        validate_orders_servable(orders, {"TRUCK": truck, "VAN": van})

    def test_one_direction_blocked_is_fatal(self):
        # dosažitelná tam, ale ne zpět = pořád neobsloužitelná
        orders = [_order("O1")]
        m = np.zeros((2, 2))
        m[1][0] = UNREACHABLE_TIME_MIN
        with pytest.raises(SystemExit):
            validate_orders_servable(orders, {"V1": m})

    def test_round_trip_over_daily_drive_limit_fatal_with_breaks(self):
        # režim řidiče EU: 5 h tam + 5 h zpět = 10 h > denní limit jízdy
        import vrp_solver_lines_v6 as S
        orders = [_order("O1")]
        m = np.array([[0, 300], [300, 0]], dtype=float)
        saved = S.CONFIG.get("_driver_breaks_enabled")
        S.CONFIG["_driver_breaks_enabled"] = True
        try:
            limit_h = float(S.CONFIG["driver_max_drive_h"])
            assert 600 > limit_h * 60, "test předpokládá limit pod 10 h"
            with pytest.raises(SystemExit) as e:
                validate_orders_servable(orders, {"TRUCK": m})
            assert "O1" in str(e.value) and "denní limit" in str(e.value)
        finally:
            if saved is None:
                S.CONFIG.pop("_driver_breaks_enabled", None)
            else:
                S.CONFIG["_driver_breaks_enabled"] = saved
        # bez režimu řidiče stejná objednávka projde
        validate_orders_servable(orders, {"TRUCK": m})


# ═════════════════════════════════════════════════════════════════════════════
#  3. solver: verify_plan_complete (finální invariant)
# ═════════════════════════════════════════════════════════════════════════════

class TestVerifyPlanComplete:
    def test_complete_plan_passes(self):
        orders = [_order("O1"), _order("O2"), _order("O3")]
        verify_plan_complete(orders, [_route(["O1", "O2"]), _route(["O3"])])

    def test_missing_order_fatal_and_named(self):
        # reálný scénář z 31. 7.: část objednávek v plánu chybí
        orders = [_order("O1"), _order("O2"), _order("O3")]
        with pytest.raises(SystemExit) as e:
            verify_plan_complete(orders, [_route(["O1", "O3"])])
        msg = str(e.value)
        assert "O2" in msg and "NENÍ KOMPLETNÍ" in msg
        assert "O1" not in msg.split("Chybí v plánu")[1]

    def test_duplicate_order_fatal(self):
        orders = [_order("O1"), _order("O2")]
        with pytest.raises(SystemExit) as e:
            verify_plan_complete(orders, [_route(["O1", "O2", "O1"])])
        assert "Duplicitně" in str(e.value)

    def test_extra_order_fatal(self):
        orders = [_order("O1")]
        with pytest.raises(SystemExit) as e:
            verify_plan_complete(orders, [_route(["O1", "OX"])])
        assert "OX" in str(e.value)

    def test_empty_plan_fatal(self):
        with pytest.raises(SystemExit):
            verify_plan_complete([_order("O1")], [])

    def test_depot_stops_ignored(self):
        # zastávky bez "id" (sklad, návrat) nejsou objednávky
        orders = [_order("O1")]
        verify_plan_complete(orders, [_route(["O1"])])


# ═════════════════════════════════════════════════════════════════════════════
#  4. phase C: diagnostika neřešitelného clusteru
# ═════════════════════════════════════════════════════════════════════════════

class TestUnsolvableClusterReport:
    def test_report_names_worst_service_orders(self):
        c_orders = [_order("O1"), _order("OBAD", service_sec=96742)]
        vehicles = [{"max_kg": 1400}, {"max_kg": 3000}]
        msg = _unsolvable_cluster_report("sweep", 0, c_orders, vehicles)
        assert "NEŘEŠITELNÝ" in msg
        assert "OBAD" in msg and "1613 min" in msg
        assert "sweep" in msg
        assert "NEUKLÁDÁ" in msg
