@echo off
rem Jeju cafe RAG local runner: API server (8000) + web (8503)
cd /d "%~dp0"

echo [1/3] Starting API server at localhost:8000 ...
start "jeju-api" cmd /k python -X utf8 -m uvicorn app.server:app --port 8000

echo [2/3] Starting web server at localhost:8503 ...
start "jeju-web" /d "%~dp0web" cmd /k python -m http.server 8503

echo [3/3] Opening browser ...
timeout /t 4 >nul
start http://localhost:8503

echo.
echo Two windows (jeju-api, jeju-web) must stay open.
echo Close this launcher window anytime.
pause
