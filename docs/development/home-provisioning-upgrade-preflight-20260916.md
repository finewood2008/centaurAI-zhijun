# 家里盒子配网升级预检

## 请求与计划

用户要求升级家里盒子程序，以解决选择设备后不能连接的问题。
范围为 `192.168.1.18` 的 CentaurOS 配网组件，不包含重置身份、Owner、网络、业务数据或云端授权。

计划：只读核验目标及事务 → 核对版本和配网前置条件 → 条件满足时按本机基线制作签名升级 →
Manager check/apply → 服务、摘要和真实配网验收。任何前置条件不满足，停止在 apply 之前。
验收要求：保留现有绑定和数据，服务能启动，桌面能读取合法 V2 设备信息；不把安装文件视为配网成功。
预计影响文件为 CentaurOS provisioning CLI、BLE GATT helper 及固定 service；依赖本机 profile、
已信任发行公钥及正式 V2 授权配置。不套用公司盒试点发行。

## 只读结果

- 主机：`user-M900`，家里盒子 `CentaurOS-Setup-E2334B`。
- 核心版本标记：`CentaurOS 1.2.3-pilot.20260914.home2`，不是所有组件版本。
- Manager：`/usr/lib/centauros/centauros-manager`；不在普通 PATH，不代表未安装。
- 最近 Manager 事务：`86a57ac1ed024704a055de0931c24436`，`complete`，
  release `home-manager-lifecycle-20260914-2`，sequence `2026091402`。
- BLE provisioning active，PID `1737680`；SPP active，PID `2732`；Remote Agent active，PID `230068`。
- root 权限确认下列项目不存在（仅查存在性，没有输出凭据正文）：
  - `/var/lib/centauros/device/device-cert.pem`
  - `/var/lib/centauros/provisioning/consumer-handoff.json`
  - `/var/lib/centauros/provisioning/verification-code`
  - `/usr/share/centauros/manufacturing-handoff-keys`

## 阻塞与处置

旧 BLE 提供 V1 device-info，桌面正式配网要求 V2；最新版 V2 服务启动前要求上述正式交付配置。
Manager 对已运行的蓝牙服务保持启停意图并执行运行验收，缺配置直接更新将启动失败并回滚。
不能关闭验收或伪造新机凭证来完成升级。

CentaurOS 现有 V2 仅支持首次交付的 claim / wifi.apply+claim；没有已绑定 Owner 单独重配 Wi-Fi 的
正式授权流程。consumer-v2-migration 迁移现有授权快照，不负责开放重新配网窗口。

因此本轮停在只读预检：没有制作/应用升级包，没有停服务，没有修改盒端文件、Wi-Fi、Owner 或数据。
需要另行确认并实现已绑定盒子安全重配网络能力，或明确仅停用旧配网、安装新版待启用的有限维护目标。
后者并不能解决当前实际配网需求。
