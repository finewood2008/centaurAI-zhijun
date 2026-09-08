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

渲染器请求从协议黑名单改为精确允许 `zhijun://desktop`、合法 `zhijun-media` 句柄，以及受限的同源 Blob/图片 Data URL。`frame-src` 和 `object-src` 均为 `none`，Electron 插件保持关闭。自定义静态资源处理器已补充 `.mjs` JavaScript MIME，CSP 仅允许同源 Worker。

## 本次产物与验收

源码基线为 `dev/zhijun-integrate-20260908` 的 `63f2f2e`。实际输出：

| 产物 | 大小 | SHA-256 |
| --- | ---: | --- |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.dmg` | 108.2 MiB | `c8cd64bf487153ab42fc9c32dc3fbbe42670d66ffbad5736d07fb8d49ff46b79` |
| `frontend/shell/release/Zhijun-0.1.0-mac-arm64.zip` | 107.4 MiB | `42ba3ef41bf0253fc49a5fec174b04b058dec5511b30fb5ce3c6aea237fe186e` |

复验结果：

- 完整 Web 86 项、Shell 146 项、Desktop UI 12 项、Electron E2E 4 项全部通过；桌面构建成功。
- 真实 Electron 37.10.3 通过 `zhijun://desktop` 加载 PDF.js 4.10.38 Worker，并把两页 PDF 夹具的第一页绘制为 833×1178 Canvas；像素检查与导出图片均确认存在实际文字内容。
- `npm audit --omit=dev --registry=https://registry.npmjs.org` 为 0 个漏洞；PDF.js 许可证为 Apache-2.0。
- 裸 App、挂载后的 DMG App、解压后的 ZIP App 均通过 `codesign --verify --deep --strict`。
- Bundle ID 为 `com.qeeshu.zhijun`，应用版本 `0.1.0`，麦克风用途说明存在。
- ASAR 共 40 个条目，Shell、生产适配器、产品策略和半人马图标均存在；桌面页面与产品操作目录位于受控资源目录。
- sidecar 为 macOS ARM64，签名后 SHA-256 为 `b48b8ff6271bcb47b28008dad22a34fcafbbbe5dd792b97e09985c839eabfd67`，包内配置一致。
- 将裸 App 复制到独立临时目录后，进程保持运行并创建一个标题为“今日来信 · 知君”的窗口，随后已正常关闭测试进程并清理临时目录。
- 家庭盒管理面仍显示在线，但最终复验时本机到 `192.168.1.18:22` 超时，Direct 返回 `DIRECT_CONNECTION_UNAVAILABLE`，因此这次 PDF 渲染器改动采用真实 Electron + 本地 PDF 夹具验收；此前同一盒子的原件读取、隐私复核状态和 1977 字解析结果已完成真机验证。本条不把当前不可达状态写成盒端通过。

`release/` 与 `package-resources/` 是构建输出，不提交 Git；重新打包会因签名时间戳产生不同的最终哈希，应以当次验证输出为准。
