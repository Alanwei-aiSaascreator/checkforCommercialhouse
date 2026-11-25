@echo off
setlocal
cd /d "%~dp0"

set "PY=%CD%\.venv\Scripts\python.exe"
set "URL=http://127.0.0.1:8000"

if not exist "%PY%" (
    echo [ERROR] python.exe not found: %PY%
    echo Run the dependency setup first: python -m venv --system-site-packages .venv
    pause
    exit /b 1
)

netstat -ano | findstr /C:"127.0.0.1:8000" | findstr /C:"LISTENING" >nul
if not errorlevel 1 (
    echo [INFO] Server is already running.
    start "" "%URL%"
    ping -n 2 127.0.0.1 >nul
    exit /b 0
)

echo [1/3] Starting server ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%PY%' -ArgumentList 'run.py' -WorkingDirectory '%CD%' -RedirectStandardOutput '%CD%\_srv_out.log' -RedirectStandardError '%CD%\_srv_err.log' -WindowStyle Hidden"

echo [2/3] Waiting ...
ping -n 7 127.0.0.1 >nul

echo [3/3] Health check ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -Uri '%URL%/' -UseBasicParsing -TimeoutSec 10; Write-Host ('  OK  HTTP ' + $r.StatusCode) -ForegroundColor Green; exit 0 } catch { Write-Host '  FAILED' -ForegroundColor Red; Get-Content '%CD%\_srv_err.log' -ErrorAction SilentlyContinue | Select-Object -Last 15; exit 1 }"

if errorlevel 1 (
    echo.
    echo [ERROR] Server failed to start. Check _srv_err.log
    pause
    exit /b 1
)

start "" "%URL%"
echo.
echo Server started: %URL%
echo Use stop.bat to shut it down.
ping -n 4 127.0.0.1 >nul
exit /b 0
