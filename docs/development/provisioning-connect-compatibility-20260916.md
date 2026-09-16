# 选择盒子后连接失败：兼容性诊断与修复

## 已确认的现场证据

家里盒子 `192.168.1.18` / `CentaurOS-Setup-E2334B` 的运行服务脚本仍为 V1：
`DeviceInfoCharacteristic.ReadValue` 调用 legacy `backend.device_info()`，CLI 输出
`schema_version:1`；没有 V2 backend。服务 UUID 与 V2 相同，因此发现成功不代表协议兼容。
当前知君正式配网使用 V2 SDK，设备信息必须通过 V2 严格校验；不能用 V1 明文降级绕过。

## 计划、范围与验收

1. [x] broker 识别 V1 设备信息，仅返回安全的“不兼容”错误；其余信息继续由真实 SDK 严格校验。
2. [x] renderer 补全连接/协议错误提示，失败后不再显示“正在建立连接”。终止流程提示关闭窗口重开，避免重复无效尝试。
3. [x] 使用真实 coordinator + 模拟 GATT I/O 回归：合法 V2 到 previewReady，V1/非法数据拒绝，错误码保留，失败关闭 transport；不调用云授权、Wi-Fi 或认领。
4. [x] shell 全量回归。当前不重新发包、不部署盒端、不提交配网或归属变更。

影响文件：`frontend/shell/provisioning-broker.cjs`、`provisioning/renderer.js`、对应 broker/renderer 测试。
依赖：现有 local-provisioning-core V2 SDK；不改 node_modules，不放宽证书/协议校验。

验证：broker/renderer 25 项通过；shell 358 项，357 通过、1 项 Windows 原生 ACL 测试在 macOS 跳过。
只读复核补充：SDK 的 terminalError/restartRequired/cancelled 不可重复 begin，页面现引导关闭重开。
纯扫描取消仍可重扫。未执行真实 V2 配网/物理确认/云认领，模拟 I/O 测试不能代替真机验收。

## 盒端后续边界

CentaurOS 的正式 V2 首次交付依赖设备证书、独立制造信任、签名交付凭证、实体六位确认码及有限配网窗口。
不能只替换脚本后就宣布配网可用。现有“添加盒子”是首次交付/认领流程；已绑定设备的重新配网
不是重新签发首次交付凭证，更不能清理 Owner 或窗口预算伪装新设备。应先确认本次是在验证新机交付，
还是为已绑定的家里盒子更换 Wi-Fi，再确定部署和授权路径。
