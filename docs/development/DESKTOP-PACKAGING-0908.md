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

该入口依次执行 Shell 146 项测试、桌面 UI 12 项测试、Electron E2E 4 项、桌面 Vite 构建、sidecar 签名与配置生成、DMG/ZIP 构建，以及裸 App、DMG、ZIP 三份内容的签名和资源复验。最后把裸 App 移动到独立临时目录启动，通过本机 DevTools 端点确认 `zhijun://desktop/desktop.html#/` 已创建“知君”页面，再关闭测试进程并清理目录。

## PDF 原件预览

Electron/Chromium 的内置 PDF 查看器在 `zhijun-media:` 自定义协议中会出现空白页，并依赖浏览器扩展资源，不适合当前关闭插件、禁止 frame 的桌面安全策略。桌面端现固定使用 Apache-2.0 的 `pdfjs-dist@4.10.38`，由同源 `.mjs` Worker 把 PDF 页面绘制到 Canvas。PDF.js 5.x 使用的 `Uint8Array.toHex()` 与当前 Electron 37.10.3 运行时不兼容，因此没有使用 5.x。

原件预览改为用户点击“查看 PDF 原件”后才打开。宿主返回 32 字节随机能力句柄对应的 `zhijun-media://session/<handle>`，页面切换、切盒、关闭预览或组件卸载时会立即中止读取、销毁 PDF.js 任务并关闭句柄。读取上限为 64 MiB；画布单轴上限 8192 像素、总像素上限 1600 万，PDF.js eval 被关闭。图片和音频继续直接使用支持 Range 的能力 URL，避免完整复制为 Blob 后增加内存和首帧等待。

真机复验发现，PDF.js 首次绘制期间 Canvas 已经进入布局，用户会短暂看到一块白色区域；容器随后变化宽度时又可能取消正在进行的绘制。本轮增加明确的“正在打开 PDF / 正在渲染 PDF”状态，只在当前页绘制完成后展示 Canvas；空白绘制会在下一次 paint 后重试一次。尺寸监听只在当前任务结束后重绘，并等待旧任务取消完成后再复用 Canvas，避免首屏白页和重复取消。

渲染器请求从协议黑名单改为精确允许 `zhijun://desktop`、合法 `zhijun-media` 句柄，以及受限的同源 Blob/图片 Data URL。`frame-src` 和 `object-src` 均为 `none`，Electron 插件保持关闭。自定义静态资源处理器已补充 `.mjs` JavaScript MIME，CSP 仅允许同源 Worker。

## 本次产物与验收

源码基线为 `dev/zhijun-integrate-20260908` 的 `6dec0cb`。实际输出：

| 产物 | 大小 | SHA-256 |
| --- | ---: | --- |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.dmg` | 108.2 MiB | `a345ac147c1a9623a4dac38be84a356f6598447da6a6b13cba076cd9a9b01d10` |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.zip` | 107.4 MiB | `c498f18027b66f77eb629cede735e1b3b0dbe2ebb61e6ee955ddebf295229b85` |

复验结果：

- 完整 Web 86 项、Shell 146 项、Desktop UI 12 项、Electron E2E 4 项全部通过；桌面构建成功。
- 真实 Electron 37.10.3 通过 `zhijun://desktop` 加载 PDF.js 4.10.38 Worker，并把两页 PDF 夹具的第一页绘制为 833×1178 Canvas；像素检查与导出图片均确认存在实际文字内容。
- `npm audit --omit=dev --registry=https://registry.npmjs.org` 为 0 个漏洞；PDF.js 许可证为 Apache-2.0。
- 裸 App、挂载后的 DMG App、解压后的 ZIP App 均通过 `codesign --verify --deep --strict`。
- Bundle ID 为 `com.qeeshu.zhijun`，应用版本 `0.1.0`，麦克风用途说明存在。
- ASAR 共 40 个条目，Shell、生产适配器、产品策略和半人马图标均存在；桌面页面与产品操作目录位于受控资源目录。
- sidecar 为 macOS ARM64，签名后 SHA-256 为 `0bedb9304c29c33f061b06c753865bc5c2e829748e2acd7ee3f69966543d6761`，包内配置一致。
- 将裸 App 复制到独立临时目录后，进程保持运行并创建一个标题为“今日来信 · 知君”的窗口，随后已正常关闭测试进程并清理临时目录。
- 最新签名 App 已安装到 `/Applications/知君.app`，其 `app.asar` SHA-256 与本次裸 App 一致，并再次通过 `codesign --verify --deep --strict`。上一版保留为 `/Applications/知君.app.before-conversation-perf-20260908`；安装后账号会话恢复成功，设备选择页列出两台在线盒子。
- 公司网络下使用本次已签裸 App 和已保存的加密登录状态，成功连接设备名 `AMD-A2A-248`；设备名称优先于设备 ID 显示。原材料列表加载约 1007 ms，含 3 条真实资料；PDF 详情首屏约 504 ms。
- 对话详情加载改为主详情优先、辅助区错峰挂载并取消过期请求。开发版连接同一公司盒连续交替打开已有会话，6 次详情可用时间为 395、588、486、631、1855、2356 ms；该测量发生在 Gateway 扫描节流上线前，后两次仍反映盒端历史任务与网络波动。
- 对盒内 1.8 MiB、12 页的真实 PDF 首次点击预览，4114 ms 内完成下载与第 1 页绘制。过程中先后出现“正在打开 PDF”和“正在渲染 PDF”，绘制完成前 Canvas 始终隐藏；完成画布为 560×315，对 200×200 缩略采样得到 23581 个非白像素、13086 个深色像素，确认不再是空白页。
- 同一材料的隐私状态返回“需要人工复核”，当前账号明确显示具有原件权限；“查看并复核”在约 462 ms 内成功打开两个受控候选块，关闭后页面清除了候选 DOM，没有提交批准、拒绝或修正写操作。
- 盒端 8618、8619、8620 的 `/api/health` 均返回 200；新知君容器为 healthy，`RestartCount=0`、`OOMKilled=false`，整机 boot ID 未变化。PDF 内嵌 CFF 字体会产生 Chromium OTS 警告，但实际页面已经通过像素验收，不影响本文件显示。

