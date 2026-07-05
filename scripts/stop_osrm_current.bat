@echo off
REM Zastav a smaz CURRENT kontejnery (stable se nedotyka)
docker stop osrm-current ors-current 2>nul
docker rm   osrm-current ors-current 2>nul
echo Hotovo. Stable instance bezi dal.
