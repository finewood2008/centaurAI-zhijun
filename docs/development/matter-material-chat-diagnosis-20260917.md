# 对话读取事情与原材料排查

最终结果：公司盒192.168.0.7已更新并完成真实Search/Evidence Resolve验收。
用户原句返回5条目标DOCX片段，完整文件名返回3条，旧回收版本未返回。
客户端0.1.35已打包，尚未安装；在线omit偏好保持。外部Agent开通是独立配套部署问题，当前仍未开通。

## 计划

用户报告旧版本中已录入的模型微调事情未进入对话，已确认的 DOCX 原材料未检索到片段。
用户已确认目标是公司盒 `192.168.0.7`。

- [x] 只读核对目标数据、绑定关系、索引状态及现有检索响应，明确是否属于当前版本。
- [x] 并行追踪事情候选召回、授权与上下文装配，以及资料检索查询和 Data Engine 边界。
- [x] 为确认的缺陷加入复现测试，按责任模块修复，保留现有权限和资料选择流程。
- [x] 执行受影响链路测试、审阅变更，说明现场恢复状态及发布需求。
- [x] 用户选择产品级工作区读取修复，原逐文件补授权/偏好调整方案停止执行。

依赖：当前分支、盒子现有 SSH/Docker 只读诊断能力、既有本地测试环境。
候选影响模块：`backend/mindos/zhijun/context_*`、`retrieval_tools.py`、对应 store/测试；
先获得证据再收敛修改范围。主代理负责现场与整合，子代理分别只读分析事情和原材料链路。

验收：匹配的事情以可追溯且获准的来源进入对话上下文；资料查询能召回目标授权且索引就绪的片段，
或精确说明空结果/索引失败原因；不把错误伪装成空结果，不混入其他工作区、不跳过资料确认。
诊断不重发真实聊天，不触发真实回答模型，不删除/重建业务数据。

## 现场证据

事情没有丢失，存在三条微调相关 active 记录。`matter_e55639ebaac6`（我准备做一个模型微调项目）
未绑定会话；旧上下文只读取当前会话绑定的事情，不会发现这条独立记录。

`conv_554bde18dc67` 已绑定 `matter_03243f210124`，其 2026-09-17 07:13 UTC 回答回执
明确记录 `matterSuspended=null`，excluded 原因为「按默认方式跳过未授权资料，原记录保留」。
实际 `routing_handling` 为 global / enabled=1 / action=omit / revision=2。
该分支的根因是用户现存的在线外发处理偏好，不应通过召回修复绕过授权。

原材料唯一未回收的当前记录为 `mindos_bd8dcae1b0c6`，文件名
《大模型自我认知微调项目复现新版.docx》，版本 1，ready / completed / indexed，32/32 片段，
generation=`text:1`。另外三个同名旧记录已回收，不能恢复旧版本或授予它们权限。

知君 RAG 应用 `agc_2c17c8b0bf2f46c1a1c12df124813ec1` 有正常身份和检索能力，
但 `allowedResources` 为空，不是该材料的 immutable application origin，也不是 Web 默认应用。
资料页面与检索应用的授权范围不同。原句和「大模型微调复现」两次正式 Search 均返回
`no_results`，trace 分别为 `atr_lx1q-OdK6LcrA9qo`、`atr_cu31PwRbpFdNhidj`。
目标当前版本在 `data_catalog` 没有行，精确授权前还需从权威材料状态补齐单条目录投影。

## 已完成源码修复

- 未绑定会话可按用户本轮明确的事情查询或项目名称发现同 scope active 事项。
  候选上限 32，经过现有权限与预算过滤后最多 3 条；不自动绑定，不恢复旧悬挂，
  不用模型补查提示扩大事项范围，多候选提示澄清。
- 回答依据之外增加可见提示，说明事情因「跳过受限资料」未提供给在线模型，并链接设置。
  不显示未获准事项标题/正文；混合已提供与被排除的情况不误称全部未读取。
- 后端相关回归 96 passed、27 subtests passed；9 个新增回归最终补强后再次通过。
- 前端 provenance 回归通过，类型检查和桌面构建通过；独立审阅未发现权限、版本或预算旁路。
- 原有 `test_context_lookup_turn.py` 两项 fixture 在修改前即因 ONLINE_SERVICE_CHANGED 失败，
  原因是 enable() 后又修改 configuration_revision；未混入本次修改。

以上源码修改现已随下述定向更新部署；客户端0.1.35已打包，现场检索结果见最终验收。

