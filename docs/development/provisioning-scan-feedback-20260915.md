# 配网扫描错误与实时结果

## 范围、计划和验收

用户报告 0.1.24 扫描只显示笼统错误，要求连续看到扫描结果。
本次修复知君配网窗口，不修改 Admin 或盒端、不放宽 GATT/证书/设备归属验证。

- preload 工作流：原生 requestDevice 异常及时反馈，扫描候选增量快照，取消/到期清理，旧会话隔离。
- renderer 工作流：实时列表和数量、扫描/到期/空结果提示、取消扫描、过期候选禁用，保留准确安全错误码。
- 校验：原生扫描拒绝、空结果、新增/移除候选、自动到期、取消后重扫、旧事件和跨隔离错误消息。
- 文件：`provisioning/preload-formal.mjs`、`provisioning/renderer.js`、必要页面资源与对应测试。
- 不凭截图推断蓝牙权限或盒端故障；真机扫描原因需真实错误证据，不伪造发现结果。

## 已确定的代码问题

1. scan 吞掉 requestDevice rejection，仅等待自己的定时器；失败原因不能及时显示。
2. scan 只返回第一批候选，正式 renderer 未订阅后续候选；候选到期也未同步禁用。
3. renderer 仅从异常 code/name 提取固定错误码；隔离桥仅保留 message 时会退回泛化提示。

## 进度

- [x] 代码定位及并行文件归属确定。
- [x] 修复并运行针对性与配网回归。
- [x] 记录结果及真机验证边界。

## 实现与验收

- 正式 preload 新增 `onDiscoverySnapshot`、`cancelScan`；只暴露不透明候选 ID 和经过清理的显示字段。
- 原生拒绝及时返回固定安全码，不再吞掉异常等待 8 秒。跨 contextBridge 仅保留 message 时，
  renderer 只接受与白名单完全一致的错误码，拒绝展示任意原始错误文本。
- 正式窗口实时更新候选新增/移除和数量；本轮结束后保留结果但禁用选择，支持重新扫描。
- 取消/超时前原生请求未真正结束时保留请求锁，避免并发 requestDevice；迟到 session 取消并退休，
  迟到选择租约撤销，不能影响新一轮。全部流程保留用户点击要求，不在后台自动无限重扫。
- 底层 SDK 每轮扫描上限为 8 秒。本次未更改 SDK、安全租约或盒端配网窗口。
- 新增和既有针对性测试共 28 项通过，含真实 vendored picker/selection endpoint 的离线联调。
- 完整 shell 测试 328 通过、1 项平台测试跳过；桌面测试 52 通过；Electron E2E 4 通过；桌面构建通过。

## 边界

截图只显示泛化结果，无法据此确认现场是否存在蓝牙权限、硬件不可用或盒端未广播。
本次修复已证明的错误传播和扫描状态缺陷，不声称真实蓝牙扫描、新设备配网和认领已成功。
没有修改 Admin、现有盒子或配网凭证，没有替换用户当前安装的应用。

## 正式安装包

- 版本：`0.1.25`，macOS arm64，production；保留既有安装包。
- DMG：`frontend/shell/release/Zhijun-0.1.25-mac-arm64.dmg`
  - SHA256：`66a5ea1b203b75e8c074ead226a90f583eee2d0e11c630243c672c4f4a78c465`
- ZIP：`frontend/shell/release/Zhijun-0.1.25-mac-arm64.zip`
  - SHA256：`147c2d54b84a0b99014376607d8f469b02ef58764684e48418be946e96554e7c`
- 正式验包和迁移目录启动 smoke 均通过；包内配网 preload、renderer、页面与本次构建一致。
- 包内发现与 BLE 配网开关均开启，信任根与生产发布输入一致。
- 使用 Developer ID 签名；本次未进行 Apple notarization。
