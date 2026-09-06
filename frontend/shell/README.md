# 知君独立桌面宿主

当前桌面已接入完整产品页面和受控业务传输。页面由 `zhijun://desktop/desktop.html` 加载，不启动本机 Python/Vite，也不依赖 PC 本机 8618 服务。Consumer账号登录、SDK连接和业务授权均由主进程管理；只有真实v2工作区校验成功后才显示“今日来信、对话、我的本体、判断、资料与边界、偏好”等原产品入口。仅登录成功、SDK连接成功或v1只读授权成功都不足以打开完整工作区。

在仓库根安装依赖并启动：

```sh
rtk proxy npm --prefix frontend/mindos-web ci
rtk proxy npm --prefix frontend/shell ci
rtk proxy bash start-desktop.sh --simulation
```

`--simulation` 显式启用合成账号与两个盒子的连接演示，页面持续标注模拟环境。合成适配器不提供v2工作区，因此不会挂载完整产品；47条合成资料仅用于旧只读接口测试。未设置配置环境变量时，运行 `rtk proxy bash start-desktop.sh` 进入未配置状态，登录返回配置未就绪。启动器每次先构建独立页面；已构建后可在本目录执行 `rtk proxy npm start` 或 `rtk proxy npm run start:simulation`。

开发宿主固定 Electron 37.10.3，已验证 macOS ARM64；尚未交付签名安装包或其他平台运行验收。打包环境禁止通过环境变量开启 simulation。旧 `ZHIJUN_BASE_URL` 不再使用，不能用它连接本机业务服务。

验证（前端依赖已安装，且先构建桌面页面）：

```sh
rtk proxy npm --prefix frontend/mindos-web run build:desktop
rtk proxy npm --prefix frontend/shell test
rtk proxy npm --prefix frontend/mindos-web run test:desktop
rtk proxy npm --prefix frontend/shell run test:e2e
```

E2E启动隔离的Electron测试实例，使用新建临时userData与未配置/合成状态，关闭后清理。无需真实SDK连接、账号或用户资料，也不会请求真实麦克风权限。`ZHIJUN_DESKTOP_USER_DATA` 是主进程测试隔离路径；日常开发默认使用独立的 `zhijun-desktop` 应用配置目录。模拟身份/资料只在内存中，renderer使用非持久session分区。自动化通过不能代替模型推理、真实文件或账号隔离验收。

当前集成范围见[完整产品执行计划](../../docs/development/FULL-PRODUCT-INTEGRATION-0906.md)，逐项结果见[完整产品验收清单](../../docs/development/FULL-PRODUCT-ACCEPTANCE-0906.md)。旧 `frontend/main.js` 与 `renderer/` 保留为历史代码，不在当前启动链。

正式账号入口：在仓库根执行：

```sh
rtk proxy bash start-desktop.sh --real
```

没有显式配置时，启动器调用 `scripts/prepare-real.cjs`，准备固定哈希sidecar并生成 `data/desktop/zhijun-product-v2.json`（新建权限0600），然后加载该文件。新配置使用 `applicationId=zhijun-desktop`、`purpose=zhijun.workspace`、`remote.p2p`及DirectOnly。配置文件的结构版本仍是 `version: 1`；文件名中的v2指工作区业务协议，不应手动把配置结构版本改成2。

准备脚本不会覆盖任何内容不同的已有配置或sidecar；内容相同则复用。旧 `data/desktop/zhijun-product.json` 保留不动。需要旧v1连接或自定义配置时，显式传入绝对路径：

```sh
rtk proxy env ZHIJUN_DESKTOP_CONFIG="$PWD/data/desktop/zhijun-product.json" bash start-desktop.sh --real
```

设置 `ZHIJUN_DESKTOP_CONFIG` 后，启动器只校验指定配置并直接加载，不执行自动准备或改写文件；v1 `mindos-person-data-pc` / `person-data.read` 仍受支持，但不会获得完整工作区能力。若默认v2文件已有不同内容，自动准备报 `EXISTING_OUTPUT_DIFFERS`，可显式加载经确认有效的自定义配置；不要通过删除或覆盖旧文件绕过检查。示例 `config/zhijun-product.example.json` 只含参考Consumer地址，配置后由用户在应用内登录；密码/token不得写入配置或Git。

新应用的生产登记与完整链路真机验收当前仍待完成。生成v2文件或打开登录页不代表Admin已登记 `zhijun-desktop` / `zhijun.workspace`，也不代表Agent/DE已部署对应能力；实际状态以本轮部署与验收记录为准。旧v1部署和[只读真机结果](../../docs/development/REAL-ACCEPTANCE-0906.md)不能自动继承为v2通过。

v2通过同一SDK session请求 `GET /api/mindos/zhijun/context`，核对账号、client、设备、应用、workspaceId及短期有效期；空闲时每10秒复核，连接失效即关闭产品能力。170个受控operation覆盖JSON、聊天流、分片上传、原生保存和媒体预览；renderer不提供任意URL/身份头或可复用业务token。工作区按设备、账号和ownershipEpoch隔离，临时任务与文件另绑定client/session。

语音输入仅在用户点击录音后申请麦克风，停止后由盒端转写并填入草稿，不自动发送。权限窗口限当前主页面的audio请求；相机及其他frame/origin拒绝，断开或切换主体会释放授权和录音资源。mac用途说明已配置，签名发行包与真实权限/转写结果仍需独立验收；自动化不探测或录制真实麦克风。

盒端部署输入可由 [prepare-bridge-release.cjs](scripts/prepare-bridge-release.cjs) 生成。生成输入不执行部署，也不证明完整产品能力已经在盒端运行；最终版本与逐项验收必须记录在本轮文档中。
