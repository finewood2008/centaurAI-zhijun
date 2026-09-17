# 知君 0.1.27 正式 macOS 打包

## 计划与验收

- 以当前已回归源码生成正式 Apple Silicon 安装包，沿用 `frontend/shell/release/` 流程，版本由 0.1.26 升为 0.1.27。
- 包含登录失效自动回登录、临时账号服务故障手动恢复及此前业务修复；仅发布客户端，不部署盒端。
- 复用既有生产配网开关与信任根，不生成或变更证书；保留旧版本 DMG/ZIP。
- 执行正式 `package:mac-arm64` 测试、构建、签名、DMG/ZIP 验包及隔离目录启动检查。
- 独立核对包内宿主和前端资源与当前构建一致；报告签名、公证状态和 SHA-256。
- 影响文件：package.json、package-lock.json 的版本，忽略的构建/安装包目录及本记录。不替换 `/Applications` 中应用、不提交仓库。

## 进度

- [x] 核对正式流水线、平台及现有版本。
- [x] 完成正式构建、签名和验包。
- [x] 独立内容核对、启动检查及交付。

## 构建结果

正式 `npm run package:mac-arm64` 全流程退出码 0。Shell 测试（335 通过、1 Windows 专用跳过）、前端 desktop 测试（54）、Electron E2E（4）、类型检查及桌面构建均通过。版本文件及锁文件仅从 0.1.26 升为 0.1.27，没有改变依赖。

macOS 专用验包验证展开应用、只读挂载 DMG 内应用及 ZIP 解压应用：应用 Developer ID 签名、Team `GLHM545ZLS`、架构、版本、权限描述、包内依赖与 sidecar 哈希均通过。搬迁到临时目录，以隔离用户目录启动冒烟通过，标题“今日来信 · 知君”，页面 `zhijun://desktop/desktop.html#/`。未替换用户已安装应用。

| 产物（frontend/shell/release/） | SHA-256 |
| --- | --- |
| Zhijun-0.1.27-mac-arm64.dmg | 319f19077d1c421ff9b8f22e26fa6a8288a04416398ea6ccf022c313adad8239 |
| Zhijun-0.1.27-mac-arm64.zip | aef61c8395f3a4021c5dee43087bc85b2c0274e1c9dcee6f4c3a96c05196a24c |

同目录 SHA256SUMS 已生成，并通过 `shasum -a 256 -c` 校验。旧版 0.1.26 DMG/ZIP 保留，哈希与上次记录一致。新包及校验文件均被 `.gitignore` 忽略。

签名后 sidecar SHA-256：`ef82ca001ed93e4ab67cf735a6cd27895981c861537585075b6541688dae7fc8`。它由既有、哈希固定的原始 sidecar 重新签名，时间戳造成签名后哈希与前版不同。

## 独立内容验收

- 26 个宿主 JS/CJS 与当前源码逐字节一致；77 个前端构建文件的路径集合与内容均一致。
- `consumer-client.cjs` 的会话失效处理已入 ASAR；账号服务故障恢复按钮与失败空态标记均出现在包内 `assets/desktop-Ch0mkeF9.js`。
- 产品配置 discovery=true、BLE=true、rootCount=1；pins/PEM 与既有 release-inputs 完全一致。
- 共享操作目录逐字节一致，ASAR 版本为 0.1.27，未启用测试版标记。

## 交付边界

- 正式 flavor 的 macOS Apple Silicon 包，不是 Windows、Intel Mac 或 test 包。
- 应用和 sidecar 使用 Developer ID 签名；DMG 本体没有单独签名。配置仍为 `notarize:false`，未进行 Apple 公证，不能保证首次下载不出现 Gatekeeper 提示。
- 配网开关与信任根沿用生产配置；未更换或导出 CA 私钥。
- 本轮只重新打包客户端，不部署盒端、不修改用户账号/资料；启动检查不代替真实盒端聊天、材料检索或蓝牙配网验收。
