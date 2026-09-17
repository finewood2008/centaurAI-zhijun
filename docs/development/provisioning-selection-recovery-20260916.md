# 配网扫描发现后立即过期修复

## 原因与计划

- 知君 preload 和 discovery SDK main 都在 8 秒后取消整个 requestDevice，晚发现的设备没有独立选择时间。
- SDK main 在同一原生选择请求更新 callback 时调用旧 callback('')，会取消仍在进行的选择请求。Electron 官方示例是保存最新 callback，仅选择或取消时调用。
- 采用知君产品级 picker，沿用 SDK IPC、选择租约和加密配网流程，不修改 node_modules 或降低设备身份验证要求。

## 验收标准

1. 用户点击后开始扫描，实时展示有效候选；等待发现最多 30 秒，首次发现后另留 30 秒选择（单轮最多 60 秒），列表更新不无限续期。
2. 列表回调变化不会取消原生请求；选择仅接受当前可信主 frame、当前 session、当前候选 ID。
3. 取消、过期、窗口关闭清理一次；过期条目不可选择，旧 session 不影响新一轮。
4. 不自动选择设备、不改 Wi-Fi、不自动绑定；物理确认码、证书与归属确认保持原状。

## 影响文件与依赖

- 新增 provisioning/picker.cjs：主进程选择生命周期，依赖现有 discovery SDK 的 IPC channel 定义。
- provisioning-window.cjs：正式模式加载产品 picker，测试版旧链路不变。
- provisioning/preload-formal.mjs：移除 8 秒竞争计时；启动兜底有界。
- provisioning/renderer.js：明确扫描与选择提示。
- shell/tests：真实 SDK endpoint + 产品 picker 联测，以及晚发现/重复 callback/取消/失效权限回归。
- scripts/verify-macos-package.cjs、verify-packaged-content.cjs：把新增 picker 纳入 macOS/Windows 安装包白名单和源码验包。

## 验证进度

- [x] 源码定位与方案确认
- [x] 修复与边界测试
- [x] shell 回归、bundle 构建
- [ ] 可用硬件下扫描验证（不提交配网或绑定）

已覆盖不同 callback 的重复扫描快照、29.9 秒发现后仍有 30 秒选择时间、重复广播不续期、过期/重复点击/旧 session、跨 frame、候选移除、窗口关闭、通知和 native callback 抛错时的清理。

桌面控制服务连续返回 timeout，未能进行当前机器蓝牙实扫；自动化测试包含产品 picker 与真实 SDK selection endpoint 联测，不等同于实际配网成功。

并行只读复核提出 teardown 异常路径加固，已加入通知失败仍结算原生请求、原生 callback 失败仍移除监听器的测试。

最终 shell 全量测试：350 项，349 通过，1 项 Windows 原生 ACL 测试在 macOS 跳过；失败 0。

## 交付

- 版本 0.1.30，正式 macOS arm64 DMG/ZIP，Developer ID 签名；构建配置未开启 Apple notarization。
- 标准 macOS 验包通过；额外逐字节核对 ASAR 内 window、preload、picker、renderer 与最终源码一致。
- 移位安装包启动冒烟通过：0.1.30，`今日来信 · 知君`，`zhijun://desktop/desktop.html#/`。
- DMG SHA256：`aaee8311c856ce45a3abdefbcb2552af4935e4998dc303d8e1814ec535d282f6`。
- ZIP SHA256：`6ff79e17d1836b09dc384a21d501d4f8eaf2cb1a8ccc940d1c1db37f81917af9`。
- 无盒端更新、无 Wi-Fi 配置提交、无设备认领操作。

额外检查记录：尝试将 Windows 通用 lockfile 验包器用于 macOS ASAR 时，发现既有 `@noble/hashes` 打包布局差异（macOS 发布基线 1.8.0，根 lock 条目 2.4.0，旧 SDK 的嵌套 lock 为 1.8.0）。标准 macOS 验包器明确固定 1.8.0 并通过。本次不更换加密依赖，也不把这个额外检查记为通过；Windows 实机打包仍需按其验包流程核验依赖布局。

参考：[Electron 官方设备访问示例](https://www.electronjs.org/docs/latest/tutorial/devices)。
