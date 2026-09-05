# 只启动本机 FastAPI 后端，不启动 Vite 或 Electron。
$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $scriptDir "backend"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "未找到后端 Python 环境: $python"
}

if ([string]::IsNullOrWhiteSpace($env:CENTAURAI_DATABASE_DATA_ROOT)) {
    $env:CENTAURAI_DATABASE_DATA_ROOT = Join-Path $scriptDir "data"
}
New-Item -ItemType Directory -Force -Path $env:CENTAURAI_DATABASE_DATA_ROOT | Out-Null

# 此脚本只服务本机研发。显式开启 MindOS Web 的 loopback 开发调试上下文；
# 部署环境不得复用该脚本或这两个变量，缺失时后端会要求阶段 2 的连接票据。
if ([string]::IsNullOrWhiteSpace($env:MINDOS_RUNTIME_ENV)) {
    $env:MINDOS_RUNTIME_ENV = "development"
}
if ([string]::IsNullOrWhiteSpace($env:MINDOS_LOCAL_WEB_DEBUG_ACCESS)) {
    $env:MINDOS_LOCAL_WEB_DEBUG_ACCESS = "true"
}

Set-Location $backendDir
& $python server.py
