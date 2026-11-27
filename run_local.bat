@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   PULSAR - LOCAL HTTPS VERSION
echo ========================================
echo.

:: Port 443 requires elevated permissions on Windows.
net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo [ERROR] Port 443 requires Administrator rights.
    echo Right-click run_local.bat and choose "Run as administrator".
    pause
    exit /b 1
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3.11 --version >nul 2>nul
    if %errorlevel%==0 (set "PYTHON=py -3.11") else (set "PYTHON=py")
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (set "PYTHON=python") else (
        echo [ERROR] Python 3.11+ not found. https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

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
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :error
)

echo [2/4] Installing dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [3/4] Preparing trusted localhost certificate...
mkcert -install
if errorlevel 1 goto :error
if not exist "localhost+2.pem" (
    mkcert -cert-file localhost+2.pem -key-file localhost+2-key.pem localhost 127.0.0.1 ::1
    if errorlevel 1 goto :error
)

echo.
echo [4/4] Starting Pulsar over HTTPS...
echo Open https://localhost
echo Keep this window open. Press Ctrl+C to stop.
echo.
set "PULSE_COOKIE_SECURE=1"
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
