# 生产配网信任配置核查（2026-09-15）

## 已配置及验证

复用现有生产 RemoteOps CA，没有生成、替换或导出 CA 私钥。

- 现有根配置：`/Users/longxiping/.config/nexusaos/release-inputs/provisioning/provisioning-roots.json`，权限 0600。
- 新增构建环境文件：同目录 `zhijun-release.env`，权限 0600。
- 其中设置 `ZHIJUN_PROVISIONING_V2_ENABLED=1` 和根配置的绝对路径；需要在构建进程中显式加载，不修改全局 shell 环境。
- 根 subject/issuer：`CN=NexusAOS RemoteOps Device CA`。
- 根有效期：2026-07-16 至 2036-07-13（UTC）。
- Root SPKI pin：`sha256/xqf8UJhnp5QLg4VeBEn0SxAobscgcIJK2DUU8UWN3Y8=`。
- 根证书 SHA-256 指纹：`3E:9D:6C:C0:D9:1F:1C:22:15:55:C3:A0:EC:CE:05:B4:3C:A2:95:31:81:3B:69:2C:76:7C:9A:65:51:4F:CB:84`。
- 与线上 Admin 当前配置的 `/data/secrets/nexusaos-remoteops/admin/device-ca-cert.pem` 指纹完全一致。
- 实际调用 `loadProvisioningReleaseConfig()` 验证通过，返回两个 Electron gate=true、rootCount=1。

构建命令（项目根目录；须先满足下方真机前置条件）：

```sh
rtk proxy bash -c '
source /Users/longxiping/.config/nexusaos/release-inputs/provisioning/zhijun-release.env
npm --prefix frontend/shell run package:mac-arm64
'
```

本次没有运行打包，没有修改已安装应用，没有开启线上 Admin 配网门禁。
该环境文件不会被普通、不带上述环境的打包命令自动读取；原流程仍默认关闭配网。

## 实测上线阻断

Admin `8.138.1.109` 当前进程 PID 2769108：

- `NEXUSAOS_CONSUMER_PAIRING_V2_ENABLED=true`。
- `NEXUSAOS_ELECTRON_WEB_BLUETOOTH_DISCOVERY_V1_ENABLED` 未设置。
- `NEXUSAOS_ELECTRON_BLE_PROVISIONING_V2_ENABLED` 未设置。

公司盒子 `192.168.0.7`：

- `centauros-provisioning.service` inactive/disabled；现有 SPP active/enabled。
- `centauros-cloud-agent.timer` inactive/disabled。
- 现有 Remote Agent leaf `/var/lib/centauros/remote-agent/client-cert.pem` 证书链校验通过；
  SAN 为公司盒子 stable ID，具有 CA=false、Digital Signature/Key Encipherment、clientAuth，
  有效期至 2026-10-11。此检查不代替完整 Attestation/Pairing 事务验收。
- 现场 GATT 代码读取 `/var/lib/centauros/device/device-cert.pem`，该文件不存在。
- `/var/lib/centauros/provisioning/consumer-handoff.json` 不存在。
- 未生成或改写制造交付凭证、设备身份、归属、Wi-Fi、证书文件和服务状态。

家里 `192.168.1.18` 本轮 SSH 超时，不能用公司检查结果代替家盒验收。

## 下一步

先确定正式交付设备批次，按受控制造流程补齐匹配设备公钥的证书部署、签名 handoff、
制造信任公钥及实体六位确认码。不能将已有远程访问证书直接视为制造交付授权，不能伪造 handoff。
随后验证 Registry/Attestation、Admin BLE 功能门禁和盒端 GATT/Cloud Agent 配套，再用正式新包
执行真实扫描、设备身份校验、加密 Wi-Fi 配置和归属授权验收。

证书配置完成不代表产品已满足正式配网上线条件。
