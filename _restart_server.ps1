$ErrorActionPreference = 'Stop'
# 项目根目录 = 本脚本所在目录（跨设备通用，不再写死路径）
$proj = $PSScriptRoot
$root = $proj

# 杀掉所有 run.py 服务进程（CIM 按 CommandLine 匹配——Get-Process 无 CommandLine 属性，
# 旧脚本因此从未杀掉旧实例，导致新实例绑定端口失败、服务一直跑旧代码）
$old = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*run.py*' }
foreach ($p in $old) {
    Write-Output ("killing old service PID=" + $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

# 杀掉本项目残留的 Chrome（命令行里带本项目路径的 user-data-dir）。
# 旧脚本只杀 python、从不杀 Chrome，导致每次重启旧浏览器窗口都挂着，
# 几十轮累积出上百个进程（实测一次清理到 146 个）——这就是"一堆窗口"的来源。
$chromes = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
    Where-Object { $_.CommandLine -match [regex]::Escape($proj) }
foreach ($p in $chromes) {
    Write-Output ("killing stale chrome PID=" + $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2

$p = Start-Process -FilePath (Join-Path $proj '.venv\Scripts\python.exe') -ArgumentList 'run.py' `
    -WorkingDirectory $proj `
    -RedirectStandardOutput (Join-Path $root '_srv_out.log') `
    -RedirectStandardError (Join-Path $root '_srv_err.log') `
    -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 6
Write-Output ("new PID=" + $p.Id + " alive=" + (-not $p.HasExited))
$listen = netstat -ano | Select-String ':8000.*LISTENING'
Write-Output ("port8000: " + $listen)
try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/' -UseBasicParsing -TimeoutSec 10
    Write-Output ("GET / -> " + $r.StatusCode)
} catch {
    Write-Output ("ERROR: " + $_.Exception.Message)
    Get-Content (Join-Path $root '_srv_err.log') -ErrorAction SilentlyContinue | Select-Object -First 20
}
