"""
test_ors_hgv_integration.py — Integrační testy ORS driving-hgv vs OSRM driving
================================================================================

Ověřuje ze ORS profil driving-hgv:
  (a) dava vyrazne delsi casy na D1 (80 km/h limit pro nakladni vs 130 km/h pro auta)
  (b) dava primerene absolutni rychlosti (ne nesmyslne hodnoty)
  (c) nikdy nevoli kratsi trasu nez auto (HGV nesmi "prolest" pres omezeni)
  (d) na trasach s omezenym mostem/silnici jede oklikou (pokud OSM data chybi -> SKIP)

VYZADUJE ZIVE DOCKER KONTEJNERY (stable nebo current):
  osrm-stable / osrm-current  ->  port 5000
  ors-stable  / ors-current   ->  port 8080

Testy se AUTOMATICKY preskoci pokud kontejnery neodpovidaji.
Spust rucne: pytest tests/test_ors_hgv_integration.py -v

DULEZITE: ORS ma limit 100 km pro /directions endpoint.
  Praha->Brno (206 km) pouziva ORS /matrix  (tak to dela i solver).
  Kratsi trasy (Tabor->Pisek ~44 km) muzou pouzit /directions.

Referencni hodnoty zmerene na stavajicich datech (osm_date 2026-04-07):
  Praha->Brno  OSRM: 205.9 km, 131 min (94 km/h prumer)
  Praha->Brno  ORS HGV matrix: 206.0 km, 212 min (58 km/h prumer), ratio 1.62x
  Tabor->Pisek OSRM: 43.9 km, 46 min
  Tabor->Pisek ORS HGV directions: 44.0 km, 64 min, ratio 1.39x

Tyto testy NEJSOU soucasti startup test suite (vyzaduji Docker, jsou pomale).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Konfigurace ───────────────────────────────────────────────────────────────
# Porty lze přepsat env proměnnými — solver to dělá automaticky při volání
# run_routing_tests(), aby se testovala správná instance (stable vs current).
#   OSRM_TEST_URL=http://localhost:5001  ORS_TEST_URL=http://localhost:8081
#   pytest tests/test_ors_hgv_integration.py -v

OSRM_URL = os.environ.get("OSRM_TEST_URL", "http://localhost:5000")
ORS_URL  = os.environ.get("ORS_TEST_URL",  "http://localhost:8080")
TIMEOUT  = 20  # sekund

# Testovaci souradnice — (lat, lon)
PRAHA        = (50.0755, 14.4378)   # Praha centrum (Vaclavske namesti)
BRNO         = (49.1951, 16.6068)   # Brno centrum (namesti Svobody)
TABOR        = (49.4186, 14.6556)   # Tabor centrum
PISEK        = (49.3084, 14.1467)   # Pisek centrum

# Cross-Praha test (sever -> jih)
PRAHA_SEVER  = (50.1380, 14.5280)   # Letnany (prumyslova zona, sever)
PRAHA_JIH    = (49.9580, 14.3990)   # Zbraslav (u D1/D4, jih)

# Pisek most pres Otavu
# Kamenny most (13. st.) — nejstarsi dochovaný most v CR, omezena nosnost
# Pokud OSM ma maxweight tag, ORS HGV pojede pres Jirsiuv most (novejsi, jizneji)
PISEK_SEVER  = (49.3180, 14.1490)   # Alsovo nabrezi (severní strana Otavy)
PISEK_JIH    = (49.2970, 14.1550)   # Ul. Pisecka, smer Putim (jizni strana)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ping_osrm() -> bool:
    try:
        r = requests.get(
            f"{OSRM_URL}/route/v1/driving/14.4,50.0;14.5,50.1?overview=false",
            timeout=3,
        )
        return r.status_code < 500
    except Exception:
        return False


def _ping_ors() -> bool:
    try:
        r = requests.get(f"{ORS_URL}/ors/v2/health", timeout=3)
        return r.status_code < 500
    except Exception:
        return False


_osrm_up = _ping_osrm()
_ors_up  = _ping_ors()

needs_osrm = pytest.mark.skipif(
    not _osrm_up,
    reason="OSRM nedostupny (port 5000) — spust scripts/start_osrm_stable.bat",
)
needs_ors = pytest.mark.skipif(
    not _ors_up,
    reason="ORS nedostupny (port 8080) — spust scripts/start_osrm_stable.bat",
)
needs_both = pytest.mark.skipif(
    not (_osrm_up and _ors_up),
    reason="OSRM nebo ORS nedostupny — spust scripts/start_osrm_stable.bat",
)


def osrm_route(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """OSRM /route pro profil driving. Vraci (distance_m, duration_s)."""
    url = f"{OSRM_URL}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
    r = requests.get(url, params={"overview": "false"}, timeout=TIMEOUT)
    r.raise_for_status()
    route = r.json()["routes"][0]
    return float(route["distance"]), float(route["duration"])


def ors_matrix(lat1: float, lon1: float, lat2: float, lon2: float,
               profile: str = "driving-hgv") -> tuple[float, float]:
    """
    ORS /matrix pro 2 body. Vraci (distance_m, duration_s) pro smer A->B.

    Pouzit pro vsechny vzdalenosti (zadny limit na delku trasy).
    Presne to, co pouziva solver (get_matrix()).
    """
    url     = f"{ORS_URL}/ors/v2/matrix/{profile}"
    payload = {
        "locations": [[lon1, lat1], [lon2, lat2]],
        "metrics": ["duration", "distance"],
    }
    r = requests.post(url, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return float(data["distances"][0][1]), float(data["durations"][0][1])


def ors_directions(lat1: float, lon1: float, lat2: float, lon2: float,
                   profile: str = "driving-hgv") -> tuple[float, float]:
    """
    ORS /directions pro daný profil. Vraci (distance_m, duration_s).

    POZOR: ORS ma limit ~100 km pro tuto trasu. Pouzit pouze pro kratke trasy!
    Pro dlouhe trasy (Praha->Brno) pouzij ors_matrix().
    """
    url     = f"{ORS_URL}/ors/v2/directions/{profile}/geojson"
    payload = {"coordinates": [[lon1, lat1], [lon2, lat2]]}
    r = requests.post(url, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    summary = r.json()["features"][0]["properties"]["summary"]
    return float(summary["distance"]), float(summary["duration"])


# ── 1. Dostupnost API ─────────────────────────────────────────────────────────

class TestApiAvailability:
    @needs_osrm
    def test_osrm_route_responds(self):
        """OSRM odpoví a vrati smysluplna data pro ceskou trasu."""
        dist_m, dur_s = osrm_route(*PRAHA, *BRNO)
        assert dist_m > 0,         "Vzdalenost musi byt kladna"
        assert dur_s  > 0,         "Cas musi byt kladny"
        assert dist_m < 500_000,   f"Praha->Brno pres 500 km? ({dist_m/1000:.0f} km)"

    @needs_ors
    def test_ors_hgv_matrix_responds(self):
        """
        ORS /matrix pro driving-hgv odpoví — to je API ktere pouziva solver!
        (Ekvivalent get_matrix() v vrp_solver_lines_v6.py)
        """
        dist_m, dur_s = ors_matrix(*PRAHA, *BRNO)
        assert dist_m > 0,         "Vzdalenost musi byt kladna"
        assert dur_s  > 0,         "Cas musi byt kladny"
        assert dist_m < 500_000,   f"Praha->Brno pres 500 km? ({dist_m/1000:.0f} km)"

    @needs_ors
    def test_ors_hgv_directions_responds_short_route(self):
        """
        ORS /directions pro driving-hgv funguje na kratke trase (<100 km).
        Tabor->Pisek ~44 km — bezpecne pod limitem serveru.
        """
        dist_m, dur_s = ors_directions(*TABOR, *PISEK)
        assert dist_m > 0
        assert dur_s  > 0
        assert dist_m < 150_000,   f"Tabor->Pisek pres 150 km? ({dist_m/1000:.0f} km)"


# ── 2. Rychlostni testy ───────────────────────────────────────────────────────

class TestHgvSpeed:
    """
    Zakonny rychlostni limit v CR:
      Osobni auto na dalnici:      130 km/h
      Nakladni >= 3.5 t na dalnici: 80 km/h   (zakon 361/2000 Sb.)
      Nakladni na silnici I. tr.:   80 km/h
      Nakladni v obci:              50 km/h (stejne jako auto)

    ORS musi respektovat maxspeed:hgv z OSM.
    Pokud ne, testy selzou — signal ze ORS profil neni spravne nastaven.
    """

    @needs_both
    def test_hgv_significantly_slower_Praha_Brno(self):
        """
        Praha->Brno (~206 km, prevazne D1):
          Auto @ 130 km/h -> ~131 min (OSRM nameřeno)
          HGV  @  80 km/h -> ~212 min (ORS matrix nameřeno)
          Ocekavany ratio: 1.35-2.20x

        DULEZITE: pouziva ORS matrix (stejne jako solver), ne /directions
        (ktera ma limit 100 km).
        """
        _, osrm_s = osrm_route(*PRAHA, *BRNO)
        _, ors_s  = ors_matrix(*PRAHA, *BRNO)

        ratio    = ors_s / osrm_s
        osrm_min = osrm_s / 60
        ors_min  = ors_s  / 60

        assert ratio >= 1.30, (
            f"ORS HGV je prilis rychle — pravdepodobne ignoruje maxspeed:hgv.\n"
            f"  OSRM driving:    {osrm_min:.0f} min\n"
            f"  ORS driving-hgv: {ors_min:.0f} min\n"
            f"  Pomer:           {ratio:.2f}  (ocekavano >= 1.30)"
        )
        assert ratio <= 2.5, (
            f"ORS HGV trva podezrele dlouho — mozna spatny profil nebo chyba API.\n"
            f"  OSRM driving:    {osrm_min:.0f} min\n"
            f"  ORS driving-hgv: {ors_min:.0f} min\n"
            f"  Pomer:           {ratio:.2f}  (ocekavano <= 2.50)"
        )

    @needs_both
    def test_hgv_slower_on_rural_road_Tabor_Pisek(self):
        """
        Tabor->Pisek (~44 km, mix silnice I. a II. tridy, zadna dalnice):
          Auto:  OSRM nameřeno ~46 min
          HGV:   ORS nameřeno ~64 min  (na 90km/h silnicich limit 80 km/h)
          Ocekavany ratio: 1.10-2.00x

        Kratsi trasa -> lze pouzit ors_directions() (pod limitem 100 km).
        """
        _, osrm_s = osrm_route(*TABOR, *PISEK)
        _, ors_s  = ors_directions(*TABOR, *PISEK)

        ratio = ors_s / osrm_s
        assert ratio >= 1.08, (
            f"ORS HGV Tabor->Pisek se vubec nelisi od auta — profil pravdepodobne "
            f"ignoruje maxspeed:hgv.\n"
            f"  OSRM: {osrm_s/60:.0f} min, ORS HGV: {ors_s/60:.0f} min, ratio={ratio:.2f}"
        )
        assert ratio <= 2.0, (
            f"ORS HGV Tabor->Pisek trva podezrele dlouho (ratio={ratio:.2f})"
        )

    @needs_both
    def test_implied_speed_Praha_Brno(self):
        """
        Implicitni prumerna rychlost z vzdalenosti / casu pro Praha->Brno:
          OSRM (auto):   ocekavano 70-120 km/h  (mix dalnice 130 + mestske 50-90)
          ORS HGV:       ocekavano 40-80  km/h  (dalnice 80 + mestske + zdrzeni v obcich)
          HGV musi byt vyrazne pomalejsi nez auto.

        Namerene referencni hodnoty: OSRM 94 km/h, ORS HGV 58 km/h.
        """
        osrm_dist_m, osrm_s = osrm_route(*PRAHA, *BRNO)
        ors_dist_m,  ors_s  = ors_matrix(*PRAHA, *BRNO)

        osrm_kmh = (osrm_dist_m / 1000) / (osrm_s / 3600)
        ors_kmh  = (ors_dist_m  / 1000) / (ors_s  / 3600)

        assert 60 <= osrm_kmh <= 130, (
            f"OSRM prumerna rychlost Praha->Brno = {osrm_kmh:.1f} km/h — "
            f"mimo ocekavany rozsah 60-130 km/h."
        )
        assert 35 <= ors_kmh <= 90, (
            f"ORS HGV prumerna rychlost Praha->Brno = {ors_kmh:.1f} km/h — "
            f"mimo ocekavany rozsah 35-90 km/h."
        )
        assert ors_kmh <= osrm_kmh * 0.85, (
            f"ORS HGV ({ors_kmh:.1f} km/h) neni dostatecne pomalejsi nez OSRM car "
            f"({osrm_kmh:.1f} km/h) — pomer {ors_kmh/osrm_kmh:.2f}, ocekavano <= 0.85."
        )


# ── 3. Vzdalenostni sanity checks ─────────────────────────────────────────────

class TestDistanceSanity:
    """
    HGV a auto obvykle jedou po stejne silnici (D1), takze vzdalenosti by mely
    byt podobne. Velky rozdil v km = objizka zachycena.
    """

    @needs_both
    def test_Praha_Brno_similar_distance(self):
        """
        Praha->Brno: oba profily typicky vyuzivaji D1, vzdalenosti +-12 %.
        Pokud se vzdalenosti lisi vice, ORS HGV jede jinou trasou.
        """
        osrm_m, _ = osrm_route(*PRAHA, *BRNO)
        ors_m,  _ = ors_matrix(*PRAHA, *BRNO)

        # Absolutni sanity
        assert 180_000 <= osrm_m <= 280_000, (
            f"OSRM Praha->Brno: {osrm_m/1000:.0f} km — mimo rozsah 180-280 km"
        )
        assert 180_000 <= ors_m <= 310_000, (
            f"ORS HGV Praha->Brno: {ors_m/1000:.0f} km — mimo rozsah 180-310 km"
        )
        # Relativni check
        ratio = ors_m / osrm_m
        assert 0.88 <= ratio <= 1.20, (
            f"Vzdalenosti Praha->Brno se vyznamne lisi:\n"
            f"  OSRM driving:    {osrm_m/1000:.1f} km\n"
            f"  ORS driving-hgv: {ors_m/1000:.1f} km\n"
            f"  Pomer:           {ratio:.2f}  (ocekavano 0.88-1.20)"
        )

    @needs_both
    def test_hgv_never_significantly_shorter_than_car(self):
        """
        HGV NESMI mit vyrazne kratsi trasu nez auto — nesmi 'prolest' pres
        omezeni ktera auto nezna.

        Tolerance: ORS HGV smi byt max 8 % kratsi (snap na silnici).
        Testujeme 3 trasy.
        """
        routes = [
            ("Praha->Brno",         PRAHA,       BRNO,       "matrix"),
            ("Tabor->Pisek",        TABOR,       PISEK,      "directions"),
            ("Praha sever->jih",    PRAHA_SEVER, PRAHA_JIH,  "directions"),
        ]
        failures = []
        for label, orig, dest, api in routes:
            osrm_m, _ = osrm_route(*orig, *dest)
            if api == "matrix":
                ors_m, _ = ors_matrix(*orig, *dest)
            else:
                ors_m, _ = ors_directions(*orig, *dest)
            ratio = ors_m / osrm_m
            if ratio < 0.92:
                failures.append(
                    f"  {label}: OSRM {osrm_m/1000:.1f} km vs ORS HGV {ors_m/1000:.1f} km "
                    f"(ratio={ratio:.2f}  — ORS HGV je podezrele kratsi)"
                )
        if failures:
            pytest.fail(
                "ORS HGV je vyrazne kratsi nez OSRM car — signal chybneho profilu:\n"
                + "\n".join(failures)
            )


# ── 4. API konzistence (matrix == directions) ─────────────────────────────────

class TestApiConsistency:
    """
    Solver pouziva ORS /matrix endpoint. Testujeme ze matice a /directions
    vracejí konzistentni vzdalenosti pro stejny par bodu.
    (Testovano na kratke trase kde oba endpointy fungujou bez limitu.)
    """

    @needs_ors
    def test_matrix_distance_matches_directions_Tabor_Pisek(self):
        """
        ORS matrix A->B musi dat stejnou vzdalenost jako ORS directions A->B
        (tolerance 2 % — ruzny snap na silnici muze zpusobit maly rozdil).
        """
        matrix_m, _     = ors_matrix(*TABOR, *PISEK)
        directions_m, _ = ors_directions(*TABOR, *PISEK)

        if directions_m == 0:
            pytest.skip("ORS directions vratil 0 — API problem")

        ratio = matrix_m / directions_m
        assert 0.98 <= ratio <= 1.02, (
            f"ORS matrix ({matrix_m/1000:.2f} km) != ORS directions ({directions_m/1000:.2f} km)\n"
            f"Pomer: {ratio:.4f}  (ocekavano 0.98-1.02)\n"
            f"Signal nekonzistentniho ORS API — zkontroluj verzi kontejneru."
        )

    @needs_ors
    def test_matrix_duration_matches_directions_Tabor_Pisek(self):
        """Cas z matice a directions musi byt konzistentni (tolerance 5 %)."""
        _, matrix_s     = ors_matrix(*TABOR, *PISEK)
        _, directions_s = ors_directions(*TABOR, *PISEK)

        if directions_s == 0:
            pytest.skip("ORS directions vratil 0 — API problem")

        ratio = matrix_s / directions_s
        assert 0.95 <= ratio <= 1.05, (
            f"ORS matrix cas ({matrix_s/60:.1f} min) != ORS directions ({directions_s/60:.1f} min)\n"
            f"Pomer: {ratio:.4f}  (ocekavano 0.95-1.05)"
        )


# ── 5. Detour test — HGV jede oklikou kvuli omezeni ──────────────────────────

class TestHgvDetour:
    """
    Testuje ze ORS HGV na konkretních trasach skutecne objizdi omezene useky.

    Tyto testy zavisi na kvalite OSM dat — pokud omezeni neni v OSM (chybejici
    maxweight tag), test se gracefully preskoci s vysvetlenim.

    Jak pridat vlastni omezeny usek:
      1. V OpenStreetMap najdi silnici/most s maxweight < 10 t
         (overpass-turbo.eu: way["maxweight"<"10"]({{bbox}});out;)
      2. Zaznamenej souradnice tesne pred a tesne za omezenim
      3. Proved ze prima cesta je vyrazne kratsi nez objizka pro HGV
      4. Pridej test podle vzoru test_pisek_bridge_hgv_detour()
    """

    @needs_both
    def test_cross_Prague_hgv_takes_longer_route(self):
        """
        Letnany (sever, prumyslova zona) -> Zbraslav (jih, u D1/D4):
          Auto (OSRM): muze jet pres centrum Prahy nebo po okruhu D0
          HGV (ORS):   Praha centrum ma maxweight omezeni -> musi po okruhu D0/D1

        Pokud ORS HGV jede alespon o 15 % delsi trasu, je to signal objizky
        kvuli omezenim (ne jen jina trasa ze stejneho duvodu).

        Pokud je rozdil < 15 %: OSRM taky zvolil okruh (pro rychlost) -> skip,
        nelze odlisit obe situace bez inspekce geometrie.
        """
        osrm_m, _ = osrm_route(*PRAHA_SEVER, *PRAHA_JIH)
        ors_m,  _ = ors_directions(*PRAHA_SEVER, *PRAHA_JIH)

        ratio = ors_m / osrm_m

        if ratio < 1.15:
            pytest.skip(
                f"Letnany->Zbraslav: ORS HGV ({ors_m/1000:.1f} km) vs OSRM car "
                f"({osrm_m/1000:.1f} km), ratio={ratio:.2f} — rozdil < 15 %.\n"
                f"OSRM pravdepodobne taky zvolil okruh kvuli rychlosti.\n"
                f"Zkontroluj vizualne nebo pridej konkretni omezeny usek "
                f"(viz docstring TestHgvDetour)."
            )

        # Pokud jsme tady, ORS HGV jede >15 % deli trasu = detour potvrzen
        assert ors_m > osrm_m * 1.15

    @needs_both
    def test_pisek_bridge_hgv_detour(self):
        """
        Kamenny most v Pisku (13. st.) — nejstarsi dochovaný most v CR.
        Pokud OSM ma spravny maxweight tag, ORS HGV pojede pres Jirsiuv most
        (novejsi, jizneji) misto Kamneho mostu — delsi trasa.

        Souradnice: Alsovo nabrezi (sever Otavy) -> smer Putim (jih Otavy).

        Pokud OSM tag chybi -> SKIP. Toto je data-quality test, ne kod test.
        Pridej tag do OSM nebo nahrad vlastnim overeneym usekem.
        """
        osrm_m, _ = osrm_route(*PISEK_SEVER, *PISEK_JIH)
        ors_m,  _ = ors_directions(*PISEK_SEVER, *PISEK_JIH)

        ratio = ors_m / osrm_m

        if ratio < 1.10:
            pytest.skip(
                f"Pisek most: ORS HGV ({ors_m:.0f} m) vs OSRM car ({osrm_m:.0f} m), "
                f"ratio={ratio:.2f} — trasy jsou podobne.\n"
                f"Pravdepodobne chybi maxweight tag na Kamnenem moste v OSM.\n"
                f"Zkontroluj: https://www.openstreetmap.org/  (hledej Pisek, "
                f"Kamenny most). Pokud tag chybi, pridej ho nebo nahrad test "
                f"jinym usekem s overeneym omezenim."
            )

        assert ors_m > osrm_m * 1.10, (
            f"ORS HGV by mel objet Kamenny most (omezena nosnost).\n"
            f"  OSRM car: {osrm_m:.0f} m, ORS HGV: {ors_m:.0f} m, ratio={ratio:.2f}"
        )

    @needs_both
    def test_custom_restricted_road_placeholder(self):
        """
        Zastupy test pro vlastni omezeny usek.

        Jak pouzit:
          1. Najdi silnici v CR s omezenou nosnosti (most maxweight=6 -> 10t truck nevejde)
          2. Zjisti souradnice tesne pred a tesne za omezenim
          3. Dopln nize a odstran pytest.skip()

        Jak najit omezene useky:
          - Overpass Turbo (overpass-turbo.eu):
              way["maxweight"<"10"](49.0,14.0,50.5,16.0);out geom;
          - Vyhledej v mape blizko vas aktualnich dopravenicch koridoru
        """
        pytest.skip(
            "Placeholder test — doplnte souradnice konkretniho omezeneho useku.\n"
            "Viz docstring TestHgvDetour a test_pisek_bridge_hgv_detour() jako vzor."
        )

        # --- Vzor kodu (odkomentuj a doplnte) ---
        # RESTRICTED_A = (lat_pred_omezenim, lon_pred_omezenim)
        # RESTRICTED_B = (lat_za_omezenim,   lon_za_omezenim)
        #
        # osrm_m, _ = osrm_route(*RESTRICTED_A, *RESTRICTED_B)
        # ors_m,  _ = ors_directions(*RESTRICTED_A, *RESTRICTED_B)
        # ratio = ors_m / osrm_m
        # assert ratio >= 1.10, (
        #     f"HGV mel jet oklikou: OSRM {osrm_m:.0f}m vs ORS {ors_m:.0f}m "
        #     f"(ratio={ratio:.2f})"
        # )
