# “程序员”对话未进入本体：公司盒现场复查

## 现场事实

2026-09-15，通过只读 SSH 和 SQLite `mode=ro` 核查公司 `192.168.0.7`，桌面当前连接为 AMD-A2A-248，安装版为 0.1.22。未读取或输出模型密钥，未重试真实任务，未修改生产数据库或服务。

- 2026-09-14 22:10 和 2026-09-15 10:10（北京时间）包含“程序员”的用户消息均已 complete。
- 两条消息对应 `extract_turn` 均 failed，错误为 `MODEL_EGRESS_CONSENT_REQUIRED`。
- 工作区共有 29 条 `extract_turn` 以同一码失败，claims 表为空。
- 线上 provider 仍引用 `CapabilityProvider`；线上 `app.py`、`jobs.py` 哈希与本地修复代码不一致。旧兼容 `model.py` 哈希一致不代表运行逻辑已更新。
- 容器 `zhijun-integration-0907` 仍挂载 `/srv/centauros-releases/company248-business-20260914-1/zhijun`。

结论：这两条消息不是因为用户没有确认或前端没有刷新，而是旧模型授权链阻止后台提取。家里盒升级记录不能当作公司盒已修复；此前家盒记录本身也保留了真实模型配置后验收的未完成项。

## 新增修复与验收方式

另确认短句“我是程序员”不足原 6 字阈值时会在入队前被忽略。`extract.py` 加入短完整身份自述的限定豁免，不降低全局字数门槛，不放宽证据、授权、长期价值或人工确认要求。

新增 `test_programmer_memory_flow.py` 从生产 workspace 的真实签名 dispatch 聊天入口出发，经 SSE 整理事件、任务入队和 `OntologyWorker.process`，验证“我是程序员”“我是医生”“我是一个程序员”进入 `who/working`，不自动变成 confirmed。测试仅模拟模型 HTTP 响应；任何 Data Engine model/models/模型预览授权能力调用都会失败。

## 上线状态与依赖

排查轮完成代码修复与本地测试后未立即切换。用户随后确认“帮我更新”，现已完成下面记录的公司盒 worker 部署；**真实在线模型重新配置及本体候选验收仍待用户操作，不能宣称真实本体生成已验收**。

需要更新公司盒完整 worker 与配套 catalog，并验证实际运行哈希。新版本使用知君工作区独立模型配置，不自动复制或沿用 Data Engine API Key；用户需在知君偏好页重新保存模型并确认当前用途授权。切换前应确认用户能配合配置，避免正常聊天在升级后因缺少配置不可用。

保留既有对话、失败任务和授权边界，不自动重试全部历史任务。上线后通过新一轮用户自述或用户明确选择的“重新整理”，检查任务成功、候选和原话证据，再通过本体页验证可见。仅健康接口通过不算本体功能验收通过。

## 公司盒部署记录

2026-09-15 14:11:47（北京时间）启动新版容器内程序。采用 SSH 定向升级，不是 manager/OTA 发布；仅替换知君 worker 和配套 catalog，未改 Data Engine 源码、Remote Agent、推理模型或桌面安装包。

- 发布包由 `build-full-product-release.py` 的 collect/源码审计逻辑生成，178 个文件、175 个 Python 文件、1317 个本地导入校验；包含本轮短身份修复工作树内容。
- 实际传到盒子的归档 SHA-256：`f85b46adcc6071ed704a679ebb04cc2264e2287fade43446522ed18cd3105dbe`。
- 全部运行文件哈希表的规范 JSON（键排序、紧凑分隔符）SHA-256：`bfb77dd0569ab2ac66378b7febf50fc08b0045185b87e94a49932a1b46f3e347`，与本地 178 文件清单一致。
- catalog SHA-256：`3b4d0a8169334cb8d6548a51f44583c5c5e56e13e54ae19832b25dd722b8722d`，与当前已安装客户端完全一致，本次不要求重装客户端。
- 暂存目录：`/home/user/zhijun-worker-update-20260915.OJR7GP`，含源码包、manifest、来源清单、阶段收据和独立预检脚本。
- 运行源码仍为 `/srv/centauros-releases/company248-business-20260914-1/zhijun`；旧目录保留为同级 `zhijun-before-memory-20260915`。
- 同级 `.memory-upgrade-backup-20260915` 为 root 0700，含 root 0600 的停机一致性 `workspace-state-secrets.tar.gz` 和部署收据。备份覆盖 domain、gateway-secrets、gateway-state，未下载或进入仓库。

切换前确认没有排队/运行本体任务或 Gateway 操作。停止原容器后完成备份、校验并成对换入 worker 与 catalog，再启动同一容器，未改环境和挂载配置。容器为 `running/healthy`，RestartCount=0；8618、8620 `/api/health` 为 200，未签名 context 为 401。旧 8619 端口未监听，不作为本次验收入口。

目标同镜像、无网络、只读代码、独立临时工作区预检通过：

- 真实签名 dispatch 聊天 → 整理任务 → `who/working` 本体候选；模型 HTTP 使用合成响应，明确禁止 DE 模型/授权能力调用。
- 模型设置创建、另一工作区隔离、重启后凭据持久化读取；9 个模型设置接口使用 domain 路由。
- 未授权请求拒绝、worker lifespan 正常退出。

生产工作区 secret root 为 UID 10001、0700。原有 29 个失败 `extract_turn` 未被自动重跑，claims 未被测试插入。部署后检查时尚无新的真实客户端 worker 进程，须用户重新连接，保存并启用在线模型、核对授权，再新发“我是一个程序员”或定向重新整理。

回滚仅在停容器后恢复旧源码及其 catalog，然后启动原容器；当前工作区数据不能从备份直接覆盖，以免丢失升级后的真实写入。
