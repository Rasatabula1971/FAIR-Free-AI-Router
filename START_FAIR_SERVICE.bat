@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" -m pip install -e ".[service]"
if errorlevel 1 exit /b 1

echo.
echo FAIR status page: http://127.0.0.1:8000/status  (the port follows FAIR_SERVICE_PORT)
echo.
".venv\Scripts\python.exe" -m fair.service
