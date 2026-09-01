#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
export CENTAURAI_DATABASE_DATA_ROOT="${CENTAURAI_DATABASE_DATA_ROOT:-$SCRIPT_DIR/data}"

# 此脚本只服务本机研发。与 PowerShell 启动器保持一致，显式开启 MindOS Web
# 的 loopback 调试上下文；生产部署不使用本脚本，缺失这些变量时仍要求连接票据。
export MINDOS_RUNTIME_ENV="${MINDOS_RUNTIME_ENV:-development}"
export MINDOS_LOCAL_WEB_DEBUG_ACCESS="${MINDOS_LOCAL_WEB_DEBUG_ACCESS:-true}"

mkdir -p "$CENTAURAI_DATABASE_DATA_ROOT"
CENTAURAI_DATABASE_DATA_ROOT="$(cd "$CENTAURAI_DATABASE_DATA_ROOT" && pwd -P)"
export CENTAURAI_DATABASE_DATA_ROOT

cd "$BACKEND_DIR"
exec "$BACKEND_DIR/.venv/bin/python" server.py
