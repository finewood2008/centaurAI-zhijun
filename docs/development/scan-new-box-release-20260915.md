# 登录后扫描新盒子：发布实施

## 授权范围

用户要求继续开启登录后扫描新设备功能。本次目标为 Admin 两个 Electron BLE 能力开关和
知君正式安装包配网开关；账号已拥有其他盒子不应隐藏新设备入口。
不重置/重新认领现有公司和家里盒子，不生成制造密钥或逐台交付凭证，不修改其 Wi-Fi。
新设备真实扫描/配网验收须有具备正式交付凭证且未认领的现场测试盒。

## 计划与验收

- [x] 核对当前前端入口条件与生产信任配置；载入生产根验证成功（rootCount=1）。
- [x] 只读核对线上 Admin 进程、功能开关、协议与配置启动入口。
- [x] 原计划条件变更无需执行：线上实际进程和 `.env` 已开启两个 Electron gate，无需备份/修改/重启。
- [x] 生成开启配网的正式 macOS arm64 包并验签、验内容、独立启动。
- [x] 记录客户端可用范围与新盒真机验收待办。

## 验证边界

入口显示依赖包内 capability；打开配网窗口还会核对 Admin bootstrap 的账号/客户端和两项能力。
开启 capability 不是绕过设备证书、签名交付凭证或所有权检查。
没有新的未认领测试盒时不得声称已完成真实 BLE 配网与认领。

## 实际结果

- 2026-09-15 约 16:00 CST，Admin `8.138.1.109:9099` 实际 PID `2779197`，
  启动目录 `/data/apps/nexusaos_admin_backend-0.1`，原 argv 使用 `--env-file .env`。
- 进程环境中 `NEXUSAOS_CONSUMER_PAIRING_V2_ENABLED`、
  `NEXUSAOS_ELECTRON_WEB_BLUETOOTH_DISCOVERY_V1_ENABLED`、
  `NEXUSAOS_ELECTRON_BLE_PROVISIONING_V2_ENABLED` 均为 `true`，启动 `.env` 同样包含三项。
  独立 `admin-remoteops.env` 只有总开关，不代表实际进程两项关闭。
- `/openapi.json` 200；无身份 bootstrap 401，认证仍生效。本次未登录生产账号调用真实签名 bootstrap。
- 本次仅只读访问 Admin，未修改服务、数据库或绑定，SSH 已退出。

## 安装包

正式 macOS Apple Silicon `0.1.24`，按完整 `package:mac-arm64` 流程构建，保留原安装包，
没有覆盖 `/Applications/知君.app`。构建时显式传入生产配网 gate 和既有 trust config。

- `frontend/shell/release/Zhijun-0.1.24-mac-arm64.dmg`
  SHA-256 `145ed86968b16ba54987031808222f3b8aba82a8a4bbd7fc97eb483fe84c1c07`
- `frontend/shell/release/Zhijun-0.1.24-mac-arm64.zip`
  SHA-256 `f9fea87d3442e2d73f7d72f0352ef61277a9551aa9e1bb52c7f5e694f41c78a9`
- Developer ID 签名通过；`notarize:false`，未做 Apple 公证。
- 完整构建测试、包内容/签名验证、搬迁后的独立启动 smoke 通过。
- 成品 `Contents/Resources/zhijun-product.json` 专项断言：两个 gate=true、
  contractVersion=2.0.0、rootCount=1，根证书和 SPKI pin 与指定生产配置逐项相等。
- 新增 runtime 回归两项通过：已拥有两台设备仍可 openProvisioning；已连接设备断开后仍可新增。
  仅新增测试，不需再次构建或升版；通用验包允许关闭配网配置，因此本次专门检查成品开关。

## 用户验收

安装 0.1.24，登录后在设备选择页使用“扫描附近盒子”；若当前已连接，先回到设备选择。
当前账号已有设备不构成新增入口限制。扫描新盒仍需其 BLE 服务、设备证书及交付凭证已准备好。
本次没有新盒设备目标和真实 BLE 交付验收，不声称扫描到设备、Wi-Fi 下发或首次认领已成功。
