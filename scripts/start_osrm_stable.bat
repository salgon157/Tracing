@echo off
REM ============================================================
REM  Start STABLE routing instance
REM  Folder:  C:\osrm  (rucne udrzovana data, neaktualizovat!)
REM  OSRM:    http://localhost:5000
REM  ORS:     http://localhost:8080
REM ============================================================

echo Spoustim OSRM stable na portu 5000...
docker run -d --name osrm-stable -p 5000:5000 ^
    -v C:\osrm:/data ^
    osrm/osrm-backend ^
    osrm-routed --algorithm mld /data/czech-republic-latest.osrm

echo Spoustim ORS stable na portu 8080...
docker run -d --name ors-stable -p 8080:8080 ^
    -v C:\osrm:/home/ors/files ^
    -v C:\osrm\ors-config.yml:/home/ors/config/ors-config.yml ^
    openrouteservice/openrouteservice:latest

echo.
echo Hotovo. Zkontroluj zda kontejnery bezi:
echo   docker ps --filter name=stable
