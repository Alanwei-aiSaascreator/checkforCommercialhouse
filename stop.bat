@echo off
setlocal
cd /d "%~dp0"
set "PROJ=%CD%"

echo [1/2] Stopping server (python run.py) ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$n=0; Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*run.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('  stopped python PID=' + $_.ProcessId); $n++ }; if ($n -eq 0) { Write-Host '  no server process found' }"

echo [2/2] Cleaning stale Chrome of this project ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$n=0; Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*%PROJ%*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }; if ($n -eq 0) { Write-Host '  no stale chrome' } else { Write-Host ('  killed chrome processes: ' + $n) }"

ping -n 3 127.0.0.1 >nul
netstat -ano | findstr /C:":8000" | findstr /C:"LISTENING" >nul
if errorlevel 1 (
    echo.
    echo Server stopped. Port 8000 released.
) else (
    echo.
    echo [WARN] Port 8000 is still in use.
)
ping -n 4 127.0.0.1 >nul
exit /b 0
