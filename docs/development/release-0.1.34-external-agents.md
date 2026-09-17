# 桌面版 0.1.34 安装包

## 计划与验收

用户要求在公司盒更新后生成新的安装包。本次沿用当前已安装正式版配置，目标 macOS Apple Silicon。

- [x] 检查当前源码、版本和发布脚本，确认保留当前 production 配置与配网开关。
- [x] 使用 Node 22 执行 `package:mac-arm64` 各阶段，包含测试、桌面构建、自动 patch 升版、签名和打包。
- [x] 通过 DMG/ZIP 内容与签名核验、隔离用户目录的搬迁启动检查。
- [x] 比对包内宿主源码、桌面资源和操作清单，确认外部 Agent 兼容修复进入产物。
- [x] 记录版本、文件路径、SHA-256 和发布限制。

依赖：本机 Node 22、Electron、现有 Developer ID 签名和已核准 sidecar。
影响：`frontend/shell/package.json` 与 `package-lock.json` 的版本号、被忽略的构建资源和 release 产物；
纳入当前工作区已有的外部 Agent 兼容修复。主代理执行构建，子代理只读审阅发布配置与验证范围。

验收：完整发布命令成功；正式 bundle ID 与签名有效；新产物与当前源码一致；独立启动能加载桌面。
沿用现有 `notarize:false` 配置，不代表已完成 Apple 公证。

## 结果

2026-09-17 完成正式 macOS arm64 0.1.34 构建，使用 Node 22.23.2。

- `frontend/shell/release/Zhijun-0.1.34-mac-arm64.dmg`
  SHA-256：`3d1097be983c7294cdfb3979b64450db759132bf9cd4ea32a20547bf41f98ac6`。
- `frontend/shell/release/Zhijun-0.1.34-mac-arm64.zip`
  SHA-256：`a725e4cc486849135e3e3731a10253ebd44c074f299843351e8b4b2d5a2b5a09`。
- Developer ID 签名、原始 App、DMG 挂载和 ZIP 解压后的验包全部通过。
- 搬迁至临时目录、使用独立用户配置启动成功，显示「今日来信 · 知君」。
- 29 个宿主源码文件、81 个桌面构建文件与包内内容逐字一致，catalog 同样一致。
  包含本轮 `WORKSPACE_OPERATION_DENIED` 精确诊断和外部 Agent 状态兼容处理。
- production 账号服务地址、连接 profile、配网配置与当前安装版本一致；两个配网 gate 保持关闭。

Shell 测试 364 passed、1 Windows 专用 skipped；desktop 测试 54 passed。
首次完整流水线在 Electron E2E 停止：产品调整已将退出登录移入设置页，旧测试仍查找原位置。
仅修改 `frontend/shell/tests/electron.e2e.cjs`，通过真实设置链接进入后点击退出；4 项 E2E 全部通过。
随后从桌面构建阶段依次执行剩余正式发布步骤，全部成功，版本仅递增一次。
此前本轮外部 Agent 单元、桥接和 Vue E2E 已通过，应用源码未再变更。

未替换 `/Applications/知君.app`，未发布远程更新。安装包已签名、未做 Apple 公证。
