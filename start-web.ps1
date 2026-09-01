# 只启动 Web 开发服务（Vite），不启动 Electron，也不启动 Python 后端。
# 后端需另行启动：.\start-backend.ps1
$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $scriptDir "frontend")
npm.cmd run web
