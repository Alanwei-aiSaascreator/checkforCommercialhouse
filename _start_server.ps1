$ErrorActionPreference = 'Stop'
# 项目根目录 = 本脚本所在目录（跨设备通用，不再写死路径）
$proj = $PSScriptRoot
$root = $proj
$p = Start-Process -FilePath (Join-Path $proj '.venv\Scripts\python.exe') -ArgumentList 'run.py' `
    -WorkingDirectory $proj `
    -RedirectStandardOutput (Join-Path $root '_srv_out.log') `
    -RedirectStandardError (Join-Path $root '_srv_err.log') `
    -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 6
Write-Output ("PID=" + $p.Id + " alive=" + (-not $p.HasExited))
try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/' -UseBasicParsing -TimeoutSec 10
    Write-Output ("GET / -> " + $r.StatusCode)
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/records' -UseBasicParsing -TimeoutSec 10
    $n = ($r.Content | ConvertFrom-Json).Count
    Write-Output ("GET /api/records -> " + $r.StatusCode + " records=" + $n)
} catch {
    Write-Output ("ERROR: " + $_.Exception.Message)
    Get-Content (Join-Path $root '_srv_err.log') -ErrorAction SilentlyContinue | Select-Object -First 20
}
