# 知君桌面打包（2026-09-08）

## 交付范围

当前正式桌面宿主为 `frontend/shell`，版本 `0.1.0`，Electron `37.10.3`。本轮补齐独立的 macOS ARM64 打包流程，不复用 Data Engine 仓库中的旧 CentuarAI PC 宿主。

安装包包含：知君 Shell、桌面 Vite 构建、受控产品操作目录、`zhijun-desktop` 正式 Consumer/Direct 配置、native 1.2.1 sidecar，以及同源半人马 PNG/ICNS。页面、操作目录和 sidecar 位于 `Contents/Resources`；应用移动后由主进程将包内相对 sidecar 路径解析为当前 Resources 下的绝对路径。

## 安全与签名顺序

打包准备脚本只接受固定 SHA-256 的 macOS ARM64 sidecar，将副本放进忽略的 staging 目录并使用公司 Developer ID 和 hardened runtime 签名。随后以签名后文件重新计算 SHA-256 并写入 `zhijun-product.json`。electron-builder 跳过对该已签 sidecar 的二次签名，再签 Electron 嵌套组件和整个应用，避免配置哈希与最终二进制不一致。

包内配置不保存账号、密码、Token、票据、私钥或本机源码绝对路径。生产资源必须是普通文件，sidecar 不允许 group/world writable；运行时继续核对其 SHA-256。桌面渲染器保持 ASAR，native sidecar 单独位于 ASAR 外。

当前机器有 `Developer ID Application: Zhuhai Qeeshu Technology Co., Ltd. (GLHM545ZLS)`，应用和 sidecar 均由 Team `GLHM545ZLS` 签名。此次没有向 Apple 提交公证，`spctl` 的预期结果是 `Unnotarized Developer ID`；因此本产物是已签名的内部测试安装包，不能写成已公证的公开发布包。

## 本机构建与输入依赖

打包还需要 Connectivity SDK 1.2.1 对应的 macOS ARM64 sidecar。该二进制是独立受控发布物，不提交到本仓库；默认从 `data/desktop/native-1.2.1/sidecar/darwin-arm64/nexusaos-connectivity-sidecar` 读取，也可用 `ZHIJUN_SIDECAR_SOURCE` 指向下载或构建得到的文件。准备脚本强制核对源文件 SHA-256 `9613fd07bdd6db4146bf8ddd54d062d513e8171b22b811b29b0f5e9ded80ea59`，同时核对 Electron 版本 `37.10.3` 和 arm64 架构。新 clone 或 CI 必须先从 SDK 的受控发布渠道取得该文件，不能仅凭本仓库重建 native sidecar。

```sh
cd frontend/shell
npm ci
npm run package:mac-arm64
```

该入口依次执行 Shell 144 项测试、桌面 UI 12 项测试、Electron E2E 4 项、桌面 Vite 构建、sidecar 签名与配置生成、DMG/ZIP 构建，以及裸 App、DMG、ZIP 三份内容的签名和资源复验。最后把裸 App 移动到独立临时目录启动，通过本机 DevTools 端点确认 `zhijun://desktop/desktop.html#/` 已创建“知君”页面，再关闭测试进程并清理目录。

## 本次产物与验收

源码基线为 `dev/zhijun-integrate-20260907` 的 `2b2aaa3` 加本轮打包代码。实际输出：

| 产物 | 大小 | SHA-256 |
| --- | ---: | --- |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.dmg` | 107.7 MiB | `40f53051375bb6c9dcb3600f5edcb55da949f2054e94eb394a05af4b9b01d71e` |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.zip` | 106.9 MiB | `6b10130420ff2c1342fb915ac9ea9da1979675290d1af5bcef12be823f82d734` |

复验结果：

- Shell 144 / Desktop UI 12 / Electron E2E 4 全部通过；桌面构建成功。
- 裸 App、挂载后的 DMG App、解压后的 ZIP App 均通过 `codesign --verify --deep --strict`。
- Bundle ID 为 `com.qeeshu.zhijun`，应用版本 `0.1.0`，麦克风用途说明存在。
- ASAR 共 40 个条目，Shell、生产适配器、产品策略和半人马图标均存在；桌面页面与产品操作目录位于受控资源目录。
- sidecar 为 macOS ARM64，签名后 SHA-256 为 `dc38813af2172276eeef174afacb5fca3647e2896f1470122eacae86e3b2f916`，包内配置一致。
- 将裸 App 复制到独立临时目录后，进程保持运行并创建一个标题为“今日来信 · 知君”的窗口，随后已正常关闭测试进程并清理临时目录。

`release/` 与 `package-resources/` 是构建输出，不提交 Git；重新打包会因签名时间戳产生不同的最终哈希，应以当次验证输出为准。
