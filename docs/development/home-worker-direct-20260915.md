# 家里盒子知君 worker 直连模型升级记录

## 范围与结果

2026-09-15，目标 `192.168.1.18`，Admin 设备标识 `2c:7b:a0:4d:68:ef`，
稳定设备 ID `centauros-c975febb427df29b0fe2334b`。

已部署当前工作树的知君 worker 与配套操作清单，聊天和本体提取使用知君模型提供者，
不再依赖旧 DE 模型授权票据。没有更新 Data Engine 源码、Remote Agent、推理服务或桌面安装包。
这次是经 SSH 执行的定向升级，不是 manager/OTA 发布。

## 制品与切换

- 使用 `scripts/build-full-product-release.py` 的源文件收集和审计逻辑：178 文件，175 Python 文件，1,317 本地导入。
- 来源 HEAD `7a8fd7dbecfec5af3bf7b3e28b36cfdfc0b439eb`，包含未提交工作树修改，不能标记为纯 HEAD 制品。
- 源码归档 SHA-256：`ae31b1518aaa383b3d260cd76b0beec4a11265bdbe119ec4b74e15c71fa0395c`。
- 配套 catalog SHA-256：`3b4d0a8169334cb8d6548a51f44583c5c5e56e13e54ae19832b25dd722b8722d`。
- 现场暂存目录：`/home/user/zhijun-worker-update-20260915.NRr4M1`。
- 运行目录：`/home/user/apps/centuarai-data-engine/releases/home-business-full-20260914-3/zhijun`。
- 同级旧代码保留为 `zhijun-before-direct-20260915`。
- 同级私密备份目录 `.zhijun-direct-backup-20260915`（root 0700），包含切换前源码摘要、激活记录和停机后的一致性 `workspace-state-secrets.tar.gz`。
- 备份覆盖知君 `workspaces`、`secrets`、`gateway-jobs`。备份含私密数据，仅留盒端，不进入仓库或安装包。

停机后校验无存活 worker/DE manager、旧源码未变化，再备份并成对切换代码和 catalog。
使用原 `centaurAI-database.service` 用户 unit 启动，未修改 unit 或环境配置。
Data Engine 进程 PID 从 `976793` 变为 `991876`；重连后新版 worker PID 为 `992381`。
Remote Agent 原 PID `230068` 仍存活，未重启。

## 验收

- [x] 本地 23 测试、16 子测试通过（工作区模型设置、provider 选择、后台整理授权）。
- [x] 使用现场 DE 的同一 Python，在独立临时工作区执行完整 worker lifespan 和签名 dispatch；模型设置创建、激活、重启读取通过；未认证请求拒绝。未调用真实模型。
- [x] 现场确认 DE manager 为每个工作区传入稳定、隔离的秘密目录。
- [x] 部署前后 178 个文件哈希一致，catalog 与客户端配套。
- [x] 服务 active/running，NRestarts=0；`8618/openapi.json` 返回 200。
- [x] 实际 Electron 重新选择家里盒子后建立加密直连，今日来信和偏好页正常加载。
- [x] 原材料页面仍显示 3 项资料、2 个文件夹，分页修复保留。
- [x] 本体任务聚合升级前后均为 done=66、failed=5，没有 queued/running；未重发对话或重试历史任务。
- [ ] 用户重新配置在线模型、确认新服务授权后，验证真实本体整理。

## 用户下一步与边界

实际偏好页显示“请在当前工作区的模型设置中重新填写并启用聊天服务；不会自动沿用数据引擎的 API Key”。
这是独立模型配置尚未设置，不是部署失败。需用户在知君偏好中保存供应商、服务地址与 Token，
选择模型并启用，核对新服务授权，然后对原失败任务点击“重新整理”。
未复制旧 DE 密钥、未修改用户授权、未配置本地模型或启用 CPU 回退。

另观察到敏感规则区提示当前账号无法查看规则应用状态，以及服务请求失败。
本次未扩大该权限或修改 DE 规则服务；原材料列表与知君模型配置读取正常，规则问题须单独核查。

回滚前须停止同一 unit，核对无并发维护；保留当前新代码，再恢复同级旧代码与其配套 catalog。
不要直接覆盖已变化的用户数据库或秘密库；数据回滚必须另行确认，并使用该私密备份。