## 原精确维护方案（已被后续产品决策替代，不执行）

1. 仅将当前材料 `material:mindos_bd8dcae1b0c6` 加入知君 RAG App 授权；保留其他原授权，
   不改 owner/scope、secret、capabilities、origin 或 Web 默认应用。
2. 可选：关闭现有固定 omit 偏好，恢复逐次询问。此操作不产生在线资料授权，
   用户仍需在实际对话中核对后批准发给在线模型的来源。
3. 检查无活动任务后停服务，备份应用授权数据库并获取正式 data-root lock。
   从 canonical read-only material status 核对目标归属、生命周期和可用性；
   仅在目录缺行时 insert-if-absent，不覆盖现有治理。
4. 使用正式 application_management 修订核对：disable → configure 精确授权集合 → enable。
   失败恢复授权数据库备份后启动原服务；不回灌业务文档、会话或其他运行数据。
5. 重启健康、核对授权差量和真实原句 Search；只检查返回状态和片段数量，
   不提交片段确认、不重发真实聊天、不调用在线回答模型。

现有 data-access UI 只管理 legacy_bearer，不能管理该 app_secret 应用，且知君操作清单没有此入口。
MCP 外部 Agent 的授权也不能替代这个内部 RAG App 权限，因此原拟采用精确维护方案；后已被产品决策替代，没有执行。

## 后续产品决策与实施计划

用户明确同意：知君应自动检索当前用户工作区中已上传且索引就绪的文件。
逐文件管理员补授权方案搁置；在线模型的发送确认独立保留，现有 omit 偏好不擅自改变。

- [x] 为可信 Gateway 创建的知君内部检索应用建立可验证的第一方工作区材料读取身份，兼容已有应用。
- [x] 搜索及证据解析使用相同 owner/scope、版本、生命周期和索引检查；普通第三方及 MCP 不获此身份。
- [x] 验证授权变化使旧主体、确认令牌和证据失效；保留资料选择、敏感检测与在线发送确认。
- [x] 隔离回归通过后制作定向盒端更新，备份并验证原句检索；不重放真实聊天或发送在线回答。

依赖与影响：相邻 `nexusaos-data-engine` 的 provisioning、application auth、材料检索及新增内部身份存储/测试；
该仓库存在其他未提交改动，定向更新必须逐文件核对，不能整仓发布。
验收包括既有上传、后续上传、其他 owner/scope 拒绝、第三方权限不扩大、回收/未索引排除、撤销后旧证据不可重放。

## 现场更新进度

2026-09-17 定向更新6个源码文件：DE内部身份/auth/provisioning/material retrieval，Worker事情候选与context plan。
远端材料检索文件比本地HEAD更新，保留远端全部PERF实现，仅移植工作区身份判断与scope复核。
本地DE选定回归85通过；目标原镜像材料检索隔离10项通过；最终派生镜像provision/auth/retrieval34项通过。
MCP新增4项加既有回归共45通过：内部范围A+B仍不允许外部仅获A的Agent读B。

已部署镜像 `sha256:8b4874f16ba2d7cd9b14c4aa020346fd9fe25247b90fcd0ea41dbb80c1f32fb9`，
新容器 `452f6c9278820471e5f9c8e8fbb867a68ec9f56d334cd7698066436d1fb37e88` 健康。
配置仅Image及Worker源码挂载变化，账号/网络/密钥/其他数据挂载/安全约束保持。
旧容器停用并保留为 `zhijun-integration-0907-before-workspace-materials-20260917`，重启策略no，避免双实例。
私有回退目录 `/home/user/zhijun-workspace-release-20260917.XQI2D3` 保存原配置和14.6MB状态备份。
状态备份SHA256 `0f36c0f626a946433858f5ced03dca2ea74a7c4c65576e1488b00241f301f0f0`。
修复首次登记后仅知君App的grant revision变为2；没有逐文件授权，也没有调整在线omit偏好。

原句与完整文件名复测均已在内部检索阶段命中5片段，authorized_sources=3。
但正式Search API后续阶段返回503 SERVICE_UNAVAILABLE；尚不能宣称完整资料使用已恢复。
trace：`atr_jg6JXLe_7siV16LE`、`atr_wzJrEcV1Z3-MZyfL`。继续定位后续异常，不重放真实聊天/不确认原文/不调用在线回答模型。

关键业务表7张逐行哈希/计数在第一次更新后与备份完全一致。知君逐文件grant仍为0。

### 后续503定位

