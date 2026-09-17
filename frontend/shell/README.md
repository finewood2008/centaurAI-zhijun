# 知君独立桌面宿主

当前桌面已接入完整产品页面和受控业务传输。页面由 `zhijun://desktop/desktop.html` 加载，不启动本机 Python/Vite，也不依赖 PC 本机 8618 服务。Consumer账号登录、SDK连接和业务授权均由主进程管理；只有真实v2工作区校验成功后才显示“今日来信、对话、我的本体、判断、资料与边界、偏好”等原产品入口。仅登录成功、SDK连接成功或v1只读授权成功都不足以打开完整工作区。

在仓库根安装依赖并启动：

```sh
rtk proxy npm --prefix frontend/mindos-web ci
rtk proxy npm --prefix frontend/shell ci
rtk proxy bash start-desktop.sh --simulation
```

`--simulation` 显式启用合成账号与两个盒子的连接演示，页面持续标注模拟环境。合成适配器不提供v2工作区，因此不会挂载完整产品；47条合成资料仅用于旧只读接口测试。未设置配置环境变量时，运行 `rtk proxy bash start-desktop.sh` 进入未配置状态，登录返回配置未就绪。启动器每次先构建独立页面；已构建后可在本目录执行 `rtk proxy npm start` 或 `rtk proxy npm run start:simulation`。

开发宿主固定 Electron 37.10.3，macOS ARM64 打包流水线包含 Developer ID 签名、DMG/ZIP 校验和搬迁启动冒烟测试；是否完成 Apple 公证以当前构建配置和发行记录为准。打包环境禁止通过环境变量开启 simulation。旧 `ZHIJUN_BASE_URL` 不再使用，不能用它连接本机业务服务。

Windows x64 一键打包使用 `npm run package:win-x64`，必须在 Windows x64 / Node.js 22 环境配置代码签名；显式内部测试使用 `npm run package:win-x64:unsigned`。流程包括测试、资源准备、NSIS/ZIP 构建、内容与签名校验、搬迁启动冒烟。原生连接程序、签名配置和人工安装验收要求见 [Windows 发布指南](../../docs/development/windows-release.md)。脚本完成不代表已经通过 Windows 真机安装或盒子联调。

验证（前端依赖已安装，且先构建桌面页面）：

```sh
rtk proxy npm --prefix frontend/mindos-web run build:desktop
rtk proxy npm --prefix frontend/shell test
rtk proxy npm --prefix frontend/mindos-web run test:desktop
rtk proxy npm --prefix frontend/shell run test:e2e
```

E2E启动隔离的Electron测试实例，使用新建临时userData与未配置/合成状态，关闭后清理。无需真实SDK连接、账号或用户资料，也不会请求真实麦克风权限。`ZHIJUN_DESKTOP_USER_DATA` 是主进程测试隔离路径；日常开发默认使用独立的 `zhijun-desktop` 应用配置目录。模拟身份/资料只在内存中，renderer使用非持久session分区。自动化通过不能代替模型推理、真实文件或账号隔离验收。

当前产品、架构和运行边界统一从[文档索引](../../docs/README.md)进入。旧 `frontend/main.js` 与 `renderer/` 保留为历史代码，不在当前启动链。

正式账号入口：在仓库根执行：

```sh
rtk proxy bash start-desktop.sh --real
```

登录成功并处于选盒页面时，“扫描附近盒子”会打开独立的本地配网窗口。扫描必须由用户点击触发，候选设备也必须手动选择；正式 v2 流程展示从设备 ID 派生的固定短码，由用户核对实体并显式确认，再校验 RSA 设备证书，通过 AES-256-GCM 加密通道发送用户手工输入的 Wi-Fi 参数。P0 不远程扫描 Wi-Fi。蓝牙临时 ID 不会写成业务设备 ID，Wi-Fi 密码也不会进入主产品页面、主 IPC 或持久存储。关闭窗口、退出登录或切换身份时会断开本次配网会话。

