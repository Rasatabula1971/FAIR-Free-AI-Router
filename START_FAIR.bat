@echo off
setlocal
title FAIR Test Console
cd /d "%~dp0"

echo ==========================================
echo          FAIR TEST CONSOLE
echo ==========================================
echo.

set "FAIR_PY=.venv\Scripts\python.exe"

if not exist "%FAIR_PY%" (
    echo No FAIR virtual environment was found.
    echo Creating .venv with Python 3.12 or newer...
    echo.

    where py >nul 2>nul
    if not errorlevel 1 (
        py -3.12 -m venv .venv >nul 2>&1
    )

    if not exist "%FAIR_PY%" (
        python -m venv .venv >nul 2>&1
    )

    if not exist "%FAIR_PY%" (
        echo ERROR: Could not create .venv.
        echo Install Python 3.12 or newer, then run this file again.
        echo.
        pause
        exit /b 1
    )
)

"%FAIR_PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if errorlevel 1 (
    echo ERROR: FAIR requires Python 3.12 or newer.
    echo Delete .venv after installing a newer Python, then run this file again.
    echo.
    pause
    exit /b 1
)

"%FAIR_PY%" -c "import fair, pytest" >nul 2>&1
if errorlevel 1 (
    echo Installing FAIR and its test tools...
    "%FAIR_PY%" -m pip install -e ".[dev]"
    if errorlevel 1 (
        echo.
        echo ERROR: FAIR installation failed.
        pause
        exit /b 1
    )
)

if not exist ".env" (
    echo WARNING: .env was not found.
    echo Offline tests will work, but live-provider tests need API keys
    echo in .env or in the Windows environment.
    echo.
)

"%FAIR_PY%" fair_test_console.py
set "FAIR_EXIT=%ERRORLEVEL%"

echo.
if not "%FAIR_EXIT%"=="0" (
    echo FAIR Test Console exited with code %FAIR_EXIT%.
)
pause
exit /b %FAIR_EXIT%