原App middleware吞掉非预期异常，仅返回SERVICE_UNAVAILABLE，导致没有可用的trace审计或失败栈。
新增安全日志仅记录服务生成的trace ID、异常类、代码文件basename/函数/行号，禁止消息、正文、输入和locals；
新增回归与E1合计51通过、1跳过。第二次定向镜像更新仅增加此诊断文件，未改变业务状态。
镜像 `sha256:8ec8d3c47fb358cec0e2526d6e421584bb93a12915210df5c816aa840400b061`，
容器 `7aa13a80d55c11b56db226e4249ef82e9ad2f7f5ba6503667a9e8405b3e31c9d` 健康。
私有回退目录 `/home/user/zhijun-workspace-diagnostics-20260917`。

新trace `atr_-3lr0LYqb3uHrP-V` 明确是 `RagV2Locator.model_validate(chunk.locator)` 的ValidationError；
调用位于 `SensitiveConfirmService._item`，不是敏感模型不可用。召回与guard已完成，
内部索引定位元数据投影为对外定位合同失败。扩展本次修复范围至共用locator规范化，
依赖实际索引metadata证据；验收覆盖Search/Read/Evidence，不放宽对外合同、不编造位置、不重传文件。

### 最终验收

实际索引144条定位元数据中，128条含内部paragraphEnd，14条另含section，2条仅page。
共享_locator现在只投影合同的7个字段，省略非法坐标，内部ordinal不编造为页码/段落；
legacy秒制时间按真实语义换为毫秒，保留所有授权/版本/PERF逻辑，不修改索引和文档。
本地137项回归通过；目标镜像隔离12项通过，含V1/V2完整runtime Search→响应投影→Evidence Resolve。

最终镜像 `sha256:1969cdfde0fe70481db5554c18545517b0e17e77979114f18ca72424a9a22741`，
标签 `zhijun-data-engine:company248-workspace-fixed-20260917`。
正式容器 `1a05d34c756032c5dc9a00cd89de554dc4f55584ccb20f8f2b283e1b33f93cda` 健康。
七个定向源码文件SHA全部符合最终候选；相对原始容器配置仅Image和Worker代码来源挂载改变。
Worker代码已随镜像固定，不再读取旧发布目录的bind mount；后续发布须基于此最终镜像/源码变更，不能仅改旧挂载目录。
最终更新前备份在私有目录 `/home/user/zhijun-workspace-locator-20260917`，
SHA256 `3c7c44b9187263c10dc5c52a9d0354f9076a354bc1b7d8741442c5e2de2f2245`。
保留原始及中间回退容器，均停止且RestartPolicy=no；正式容器沿用unless-stopped。

- 用户原句：status=ok，5条，全部materialId=`mindos_bd8dcae1b0c6`；trace `atr_x_aj0ppE212SDyg0`。
- 返回引用正式Evidence Resolve：成功1条，paragraph定位有效；trace `atr_-hiIv-gaMrXxzx7f`。
- 完整文件名：status=ok，3条，全部来自同一有效文件；trace `atr_d9HZ8u-9LC0ZOEDY`。
- 三个已回收版本没有返回。诊断只输出状态/数量/ID，不输出片段正文，不提交确认，不重放聊天、不调用在线回答模型。
- 最终更新前后7张关键业务表计数及逐行SHA一致；原Web默认App及revision1保持，逐文件grant为0，在线omit偏好未改变。

事情候选发现修复已上线，但现有在线omit仍会跳过未获得在线使用许可的事情；
0.1.35客户端会明确说明此原因并提供设置入口，不将本地召回误当成在线外发授权。

客户端0.1.35已签名打包并验包/隔离启动通过，包含事项跳过原因提示，未安装或上传。

## 外部 Agent 尚未开通的独立问题

用户截图中的available=false来自Worker缺少`ZHIJUN_MCP_RESOURCE_URL`，触发MCP_NOT_PROVISIONED。
盒子虽已有MCP源码与依赖，但没有MCP/connector进程和服务配置；当前DE Gateway缺两条UDS路由、独立MCP服务身份、资料ACL capabilities及Worker资源URL注入。
本地Admin未发现配套OAuth PKCE/consent票据实现，尚未核查云端其他部署。
必须完成Gateway/Admin/盒端MCP配套实现与联合验收，再配置稳定入口域名、relay、TLS证书及客户端注册；不能只填URL让页面显示可用。
此问题不阻碍知君自身的原材料检索修复，未伪造available或启用外部授权。
