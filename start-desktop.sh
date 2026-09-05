#!/bin/bash
set -e

# 只启动 Electron 桌面应用（由原 start-frontend.sh 迁移而来）。
# 桌面模式允许 Electron 托管后端子进程；不启动任何 Vite 开发服务。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/frontend"
export PATH="$HOME/.local/bin:$PATH"
# Some GUI launchers inherit this from Electron-based parent applications.  If
# left set, Electron behaves like plain Node.js and the desktop window vanishes.
unset ELECTRON_RUN_AS_NODE
exec ./node_modules/.bin/electron . --no-sandbox
