@echo off
REM ============================================================
REM  Spusti webove rozhrani VRP planovace (FastAPI + uvicorn).
REM  http://127.0.0.1:8777
REM
REM  chcp 65001 = UTF-8 konzole (diakritika v logu).
REM  cd /d %~dp0.. = prepni do korene repa (skript je ve webui/).
REM ============================================================
chcp 65001 >nul
cd /d %~dp0..
echo Spoustim webui na http://127.0.0.1:8777  (Ctrl+C ukonci)
python -m uvicorn webui.app.main:app --host 127.0.0.1 --port 8777
