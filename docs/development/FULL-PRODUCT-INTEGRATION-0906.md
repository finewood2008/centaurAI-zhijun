# 完整产品 Electron / 盒端接入执行计划

日期：2026-09-06。用户要求原产品所有已实现功能全部接入并完成验收。起点：知君 `e6f5312`、DE `c16dc17`（基于盒端 `6b549ad`）、Agent `0a004c9`、Connectivity SDK 1.2.0。早期资料空页与一次重连仅是v1只读证据。当前完整产品v2已形成实现与本地回归；隔离真盒已完成hardware-candidate5五项及gateway-candidate6十项通过，首次失败与修复过程保留于审核账本。生产Admin发布输入与正式UI/SDK全链路验收仍待完成。

## 交付范围与完成标准

以原 Vue router 中实际可达的功能为准：15个页面组件、20条页面路由、2条重定向，五个主导航及偏好设置；包含这些页面内的事项/成果、学习、章程、附件、资料/知识/目录/生命周期、搜索/图谱、导出、模型和偏好操作。无调用方的旧wrapper或尚未实现的产品规划单列，不用占位页替代已实现功能。

完成必须满足：

1. 正式桌面默认显示原产品导航和完整交互；路由切换保持同一个真实账号/盒子会话。
2. 所有业务请求使用受控主进程操作和SDK；renderer不持有票据、不直接访问本机HTTP，Web原入口继续可用。
3. 原JSON读写、SSE、文件导入/附件、下载导出均有实际传输；失败、取消、不确定写入结果和有界资源清理可验证。
4. 盒端知君领域使用独立持久存储，身份绑定account/device/ownershipEpoch；复用DE资料/检索/模型设施，避开DE个人记忆退役清理范围，不复制第二套全局模型/索引。
5. 逐功能执行真实UI/SDK/盒端业务验收，另执行隔离负向和重启/断线/跨主体检查。合成测试、空列表、菜单可见不能代替业务验收；缺失模型/第二账号等实测输入须明确记录，不能填通过。
6. 代码、配置与实际部署版本一致；保留原运行数据、备份与回退；更新功能矩阵/架构/集成/验收文档并推送远程分支。

## 依赖与实现决策

现有SDK1.2.0是整包request/close（请求2MiB、响应16MiB），宿主单请求watchdog15秒。继续使用当前已验SDK，新增盒端真实异步任务、有限游标轮询/取消与上传下载分片；原SSE在renderer以ReadableStream适配保留事件语义。不可把未实现的SDK流API写成现有能力。

D03 v1只签空body GET context/materials。完整产品已实现版本化操作清单、body哈希签名、精确路由与能力校验，桌面/Agent/DE协同执行；保留v1只读兼容。主进程接收operationId与经校验参数，不能接受任意URL/header。

领域部署采用DE鉴权入口管理的独立UDS worker：每个owner/设备所有权代次有独立数据根及唯一worker；只装配知君领域与其任务，不启动旧server的全局watcher/Chroma/模型。通过受限内部能力接口复用DE。存储、配置、内部认证、worker上限/无活租约空闲回收、租约和撤销均已实现并完成本地回归；正式配置和运行行为仍待盒端验收，不以开启local-debug绕过权限。

已确认DE新版本会退役清理旧personal memory/profile形态数据，知君新存储不得位于这些目录；旧global资料不自动归属首次登录者。资料复用须对齐当前canonical service的owner、隐私和生命周期规则。

## 工作流、文件归属与验证

