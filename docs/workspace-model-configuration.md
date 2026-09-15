# 工作区聊天模型配置

知君 worker 的聊天和本体模型直接复用 Web provider；聊天服务配置属于当前工作区。
设置页的聊天配置、供应商保存、模型发现和启用共 9 条操作由 worker 接收。
数据引擎仍负责 RAG 和材料管理；材料模型管理、模型安装任务与监控没有开放给 worker。

非密钥配置保存在 `<workspace-data-root>/db/runtime_settings.db`，聊天密钥保存在该工作区
独立的 `<CENTAUR_SECRET_STORE_DIR>/model-secrets.db`。后者必须位于数据根之外的私有目录，
应随当前工作区稳定挂载，以便重启保留密钥。应用生成的加密密钥与密文存于这个私有数据库；
它不提供对完整秘密数据库副本的离线保密能力。worker 不读取数据引擎密钥，也不把全局
`CENTAUR_QA_AI_API_KEY`、旧 Web 默认 key 或 `OPENAI_*` 当成用户配置。恢复配置数据库后若
工作区 secret root 中没有相应密钥，设置显示未配置；用户必须重新输入并启用服务。

新工作区没有隐式本地 Ollama 地址或 CPU 模型回退。只有明确配置过的本地服务才可调用。
worker 若直接启动，可显式传入 `ZHIJUN_LOCAL_NPU_BASE_URL` 和 `ZHIJUN_LOCAL_NPU_MODEL`；
这两个字段必须同时有效。受数据引擎进程管理器启动的 worker 可能过滤额外环境变量，
所以部署时推荐以下持久化配置方式，不能只修改父容器环境。

## 为现有工作区配置已运行的本地 NPU 服务

先停止目标工作区的 worker，并防止管理器立即自动重启。使用与 worker 相同的系统用户，
在本仓库 `backend` 目录运行；Python 环境需要安装本仓库 backend 依赖。
路径必须是已经绑定身份的真实绝对路径（不能含符号链接或 `..`），并明确提供目标
workspace ID。命令读取既有 `.workspace.json` 验证身份，持有相同排他锁；正在运行的
worker、错误身份、公开目录权限和过期 revision 都会拒绝。

先查询当前非密钥设置与版本号：

```sh
python -m zhijun_worker.configure_local_model \
  --data-root /ABSOLUTE/EXISTING/WORKSPACE \
  --workspace-id WORKSPACE_ID \
  --show
```

再使用已经验收的 NPU 服务地址和模型名保存本地设置。以下为待替换参数，不是服务默认值：

```sh
python -m zhijun_worker.configure_local_model \
  --data-root /ABSOLUTE/EXISTING/WORKSPACE \
  --workspace-id WORKSPACE_ID \
  --npu-base-url http://NPU_SERVICE_HOST:NPU_SERVICE_PORT \
  --npu-model DEPLOYED_NPU_MODEL \
  --revision LOCAL_REVISION \
  --activate --chat-revision CHAT_REVISION
```

`--revision` 和 `--chat-revision` 使用查询返回的整数，首次不存在时为 `0`。
`--activate` 明确将聊天切到这个本地服务；省略它和 `--chat-revision` 只保存本地服务配置，
不会切换已启用的在线服务。此命令不探测、下载、启动或切换模型服务，不判断服务实际使用
哪种硬件，也不读取或复制数据引擎密钥。地址必须是已部署的 Ollama 兼容 NPU 服务。

保存成功后重新启动目标 worker，通过聊天/本体的实际调用验证。脚本不会启动 worker；
最终调用是否使用 NPU 需由既有服务的运行指标确认。配置写入失败时先用 `--show` 检查当前
版本后再重试，不要沿用旧 revision。

客户端和盒端需一起更新 `frontend/shared/product-operations.json`，以确保上述 9 条操作
从 `models` 路由到 `domain`；无需修改数据引擎仓库的实现。
