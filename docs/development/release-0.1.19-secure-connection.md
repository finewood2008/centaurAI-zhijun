# 知君 0.1.19 安全连接体验发布记录

日期：2026-09-13。

## 范围与发布流程

- 用户授权：重新生成正式安装包。目标为 macOS Apple Silicon，DMG / ZIP 输出到 `frontend/shell/release/`，不生成 test 包。
- 本版包含安全连接进度卡、真实连接阶段、等待提示、取消连接、已就绪连接的加密标识，以及此前已完成的知君检索更新。
- 不更改打洞预算、连接协议、资料授权或模型路由；不宣称外网耗时已经缩短。
- 主代理执行现有 `npm run package:mac-arm64`，使用缓存 Node.js 22.23.2；版本由脚本从 0.1.18 升至 0.1.19。
- 独立复核负责合成安全连接浏览器测试、Git 忽略规则及最终包内资源；不使用真实账号或客户资料。
- 旧 DMG/ZIP 保留，构建暂存目录与 `release/mac-arm64` 按正常流水线更新；不覆盖已安装应用，不更新盒端，不发布 OTA。

## 验证记录

- Shell：281 项通过，1 项仅适用于 Windows PowerShell 的测试在 macOS 跳过，0 失败。
- 桌面 UI：46/46 通过。
- Electron E2E：4/4 通过。
- 桌面页面类型检查、生产构建通过；保留已有 `taskRouting` 动静态混合导入警告。
- 首次发布前检查命中旧 Electron 测试定位：新界面不再在连接卡突出账号/设备原始 ID。测试改为验证友好设备名、模拟提示、工作区能力限制及实际 snapshot 设备 ID；保留原有权限与切换隔离断言后完整重跑通过。

## 交付边界

沿用现有 Developer ID 签名配置及 Hardened Runtime；`notarize: false`，未进行 Apple 公证。安装包验证和合成连接测试不代表真实账号、外网打洞或盒端模型调用已经完成现场验收。

## 正式产物与验包结果

完整发布流程已通过，产物位于 `frontend/shell/release/`：

| 产物 | SHA256 |
|---|---|
| `Zhijun-0.1.19-mac-arm64.dmg` | `14de6526a0a04518f34aa8674ade2dfd4a4faecde8ad4962fb5badb11a48149f` |
| `Zhijun-0.1.19-mac-arm64.zip` | `1fd495fc4dad03ae3262bca9193259c2330a9df546fefb02e20a49f5ee8622ae` |

同目录 `Zhijun-0.1.19-mac-arm64.SHA256SUMS` 保存上述校验和。

- 正式 flavor、Bundle ID `com.qeeshu.zhijun`、版本 0.1.19 核对通过。
- 独立复核：最终包内前端目录与重新生成的 `dist-desktop` 共 77 个文件逐一同名、同 SHA256，无缺失、额外或变化文件；安全连接界面与取消/安全说明标记均存在。最终构建的合成安全连接浏览器回归通过。
- 原始应用、只读挂载 DMG 内应用、ZIP 解包后应用均通过签名、资源目录、依赖白名单、sidecar 摘要及架构检查。
- 签名 sidecar SHA256：`888482170a8eb9978bdc855345a1a7329013ee3c02f3c08c325075c039a8ba0c`；Direct 预算保持 8000ms。
- 应用搬移到临时目录、使用隔离用户配置启动通过，页面为 `zhijun://desktop/desktop.html#/`，标题为“今日来信 · 知君”。没有替换用户已安装应用。
- `release/`、`package-resources/`、`dist-desktop/` 均由现有 `.gitignore` 排除；不提交安装包、构建资源或临时数据。