正式包只允许 `NEXUSAOS_LOCAL_AEAD_V2`。未配置发行信任根时功能会保持关闭；不会自动降级到 v1。受控开发环境需要联调旧固件时，可显式启用一次开发兼容入口：

```sh
rtk proxy env ZHIJUN_ALLOW_LEGACY_PLAINTEXT_PROVISIONING=1 bash start-desktop.sh --real
```

该开关在安装包内无效；开发窗口中仍需先勾选风险提示，并在提交时再次确认。它不会把旧协议变成加密协议，不能用于正式交付。

构建已开启安全配网的正式 macOS 包时，使用一个仓库外的绝对路径 JSON 文件提供生产 CA 根证书和对应 SPKI pin。文件必须不可被 group/world 写入，且只允许 `trustedRootSpkiPins` 和 `trustedRootCertificatesPem` 两个字段：

```sh
rtk proxy env \
  ZHIJUN_PROVISIONING_V2_ENABLED=1 \
  ZHIJUN_PROVISIONING_TRUST_CONFIG=/absolute/path/production-provisioning-roots.json \
  npm --prefix frontend/shell run package:mac-arm64
```

如不显式设置这两项，准备脚本会生成两个 Gate 均为 `false` 的失败关闭配置。正式发布还必须先执行 Admin v2 SQL migration、打开 `NEXUSAOS_CONSUMER_PAIRING_V2_ENABLED=true`，并为盒子换发匹配该根的设备证书。

需要在可控盒子和测试网络上验收旧固件时，可构建隔离的 macOS ARM64 测试包：

```sh
rtk proxy npm --prefix frontend/shell run package:test:mac-arm64
```

测试版使用独立的 Bundle ID `com.qeeshu.zhijun.provisioning-test`、应用名“知君配网测试版”、用户数据目录和 `release-test/` 产物目录，不会通过环境变量改变正式包。它会始终显示测试警告，但仍须在配网窗口勾选旧固件兼容选项并二次确认后才发送 Wi-Fi 密码。测试版发送的是旧固件明文 GATT 指令，只能用于受控验收；验证结束后应卸载，不能对外分发或替代正式安全配网包。

没有显式配置时，启动器调用 `scripts/prepare-real.cjs`，准备固定哈希sidecar并生成 `data/desktop/zhijun-product-v2.json`（新建权限0600），然后加载该文件。新配置使用 `applicationId=zhijun-desktop`、`purpose=zhijun.workspace`、`remote.p2p`及DirectOnly。配置文件的结构版本仍是 `version: 1`；文件名中的v2指工作区业务协议，不应手动把配置结构版本改成2。

准备脚本不会覆盖任何内容不同的已有配置或sidecar；内容相同则复用。旧 `data/desktop/zhijun-product.json` 保留不动。需要旧v1连接或自定义配置时，显式传入绝对路径：

```sh
rtk proxy env ZHIJUN_DESKTOP_CONFIG="$PWD/data/desktop/zhijun-product.json" bash start-desktop.sh --real
```

设置 `ZHIJUN_DESKTOP_CONFIG` 后，启动器只校验指定配置并直接加载，不执行自动准备或改写文件；v1 `mindos-person-data-pc` / `person-data.read` 仍受支持，但不会获得完整工作区能力。若默认v2文件已有不同内容，自动准备报 `EXISTING_OUTPUT_DIFFERS`，可显式加载经确认有效的自定义配置；不要通过删除或覆盖旧文件绕过检查。示例 `config/zhijun-product.example.json` 只含参考Consumer地址，配置后由用户在应用内登录；密码/token不得写入配置或Git。

生成配置文件或打开登录页不代表 Admin、Agent、Data Engine 和盒端能力已经匹配部署；必须由当前环境的身份、workspace、能力校验和业务回执确认。旧 v1 只读结果不能自动继承为 v2 通过。

