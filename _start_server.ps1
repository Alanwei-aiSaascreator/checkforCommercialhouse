$ErrorActionPreference = 'Stop'
$root = 'c:\Users\26562\CodeBuddy\20261111100641'
$proj = Join-Path $root 'address-check'
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