- [x] 核查基线、原入口与现有只读闭环，区分历史与v2证据。
- [x] 冻结170项受控操作清单与v2传输合同、内部HMAC系统事件；170项操作不等于170项UI实测。
- [x] 完整桌面布局、独立hash router、连接provider与API/SSE入口适配。
- [x] 主进程策略、分片/保存/媒体、取消/代次/资源预算、SDK装配和主动录音端口。
- [x] Agent v2签名/精确应用策略、Admin独立应用登记、DE任务/分片/主体隔离入口的本地实现。
- [x] 独立UDS领域worker、持久workspace、DE资料/模型能力、来源事件与任务生命周期。
- [x] 各模块隔离回归、Web/Desktop构建、Electron隔离E2E和网关审核8项修复复验；范围见下表。
- [x] 将OS `5f5f4c9`、Admin `44a0950`提交推送；DE连接FD热修 `132b97d`单独提交推送并部署。
- [ ] 取得boss Admin生产发布路径并发布新 `zhijun-desktop / zhijun.workspace` 登记。
- [ ] 固定完整v2部署产物、复核实际版本、备份后部署Agent/DE/worker/catalog；不以已部署FD热修代替。
- [x] 执行首轮隔离真盒解析/转写与Gateway业务复测，记录通过项与实际失败。
- [x] 修复并复验知识dispatch导入冲突、DeletionStore连接释放与safe_derived有界纠错；hardware-candidate5为5/5、gateway-candidate6为10/10，限于隔离真盒合成主体/输入。
- [ ] 完成正式Consumer/UI/SDK逐功能、跨主体/故障验收；隔离脚本通过不关闭完整产品验收。
- [ ] 完成最终文档/代码合流提交推送，核对实际部署SHA与远程源码一致，关闭完整产品交付。

主要影响：知君 `frontend/mindos-web/src/{main-desktop,desktop,layouts,router,services}`、`frontend/{shared,shell}`，知君新增独立领域worker装配/适配模块及测试；DE live worktree新增workspace/内部能力模块与注册入口；Agent独立worktree的mindosbridge及应用清单。原DE dirty工作区保持不动。

主代理负责合同合流、runtime整合、远程部署和最终验证；并行代理先分别盘点前端、领域、传输和DE能力，编码时分配不相交文件。所有验证使用临时数据或明确验收记录；永久清除功能使用专用隔离数据集，不能清空用户真实资料。

## 当前实现与本地验证

原15页、20条页面路由和2条重定向已装配；170项catalog覆盖原可达操作。新传输是SDK整响应之上的真实盒端任务/游标/分片，没有增加任意原API代理或伪装SDK原生stream。8项网关审核问题已修复并完成本地回归，详见[审核复核账本](../reports/FULL-PRODUCT-GATEWAY-REVIEW-0906.md)。

以下是不同范围、不同批次的验证记录，**不得相加为一次全链路通过数，也不是逐功能UI通过数**：

| 范围 | 已记录本地结果 | 边界 |
| --- | --- | --- |
| Vue | 63项通过；Web/Desktop构建通过 | 原页面/transport/录音等自动化，非真实盒端全功能 |
| Electron main/preload | 最终117项Node测试通过；vue-tsc通过；既有4项隔离Electron E2E通过 | 独立15项配额验证已包含在117内，不另加；非正式账号SDK闭环 |
| 知君领域worker | 113项通过、4个subtests通过 | 隔离领域/UDS/端口与生命周期 |
| DE资料能力 | 最新相关范围189项通过；既有156项与内联SHA阶段172项保留为历史批次 | 不累计；真实材料最终正文82字符、摘要43字符、实体2、关系0，非正式UI |
| DE模型能力 | 99项通过、7个subtests通过；近期针对性40项通过 | 后一批为针对性复验，不与前批累加；真实模型仍须实测 |
| DE完整Gateway集合 | 166项通过 | protocol/store/routes/manager/reverse及7项consent合同验证；非生产SDK流量 |
| Agent | 全量Go与相关包race通过 | 新应用、严格JSON、v2签名与旧v1回归 |
| Admin | 新应用登记已本地验证并提交推送 | 生产发布仍缺实际入口 |

Gateway复验覆盖真实隔离UDS CRUD、409领域错误、稳定start幂等、Owner/epoch隔离、client/session绑定、流取消不确定写结果、实际TCP反向能力口和HMAC来源事件。新增consent ledger TTL从600秒修为1860秒，以覆盖canonical DE receipt的1800秒有效期和签发先后差；仍按真实receipt过期时间、当前来源/配置/执行权限校验，不能把缓存保留时间当作延长授权。取消后的反向模型投递、后台来源结束/撤销、无活租约idle worker回收均已有针对性测试。

启动检查期间曾有临时语音probe错误引用Electron包路径产生弹窗，已停止并清理，不进入产品。正式代码新增明确按钮录音→16kHz PCM16 mono WAV→盒端离线ASR→草稿的输入通道；最长120秒，不自动发送。权限/音频合成回归通过；盒端voice API已用TTS合成WAV实测通过，返回40字符、0条资料、两个指定短语均匹配。实际麦克风采集及正式桌面端到端仍待验收。

