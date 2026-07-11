#!/usr/bin/env bash
# ============================================================
#  Spustí webové rozhraní VRP plánovače (FastAPI + uvicorn).
#  http://127.0.0.1:8777
#  Protějšek start_webui.bat pro Linux/macOS (budoucí server).
# ============================================================
set -e
cd "$(dirname "$0")/.."
echo "Spouštím webui na http://127.0.0.1:8777  (Ctrl+C ukončí)"
exec python -m uvicorn webui.app.main:app --host 127.0.0.1 --port 8777
