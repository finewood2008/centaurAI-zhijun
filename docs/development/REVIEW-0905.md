# 0905 架构与集成方案二次审核

日期：2026-09-05。范围：[技术架构](ARCHITECTURE-0905.md)、[集成方案](INTEGRATION-0905.md) 与所引用的本地源码。本记录保留同步前二次审核结论，并在下节追加产品源 `22dc9a3` 的复核；完整变更与测试见 [上游同步记录](UPSTREAM-SYNC-0905.md)。尚未开始 SDK / data-engine 业务集成。

## 上游更新后复核

- 既有身份桥、目录隔离、scope/owner、sidecar 来源、上传与流传输缺口均仍成立，相邻 SDK/OS/data-engine 基线未变。
- 新增事项/成果 API、四张 work_* 表、matter/artifact 来源、发送恢复及 health 轮询，已写入架构图、API 矩阵、迁移依赖和 P3–P5 验收；5 份领域合同文档也已更新。
- 本次发现并修复文稿 A 未保存时保存回复 B 覆盖 A 输入的问题，新增真实组件回归先失败后通过。此为业务修复，不与下表中“仅完善文档”的历史处理混淆。
- 本次仍待处理：成果创建指纹依赖可变标题/回复，列表和历史缺分页，新增数据无独立清除路径，运行入口/测试存在平台假设。已明确触发条件和验证边界，详见同步记录。

## 审核结论

「Electron 薄客户端 + 盒端服务」方向可以保留，但原方案有若干事实需要更新，也有影响实施正确性的遗漏。已将下表修订同步写回正文；**修正文档不代表已经修复对应代码缺口**。

| 优先级 | 审核发现 | 已完善的方案 / 实施门槛 |
| --- | --- | --- |
| P0 | 知君隐私规则不全在领域目录内；基础 QA 排除受保护附件/衍生卡，上传新版本在摄取前继承保护 | 增加依赖闭包与保护策略扩展点；P4 先验证保护，P6 再开放附件完整流程；避免搬路由后沿用缺少保护的基础实现 |
| P0 | data-engine 的 folders 没有设备归属，资料列表附带目录、目录 API 计数及目录删除未按 deviceScope 过滤 | 首轮收窄为已核验的资料字段，目录写不开放；完整目录能力先补归属、查询、计数、删除与迁移测试 |
| P0 | 原文“Consumer JWT”容易误解为普通登录 token，当前 P2P 到业务会话的身份桥仍缺失 | 明确专用 JWT 声明、JWKS 配置、身份映射/续期/撤销责任；实测受保护资料 API，不能用 health 或无 guard 的 `/validate` 代替 |
| P0 | SDK / OS 已更新为干净提交，但 sidecar 仍记旧 dirty 源码，data-engine 上传仍未提交 | 更新仓库基线；区分源码提交、npm/tgz 与二进制来源，要求冻结可核对的交付组合 |
| P1 | 多个知君本体操作仅支持 global；投影落盘也仅 global | 增加真实设备能力表，按范围实现或隐藏；不解除 guard，不承诺旧 MCP 自动读取设备画像 |
| P1 | data-engine 已有可选 Claim/Profile 域；deviceScope 又不等于同盒账号隔离 | 修正“缺失”为“无知君兼容接口”；先定 Claim 事实源、owner/device 归属和设备转让规则 |
| P1 | 原计划只关注 2 MiB 请求上限，遗漏 64 MiB PC 会话总量、1024 次请求和默认 60 秒总时限 | 增加全链路预算表、调度/重授权/同主体续传合同；200 MiB 文件不是拆小片就能完成 |
| P1 | 新上传的 parts/complete 与 PC policy 不符；DELETE 又未被 PC Agent manifest 放行 | 明确 Vue、main policy、Agent parser/manifest、后端四层对齐；移动应用权限不能代替 PC |
| P1 | Composer/章程草稿含正文，现有 sessionStorage 键不含账号/设备 | 增加草稿命名空间、旧键处置、退出清理与未保存内容交互，扩大设备切换验证范围 |
| P1 | 原阶段把完整建档/判断放在聊天传输之前验收 | P4 改为领域基础与迁移验收；P5 与传输合流后验收完整产品闭环；附件自动回复依赖 P5 |
| P2 | “没有流”需定位准确：Agent 已发 chunk，Core 与 SDK 仍整包返回；普通切页也不等于停止 | 明确扩展现有帧协议与 Core 缓冲模型；分别定义切页解除订阅、显式停止、换身份关闭 |
| P2 | 部分鉴权细分错误已被服务端丢弃，无法仅由前端恢复 | 补服务端结构化 code 合同；禁止按错误文案猜重连；身份桥同时检查真实 Bearer 长度 |

