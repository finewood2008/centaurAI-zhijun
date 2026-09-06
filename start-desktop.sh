#!/bin/bash
set -euo pipefail

# 独立知君桌面；构建本地页面后启动新宿主，不启动 Python 或 Vite 服务。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
export PATH="$HOME/.local/bin:$PATH"
unset ELECTRON_RUN_AS_NODE
case "${1:-}" in
  ""|--simulation|--real) ;;
  *) echo "用法：./start-desktop.sh [--simulation|--real]" >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then echo "仅支持一个启动模式参数" >&2; exit 2; fi
if [ ! -x frontend/shell/node_modules/.bin/electron ] || [ ! -x frontend/mindos-web/node_modules/.bin/vite ]; then
  echo "请先执行 npm --prefix frontend/mindos-web ci 和 npm --prefix frontend/shell ci" >&2
  exit 1
fi
if [ "${1:-}" = --real ]; then
  if [ -n "${ZHIJUN_DESKTOP_CONFIG:-}" ]; then
    node -e 'require("./frontend/shell/production/config.cjs").loadConfig(process.env.ZHIJUN_DESKTOP_CONFIG).catch(() => { console.error("指定的桌面配置无效"); process.exitCode = 1; })'
  else
    node frontend/shell/scripts/prepare-real.cjs
    export ZHIJUN_DESKTOP_CONFIG="$SCRIPT_DIR/data/desktop/zhijun-product.json"
  fi
  unset ZHIJUN_DESKTOP_MODE
  set --
fi
npm --prefix frontend/mindos-web run build:desktop
exec node frontend/shell/launch.cjs "$@"
