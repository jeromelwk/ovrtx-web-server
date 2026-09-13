@echo off
setlocal
cd /d "%~dp0"

echo Demarrage du serveur ovrtx (RTX Viewer)...
start "ovrtx server" cmd /k python -m uvicorn app.server:app --host 127.0.0.1 --port 8000

timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8000/

endlocal
