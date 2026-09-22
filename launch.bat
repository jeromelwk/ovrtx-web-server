@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Environnement virtuel introuvable dans .venv\
    echo Creez-le avec : py -m venv .venv
    echo Puis installez les dependances avec :
    echo   .venv\Scripts\python.exe -m pip install fastapi "uvicorn[standard]" pydantic numpy pillow ovrtx ovstage "usd-core==25.11"
    pause
    exit /b 1
)

echo Demarrage du serveur ovrtx (RTX Viewer)...
start "ovrtx server" cmd /k "%~dp0.venv\Scripts\python.exe" -m uvicorn app.server:app --host 127.0.0.1 --port 8000

timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8000/

endlocal
