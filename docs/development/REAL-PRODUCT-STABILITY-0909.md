# 知君真实产品链稳定性验收

`frontend/shell/scripts/run-product-stability.cjs` 从 Electron 主进程复用正式 Consumer 客户端、系统安全存储、Connectivity SDK sidecar、Admin 票据、盒端工作区授权和产品 operation catalog。它不接受任意 HTTP 地址，也不会绕过 `zhijun-desktop` 的应用授权。

## 前提

1. 先关闭正在运行的知君桌面。验收工具和桌面使用同一个 `userData`，工具通过 Electron 单实例锁避免两个进程同时刷新加密会话。
2. 桌面此前至少成功登录一次。默认只恢复现有加密会话；会话不存在时，只有显式使用 `--allow-saved-login` 才会读取系统安全存储中的已保存密码重新登录。manifest、命令行和验收收据都不得包含账号、密码、token 或模型密钥。
3. 盒子已安装 `/usr/lib/centauros/centauros-resource-evidence --json`，Mac 已配置 SSH key 和可信 `known_hosts`。工具以 `-F /dev/null` 忽略可能重写裸 IP 的用户 SSH 配置，并固定以 `BatchMode=yes` 调用该命令；不支持命令行密码或 manifest 自定义远端命令。
4. 使用正式 `zhijun-product-v2.json` 和匹配 SHA-256 的 sidecar。SDK 始终使用 `SOVEREIGN_DIRECT_ONLY` / `DIRECT_ONLY`。

## Manifest

复制 `frontend/shell/config/product-stability.example.json` 到仓库外或被忽略的 `data/` 目录。manifest 必须是不可组写、不可全局写的普通文件，且只允许以下非秘密字段：

- `deviceId`：Admin 账号下明确选择的设备 ID。工具只连接该 ID，离线或无权限时直接失败，不会自动选择其他盒子。
- `host`、`sshUser`、`sshPort`：同一盒子的资源证据 SSH 目标。
- `topology`：`systemd` 或 `hybrid`。`systemd` 要求 Ollama、Data Engine、Remote Agent 三个 unit 的证据；`hybrid` 要求 Ollama、Remote Agent 和 `requiredContainers`。
- `expectedOllamaBackend`：本次验收要求的实际执行后端，只允许 `cpu_avx2`、`cpu`、`rocm`、`vulkan`、`cuda`、`metal`。当前 AMD 稳定性隔离配置填写 `cpu_avx2`。
- `rounds`：默认 10，允许 1–50。命令行 `--rounds` 可覆盖。

manifest 显式同时指定设备 ID 与 SSH 主机，但当前 CentaurOS 资源证据没有设备身份字段，工具无法独立证明两者属于同一台物理盒。验收负责人需在运行前核对这项映射。

## 运行

只生成计划，不访问账号或设备：

```bash
rtk proxy npm --prefix frontend/shell run acceptance:product -- \
  --manifest "$PWD/data/acceptance/office.json" --plan
```

只做登录恢复、设备精确匹配、Direct 连接、工作区授权和资源采样：

```bash
rtk proxy npm --prefix frontend/shell run acceptance:product -- \
  --manifest "$PWD/data/acceptance/office.json" \
  --config "$PWD/data/desktop/zhijun-product-v2.json" \
  --output "$PWD/data/acceptance/office-preflight.jsonl" \
  --preflight --allow-saved-login
```

执行默认 10 轮：

```bash
rtk proxy npm --prefix frontend/shell run acceptance:product -- \
  --manifest "$PWD/data/acceptance/office.json" \
  --config "$PWD/data/desktop/zhijun-product-v2.json" \
  --output "$PWD/data/acceptance/office-10-rounds.jsonl" \
  --allow-saved-login
```

未显式传 `--config` 时使用 `ZHIJUN_DESKTOP_CONFIG`，再回退到 `data/desktop/zhijun-product-v2.json`。未传 `--user-data` 时与桌面使用相同的 `zhijun-desktop` 用户目录。输出文件必须是新文件，已有文件不会覆盖。

旧的匿名网络预检不再内置任何家庭或公司盒 IP。需要运行时显式传入：

```bash
rtk proxy env ZHIJUN_DEVICE_HOSTS=192.168.0.7 node frontend/shell/scripts/verify-real-environment.cjs
```

## 每轮行为和通过条件

每轮建立一条新的、绑定相同 `deviceId` 的正式 Direct 会话。意外断开立即终止整次验收；正常完成后主动关闭本轮产品 session 和 Direct session，不进行失败重连或设备切换。

工具确定性生成 12–16 KiB 的非敏感 Markdown，每轮文件名和内容标记唯一，然后依次执行：

1. `uploadCreate → uploadChunk → uploadComplete`，核对整文件 SHA-256。
2. `post_api_mindos_uploads`，再删除仅用于传输的 upload handle。
3. 轮询 `get_api_mindos_uploads_material_id` 直到材料 `available`。
4. 以 5 秒间隔轮询 analysis 与 draft-card；summary、tagSuggestions、entities、relations 必须全部为 `ok`，草稿必须为未确认的 `ok/draft` 且标题、正文、revision 非空。
5. 单独读取 summary 和 detail，核对 detail 中的草稿 revision。
6. 通过 `get_api_mindos_materials_material_id_file` 得到产品 blob，再执行 offset 0、最大 64 KiB 的 `blobRead`。本夹具小于 64 KiB，因此必须一次读完且 SHA-256 与上传内容一致；随后关闭本轮 product session，释放 job/blob/上传状态。

单轮 deadline 为 15 分钟。写入操作出现未知结果、连接断开、HTTP/合同错误、派生失败、超时均立即终止，不自动重放上传。

每轮前后固定采集资源证据并检查：

- boot ID 不变；
- 所需 systemd unit 的 `MainPID`、`NRestarts` 不变；
- Ollama 前后采样均明确识别实际 compute backend，且等于 manifest 的 `expectedOllamaBackend`；
- `memory.events` 中 `oom`、`oom_kill` 不增加；
- 所需容器 ID、非零 PID、RestartCount 不变，`OOMKilled=false` 且保持 running。

收据结束时还会把首次和最终资源样本再比较一次。只有所有轮次和资源检查都通过，才写入 `run-finished/passed`。

## 证据与隐私

主 JSONL 和 `.resources/` 目录均以 0600 文件、0700 目录创建。主 JSONL 只记录运行状态、设备/材料/operation ID、阶段耗时、文件大小与 hash、boot ID、资源样本引用。资源文件只保存 `centauros-resource-evidence` 的本地 allowlist 投影。错误只记录稳定错误码和阶段，不记录远端 message、HTTP body、文件正文、账号或凭据。

`ollamaCompute` 取自当前启动范围内的受限服务日志。日志不足、无法识别、返回未知后端或与期望不符都会使验收失败；不会把配置环境变量当成实际执行后端。资源证据仍未包含设备身份字段，因此 manifest 中 SSH host 与 Admin deviceId 的物理映射需要在运行前人工核对。
