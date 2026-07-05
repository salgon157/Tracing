@echo off
REM ============================================================
REM  Start CURRENT (fresh) routing instance
REM  Folder:  C:\osrm_current  (data spravuje skript update_osrm.py)
REM  OSRM:    http://localhost:5001
REM  ORS:     http://localhost:8081
REM
REM  Pred prvnim spustenim: spust update_osrm.py pro pripravu dat.
REM ============================================================

REM Overeni ze data jsou pripravena
if not exist C:\osrm_current\czech-republic-latest.osrm (
    echo [CHYBA] C:\osrm_current\czech-republic-latest.osrm neexistuje.
    echo         Nejdrive spust: python update_osrm.py
    exit /b 1
)

echo Spoustim OSRM current na portu 5001...
docker run -d --name osrm-current -p 5001:5000 ^
    -v C:\osrm_current:/data ^
    osrm/osrm-backend ^
    osrm-routed --algorithm mld /data/czech-republic-latest.osrm

echo Spoustim ORS current na portu 8081...
docker run -d --name ors-current -p 8081:8080 ^
    -v C:\osrm_current:/home/ors/files ^
    -v C:\osrm_current\ors-config.yml:/home/ors/config/ors-config.yml ^
    openrouteservice/openrouteservice:latest

echo.
echo Hotovo. Pri prvnim startu ORS si stavi graf z PBF (15-30 min).
echo   docker logs -f ors-current   # sledovat progress
echo   docker ps --filter name=current
