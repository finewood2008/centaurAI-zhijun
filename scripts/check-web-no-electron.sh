#!/bin/bash
# 阶段A CI 检查：Web 运行路径不得启动 Electron（也不得被误当作 Electron 启动命令）。
#   - 静态断言：npm run web / web:build 脚本不得引用 electron；desktop 必须引用 electron；
#   - 可选动态断言（CI_WEB_NO_ELECTRON_RUNTIME=1）：启动 Vite 后进程树无 Electron 可执行文件。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/frontend"

fail() { echo "FAIL: $*" >&2; exit 1; }

web_script="$(node -e "console.log(require('./package.json').scripts.web)")"
web_build="$(node -e "console.log(require('./package.json').scripts['web:build'])")"
desktop_script="$(node -e "console.log(require('./package.json').scripts.desktop)")"

for s in "$web_script" "$web_build"; do
  case "$s" in
    *electron*) fail "web 脚本引用了 electron（$s）" ;;
  esac
done
case "$desktop_script" in
  *electron*) : ;;
  *) fail "desktop 脚本未引用 electron（$desktop_script）" ;;
esac

# Web 源码不得反向依赖桌面 IPC / preload。仅检查 package 脚本不足以阻止以后
# 在 Vue/TypeScript 中直接 import Electron 相关模块。
if grep -R -E -n \
  --include='*.ts' --include='*.tsx' --include='*.js' --include='*.vue' \
  '(electron|backend-rpc\.js|preload\.js|renderer/)' \
  mindos-web/src >/dev/null; then
  fail "mindos-web 源码引用了 Electron、桌面 RPC 或 preload 模块"
fi

if [ "${CI_WEB_NO_ELECTRON_RUNTIME:-0}" = "1" ]; then
  timeout 30 npm run web >/tmp/web-no-electron.log 2>&1 &
  pid=$!
  sleep 10
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  if pgrep -af "/node_modules/electron" >/dev/null 2>&1; then
    fail "npm run web 进程树中出现 Electron 可执行文件"
  fi
fi

echo "PASS: Web 运行路径不启动 Electron"
