#!/bin/bash
# 本脚本已废弃：名称容易与「Web 前端」混淆。
# 请改用明确的启动命令：
#   ./start-web.sh        # 只启动 Web 开发服务（Vite），不启动 Electron
#   ./start-desktop.sh    # 只启动 Electron 桌面应用
# 前端 package.json 内同样区分 npm run web 与 npm run desktop。
cat <<'EOF'
start-frontend.sh 已废弃：该名称易被误认为 Web 前端启动脚本。

请改用：
  ./start-web.sh        # 只启动 Web 开发服务（Vite，不启动 Electron）
  ./start-desktop.sh    # 只启动 Electron 桌面应用
EOF
exit 1
