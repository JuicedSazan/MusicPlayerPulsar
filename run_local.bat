@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   PULSAR - LOCAL HTTPS VERSION
echo ========================================
echo.

:: Port 443 requires elevated permissions on Windows.
net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Port 443 requires Administrator rights.
    echo Right-click run_local.bat and choose "Run as administrator".
    pause
    exit /b 1
)

:: Prefer the Python launcher, but accept any Python 3.11+ installation.
set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)

:: Fall back to python.exe if the launcher is unavailable or too old.
if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYTHON_CMD=python"
    )
)

if not defined PYTHON_CMD (
    echo [ERROR] Python 3.11 or newer was not found.
    echo Install Python 3.11+ from https://www.python.org/downloads/
    echo During installation, enable "Add Python to PATH".
    echo Then reopen this window and run this file again as Administrator.
    pause
    exit /b 1
)

echo Using:
%PYTHON_CMD% --version
echo.

where mkcert >nul 2>nul
if errorlevel 1 (
    echo [ERROR] mkcert is required to create a locally trusted HTTPS certificate.
    echo Install it with: winget install FiloSottile.mkcert
    echo Then close and reopen this window and run this file again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating Python environment...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 goto :error
)

echo [2/5] Installing dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [3/5] Installing Chromium for local VK login...\n".venv\\Scripts\\python.exe" -m playwright install chromium\nif errorlevel 1 goto :error\n\necho [4/5] Preparing trusted localhost certificate...
mkcert -install
if errorlevel 1 goto :error
if not exist "localhost+2.pem" (
    mkcert -cert-file localhost+2.pem -key-file localhost+2-key.pem localhost 127.0.0.1 ::1
    if errorlevel 1 goto :error
)

echo.
echo [5/5] Starting Pulsar over HTTPS...
echo Open https://localhost
echo Keep this window open. Press Ctrl+C to stop.
echo.
set "PULSE_LOCAL_VK_BROWSER=1"\nset "PULSE_COOKIE_SECURE=1"
set "PULSE_REQUIRE_HTTPS=1"
set "PORT=443"
".venv\Scripts\python.exe" server.py
goto :end

:error
echo.
echo [ERROR] Could not prepare or start Pulsar.
pause
exit /b 1

:end
endlocal
