# 知君 0.1.26 正式 macOS 安装包

日期：2026-09-15。范围：本轮测试问题修复后的桌面客户端正式重新打包；盒端部署另见本次公司盒部署记录。

## 构建及验证

- 使用 Node.js 22.23.2、现有 `frontend/shell` 的 `package:mac-arm64` 全流程，自动将版本由 0.1.25 升至 0.1.26，非 test flavor。
- Shell 全套测试、桌面专用测试、Electron E2E、桌面前端构建全部通过。
- macOS 正式验包通过：展开应用、只读挂载 DMG 内应用及解压 ZIP 内应用的签名、架构、资源、依赖和产品配置均通过检查。
- 隔离用户目录、搬迁路径下的已打包应用启动冒烟通过，打开 `zhijun://desktop/desktop.html#/`，标题为“今日来信 · 知君”。没有替换 `/Applications` 中现有应用。
- 额外逐字节核验 26 个宿主文件与 77 个前端构建文件均与本次源码/产物一致。
- 复用本机既有生产配网环境输入；发现和 BLE 配网开关均为 true，信任根 1 个，与 0.1.25 及既有生产信任配置一致。未生成、替换或导出 CA 私钥。
- 0.1.25 DMG/ZIP 均保留，校验和未变化。

## 产物

| 文件（`frontend/shell/release/`） | SHA-256 |
| --- | --- |
| `Zhijun-0.1.26-mac-arm64.dmg` | `54e2d8a913c9535ee2c303ed30e4af6e8c61e4524d2029eb05e437b5c82207a0` |
| `Zhijun-0.1.26-mac-arm64.zip` | `d5225303ba2c24358936cab1e0cb02cb2751eaba1bb30b0c5f59f59709b42090` |

同目录 `Zhijun-0.1.26-mac-arm64.SHA256SUMS` 保存上述校验和。

应用和 sidecar 使用 Developer ID Application 签名，Team ID `GLHM545ZLS`。包内 sidecar SHA-256 为 `6711981b12d0bc9f1ba13050ef7c5bad0879a72d9537969be6911008a2f95ce4`。

## 验证边界

- 配置仍为 `notarize: false`；没有进行 Apple 公证，`stapler validate` 确認应用没有附加公证票据。Developer ID 签名不等于已公证，也不保证首次下载的 Gatekeeper 提示消失。
- 此为 macOS Apple Silicon 包，不是 Windows 或 Intel Mac 包。
- 打包和启动冒烟不替代真实盒端聊天、本体候选确认、材料检索及蓝牙配网验收。
- 额外尝试通用 `verify-packaged-content.cjs` 时发现其按顶层开发依赖 lock 期望 `@noble/hashes` 2.4.0，而 macOS 既有生产依赖清单为 1.8.0（lock 中生产子树同为 1.8.0）。因此未把该非 macOS 流水线检查声称为通过；未修改锁定依赖或验包脚本。正式 macOS 专用验包及额外源码/资源逐字节校验均通过。
