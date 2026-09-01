#!/bin/bash
set -e

# 只启动 Web 开发服务（Vite），不启动 Electron，也不启动 Python 后端。
# 后端需另行启动：./start-backend.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/frontend"
exec npm run web