上述优先级表示实施阻塞程度，不是对真实部署漏洞影响面的评估。未连接部署环境，未证明这些路径在生产中可达。

## 关键证据入口

- 隐私扩展：[知君 QA](../../backend/mindos/qa.py#L329)、[知君版本摄取](../../backend/mindos/uploads.py#L697)、[附件保护存储](../../backend/mindos/stores/chat_import_store.py#L1)。
- 目录范围：[data-engine 目录 schema](../../../nexusaos-data-engine/backend/mindos/stores/job_store.py#L55)、[目录统计](../../../nexusaos-data-engine/backend/mindos/stores/job_store.py#L1152)、[资料返回 folders](../../../nexusaos-data-engine/backend/mindos/uploads.py#L438)。
- 身份：[JWT 合同](../../../nexusaos-data-engine/backend/mindos/connectivity_ticket.py#L117)、[设备 scope](../../../nexusaos-data-engine/backend/mindos/device_context.py#L130)、[另一套 Claim 身份](../../../nexusaos-data-engine/backend/mindos/claims.py#L58)。
- 设备能力：[global-only](../../backend/mindos/ontology.py#L81)、[投影文件](../../backend/mindos/zhijun/projection.py#L93)。
- 传输与预算：[Core HTTP](../../../nexusaos-centuarai-os/p2p-core-go/http.go#L89)、[Agent 应用策略](../../../nexusaos-centuarai-os/manifests/remote-agent-applications.yaml#L25)、[sidecar 来源](../../../nexusaos-centuarai-conn-sdks/release/electron-sidecars-1.2.0/manifest.json#L1)。
- UI 生命周期：[对话草稿](../../frontend/mindos-web/src/components/conversation/Composer.vue#L36)、[章程草稿](../../frontend/mindos-web/src/components/conversation/CharterWorkspaceEditor.vue#L32)、[切页与停止](../../frontend/mindos-web/src/pages/ConversationPage.vue#L1160)。

## 同步前二次审核执行与验收（历史）

- [x] 主代理检查版本变化、传输预算、UI 状态与生命周期。
- [x] 独立审核鉴权、路由/headers、响应错误及资料隔离。
- [x] 独立审核领域依赖、隐私保护、持久数据、worker 与阶段顺序。
- [x] 等待两路审核完成，交叉核对后修改架构与方案，新增本记录并更新 README 导航。
- [x] 97 处本地链接/引用行号检查通过；无未闭合代码块或尾随空白；四张 Mermaid 重新渲染通过，SVG XML 有效，目标部署拓扑未变。

当时受影响文件仅为三个 Markdown 文档和 README，目标拓扑未变化。产品同步后架构图已补入事项/成果与聊天恢复并重新导出 SVG；后续实施依赖、拟改业务文件与测试条件统一维护在集成方案第 6 节。

二次审核阶段未运行后端；后续上游同步执行了隔离产品回归与 fixture 浏览器测试，未读取用户运行数据或凭据，未进行真实账号登录、设备联调、SDK 发布或服务部署。这些文档随本次产品同步在 `dev/first-integrate-check-0905` 提交，远程状态以同步记录及 Git 为准。