v2通过同一SDK session请求 `GET /api/mindos/zhijun/context`，核对账号、client、设备、应用、workspaceId及短期有效期；空闲时每10秒复核，连接失效即关闭产品能力。170个受控operation覆盖JSON、聊天流、分片上传、原生保存和媒体预览；renderer不提供任意URL/身份头或可复用业务token。工作区按设备、账号和ownershipEpoch隔离，临时任务与文件另绑定client/session。

语音输入仅在用户点击录音后申请麦克风，停止后由盒端转写并填入草稿，不自动发送。权限窗口限当前主页面的audio请求；相机及其他frame/origin拒绝，断开或切换主体会释放授权和录音资源。mac用途说明已配置，签名发行包与真实权限/转写结果仍需独立验收；自动化不探测或录制真实麦克风。

`prepare-bridge-release.cjs` 仅保留旧 v1 bridge 的固定基线兼容能力，不是当前 CentaurOS、`zhijun.workspace` 或 NPU 运行时的部署入口。它生成本地审阅包，不执行部署。

## 设备认领码

设备认领对接 Device Console Claim：新码严格为 `[A-Z2-7]{10}`，兼容旧 `[A-Z2-7]{20}`；不接受 6 位数字、空格、小写或分隔符，也不自动修正输入。账号短信验证码是独立的账号验证流程；蓝牙配网使用固定设备短码供用户核对，不再生成或输入六位 BLE 确认码。

客户端向 `POST /app-api/device-console-claims/redeem` 提交 `{claimCode, clientAttemptId}`，后者为 UUIDv4，并与 `Idempotency-Key` 一致；使用 `NEXUSAOS-CONSUMER-V1` 签名，不再调用旧 `/device-claims/redeem`。网络结果不确定时沿用本次幂等键，不记录或显示完整认领码。

认领成功只表示提交归属，不等于可连接。界面提示等待盒子授权，用户可点击“刷新设备”；以 `GET /app-api/v1/sync/bootstrap` 返回的同一设备同时满足 `ownershipStatus=active`、`accessStatus=ready`、`securityStatus=normal`、`capabilities.canConnect.enabled=true` 为准。Bootstrap 使用 `NEXUSAOS-CONSUMER-APP-V1` 签名。状态缺失或服务不可用时不绕过校验、不自动重复认领，也不启用服务端功能开关。

发行前须确认目标 Admin 支持新认领接口和 Bootstrap，并已完成 Console Claim / Consumer Pairing V2 的数据库迁移、功能门禁及盒端最新授权 ACK 验收。新版设备列表依赖 Bootstrap；门禁关闭或服务端仍是旧版本时不会退回旧列表。已有设备不需要重新认领，升级不修改其 Owner、认领历史或业务数据。本地测试不代表服务端门禁已开启或真实贴纸兑换已验收。

## 应用图标

开发启动会在 macOS Dock 使用原有半人马图像；Windows/Linux 窗口和打包配置也指向同源资产。原图为 `../mindos-web/logo.jpg`。运行 `npm run icons:build` 会通过仓库内的 Swift/CoreGraphics 脚本去除原图近白背景，在暖白圆角底板外保留真实透明留白，再用 `sips` / `iconutil` 重建 `assets/centaur.png` 和 `assets/centaur.icns`。`assets/centaur-source.json` 记录源图、渲染脚本、构建脚本、布局参数和产物哈希，重复构建应得到相同结果。正式安装包仍需在对应平台构建核验，设置打包配置不代表安装包已经产出。

账号已登录时保留主导航；未连接的业务内容区显示选盒或错误提示。应用授权拒绝和账号服务故障会单独提示，不应据此认定盒子离线。

完成 Desktop 构建后，在本目录执行 `node --test tests/product-navigation.e2e.cjs` 可验证真实 Electron main/preload/自定义协议在合成失败账号下的首次挂载、导航及 Reload；不访问真实账号、SDK 或麦克风。真实账号能力必须在当前部署单独验收。
