# 桌面版 0.1.35 安装包

## 计划与验收

在当前分支构建 macOS Apple Silicon 正式安装包，纳入本轮事项上下文授权提示。

- [x] 核实正式发布脚本、Node 22.23.2 和 Developer ID 签名环境。
- [x] 核实 E2E 与搬迁启动使用独立临时用户目录，仅结束自己的测试子进程，不替换或退出用户已安装应用。
- [x] 执行完整 `package:mac-arm64` 流水线，版本由 0.1.34 自动递增一次到 0.1.35。
- [x] 验证 DMG、ZIP、应用签名、隔离目录启动及包内前端与当前构建一致。
- [x] 记录 SHA-256 与构建结果。

影响文件：`frontend/shell/package.json`、`package-lock.json` 版本信息、被忽略的构建资源与安装包；本报告。
依赖：已有前端依赖、固定 sidecar、Developer ID Application 签名证书。
保留正式 production 配置，沿用 `notarize:false`。不安装、不上传、不发布远程更新。
盒端检索修复独立部署；客户端构建成功不代表盒端业务验收。

## 构建结果

2026-09-17 正式流水线一次执行完成，退出码 0。Node 22.23.2，Electron 37.10.3。

- Shell：364 passed、1 Windows 专用 skipped。
- Desktop：54 passed；Electron E2E：4 passed；Vue 类型检查与桌面构建通过。
- Developer ID Application 签名有效，Team `GLHM545ZLS`；原应用、DMG 只读挂载应用、ZIP 解压应用均通过官方内容及签名核验。
- 搬迁至临时目录、独立用户目录启动成功，标题「今日来信 · 知君」，页面 `zhijun://desktop/desktop.html#/`。
- 包内 30 个宿主资源、81 个桌面构建文件及操作清单与当前构建逐字一致。
  已验证包含本轮「本轮有事情记录因你的“跳过受限资料”设置，未提供给在线模型」提示。
- production 配置与已安装版一致，签名 sidecar 的哈希更新为 `2106380a561ee68793b7a15c381a440e185858d9bf3f25be6e7e3d9a4c130dd7`。
- 版本与锁文件仅修改版本号，没有改变依赖。原 0.1.34 DMG/ZIP 保留。

| 产物 | 字节数 | SHA-256 |
|---|---:|---|
| `frontend/shell/release/Zhijun-0.1.35-mac-arm64.dmg` | 113900284 | `d6fb1aa0403406bca60c335825537ce84ba541928b486a26151467df589d90f5` |
| `frontend/shell/release/Zhijun-0.1.35-mac-arm64.zip` | 113418542 | `0283ecc5cc593afd46edac2b09b4861aec7aea7ea0333248c724874ca32d9950` |

构建日志：`frontend/shell/release/build-0.1.35.log`；机器核验回执：`verification-0.1.35.json`、`source-verification-0.1.35.json`（同目录，均为忽略的生成文件）。

未替换 `/Applications/知君.app`；原进程持续运行。未安装、未上传、未发布远程更新。
沿用现有配置，应用及 sidecar 已签名，DMG 本体未单独签名，未做 Apple 公证。