`release/` 与 `package-resources/` 是构建输出，不提交 Git；重新打包会因签名时间戳产生不同的最终哈希，应以当次验证输出为准。

## 在线理解修复后的重新打包

在线模型 403 根因修复后重新执行完整 `package:mac-arm64` 流水线。新包包含稳定错误码的具体中文提示；当盒端外发治理、供应商域名或模型流媒体类型配置错误时，不再只显示通用 `请求失败（403）`。本轮输出为：

| 产物 | 大小 | SHA-256 |
| --- | ---: | --- |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.dmg` | 108.2 MiB | `42b3409c78869951107042e8eeb7e248cf7e60e827e0ac40f641daf334ed7714` |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.zip` | 107.4 MiB | `8ff38baf660627b6547627bde1807007b729f0d79d40aaafdd984df6b3552c4d` |

Shell 146 项、桌面 UI 12 项、Electron E2E 4 项均通过；签名后的 sidecar SHA-256 为 `995597515dee69a38d1bf80af7e5eca3b6898d8526901745d25cad2ec441a93b`。裸 App、DMG、ZIP 资源和签名检查通过，应用从独立临时目录启动后页面为 `zhijun://desktop/desktop.html#/`，标题为“今日来信 · 知君”。该包仍是未公证的内部测试安装包。

## 授权入口与登录记忆后的重新打包

本轮在偏好页补充“在线模型与资料授权”入口，并明确区分资料来源默认授权与桌面端每次向外部模型发送内容前的确认。现有资料授权可以减少资料范围相关的重复选择，但当前桌面连接协议仍要求逐次确认实际外发内容；前端不会把资料默认授权误写为全局免确认开关。

登录页会记住上一次成功登录的手机号。用户勾选“使用系统安全存储记住密码”后，密码只在 Electron 主进程中通过 macOS `safeStorage` 加密并以 `0600` 权限保存；渲染页面只能读取手机号和“是否已保存密码”布尔值，密码输入框始终为空且不会回显。使用已保存密码登录时，解密与提交均留在主进程；取消勾选并成功登录后会覆写为只保留手机号。连接会话过期或主动退出不会自动登录，也不会删除用户明确保存的账号资料。

本次输出：

| 产物 | SHA-256 |
| --- | --- |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.dmg` | `fb38ea2c5453dce29f6fb85e3d41b9763c45a43f15904f9ddbb6850fb8d64051` |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.zip` | `9eb684b9d793a7656c29600604c2eacdd2443ad64d9b18a35dd552d47cdf25eb` |

完整 Shell 155 项、Desktop UI 15 项、Electron E2E 4 项、前端产品传输 24 项均通过，TypeScript 检查和桌面构建成功。签名后的 sidecar SHA-256 为 `d36951919d922558ddbfc815e2aeb12cc5cfd407abd779095327c485d0b564ec`；裸 App、DMG、ZIP 的签名、资源复验和独立启动冒烟检查均通过。

最新 App 已安装到 `/Applications/知君.app`，上一版备份为 `/Applications/知君.app.before-login-memory-20260908`。真机验收完成：退出后登录页自动填入上次手机号，密码框为空并显示安全保存提示；不重新输入密码即可由主进程完成登录，随后成功连接公司盒子 `AMD-A2A-248`。验收过程和仓库文档均未记录真实密码。

## 默认在线发送授权后的重新打包

本轮新增独立的“符合范围时不再逐次确认”开关。它与“资料来源默认授权”分开保存；开启时，桌面仍为每次请求取得短期凭据，盒端会重新核对服务、配置版本、用途和全部资料版本。关闭开关、撤销资料、切换供应商或配置版本变化后，下一次请求立即恢复逐次确认。旧版资料授权升级后不会自动获得免确认权限。

本次完整 `package:mac-arm64` 流水线通过，产物为：

| 产物 | SHA-256 |
| --- | --- |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.dmg` | `1b89250ce8fd56d4d9aff2709dd4d166dcfa4ce16517a191d77cdef9030eaccc` |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.zip` | `b9ec9d9b5b83768df614c09b26c532afa4eda2ee515549fe4fc5b5ae2080e0e4` |

签名后的 sidecar SHA-256 为 `43ff4b11bb9f2311551b41c0a4ec915dfb1a133e2d978a03e152ce5cabc78300`。裸 App、DMG、ZIP 的签名与资源复验通过，独立启动冒烟检查成功。最新 App 已安装到 `/Applications/知君.app`，安装前版本保存在 `/Applications/知君.app.before-standing-consent-20260908`。

公司盒真机验收中，默认授权关闭后界面立即恢复“每次在线发送仍会单独确认”；重新明确勾选范围并启用后，新建在线对话连续两次收到指定的 DeepSeek 回复，均未弹出外发确认。在线通道独立测试成功，耗时约 1496 ms。盒端容器保持 `healthy`、`RestartCount=0`、`OOMKilled=false`。