## 提交、部署与真实验收边界

| 组件 | 当前交付 | 是否代表v2生产已通过 |
| --- | --- | --- |
| OS / Agent | `5f5f4c9`已提交推送 | 否，完整v2匹配部署/验收待完成 |
| Admin | `44a0950`已提交推送，独立新应用登记 | 否，boss后端生产发布路径未提供 |
| DE连接FD热修 | `132b97d`已推送并单文件部署 | 只证明现有服务热修，非完整v2 |
| 知君worker / 桌面完整增量 | `58dac31`已提交推送 | 正式SDK/UI匹配部署与验收待完成 |
| DE Gateway / 能力完整增量 | `015c659`已提交推送 | 隔离候选真盒通过不等于正式Consumer部署；最终部署SHA由主代理核对 |

FD故障中旧PID3108033达到1024 soft上限、约970条connectivity.db连接；最小修复确定关闭21处SQLite事务连接。新PID3144495四次30秒样本FD52/55/52/52、connectivity FD0/0/0/0、HTTP200、NRestarts0；细节和211项必要回归见[独立热修报告](../reports/CONNECTIVITY-FD-HOTFIX-0906.md)。

盒端已准备离线faster-whisper-small模型（model.bin为483546902字节）；仅在发布阶段下载，解析/转写运行时禁止外发和下载。当前hardware-candidate5完整5/5的依据是实际脚本输出，包含voice API与safe material；不是仅凭模型准备或单项成功推断。逐项结果见下节。

当前正式联调前置是boss Admin后端可用发布路径（已向用户询问），用于发布新应用。仓库仅查到Admin `scripts/run.sh`启动脚本，未找到boss生产CI/SSH主机/代码目录；Gateway独立部署文档不能替代Admin发布证据。不能复用旧只读应用冒充完整权限。

下一步取得Admin发布入口并登记上线，重建并固定匹配产物，再完成v2匹配部署、正式账号UI/SDK与负向验收、整理提交/部署SHA。完整功能矩阵由[验收清单](FULL-PRODUCT-ACCEPTANCE-0906.md)维护；旧真实只读事实见[REAL-ACCEPTANCE](REAL-ACCEPTANCE-0906.md)，不回写成新产品完成。

## 新增硬件和限流证据（非正式SDK全链路）

| 项目 | 当前可确认结果 | 未关闭边界 |
| --- | --- | --- |
| PDF / DOCX / OCR | 真实盒端分别提取46 / 48 / 110字符，通过 | 不替代原UI导入操作与所有文件格式验收 |
| voice API | 40字符、0条资料、2个指定短语均match，通过 | 使用合成WAV，未采集实际麦克风，正式SDK/P2P未测 |
| 内联快照与分析 | 最新相关范围189项通过；最终safe material正文82、摘要43字符、实体2、关系0 | 首次曾有snapshot缺失及evidence_invalid；修复后hardware-candidate5完整5/5，不代表UI |
| v2 Gateway真盒 | gateway-candidate6为10/10；60请求、21个completed操作；knowledge CRUD/confirm/search/purge通过 | 仅合成主体，非正式Consumer/UI/SDK链路 |
| 200MiB调度 | 真实JS模块 + 内存严格Agent配额 + 虚拟时钟：400块完成，243秒，滚动60秒最多101请求 | 时间为虚拟时间；SDK/P2P真实200MiB传输仍未实测 |

统一调度纳入心跳、业务poll和上传，传输前预留编码后字节与请求额度；确认未派发的限流拒绝可按同字节有限重试，已派发的不确定写入不重放。15项独立配额测试属于shell最终117项集合。Agent120rpm、Core1024请求限制与1GiB会话额度保持不变，不通过重连刷新预算。

缺陷机制与修复边界见[审核报告新增R-09至R-14](../reports/FULL-PRODUCT-GATEWAY-REVIEW-0906.md#后续硬件与配额缺陷复核)，实际硬件环境/原始结果由[硬件报告](../reports/FULL-PRODUCT-HARDWARE-0906.md)维护。
