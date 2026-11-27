@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   PULSAR - LOCAL WEB VERSION
echo ========================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    py -3.11 --version >nul 2>nul
    if %errorlevel%==0 (
        set "PYTHON=py -3.11"
    ) else (
        set "PYTHON=py"
    )
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PYTHON=python"
    ) else (
        echo [ERROR] Python not found. Install Python 3.11 or newer.
        echo https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :error
)

echo [2/3] Checking dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo [3/3] Starting Pulsar...
echo Open http://127.0.0.1:8765
echo To stop the server, press Ctrl+C.
echo.
set "PULSE_COOKIE_SECURE=0"
set "PORT=8765"
".venv\Scripts\python.exe" server.py
goto :end

:error
echo.
echo [ERROR] Could not prepare or start Pulsar.
pause
exit /b 1

:end
endlocal
