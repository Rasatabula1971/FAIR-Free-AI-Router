@echo off
setlocal EnableExtensions
title FAIR Free AI Test Launcher
cd /d "%~dp0"

set "FAIR_PY=.venv\Scripts\python.exe"

call :bootstrap
if errorlevel 1 exit /b 1

if not "%~1"=="" (
    "%FAIR_PY%" fair_test_console.py %*
    exit /b %ERRORLEVEL%
)

:menu
cls
echo ============================================================
echo                 FAIR FREE AI TEST LAUNCHER
echo ============================================================
echo Repo: %CD%
if exist ".env" (
    echo .env: FOUND
) else (
    echo .env: NOT FOUND - live providers may be unavailable
)
echo.
echo  1. Interactive FAIR test console
echo  2. TEST ALL configured free AI providers
echo  3. Show provider configuration matrix
echo  4. Run offline validation + failover tests
echo  5. Run full pytest suite
echo  6. Exit
echo.
echo Recommended first run: 3, then 2
echo.
set /p "FAIR_CHOICE=Choose 1-6: "

if "%FAIR_CHOICE%"=="1" goto interactive
if "%FAIR_CHOICE%"=="2" goto sweep
if "%FAIR_CHOICE%"=="3" goto inventory
if "%FAIR_CHOICE%"=="4" goto offline
if "%FAIR_CHOICE%"=="5" goto pytest
if "%FAIR_CHOICE%"=="6" exit /b 0

echo.
echo Invalid choice.
call :pause_return
goto menu

:interactive
"%FAIR_PY%" fair_test_console.py --menu
call :show_exit
goto menu

:sweep
echo.
echo FAIR will test each CONFIGURED provider separately.
echo A provider that cannot prove zero-cost use will FAIL-CLOSED.
echo Missing providers will show as SKIP.
echo.
"%FAIR_PY%" fair_test_console.py --sweep
call :show_exit
goto menu

:inventory
"%FAIR_PY%" fair_test_console.py --inventory
call :show_exit
goto menu

:offline
"%FAIR_PY%" fair_test_console.py --offline
call :show_exit
goto menu

:pytest
"%FAIR_PY%" fair_test_console.py --pytest
call :show_exit
goto menu

:show_exit
set "FAIR_EXIT=%ERRORLEVEL%"
echo.
if "%FAIR_EXIT%"=="0" (
    echo FAIR command completed successfully.
) else (
    echo FAIR command returned exit code %FAIR_EXIT%.
    echo Review the result above. FAIL-CLOSED is expected when free use
    echo cannot be proven safely.
)
call :pause_return
exit /b 0

:pause_return
echo.
pause
exit /b 0

:bootstrap
echo ============================================================
echo                 FAIR ENVIRONMENT CHECK
echo ============================================================
echo.

if not exist "%FAIR_PY%" (
    echo FAIR virtual environment not found.
    echo Creating .venv with Python 3.12 or newer...
    echo.

    where py >nul 2>nul
    if not errorlevel 1 (
        py -3.12 -m venv .venv >nul 2>&1
    )

    if not exist "%FAIR_PY%" (
        where python >nul 2>nul
        if not errorlevel 1 (
            python -m venv .venv >nul 2>&1
        )
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

echo Environment: READY
echo.
exit /b 0
