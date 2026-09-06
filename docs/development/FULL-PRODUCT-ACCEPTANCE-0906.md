# 完整产品真机验收清单（0906）

日期：2026-09-06。本文是待执行、待填证据的验收计划，不是“全部功能已通过”的结论。真实 UI、真实 SDK、真实盒端业务三列统一初始化为 `pending`，由主代理完成实际交互后逐项更新。

## 1. 范围、基线与结果规则

范围以 [产品路由](../../frontend/mindos-web/src/router/routes.ts) 与 [操作清单](../../frontend/shared/product-operations.json) 为准：15 个页面组件、20 条页面路由、2 条重定向；170 个受控 operation。五个主导航为今日来信、对话、我的本体、判断、资料与边界，另有偏好入口。路由可见、空列表或接口注册不等于功能已验收。

本清单更新时 catalog schemaVersion=1，SHA-256 `e339894c17a39b92d7ffd7c0d3c47a69b6e47c2e65fcce08a9062c08599d2722`。操作数量：materials 54、domain 95、models 21；其中 multipart 4 个、二进制响应 2 个、SSE 响应 1 个。multipart包括三个文件导入路径与一个盒端语音转写操作；三个 JSON/Markdown/二进制导出类别及纯前端交互需额外验收，不能只数 API。

关联：[执行计划](FULL-PRODUCT-INTEGRATION-0906.md)、[v2 合同](contracts/zhijun-workspace-v2.json)、[此前只读验收](REAL-ACCEPTANCE-0906.md)、[本轮网关审核](../reports/FULL-PRODUCT-GATEWAY-REVIEW-0906.md)。此前只读成功不自动继承为本轮完整产品通过。

结果只用 `pending`（尚未执行）、`pass`（有对应层证据）、`fail`（执行后不符预期）、`blocked`（缺前置条件）四种。blocked 必须写缺少的具体账号、模型、样本或环境，不能写成通过；恢复后需重跑。每个功能只有 UI 交互、SDK 受控传输、盒端真实结果均通过，且所依赖故障检查通过，才可标端到端通过。

本轮 shell Node 测试已增至100项（含7项麦克风权限模拟测试），另有4项Electron隔离E2E及前端自动化测试。它们属于自动化证据，不能填入下面真实三列，也不能证明模型实际可用、内容已落到真实盒子、跨实际账号隔离或真实麦克风转写通过。后续代码变化后由主代理记录最终执行次数与版本；本清单编写过程没有触发真实麦克风权限、探测或录音。

## 2. 执行记录和证据约定

每轮建立本地忽略目录 `data/desktop/full-product-acceptance-0906/<run-id>/`；这里是约定位置，当前不宣称目录或证据已经生成。每个用例保存 `cases/<case-id>/ui/`、`sdk-summary.json`、`box-summary.json`、`result.md`。目录内保留操作前后截图、时间戳、脱敏 operation/request/job/object 标识、HTTP 状态/事件游标、实际业务读回和清理记录；内容含个人资料的原始证据不提交仓库。

每轮先填写：

| 项目 | 实际值 |
|---|---|
| run-id / 验收人 / 起止时间 | pending |
| 桌面提交 / 构建 SHA / SDK与sidecar SHA | pending |
| Agent、DE、domain worker提交和实际进程/目录 | pending |
| catalog SHA / v2应用与purpose / Direct状态 | pending |
| 账号A/B、盒子A/B、client1/2别名 | pending |
| 本地/在线模型实际ID、配置revision和可用性 | pending |
| 上一版配置/数据备份与恢复记录 | pending |

三层证据应可用时间和脱敏关联标识连接：UI 用户动作 → 主进程 operationId/requestId → SDK Direct 请求 → Agent验权 → DE job/event → 业务对象/文件。禁止保存账号密码、SSH密码、access/refresh token、设备私钥、工作区密钥或完整授权证明；也不能为观察证据新增 renderer 凭据读取或任意请求调试接口。

## 3. 前置数据与环境

| 编号 | 前置条件 | 缺失处理 |
|---|---|
| E-AUTH | 真实Consumer账号A已绑定盒子A；v2 `zhijun-desktop` / `zhijun.workspace` / DirectOnly可用；调试访问关闭 | 连接及所有业务均pending/blocked，禁止启用local-debug替代 |
| E-ISOLATE | 第二测试账号、第二授权client；所有权代次测试只用可回退的独立测试设备 | 跨账号/客户端/代次项blocked，不能用改JSON字段的合成测试替代真机 |
| E-MODEL-LOCAL | 盒端已有可运行的对话与资料处理模型，容量及调度可检查 | 手工功能可独立验收，模型生成/处理功能blocked |
| E-MODEL-ONLINE | 专用workspace内有获授权使用的外部provider与凭据；验收文本无敏感内容 | 在线真实推理/逐任务授权项blocked，不复制别的workspace凭据 |
| E-VOICE | 用户在场并明确点击录音；可用麦克风；盒端转写模型可运行；预先选定一段不含隐私的短句 | 未经用户点击不得自动请求权限或录音；设备/模型缺失时语音项blocked，文件上传不能替代 |
| D-TXT / D-TXT-V2 | 自制UTF-8文档，含唯一验收标记；V2只改一处；记录源文件SHA | 先准备样本，不用私有历史资料代替 |
| D-PDF / D-IMG / D-AUDIO | 可分享PDF、PNG/JPEG与短音频样本；可选短视频；记录内容来源、大小/SHA | 对应媒体/处理格式单列blocked，不以别的格式通过替代 |
| D-PART | 解析后有可预览子文件的资料样本 | 原件预览通过不能覆盖part路径 |
| D-BOUND | 524288+1字节文件；限额/限额+1专用样本；10次小上传 | 只在独立配额下执行，不挤占用户生产任务 |
| D-LIFECYCLE | 带 `ZJ-ACCEPT-0906-<run-id>` 前缀的资料、知识、认识、事项与目录；只清理这些对象 | 永久清除和全认识清理使用专用workspace，不清空真实用户数据 |
| E-RESTART | 可回退的验收实例或已有授权的维护窗口，版本/配置/数据备份已记录 | 不为了验收强停其他用户任务；相应项blocked |

## 4. 页面和路由覆盖

| 页面组件 | 路由（桌面使用hash） | 产品入口 | 主要用例 | UI / SDK / 盒端 |
|---|---|---|---|---|
| `OnboardingPage.vue` | `/onboarding` | 首次引导 | F-ONB-01 | pending / pending / pending |
| `TodayPage.vue` | `/` | 今日来信 | F-HOME-01 | pending / pending / pending |
| `ConversationPage.vue` | `/chat；/c/:conversationId；/onboarding/chat；/onboarding/c/:conversationId` | 对话及首次认识 | F-CHAT-01…04、F-VOICE-01、F-IMP-03、F-CHAR、F-LEARN、F-MEM、F-ROUTE | pending / pending / pending |
| `OntologyPage.vue` | `/me；/me/inbox` | 我的本体/收件箱 | F-ONTO-01…04 | pending / pending / pending |
| `CharterPage.vue` | `/me/charter` | 人生章程 | F-CHAR-01…03、F-EXP-02 | pending / pending / pending |
| `GrowthPage.vue` | `/judgments` | 判断 | F-JDG-01…03 | pending / pending / pending |
| `DataHubPage.vue` | `/data` | 资料与边界 | F-EXP-01、F-LIFE-04 | pending / pending / pending |
| `RawMaterialsPage.vue` | `/materials` | 原材料 | F-IMP-01、F-MAT-01、F-FOLDER-01 | pending / pending / pending |
| `MaterialDetailPage.vue` | `/materials/:materialId` | 资料详情 | F-IMP-02、F-KNOW-01、F-LIFE-01/03、F-MEDIA-01/02 | pending / pending / pending |
| `KnowledgePage.vue` | `/knowledge` | 知识档案 | F-KNOW-02、F-FOLDER-02、F-TAG-01 | pending / pending / pending |
| `KnowledgeEditPage.vue` | `/knowledge/new；/knowledge/:knowledgeId` | 知识新建/编辑 | F-KNOW-02…04、F-LIFE-02/03 | pending / pending / pending |
| `RecycleBinPage.vue` | `/recycle-bin` | 回收站 | F-LIFE-01…03 | pending / pending / pending |
| `SearchPage.vue` | `/search` | 搜索记忆 | F-SEARCH-01 | pending / pending / pending |
| `GraphPage.vue` | `/graph` | 关系图谱 | F-GRAPH-01 | pending / pending / pending |
| `SettingsPage.vue` | `/settings` | 偏好 | F-PREF-01/02、F-MODEL-01…04 | pending / pending / pending |

重定向另验：`/growth` → `/judgments`、未知路径 → `/`；登录前/失去v2能力时不得通过hash进入业务组件。每条动态路由至少用真实创建的当前主体ID打开一次，再用已回收/不存在/另一主体ID验证受控拒绝。

## 5. 真实功能与故障验收用例

以下 66 项均未填真实通过。动作里出现确认、发布、清除、在线授权时，使用对应产品的明确用户操作；只有允许的专用验收数据可执行破坏性分支。

### F-CONN-01 完整入口、导航和同一连接

- 前置：正式Electron；账号A/盒子A，v2注册与生产配置齐备。
- 操作：启动正式桌面→密码登录→选择盒子A→按次序点击五个主导航和偏好→从资料枢纽打开隐藏页面→返回首页。
- 通过标准：必须显示原产品布局；所有页面共用同一真实Direct会话与workspaceId；不能退回只读验收页或由页面另建身份。
- 证据：`cases/F-CONN-01/`；逐次截图；SDK连接数量与generation摘要；v2context匹配记录。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-CONN-02 首次连接、空闲心跳和健康提示

- 前置：F-CONN-01；盒端服务可检查。
- 操作：连接后静置至少65秒→回到今日来信与偏好刷新→确认10秒空闲心跳及最新context→检查健康/流水线状态。
- 通过标准：超过30秒租约仍可正常读写；只在v2context匹配后启用product；健康页不暴露其他workspace资料或任务ID。
- 证据：`cases/F-CONN-02/`；带时间戳的UI状态、context/心跳次数和服务状态摘要。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-ONB-01 首次引导和首次认识对话

- 前置：专用空workspace或未完成引导的账号；本地模型可用。
- 操作：打开首次引导→进入第一次认识→发送验收文本→查看小结→选择先使用稍后核对→重开应用。
- 通过标准：首次状态和对话持久化；可继续同一引导对话；完成后进入首页；不得清空已有用户资料来制造首次状态。
- 证据：`cases/F-ONB-01/`；首次/完成/重启后三组UI与业务记录ID摘要。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-HOME-01 今日来信、来源和行动入口

- 前置：已建立对话、事项、判断及一条可追溯资料；模型可用。
- 操作：打开今日来信→等待摘要刷新→逐个打开来源及主行动→从今日地图进入关联事项或对话。
- 通过标准：有真实来源和可用跳转；后台生成中、空状态和失败均真实显示；无旧账号缓存残留。
- 证据：`cases/F-HOME-01/`；来信/来源详情/跳转后页面截图与对应operation记录。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-CHAT-01 对话创建、列表、搜索、标题、归档和删除

- 前置：账号A；至少两条验收对话。
- 操作：新建两条对话→更改标题→搜索并打开指定对话→修改状态/归档→恢复可见状态→删除一条专用验收对话→刷新及重开。
- 通过标准：列表和详情一致；只影响指定验收对象；标题和状态持久；删除后旧详情不继续显示缓存。
- 证据：`cases/F-CHAT-01/`；各动作前后对象ID、UI截图、mutation结果和刷新结果。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-CHAT-02 真实聊天流、停止和恢复检查

- 前置：可用本地模型；固定无敏感提示词。
- 操作：发送需要分段回答的问题→观察至少两个真实增量→完成后刷新对话→再发长回答并点击停止→检查任务真实终态及已保存消息。
- 通过标准：首段无需等完整响应；顺序/UTF-8无损；完成消息不重复；停止请求不伪称远端同步写已终止；不自动重发用户消息。
- 证据：`cases/F-CHAT-02/`；首段/末段时刻、SSE事件seq摘要、任务终态、刷新后的消息截图。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-CHAT-03 回复辅助和判断建议

- 前置：有消息的验收对话；本地模型可用。
- 操作：打开回复辅助→生成建议→修改输入并再次请求→采纳到输入框后由用户发送；另生成判断候选。
- 通过标准：建议来自真实任务；采纳不自动发送；过期响应不覆盖新输入；失败有可恢复提示。
- 证据：`cases/F-CHAT-03/`；辅助请求与结果、采纳前后输入框、最终消息ID。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-CHAT-04 对话引用和附件预览授权

- 前置：已完成导入并可读的验收资料；含两个不同版本。
- 操作：在对话选择资料引用→查看引用预览→修改引用集合→检查文件授权提示→撤销/更改允许范围。
- 通过标准：只带选定资料和版本；未授权或版本变化时阻止旧预览；不自动附带同目录其他文件。
- 证据：`cases/F-CHAT-04/`；选中集合、授权界面、预览版本摘要和拒绝记录。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-VOICE-01 用户主动录音、盒端转写与草稿填入

- 前置：E-VOICE、F-CONN-01；真实转写模型可运行；记录系统/应用版本和无敏感测试短句。系统权限拒绝分支仅由用户在场操作，不自动改系统隐私设置。
- 操作：先正常打开对话，确认无自动权限弹窗；用户明确点击录音→允许麦克风→读短句→点击停止→观察转写中状态及最终草稿。保留已有草稿再重试一次，核对文字追加且不自动发送。另验证拒绝权限、静音录音、转写模型不可用、录音过程中断开/换盒/退出页面，以及等待转写时编辑草稿不会被覆盖、换会话后迟到结果丢弃。时长/容量边界用专用受控样本验证120秒和压缩录音2MiB上限，不自动录制用户环境声音。
- 通过标准：只有当前已就绪v2主frame的明确用户操作能申请系统麦克风；15秒授权窗口仅允许audio，camera/其他frame/origin拒绝；停止/取消/断代后音轨和音频处理资源释放。WAV经受控分片上传和`post_api_mindos_voice_transcribe`到当前workspace，盒端返回真实文本；结果只填草稿、不自动发送；静音/拒绝/失败不得显示假文本或假成功，用户未确认发送前无聊天消息写入。
- 证据：`cases/F-VOICE-01/`；用户点击和系统授权时点、录音/停止UI、WAV大小/SHA与任务标识、盒端模型及真实转写文本、草稿前后及停止后的音轨状态。麦克风原音不入仓；若用户未现场执行，这一用例保持pending/blocked。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-IMP-01 导入路径1：原材料文档/图片/音频

- 前置：D-TXT/D-PDF/D-IMG/D-AUDIO无敏感样本；本地处理模型可用。
- 操作：资料与边界→原材料→导入资料；分别用选择文件和页面支持的拖入方式导入样本→查看上传、排队、处理中及最终详情。
- 通过标准：真实字节经uploadCreate/chunk/complete和受控业务任务到盒端；名称、类型、大小/内容指纹匹配；完成后可读取内容和处理结果。
- 证据：`cases/F-IMP-01/`；源文件SHA/大小、上传ACK/总SHA、materialId、处理完成UI。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-IMP-02 导入路径2：已有资料的新版本

- 前置：F-IMP-01成功的文档；D-TXT-V2修改一处标记。
- 操作：原材料详情→新增版本→选择V2→查看版本影响预览→确认提交→打开版本列表并核对当前版本及旧来源。
- 通过标准：版本号/前后关联正确；旧引用不静默指向新内容；过期预览409后只重新预览，不自动重放确认。
- 证据：`cases/F-IMP-02/`；V1/V2指纹、影响预览revision、版本链和关联引用截图。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-IMP-03 导入路径3：对话附件批次

- 前置：新验收对话；两份小文件和已有短音频。
- 操作：对话附件入口选择多份文件→建立导入批次→逐文件上传并封口→等待附件处理→打开各附件预览→按提示确认文件访问范围。
- 通过标准：批次/单文件状态准确；封口不漏文件；文件经同一workspace处理；失败文件与成功文件区分，不将音频上传描述为实时语音识别。
- 证据：`cases/F-IMP-03/`；batchId/fileId映射、各文件SHA、封口/预览/授权事件和UI。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-IMP-04 导入失败、取消排队、继续和重试

- 前置：专用大于524288字节样本；可恢复的模型不可用场景。
- 操作：上传中制造断线→恢复后检查状态；对新资料取消排队后恢复；制造处理失败再手动重试；附件单文件及整批重试分别执行。
- 通过标准：不隐式重传写；同index同SHA可重复ACK但内容不同拒绝；只恢复明确任务；失败原因与实际服务一致。
- 证据：`cases/F-IMP-04/`；每次故障起止、received/nextIndex、任务状态变化、重试后的唯一对象。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-IMP-05 上传资源释放与大文件边界

- 前置：至少10次小文件验收；边界大小样本；独立测试配额。
- 操作：连续完成10次正常导入并结束业务任务→检查完成上传被释放→验证524288+1字节分片；执行文件大小上限和上限+1拒绝案例。
- 通过标准：第9/10次不因旧完成上传占满8个额度而失败；总大小/SHA匹配；越界在受控层拒绝；不清空用户已有文件腾配额。
- 证据：`cases/F-IMP-05/`；上传/释放记录、进程资源趋势、边界拒绝码和无副作用查询。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-MAT-01 资料列表、筛选、详情和处理结果

- 前置：有文档/图片/音频及不同处理状态的专用资料。
- 操作：按关键字、类型、状态、目录、标签切换筛选→打开详情→查看摘要、分析和相关资料→刷新并返回列表。
- 通过标准：筛选与详情对应真实对象；空结果准确；摘要/分析状态不以占位成功替代；来源版本可追溯。
- 证据：`cases/F-MAT-01/`；筛选参数、可见集合、详情/摘要/分析operation及截图。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-FOLDER-01 原材料目录完整操作

- 前置：仅使用ZJ-ACCEPT前缀目录和资料。
- 操作：创建根目录与子目录→重命名→移动验收资料→删除目录时分别选择移到根目录或指定目标目录→刷新树。
- 通过标准：RAW目录树和列表一致；拒绝环/非法父级；删除目录不误删资料；不同主体目录不互见。
- 证据：`cases/F-FOLDER-01/`；目录树前后、移动结果和两种删除分支。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-FOLDER-02 知识目录、移动和分类隔离

- 前置：两张验收知识卡与KNOWLEDGE目录。
- 操作：知识档案创建/改名目录→移动卡片→删除目录并转移卡片→切回原材料比较目录集合。
- 通过标准：知识/原材料scope隔离；不能用另一类或另一workspace目录ID移动对象。
- 证据：`cases/F-FOLDER-02/`；两类目录快照、成功移动及无权目标拒绝记录。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-TAG-01 资料和知识标签及建议确认

- 前置：至少一条带标签建议的验收资料及一张卡片。
- 操作：编辑资料标签→确认一条标签建议→修改知识标签→以标签筛选→刷新。
- 通过标准：只改指定对象；建议确认后持久且不重复；标签筛选和详情同步。
- 证据：`cases/F-TAG-01/`；标签前后值、suggestionId、筛选结果。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-KNOW-01 资料草稿卡片保存和确认

- 前置：分析完成且有draft-card的验收资料。
- 操作：资料详情打开草稿卡片→修改正文/标题→保存→确认成知识卡→从资料链接进入卡片。
- 通过标准：草稿保存与正式确认分离；确认返回唯一知识卡；重复确认用稳定请求标识去重，不生成副本。
- 证据：`cases/F-KNOW-01/`；draft revision、确认requestId摘要、知识卡ID、资料关联。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-KNOW-02 手动知识卡新建、编辑和确认

- 前置：新建知识入口；无需模型的手工内容。
- 操作：知识档案→新建知识卡片→填写标题正文/标签→保存草稿→刷新继续→确认→检索并打开。
- 通过标准：草稿/确认状态真实持久；未保存离开提示有效；取消离开不丢编辑；确认内容与输入一致。
- 证据：`cases/F-KNOW-02/`；草稿和确认截图、knowledgeId、刷新读回内容摘要。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-KNOW-03 已确认知识的修订草稿

- 前置：F-KNOW-02的已确认卡片。
- 操作：选择编辑已确认卡片→生成修订草稿→编辑并保存→重试失败草稿生成→确认更新→制造revision冲突并按界面重新读取。
- 通过标准：已确认正文不会被未确认草稿直接覆盖；409保留冲突上下文；确认不自动重复写；新旧版本关系可查。
- 证据：`cases/F-KNOW-03/`；修订revision、409 detail、最终确认内容与计数。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-KNOW-04 知识索引、来源及相关项

- 前置：两份有关联内容的资料和已确认卡；索引服务可用。
- 操作：编辑卡片来源→查看来源与相关项→执行索引重试→搜索独特标记→从结果回到来源。
- 通过标准：索引只包含可用/允许的本workspace内容；索引失败保留可见错误；回收源不继续被当成可用证据。
- 证据：`cases/F-KNOW-04/`；source引用版本、索引任务、搜索命中和来源截图。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-LIFE-01 原材料回收和恢复

- 前置：专用资料及由其派生的知识卡/引用。
- 操作：查看删除影响→回收指定资料→在普通列表/搜索/图谱/对话引用检查不可用→回收站打开→恢复→重新检查。
- 通过标准：影响预览准确；回收期间默认入口不再使用内容；恢复按服务实际规则显示重新处理/索引状态；不误报立即全部恢复。
- 证据：`cases/F-LIFE-01/`；回收前影响、回收后四入口、恢复状态与最终可读证据。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-LIFE-02 知识卡回收和恢复

- 前置：专用知识卡，带来源/关联项。
- 操作：查看影响→回收→验证默认列表、搜索、图谱和问答引用→回收站恢复→刷新。
- 通过标准：知识生命周期作用于同一对象；已回收卡不能通过旧链接绕过；恢复后允许状态与索引一致。
- 证据：`cases/F-LIFE-02/`；knowledgeId、影响revision、旧链接拒绝及恢复结果。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-LIFE-03 资料/知识永久清除和过期影响预览

- 前置：只允许专用验收对象；保留无敏感样本副本。
- 操作：分别回收一份验收资料和卡片→读取清除影响→制造依赖变化使旧预览过期→确认应被拒绝→重读影响后由验收人确认永久清除。
- 通过标准：旧预览409不自动重放；只删除指定验收对象；清除后详情、原文件、来源、搜索和图谱不可再访问；其他资料未变化。
- 证据：`cases/F-LIFE-03/`；确认前对象白名单、过期预览拒绝、最终清除回执及各入口复查。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-LIFE-04 删除全部认识及可选对话清理

- 前置：独立专用空白验收账号/workspace；不能使用用户日常workspace。
- 操作：先导出验收认识→资料与边界高级区打开全部认识清理→分别在两套独立验收数据验证保留对话/一并删除对话分支→重开。
- 通过标准：按产品勾选范围删除；资料原件和索引不被此操作误删；不能为了验收清空用户真实认识。前置账号不具备则blocked。
- 证据：`cases/F-LIFE-04/`；专用workspace确认记录、导出指纹、两分支前后对象计数和资料仍在证据。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-MEDIA-01 媒体路径1：资料原文件预览与保存

- 前置：有效PDF/PNG或JPEG/短音频；可选短视频；非渲染文档。
- 操作：资料详情分别打开PDF、图片、音频→音频播放/定位→选择保存原件→取消一次保存，再选择验收目标目录保存→核对SHA。
- 通过标准：真实blob分片；PDF/图片可见、音频可播放和seek；原生保存取消无成功提示；完整文件SHA匹配；不把不支持格式错误当通过。
- 证据：`cases/F-MEDIA-01/`；媒体截图/播放时点、Range 206、原件/保存SHA和取消记录。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-MEDIA-02 媒体路径2：解析子文件及句柄撤销

- 前置：含可展开图片/音频等part的验收资料。
- 操作：打开资料的指定part预览/保存→对照part文件→关闭预览再尝试旧句柄→断开并切换主体后重试旧媒体URL。
- 通过标准：partId属于该资料及主体；关闭/断代后旧句柄拒绝；Range越界416；不允许HTML/SVG可执行文档伪装为受支持媒体。
- 证据：`cases/F-MEDIA-02/`；父子ID关系、part SHA、close/断代拒绝和范围请求记录。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-EXP-01 导出类型1：全部/分区认识JSON

- 前置：已有已确认和未确认认识，至少两个分区。
- 操作：资料与边界→查看可导出的认识→导出全部认识JSON→仅选一分区再导出→检查文件可解析、确认状态和选区内容。
- 通过标准：原生保存真实产物；JSON只含允许导出范围；默认不混入其他workspace或未确认工作稿；取消保存不报告成功。
- 证据：`cases/F-EXP-01/`；全部/分区导出SHA、结构检查、源认识ID对照和原生取消。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-EXP-02 导出类型2：人生章程Markdown

- 前置：已发布章程和未发布修订草稿。
- 操作：人生章程→下载.md→检查中文、换行及当前文档；另从章程工作区导出界面实际显示的稿件→比对标题版本。
- 通过标准：UTF-8正文无损；明确区分正在读的已发布版/工作稿；不把仅弹出对话框作为下载完成。
- 证据：`cases/F-EXP-02/`；文件SHA、正文摘要/版本、保存路径只留本地记录不进IPC。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-EXP-03 导出类型3：资料原件及part二进制

- 前置：F-MEDIA-01/02所用可下载资料。
- 操作：分别保存一份完整原件和一个part→在系统文件管理器核对大小→计算源/目标SHA→下载中断后检查是否遗留可见半文件。
- 通过标准：完整文件/part各自校验；只有完整且校验通过后发布目标文件；中断不覆盖原有目标文件。
- 证据：`cases/F-EXP-03/`；SHA/大小对照、下载终态、无半文件及旧目标保留。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-MATTER-01 事项列表、创建、详情和状态

- 前置：专用事项标题与描述。
- 操作：从对话/事项入口新建→修改目标/状态→按状态筛选→打开详情→刷新及重开。
- 通过标准：事项状态和内容持久；只影响目标事项；详情不显示旧主体事项。
- 证据：`cases/F-MATTER-01/`；matterId、修改前后/重启后截图与operation。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-MATTER-02 对话关联事项、结果和历史

- 前置：一条验收对话及F-MATTER-01事项。
- 操作：关联/修改当前对话事项→记录本次结果→查看结果历史→从事项回到对话。
- 通过标准：对话与事项关系一致；结果只写一次且保留历史；旧版本冲突有明确提示。
- 证据：`cases/F-MATTER-02/`；关联ID、outcome ID、结果历史和刷新回读。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-MATTER-03 事项成果创建和更新

- 前置：专用事项；至少两种界面支持的成果内容。
- 操作：事项工作区创建成果→编辑标题/内容/状态→在成果列表定位→从对话重新打开同事项。
- 通过标准：成果归属正确；更新持久；跨事项/主体ID不能修改；失败不显示假保存。
- 证据：`cases/F-MATTER-03/`；artifactId/matterId映射、patch回执和读回。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-CHAR-01 章程建立、手动编辑与保存

- 前置：空验收章程或专用修订稿。
- 操作：我的本体→人生章程→选择手动或编辑正文→保存→返回并重开；有模型时再通过对话生成工作稿。
- 通过标准：手动路径不依赖模型伪造；对话建议来自真实推理；工作稿和稳定已发布章程分离。
- 证据：`cases/F-CHAR-01/`；workspaceId（章程稿件ID）/版本、手动保存/模型建议结果。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-CHAR-02 章程建议、合并、暂停和继续

- 前置：F-CHAR-01工作稿；本地模型。
- 操作：请求建议→核对差异→合并选定内容→暂停工作区→重新进入继续→取消一项建议。
- 通过标准：建议不会自行发布；合并范围准确；暂停状态持久且可继续；异步迟到结果不覆盖较新编辑。
- 证据：`cases/F-CHAR-02/`；建议/合并revision、暂停/继续截图、原稿与最终稿摘要。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-CHAR-03 章程发布和修订冲突

- 前置：已保存工作稿；可在第二窗口/受控测试制造旧revision。
- 操作：发布章程→主页/对话查看稳定版本→新建修订稿但不发布→校验稳定版未变→提交旧revision应409→读最新后人工决策。
- 通过标准：只有明确发布才改变稳定章程；冲突不会自动覆盖；相关对话使用正确已发布版本。
- 证据：`cases/F-CHAR-03/`；发布前后版本、未发布稿、409详情和引用版本。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-LEARN-01 学习开始、建议、提案和确认/拒绝

- 前置：有内容的对话；本地模型；独立学习主题。
- 操作：打开学习入口→开始主题→生成建议→提出学习提案→分别确认/拒绝→刷新状态。
- 通过标准：每个阶段和选择持久；提案不等于已确认知识；拒绝不产生正式认识；模型不可用显示失败。
- 证据：`cases/F-LEARN-01/`；学习状态机各步、提案ID、确认/拒绝后的资料/认识变化。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-MEM-01 记忆待核对、关注、草稿审核与忽略

- 前置：含可生成个人理解的验收对话；模型可用。
- 操作：查看待核对记忆→关注一条→审核草稿→忽略另一条→打开pending后执行pending-dismiss→刷新。
- 通过标准：忽略和确认语义不同；未确认理解不自动成为稳定记忆；处理过候选不重复弹出。
- 证据：`cases/F-MEM-01/`；候选ID、审核动作、pending列表前后及稳定认识对照。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-ONTO-01 本体列表、分区、信任和认识详情

- 前置：至少两分区、不同信任状态的验收认识。
- 操作：我的本体切换列表/图谱/分区/信任过滤→打开认识详情→手动新增一条→刷新统计。
- 通过标准：过滤和统计来自真实存储；详情证据及状态对应；新增对象归属当前workspace。
- 证据：`cases/F-ONTO-01/`；过滤集合、认识ID、统计前后与详情截图。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-ONTO-02 认识整理、合并和冲突提案

- 前置：可产生合并/冲突的专用认识；模型可用。
- 操作：运行整理→查看收件箱与提案→分别处理一项合并和冲突→重新整理/刷新。
- 通过标准：候选提案需要明确处理；被拒绝/已处理提案不重复落正式记录；处理版本冲突不自动重试写。
- 证据：`cases/F-ONTO-02/`；整理任务、proposal/conflict ID、选择前后认识关系。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-ONTO-03 认识复核和导出范围开关

- 前置：已确认及待核对认识各一。
- 操作：对认识执行界面提供的确认/修订/忽略等复核→更改可导出状态→在JSON投影与导出核对。
- 通过标准：复核记录真实；导出范围更改立即遵守；不把导出开关误当文件已经保存。
- 证据：`cases/F-ONTO-03/`；review回执、export状态、JSON投影前后。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-ONTO-04 对话与认识校准授权、提案和撤销

- 前置：有关联证据的认识和对话；本地模型。
- 操作：打开对话校准→确认授权范围→对指定认识生成校准提案→应用一次→撤销授权/关联→重试旧操作。
- 通过标准：只校准被选认识/对话；撤销后旧授权不可继续；来源版本可追溯，未选内容不带入模型。
- 证据：`cases/F-ONTO-04/`；授权和提案摘要、适用对象列表、撤销拒绝记录。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-JDG-01 手工判断记录与筛选

- 前置：专用判断标题、备选项和检验标准。
- 操作：判断→记下当下的判断→完成两步表单→保存→按状态查看→刷新。
- 通过标准：当时理由、选择和验证日期真实持久；不依赖模型才能完成手工路径。
- 证据：`cases/F-JDG-01/`；decisionId、输入/存储回读及状态筛选。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-JDG-02 对话判断草稿、建议、确认与丢弃

- 前置：两条验收对话；模型可用。
- 操作：在对话生成判断草稿与建议→编辑后一条确认写入判断簿→另一条丢弃→刷新草稿和判断列表。
- 通过标准：确认才新增正式判断；丢弃无正式记录；重复确认不重复写；候选错误不被吞成成功。
- 证据：`cases/F-JDG-02/`；draft/decision ID对应、丢弃无记录、唯一性检查。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-JDG-03 结果、回访和复盘

- 前置：F-JDG-01/02判断；可记录模拟业务结果的专用样本。
- 操作：判断簿→记结果→和知君回访→提交复盘→查看最近复盘和趋势。
- 通过标准：结果/复盘版本匹配；对话关联正确；过期修改409保留用户输入；趋势只计当前主体记录。
- 证据：`cases/F-JDG-03/`；结果、回访conversationId、review ID与重启后读回。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-SEARCH-01 跨资料/知识搜索与跳转

- 前置：含唯一标记的已确认知识及允许的可用资料。
- 操作：搜索标记词→分别打开原材料与知识结果→修改关键词→回收某结果后再次搜索。
- 通过标准：命中可追溯且不越权；回收/清除对象被排除；不把空结果当成检索完成的唯一证据。
- 证据：`cases/F-SEARCH-01/`；搜索输入/结果ID、跳转、生命周期前后对照。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-GRAPH-01 真实关系图谱、缩放和来源

- 前置：至少一条真实材料—知识关联及另一类支持关系。
- 操作：打开图谱→缩放/拖动→选择节点→查看关联边→从边或详情按钮跳转。
- 通过标准：显示真实关系；交互和详情ID准确；不可读/回收对象不泄漏节点标签。
- 证据：`cases/F-GRAPH-01/`；图谱全景、选中节点/边、详情与生命周期复查。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-PREF-01 关系、提醒和记忆整理偏好

- 前置：专用workspace；记录原偏好以便恢复。
- 操作：偏好→修改关系/提醒策略→保存→修改记忆整理策略→保存→离开返回及重开应用→恢复原设置。
- 通过标准：配置按workspace持久；保存失败不显示成功；另一主体偏好不改变。
- 证据：`cases/F-PREF-01/`；前/后/恢复后的策略摘要及重启读回。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-PREF-02 对话默认路由与记忆处理偏好

- 前置：本地模型可用；在线配置可选。
- 操作：修改全局对话默认路由→创建新对话→为旧对话设置独立路由、默认同意和处理方式→检查新旧对话差异。
- 通过标准：默认设置与单次/单对话授权严格区分；切到在线不自动批准当前未预览的内容外发。
- 证据：`cases/F-PREF-02/`；全局/单对话配置及新建对话有效值。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-ROUTE-01 本地任务路由和预览

- 前置：本地模型可用；选定资料与认识。
- 操作：对话选择本地→预览本次拟使用来源→发送→查看服务、用途、来源版本和实际模型结果。
- 通过标准：本地模式不发外部模型请求；预览与实际请求摘要匹配；非法/不可读来源拒绝。
- 证据：`cases/F-ROUTE-01/`；preview revision、来源集合、模型执行渠道与返回流。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-ROUTE-02 在线逐任务授权与章程例外

- 前置：获授权测试的外部provider已配置；无敏感样本；明确人工点击同意。
- 操作：切换在线→查看任务用途/服务/来源→先取消授权验证未调用→再明确批准选中来源→如章程冲突则走显示的例外流程→发送。
- 通过标准：配置在线不等于本次授权；仅提交批准的精确请求和来源版本；不自动上传原件；账单/调用侧可核对真实请求。
- 证据：`cases/F-ROUTE-02/`；授权前无外发证据、显式授权截图、请求摘要/服务ID/调用结果（不记录密钥）。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-ROUTE-03 待处理预览、恢复、撤销和配置变更

- 前置：F-ROUTE-02；可更新来源或provider revision。
- 操作：打开pending预览→恢复→撤销授权→用旧revision尝试继续→更改来源/模型配置后重新预览并由用户重新决定。
- 通过标准：旧grant、旧服务配置或旧来源版本不可继续；409可有限重预览但不自动重放消息/写；撤销后的任务不给假成功。
- 证据：`cases/F-ROUTE-03/`；pending ID、revoke回执、各类过期拒绝及新preview。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-MODEL-01 对话模型配置、测试与暂停在线

- 前置：已存在可用本地模型；专用workspace模型配置。
- 操作：偏好读取对话配置→更改受支持模型参数→保存→测试真实推理→开启/暂停在线通道→刷新。
- 通过标准：测试返回实际服务结果/失败原因；密钥不回显；暂停在线后新请求不继续用外部服务；恢复前配置有记录。
- 证据：`cases/F-MODEL-01/`；配置revision、测试响应摘要、暂停前后执行渠道。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-MODEL-02 本地资料模型配置和真实处理测试

- 前置：盒子有可运行的Ollama/资料模型；固定无敏感样本。
- 操作：偏好读取资料处理配置/模型列表→修改并保存→执行连接测试与真实推理测试→导入样本检查处理链。
- 通过标准：配置保存、连通性测试和实际推理分别有证据；模型缺失不显示通过；不复制一套未经授权的全局模型索引。
- 证据：`cases/F-MODEL-02/`；模型ID/revision、连接及推理结果、样本处理最终状态。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-MODEL-03 外部provider完整管理

- 前置：专用测试provider配置/可使用的密钥；不复用其他workspace密钥。
- 操作：新增provider→读取模型列表→修改配置→激活→真实测试→用过期revision编辑/删除应冲突→停用并删除专用provider。
- 通过标准：密钥写入安全存储且读回脱敏；操作均受workspace权限和revision控制；provider管理不替代逐任务内容授权。
- 证据：`cases/F-MODEL-03/`；脱敏provider列表、revision冲突、真实测试、清理后列表。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### F-MODEL-04 模型下载/加载/卸载、任务取消与监控

- 前置：专用可测试模型；盒端空间/内存足够；当前生产模型不受影响。
- 操作：读取模型和任务列表→对专用模型执行下载、加载、卸载→观察queued/running/终态→另起可取消任务并取消→刷新资源/流水线监控。
- 通过标准：任务状态反映真实模型操作；缓存命中不能证明冷下载；取消保留实际cancelRequested/终态；监控不泄漏其他workspace任务内容。
- 证据：`cases/F-MODEL-04/`；模型文件/运行状态、jobId与终态、资源曲线、缓存/冷下载说明。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### X-BOUND-01 渲染边界、任意操作和非法参数

- 前置：隔离正式配置测试会话；不向生产注入未审核代码。
- 操作：以独立安全测试工具检查无Node/任意fetch；发送未列operation、路径/headers注入、重复query、超大body、非法chunk/Range。
- 通过标准：主进程/Agent/DE分别拒绝；renderer没有票据、主机路径或任意URL调用；拒绝无业务副作用。
- 证据：`cases/X-BOUND-01/`；工具版本、拒绝码、对应层次记录；单列是否真实SDK负向。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### X-AUTH-01 不同账号/盒子/所有权代次隔离

- 前置：第二个合法测试账号/盒子或独立可回退所有权测试设备。
- 操作：A创建标记内容→退出切B→验证A缓存和详情/媒体句柄不可用→再回A；独立测试设备改变ownershipEpoch后重试旧会话。
- 通过标准：UI卸载旧主体；服务拒绝旧主体资源；新Owner不继承旧global数据；不改变用户日常盒子所有权进行破坏性验收。
- 证据：`cases/X-AUTH-01/`；A/B主体仅用别名、拒绝请求、ownershipEpoch前后摘要。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### X-AUTH-02 同账号不同client和同client新session

- 前置：两台授权客户端或隔离安装；同账号同盒子。
- 操作：client1创建任务/上传/blob→client2尝试已知ID与相同requestId→client1重连新session按明确恢复规则检查完成上传/幂等任务。
- 通过标准：跨client不可读/取消/拼接临时资源；同client恢复必须经服务端重新授权；主进程代次检查不能代替盒端绑定。
- 证据：`cases/X-AUTH-02/`；client别名、资源绑定摘要、跨client拒绝及允许恢复的准确范围。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### X-AUTH-03 授权撤销、心跳失败和租约到期

- 前置：专用授权会话；已启动真实流和一个后台任务。
- 操作：撤销测试授权或使context验证失败→观察desktop离开ready并清列表/媒体→验证停止续租；另断网超过30秒检查盒端任务。
- 通过标准：不持续续租；未签名/伪造证明拒绝；后台不无限使用旧授权；写结果不确定时显示interrupted/unknown，不伪称已回滚。
- 证据：`cases/X-AUTH-03/`；撤销时刻、最后心跳、UI代次、任务终态及恢复后核对。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### X-FAULT-01 GET/流/写入中断、超时和取消

- 前置：专用读任务与可观察唯一写入任务。
- 操作：分别在读取前、首段后、提交写入后断开连接→恢复并人工检查对象/任务→用原requestId查询或显式恢复；取消慢模型任务。
- 通过标准：不自动重放写；HTTPX断开不视为同步handler已停；重复结果无额外副作用；迟到响应不投递新代次。
- 证据：`cases/X-FAULT-01/`；故障时点、requestId摘要、unknown/interrupted状态及最终唯一性。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### X-FAULT-02 资源背压、队列满和连续使用

- 前置：隔离负载样本；不会挤占生产资料处理。
- 操作：启动超过4并行/8排队任务→观察有界拒绝→同时持续poll→消费流中途关闭；连续创建/关闭媒体和保存对话框。
- 通过标准：队列/内存有界；取消满队列无死锁；上传/媒体/preview资源能回收；保存对话框未关闭前不能无限打开新对话框。
- 证据：`cases/X-FAULT-02/`；任务并发/内存趋势、拒绝码、流关闭时间、资源回收记录。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### X-RESTART-01 桌面重启持久化和缓存清理

- 前置：已建立对话、认识、事项、章程、偏好和资料。
- 操作：记录验收对象ID→正常退出桌面→启动并重新登录同账号/盒子→依次打开各对象→再切主体。
- 通过标准：业务数据在盒端持久；重启不靠renderer缓存假恢复；凭据按既定策略重新登录；切主体无旧对象残留。
- 证据：`cases/X-RESTART-01/`；退出/登录/各对象读回UI、盒端记录与缓存隔离。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### X-RESTART-02 DE/worker重启与未完成写结果

- 前置：专用可中断任务；已批准的服务维护窗口/独立验收实例。
- 操作：同时保留已完成对象和queued/running任务→重启worker/DE→重新连接→查询旧任务与对象→检查临时文件和key目录清理。
- 通过标准：完成数据不丢；未完成任务标interrupted，不自动重放写；同workspace只一个worker；未知提交结果人工核对；原用户目录不触及。
- 证据：`cases/X-RESTART-02/`；前后PID/源码hash、任务tombstone、对象读回、runtime目录和服务日志摘要。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

### X-RESTART-03 服务发布与真实源码对应

- 前置：完成部署后只读检查；无并发发布覆盖。
- 操作：记录桌面/Agent/DE/domain/catalog hash与配置版本→确认服务cwd/PID及drop-in→完成一项真实业务→再次核验版本未被其他发布替换。
- 通过标准：源码/清单与实际运行一致；允许回退且不开启local-debug；没有把旧实例通过当新版本通过。
- 证据：`cases/X-RESTART-03/`；版本manifest、服务active/NRestarts、最后业务证据与复核时间。

| 真实UI | 真实SDK | 真实盒端业务 | 结果/缺项/清理 |
|---|---|---|---|
| pending | pending | pending | pending |

## 6. 语音输入实现与验收边界

原Web Composer使用浏览器Web Speech，识别结果只填入输入框、不自动发送。桌面已接入用户点击录音、停止后生成WAV、通过同一SDK连接送到盒端转写、文本填入草稿的完整代码路径，真实执行按F-VOICE-01回填。录音最多120秒，压缩录音缓存最多2MiB，转换后的16kHz单声道PCM16 WAV最多3,840,044字节；这是停止后返回文本的语音输入，尚未实现边说边显示识别片段的连续流式转写。音频附件导入F-IMP-03和媒体播放F-MEDIA-01分别验收，不能覆盖麦克风采集或真实转写。

| 编号 | 实测步骤 | 必需证据 | 结果 |
|---|---|---|---|
| L-VOICE-01 | 用户在场按F-VOICE-01执行；启动/导航不触发权限；只有点击录音申请权限；停止后真实转写 | 采集/转写时间、准确文本、停止/断代/断网记录，未自动发送的草稿截图 | pending；代码与模拟权限测试不是实际采集证据 |
| L-VOICE-02 | 核对产品对“录音中”和“转写中”的准确说明；记录停止后返回文本的时延 | 界面截图与时间戳，不把既有录音上传或音频播放标为语音输入通过 | 连续流式识别尚未实现；停止后转写真机待验收 |
| L-VOICE-03 | 开发与正式打包分别核验NSMicrophoneUsageDescription；不在应用启动时调用系统权限探测 | 开发Electron包用途key、打包配置和实际发布包Info.plist；系统授权仅用户明确点击时验证 | 开发key及打包配置已只读/代码检查；发布包及真实权限行为pending |

## 7. 170个operation逐项回填索引

每行必须关联至少一次该具体operation的真实传输及业务结果；同组用例通过不会自动覆盖组内没有实际调用的端点。纯查询需至少一次非空/有意义内容校验；错误/取消端点需真实触发相应分支。若界面入口实际不可达，记录具体入口差异为fail/blocked，不用直接HTTP调用假装UI覆盖。网关10条传输路由、v2context/心跳与三个导出类别另由前述用例覆盖。

| # | operationId | 受控业务目标 | body → response | 验收用例 | 真实UI | 真实SDK | 真实盒端 | 证据/问题 |
|---|---|---|---|---|---|---|---|---|
| 001 | `get_api_health` | `GET /api/health` | none → json | F-CONN-02 | pending | pending | pending | pending |
| 002 | `patch_api_mindos_artifacts_artifact_id` | `PATCH /api/mindos/artifacts/{artifactId}` | json → json | F-MATTER-03 | pending | pending | pending | pending |
| 003 | `get_api_mindos_conversations` | `GET /api/mindos/conversations` | none → json | F-CHAT-01 | pending | pending | pending | pending |
| 004 | `post_api_mindos_conversations` | `POST /api/mindos/conversations` | json → json | F-CHAT-01 | pending | pending | pending | pending |
| 005 | `delete_api_mindos_conversations_conversation_id` | `DELETE /api/mindos/conversations/{conversationId}` | none → json | F-CHAT-01 | pending | pending | pending | pending |
| 006 | `get_api_mindos_conversations_conversation_id` | `GET /api/mindos/conversations/{conversationId}` | none → json | F-CHAT-01 | pending | pending | pending | pending |
| 007 | `patch_api_mindos_conversations_conversation_id` | `PATCH /api/mindos/conversations/{conversationId}` | json → json | F-CHAT-01 | pending | pending | pending | pending |
| 008 | `get_api_mindos_conversations_conversation_id_charter` | `GET /api/mindos/conversations/{conversationId}/charter` | none → json | F-CHAR-01 | pending | pending | pending | pending |
| 009 | `put_api_mindos_conversations_conversation_id_charter_workspace_workspace_id` | `PUT /api/mindos/conversations/{conversationId}/charter/workspace/{workspaceId}` | json → json | F-CHAR-01 | pending | pending | pending | pending |
| 010 | `post_api_mindos_conversations_conversation_id_charter_workspace_workspace_id_merge` | `POST /api/mindos/conversations/{conversationId}/charter/workspace/{workspaceId}/merge` | json → json | F-CHAR-02 | pending | pending | pending | pending |
| 011 | `post_api_mindos_conversations_conversation_id_charter_workspace_workspace_id_pause` | `POST /api/mindos/conversations/{conversationId}/charter/workspace/{workspaceId}/pause` | json → json | F-CHAR-02 | pending | pending | pending | pending |
| 012 | `post_api_mindos_conversations_conversation_id_charter_workspace_workspace_id_publish` | `POST /api/mindos/conversations/{conversationId}/charter/workspace/{workspaceId}/publish` | json → json | F-CHAR-03 | pending | pending | pending | pending |
| 013 | `post_api_mindos_conversations_conversation_id_charter_workspace_workspace_id_suggest` | `POST /api/mindos/conversations/{conversationId}/charter/workspace/{workspaceId}/suggest` | json → json | F-CHAR-02 | pending | pending | pending | pending |
| 014 | `post_api_mindos_conversations_conversation_id_charter_workspace_start` | `POST /api/mindos/conversations/{conversationId}/charter/workspace/start` | json → json | F-CHAR-01 | pending | pending | pending | pending |
| 015 | `get_api_mindos_conversations_conversation_id_decision_draft` | `GET /api/mindos/conversations/{conversationId}/decision-draft` | none → json | F-JDG-02 | pending | pending | pending | pending |
| 016 | `post_api_mindos_conversations_conversation_id_decision_draft_confirm` | `POST /api/mindos/conversations/{conversationId}/decision-draft/confirm` | json → json | F-JDG-02 | pending | pending | pending | pending |
| 017 | `post_api_mindos_conversations_conversation_id_decision_draft_discard` | `POST /api/mindos/conversations/{conversationId}/decision-draft/discard` | json → json | F-JDG-02 | pending | pending | pending | pending |
| 018 | `post_api_mindos_conversations_conversation_id_decision_draft_suggestions` | `POST /api/mindos/conversations/{conversationId}/decision-draft/suggestions` | json → json | F-JDG-02 | pending | pending | pending | pending |
| 019 | `post_api_mindos_conversations_conversation_id_file_consent` | `POST /api/mindos/conversations/{conversationId}/file-consent` | json → json | F-CHAT-04 / F-IMP-03 | pending | pending | pending | pending |
| 020 | `get_api_mindos_conversations_conversation_id_files_material_id_preview` | `GET /api/mindos/conversations/{conversationId}/files/{materialId}/preview` | none → json | F-CHAT-04 / F-IMP-03 | pending | pending | pending | pending |
| 021 | `get_api_mindos_conversations_conversation_id_imports` | `GET /api/mindos/conversations/{conversationId}/imports` | none → json | F-IMP-03 | pending | pending | pending | pending |
| 022 | `post_api_mindos_conversations_conversation_id_imports` | `POST /api/mindos/conversations/{conversationId}/imports` | json → json | F-IMP-03 | pending | pending | pending | pending |
| 023 | `post_api_mindos_conversations_conversation_id_imports_batch_id_files_file_id` | `POST /api/mindos/conversations/{conversationId}/imports/{batchId}/files/{fileId}` | multipart → json | F-IMP-03 | pending | pending | pending | pending |
| 024 | `post_api_mindos_conversations_conversation_id_imports_batch_id_files_file_id_failed` | `POST /api/mindos/conversations/{conversationId}/imports/{batchId}/files/{fileId}/failed` | json → json | F-IMP-04 | pending | pending | pending | pending |
| 025 | `post_api_mindos_conversations_conversation_id_imports_batch_id_files_file_id_retry` | `POST /api/mindos/conversations/{conversationId}/imports/{batchId}/files/{fileId}/retry` | json → json | F-IMP-04 | pending | pending | pending | pending |
| 026 | `post_api_mindos_conversations_conversation_id_imports_batch_id_retry` | `POST /api/mindos/conversations/{conversationId}/imports/{batchId}/retry` | json → json | F-IMP-04 | pending | pending | pending | pending |
| 027 | `post_api_mindos_conversations_conversation_id_imports_batch_id_seal` | `POST /api/mindos/conversations/{conversationId}/imports/{batchId}/seal` | json → json | F-IMP-03 | pending | pending | pending | pending |
| 028 | `get_api_mindos_conversations_conversation_id_learning` | `GET /api/mindos/conversations/{conversationId}/learning` | none → json | F-LEARN-01 | pending | pending | pending | pending |
| 029 | `post_api_mindos_conversations_conversation_id_learning_propose` | `POST /api/mindos/conversations/{conversationId}/learning/propose` | json → json | F-LEARN-01 | pending | pending | pending | pending |
| 030 | `post_api_mindos_conversations_conversation_id_learning_resolve` | `POST /api/mindos/conversations/{conversationId}/learning/resolve` | json → json | F-LEARN-01 | pending | pending | pending | pending |
| 031 | `post_api_mindos_conversations_conversation_id_learning_start` | `POST /api/mindos/conversations/{conversationId}/learning/start` | json → json | F-LEARN-01 | pending | pending | pending | pending |
| 032 | `post_api_mindos_conversations_conversation_id_learning_suggest` | `POST /api/mindos/conversations/{conversationId}/learning/suggest` | json → json | F-LEARN-01 | pending | pending | pending | pending |
| 033 | `get_api_mindos_conversations_conversation_id_matter` | `GET /api/mindos/conversations/{conversationId}/matter` | none → json | F-MATTER-02 | pending | pending | pending | pending |
| 034 | `put_api_mindos_conversations_conversation_id_matter` | `PUT /api/mindos/conversations/{conversationId}/matter` | json → json | F-MATTER-02 | pending | pending | pending | pending |
| 035 | `post_api_mindos_conversations_conversation_id_memory_attention` | `POST /api/mindos/conversations/{conversationId}/memory/attention` | json → json | F-MEM-01 | pending | pending | pending | pending |
| 036 | `post_api_mindos_conversations_conversation_id_memory_dismiss` | `POST /api/mindos/conversations/{conversationId}/memory/dismiss` | json → json | F-MEM-01 | pending | pending | pending | pending |
| 037 | `post_api_mindos_conversations_conversation_id_memory_draft_review` | `POST /api/mindos/conversations/{conversationId}/memory/draft-review` | json → json | F-MEM-01 | pending | pending | pending | pending |
| 038 | `get_api_mindos_conversations_conversation_id_memory_pending` | `GET /api/mindos/conversations/{conversationId}/memory/pending` | none → json | F-MEM-01 | pending | pending | pending | pending |
| 039 | `post_api_mindos_conversations_conversation_id_memory_pending_dismiss` | `POST /api/mindos/conversations/{conversationId}/memory/pending-dismiss` | json → json | F-MEM-01 | pending | pending | pending | pending |
| 040 | `post_api_mindos_conversations_conversation_id_messages` | `POST /api/mindos/conversations/{conversationId}/messages` | json → sse | F-CHAT-02 | pending | pending | pending | pending |
| 041 | `post_api_mindos_conversations_conversation_id_outcome` | `POST /api/mindos/conversations/{conversationId}/outcome` | json → json | F-MATTER-02 | pending | pending | pending | pending |
| 042 | `get_api_mindos_conversations_conversation_id_outcomes` | `GET /api/mindos/conversations/{conversationId}/outcomes` | none → json | F-MATTER-02 | pending | pending | pending | pending |
| 043 | `put_api_mindos_conversations_conversation_id_references` | `PUT /api/mindos/conversations/{conversationId}/references` | json → json | F-CHAT-04 / F-IMP-03 | pending | pending | pending | pending |
| 044 | `get_api_mindos_conversations_conversation_id_reply_assistance` | `GET /api/mindos/conversations/{conversationId}/reply-assistance` | none → json | F-CHAT-03 | pending | pending | pending | pending |
| 045 | `post_api_mindos_conversations_conversation_id_reply_assistance` | `POST /api/mindos/conversations/{conversationId}/reply-assistance` | json → json | F-CHAT-03 | pending | pending | pending | pending |
| 046 | `get_api_mindos_conversations_conversation_id_routing` | `GET /api/mindos/conversations/{conversationId}/routing` | none → json | F-ROUTE-01 | pending | pending | pending | pending |
| 047 | `put_api_mindos_conversations_conversation_id_routing` | `PUT /api/mindos/conversations/{conversationId}/routing` | json → json | F-ROUTE-01 | pending | pending | pending | pending |
| 048 | `post_api_mindos_conversations_conversation_id_routing_charter_exception` | `POST /api/mindos/conversations/{conversationId}/routing/charter-exception` | json → json | F-CHAR-01 | pending | pending | pending | pending |
| 049 | `put_api_mindos_conversations_conversation_id_routing_default_consent` | `PUT /api/mindos/conversations/{conversationId}/routing/default-consent` | json → json | F-PREF-02 | pending | pending | pending | pending |
| 050 | `post_api_mindos_conversations_conversation_id_routing_grant` | `POST /api/mindos/conversations/{conversationId}/routing/grant` | json → json | F-ROUTE-02 | pending | pending | pending | pending |
| 051 | `put_api_mindos_conversations_conversation_id_routing_handling` | `PUT /api/mindos/conversations/{conversationId}/routing/handling` | json → json | F-PREF-02 | pending | pending | pending | pending |
| 052 | `get_api_mindos_conversations_conversation_id_routing_pending_preview_id` | `GET /api/mindos/conversations/{conversationId}/routing/pending/{previewId}` | none → json | F-ROUTE-03 | pending | pending | pending | pending |
| 053 | `post_api_mindos_conversations_conversation_id_routing_preview` | `POST /api/mindos/conversations/{conversationId}/routing/preview` | json → json | F-ROUTE-01 | pending | pending | pending | pending |
| 054 | `post_api_mindos_conversations_conversation_id_routing_resume` | `POST /api/mindos/conversations/{conversationId}/routing/resume` | json → json | F-ROUTE-03 | pending | pending | pending | pending |
| 055 | `post_api_mindos_conversations_conversation_id_routing_revoke` | `POST /api/mindos/conversations/{conversationId}/routing/revoke` | json → json | F-ROUTE-03 | pending | pending | pending | pending |
| 056 | `get_api_mindos_conversations_routing_default` | `GET /api/mindos/conversations/routing/default` | none → json | F-PREF-02 | pending | pending | pending | pending |
| 057 | `put_api_mindos_conversations_routing_default` | `PUT /api/mindos/conversations/routing/default` | json → json | F-PREF-02 | pending | pending | pending | pending |
| 058 | `get_api_mindos_folders` | `GET /api/mindos/folders` | none → json | F-FOLDER-01 / F-FOLDER-02 | pending | pending | pending | pending |
| 059 | `post_api_mindos_folders` | `POST /api/mindos/folders` | json → json | F-FOLDER-01 / F-FOLDER-02 | pending | pending | pending | pending |
| 060 | `delete_api_mindos_folders_folder_id` | `DELETE /api/mindos/folders/{folderId}` | none → json | F-FOLDER-01 / F-FOLDER-02 | pending | pending | pending | pending |
| 061 | `patch_api_mindos_folders_folder_id` | `PATCH /api/mindos/folders/{folderId}` | json → json | F-FOLDER-01 / F-FOLDER-02 | pending | pending | pending | pending |
| 062 | `get_api_mindos_graph` | `GET /api/mindos/graph` | none → json | F-GRAPH-01 | pending | pending | pending | pending |
| 063 | `get_api_mindos_growth_charter` | `GET /api/mindos/growth/charter` | none → json | F-CHAR-01 | pending | pending | pending | pending |
| 064 | `get_api_mindos_growth_decisions` | `GET /api/mindos/growth/decisions` | none → json | F-JDG-01 | pending | pending | pending | pending |
| 065 | `post_api_mindos_growth_decisions` | `POST /api/mindos/growth/decisions` | json → json | F-JDG-01 | pending | pending | pending | pending |
| 066 | `post_api_mindos_growth_decisions_decision_id_outcome` | `POST /api/mindos/growth/decisions/{decisionId}/outcome` | json → json | F-JDG-03 | pending | pending | pending | pending |
| 067 | `post_api_mindos_growth_reviews` | `POST /api/mindos/growth/reviews` | json → json | F-JDG-03 | pending | pending | pending | pending |
| 068 | `get_api_mindos_knowledge` | `GET /api/mindos/knowledge` | none → json | F-KNOW-02 | pending | pending | pending | pending |
| 069 | `post_api_mindos_knowledge` | `POST /api/mindos/knowledge` | json → json | F-KNOW-02 | pending | pending | pending | pending |
| 070 | `get_api_mindos_knowledge_knowledge_id` | `GET /api/mindos/knowledge/{knowledgeId}` | none → json | F-KNOW-02 | pending | pending | pending | pending |
| 071 | `put_api_mindos_knowledge_knowledge_id` | `PUT /api/mindos/knowledge/{knowledgeId}` | json → json | F-KNOW-02 | pending | pending | pending | pending |
| 072 | `post_api_mindos_knowledge_knowledge_id_confirm` | `POST /api/mindos/knowledge/{knowledgeId}/confirm` | json → json | F-KNOW-02 | pending | pending | pending | pending |
| 073 | `get_api_mindos_knowledge_knowledge_id_deletion_impact` | `GET /api/mindos/knowledge/{knowledgeId}/deletion-impact` | none → json | F-LIFE-02 | pending | pending | pending | pending |
| 074 | `get_api_mindos_knowledge_knowledge_id_edit_draft` | `GET /api/mindos/knowledge/{knowledgeId}/edit-draft` | none → json | F-KNOW-03 | pending | pending | pending | pending |
| 075 | `post_api_mindos_knowledge_knowledge_id_edit_draft` | `POST /api/mindos/knowledge/{knowledgeId}/edit-draft` | json → json | F-KNOW-03 | pending | pending | pending | pending |
| 076 | `put_api_mindos_knowledge_knowledge_id_edit_draft` | `PUT /api/mindos/knowledge/{knowledgeId}/edit-draft` | json → json | F-KNOW-03 | pending | pending | pending | pending |
| 077 | `post_api_mindos_knowledge_knowledge_id_edit_draft_confirm` | `POST /api/mindos/knowledge/{knowledgeId}/edit-draft/confirm` | json → json | F-KNOW-03 | pending | pending | pending | pending |
| 078 | `post_api_mindos_knowledge_knowledge_id_edit_draft_retry` | `POST /api/mindos/knowledge/{knowledgeId}/edit-draft/retry` | json → json | F-KNOW-03 | pending | pending | pending | pending |
| 079 | `post_api_mindos_knowledge_knowledge_id_move` | `POST /api/mindos/knowledge/{knowledgeId}/move` | json → json | F-FOLDER-02 | pending | pending | pending | pending |
| 080 | `post_api_mindos_knowledge_knowledge_id_purge` | `POST /api/mindos/knowledge/{knowledgeId}/purge` | json → json | F-LIFE-03 | pending | pending | pending | pending |
| 081 | `post_api_mindos_knowledge_knowledge_id_recycle` | `POST /api/mindos/knowledge/{knowledgeId}/recycle` | json → json | F-LIFE-02 | pending | pending | pending | pending |
| 082 | `get_api_mindos_knowledge_knowledge_id_related` | `GET /api/mindos/knowledge/{knowledgeId}/related` | none → json | F-KNOW-04 | pending | pending | pending | pending |
| 083 | `post_api_mindos_knowledge_knowledge_id_retry_index` | `POST /api/mindos/knowledge/{knowledgeId}/retry-index` | json → json | F-KNOW-04 | pending | pending | pending | pending |
| 084 | `get_api_mindos_knowledge_knowledge_id_sources` | `GET /api/mindos/knowledge/{knowledgeId}/sources` | none → json | F-KNOW-04 | pending | pending | pending | pending |
| 085 | `put_api_mindos_knowledge_knowledge_id_sources` | `PUT /api/mindos/knowledge/{knowledgeId}/sources` | json → json | F-KNOW-04 | pending | pending | pending | pending |
| 086 | `post_api_mindos_knowledge_knowledge_id_tags` | `POST /api/mindos/knowledge/{knowledgeId}/tags` | json → json | F-TAG-01 | pending | pending | pending | pending |
| 087 | `post_api_mindos_knowledge_knowledge_id_unrecycle` | `POST /api/mindos/knowledge/{knowledgeId}/unrecycle` | json → json | F-LIFE-02 | pending | pending | pending | pending |
| 088 | `get_api_mindos_materials` | `GET /api/mindos/materials` | none → json | F-MAT-01 | pending | pending | pending | pending |
| 089 | `get_api_mindos_materials_material_id` | `GET /api/mindos/materials/{materialId}` | none → json | F-MAT-01 | pending | pending | pending | pending |
| 090 | `get_api_mindos_materials_material_id_analysis` | `GET /api/mindos/materials/{materialId}/analysis` | none → json | F-MAT-01 | pending | pending | pending | pending |
| 091 | `get_api_mindos_materials_material_id_deletion_impact` | `GET /api/mindos/materials/{materialId}/deletion-impact` | none → json | F-LIFE-01 | pending | pending | pending | pending |
| 092 | `get_api_mindos_materials_material_id_draft_card` | `GET /api/mindos/materials/{materialId}/draft-card` | none → json | F-KNOW-01 | pending | pending | pending | pending |
| 093 | `put_api_mindos_materials_material_id_draft_card` | `PUT /api/mindos/materials/{materialId}/draft-card` | json → json | F-KNOW-01 | pending | pending | pending | pending |
| 094 | `post_api_mindos_materials_material_id_draft_card_confirm` | `POST /api/mindos/materials/{materialId}/draft-card/confirm` | json → json | F-KNOW-01 | pending | pending | pending | pending |
| 095 | `get_api_mindos_materials_material_id_file` | `GET /api/mindos/materials/{materialId}/file` | none → bytes | F-MEDIA-01 / F-EXP-03 | pending | pending | pending | pending |
| 096 | `post_api_mindos_materials_material_id_move` | `POST /api/mindos/materials/{materialId}/move` | json → json | F-FOLDER-01 | pending | pending | pending | pending |
| 097 | `get_api_mindos_materials_material_id_parts_part_id_file` | `GET /api/mindos/materials/{materialId}/parts/{partId}/file` | none → bytes | F-MEDIA-02 / F-EXP-03 | pending | pending | pending | pending |
| 098 | `post_api_mindos_materials_material_id_purge` | `POST /api/mindos/materials/{materialId}/purge` | json → json | F-LIFE-03 | pending | pending | pending | pending |
| 099 | `delete_api_mindos_materials_material_id_queue` | `DELETE /api/mindos/materials/{materialId}/queue` | none → json | F-IMP-04 | pending | pending | pending | pending |
| 100 | `post_api_mindos_materials_material_id_recycle` | `POST /api/mindos/materials/{materialId}/recycle` | json → json | F-LIFE-01 | pending | pending | pending | pending |
| 101 | `post_api_mindos_materials_material_id_regenerate` | `POST /api/mindos/materials/{materialId}/regenerate` | json → json | F-IMP-04 | pending | pending | pending | pending |
| 102 | `get_api_mindos_materials_material_id_related` | `GET /api/mindos/materials/{materialId}/related` | none → json | F-MAT-01 | pending | pending | pending | pending |
| 103 | `get_api_mindos_materials_material_id_summary` | `GET /api/mindos/materials/{materialId}/summary` | none → json | F-MAT-01 | pending | pending | pending | pending |
| 104 | `post_api_mindos_materials_material_id_tag_suggestions_suggestion_id_confirm` | `POST /api/mindos/materials/{materialId}/tag-suggestions/{suggestionId}/confirm` | json → json | F-TAG-01 | pending | pending | pending | pending |
| 105 | `post_api_mindos_materials_material_id_tags` | `POST /api/mindos/materials/{materialId}/tags` | json → json | F-TAG-01 | pending | pending | pending | pending |
| 106 | `post_api_mindos_materials_material_id_unrecycle` | `POST /api/mindos/materials/{materialId}/unrecycle` | json → json | F-LIFE-01 | pending | pending | pending | pending |
| 107 | `get_api_mindos_materials_material_id_version_impact` | `GET /api/mindos/materials/{materialId}/version-impact` | none → json | F-IMP-02 | pending | pending | pending | pending |
| 108 | `get_api_mindos_materials_material_id_versions` | `GET /api/mindos/materials/{materialId}/versions` | none → json | F-IMP-02 | pending | pending | pending | pending |
| 109 | `post_api_mindos_materials_material_id_versions` | `POST /api/mindos/materials/{materialId}/versions` | multipart → json | F-IMP-02 | pending | pending | pending | pending |
| 110 | `get_api_mindos_matters` | `GET /api/mindos/matters` | none → json | F-MATTER-01 | pending | pending | pending | pending |
| 111 | `post_api_mindos_matters` | `POST /api/mindos/matters` | json → json | F-MATTER-01 | pending | pending | pending | pending |
| 112 | `get_api_mindos_matters_matter_id` | `GET /api/mindos/matters/{matterId}` | none → json | F-MATTER-01 | pending | pending | pending | pending |
| 113 | `patch_api_mindos_matters_matter_id` | `PATCH /api/mindos/matters/{matterId}` | json → json | F-MATTER-01 | pending | pending | pending | pending |
| 114 | `get_api_mindos_matters_matter_id_artifacts` | `GET /api/mindos/matters/{matterId}/artifacts` | none → json | F-MATTER-03 | pending | pending | pending | pending |
| 115 | `post_api_mindos_matters_matter_id_artifacts` | `POST /api/mindos/matters/{matterId}/artifacts` | json → json | F-MATTER-03 | pending | pending | pending | pending |
| 116 | `get_api_mindos_memory_policy` | `GET /api/mindos/memory-policy` | none → json | F-PREF-01 | pending | pending | pending | pending |
| 117 | `put_api_mindos_memory_policy` | `PUT /api/mindos/memory-policy` | json → json | F-PREF-01 | pending | pending | pending | pending |
| 118 | `get_api_mindos_nudges_policy` | `GET /api/mindos/nudges/policy` | none → json | F-PREF-01 | pending | pending | pending | pending |
| 119 | `put_api_mindos_nudges_policy` | `PUT /api/mindos/nudges/policy` | json → json | F-PREF-01 | pending | pending | pending | pending |
| 120 | `get_api_mindos_ontology_alignment_conversations_conversation_id` | `GET /api/mindos/ontology/alignment/conversations/{conversationId}` | none → json | F-ONTO-04 | pending | pending | pending | pending |
| 121 | `post_api_mindos_ontology_alignment_conversations_conversation_id_consent` | `POST /api/mindos/ontology/alignment/conversations/{conversationId}/consent` | json → json | F-ONTO-04 | pending | pending | pending | pending |
| 122 | `get_api_mindos_ontology_claims` | `GET /api/mindos/ontology/claims` | none → json | F-ONTO-01 | pending | pending | pending | pending |
| 123 | `post_api_mindos_ontology_claims` | `POST /api/mindos/ontology/claims` | json → json | F-ONTO-01 | pending | pending | pending | pending |
| 124 | `get_api_mindos_ontology_claims_claim_id` | `GET /api/mindos/ontology/claims/{claimId}` | none → json | F-ONTO-01 | pending | pending | pending | pending |
| 125 | `post_api_mindos_ontology_claims_claim_id_alignment` | `POST /api/mindos/ontology/claims/{claimId}/alignment` | json → json | F-ONTO-04 | pending | pending | pending | pending |
| 126 | `post_api_mindos_ontology_claims_claim_id_alignment_proposals` | `POST /api/mindos/ontology/claims/{claimId}/alignment/proposals` | json → json | F-ONTO-04 | pending | pending | pending | pending |
| 127 | `post_api_mindos_ontology_claims_claim_id_alignment_revoke` | `POST /api/mindos/ontology/claims/{claimId}/alignment/revoke` | json → json | F-ONTO-04 | pending | pending | pending | pending |
| 128 | `post_api_mindos_ontology_claims_claim_id_export` | `POST /api/mindos/ontology/claims/{claimId}/export` | json → json | F-ONTO-03 | pending | pending | pending | pending |
| 129 | `post_api_mindos_ontology_claims_claim_id_review` | `POST /api/mindos/ontology/claims/{claimId}/review` | json → json | F-ONTO-03 | pending | pending | pending | pending |
| 130 | `post_api_mindos_ontology_consolidate` | `POST /api/mindos/ontology/consolidate` | json → json | F-ONTO-02 | pending | pending | pending | pending |
| 131 | `get_api_mindos_ontology_context_pack` | `GET /api/mindos/ontology/context-pack` | none → json | F-EXP-01 | pending | pending | pending | pending |
| 132 | `get_api_mindos_ontology_export` | `GET /api/mindos/ontology/export` | none → json | F-EXP-01 | pending | pending | pending | pending |
| 133 | `get_api_mindos_ontology_inbox` | `GET /api/mindos/ontology/inbox` | none → json | F-ONTO-01 | pending | pending | pending | pending |
| 134 | `get_api_mindos_ontology_projection` | `GET /api/mindos/ontology/projection` | none → json | F-EXP-01 | pending | pending | pending | pending |
| 135 | `get_api_mindos_ontology_proposals` | `GET /api/mindos/ontology/proposals` | none → json | F-ONTO-02 | pending | pending | pending | pending |
| 136 | `post_api_mindos_ontology_proposals_conflicts_conflict_id_resolve` | `POST /api/mindos/ontology/proposals/conflicts/{conflictId}/resolve` | json → json | F-ONTO-02 | pending | pending | pending | pending |
| 137 | `post_api_mindos_ontology_proposals_merges_proposal_id_resolve` | `POST /api/mindos/ontology/proposals/merges/{proposalId}/resolve` | json → json | F-ONTO-02 | pending | pending | pending | pending |
| 138 | `post_api_mindos_ontology_purge` | `POST /api/mindos/ontology/purge` | json → json | F-LIFE-04 | pending | pending | pending | pending |
| 139 | `get_api_mindos_ontology_stats` | `GET /api/mindos/ontology/stats` | none → json | F-ONTO-01 | pending | pending | pending | pending |
| 140 | `get_api_mindos_search` | `GET /api/mindos/search` | none → json | F-SEARCH-01 | pending | pending | pending | pending |
| 141 | `post_api_mindos_uploads` | `POST /api/mindos/uploads` | multipart → json | F-IMP-01 | pending | pending | pending | pending |
| 142 | `get_api_mindos_uploads_material_id` | `GET /api/mindos/uploads/{materialId}` | none → json | F-IMP-04 | pending | pending | pending | pending |
| 143 | `post_api_mindos_uploads_material_id_resume` | `POST /api/mindos/uploads/{materialId}/resume` | json → json | F-IMP-04 | pending | pending | pending | pending |
| 144 | `post_api_mindos_uploads_material_id_retry` | `POST /api/mindos/uploads/{materialId}/retry` | json → json | F-IMP-04 | pending | pending | pending | pending |
| 145 | `get_api_mindos_zhijun_home` | `GET /api/mindos/zhijun/home` | none → json | F-HOME-01 | pending | pending | pending | pending |
| 146 | `get_api_mindos_zhijun_onboarding` | `GET /api/mindos/zhijun/onboarding` | none → json | F-ONB-01 | pending | pending | pending | pending |
| 147 | `post_api_mindos_zhijun_onboarding` | `POST /api/mindos/zhijun/onboarding` | json → json | F-ONB-01 | pending | pending | pending | pending |
| 148 | `get_api_mindos_zhijun_status` | `GET /api/mindos/zhijun/status` | none → json | F-CONN-02 | pending | pending | pending | pending |
| 149 | `get_api_system_mindos_pipeline_status` | `GET /api/system/mindos-pipeline/status` | none → json | F-CONN-02 | pending | pending | pending | pending |
| 150 | `get_api_system_models_chat_provider` | `GET /api/system/models/chat-provider` | none → json | F-MODEL-01 | pending | pending | pending | pending |
| 151 | `put_api_system_models_chat_provider` | `PUT /api/system/models/chat-provider` | json → json | F-MODEL-01 | pending | pending | pending | pending |
| 152 | `post_api_system_models_chat_provider_test` | `POST /api/system/models/chat-provider/test` | json → json | F-MODEL-01 | pending | pending | pending | pending |
| 153 | `get_api_system_models_external_providers` | `GET /api/system/models/external-providers` | none → json | F-MODEL-03 | pending | pending | pending | pending |
| 154 | `post_api_system_models_external_providers` | `POST /api/system/models/external-providers` | json → json | F-MODEL-03 | pending | pending | pending | pending |
| 155 | `delete_api_system_models_external_providers_provider_id` | `DELETE /api/system/models/external-providers/{providerId}` | none → json | F-MODEL-03 | pending | pending | pending | pending |
| 156 | `put_api_system_models_external_providers_provider_id` | `PUT /api/system/models/external-providers/{providerId}` | json → json | F-MODEL-03 | pending | pending | pending | pending |
| 157 | `post_api_system_models_external_providers_provider_id_activate` | `POST /api/system/models/external-providers/{providerId}/activate` | json → json | F-MODEL-03 | pending | pending | pending | pending |
| 158 | `post_api_system_models_external_providers_provider_id_models` | `POST /api/system/models/external-providers/{providerId}/models` | json → json | F-MODEL-03 | pending | pending | pending | pending |
| 159 | `get_api_system_models_jobs` | `GET /api/system/models/jobs` | none → json | F-MODEL-04 | pending | pending | pending | pending |
| 160 | `post_api_system_models_jobs_job_id_cancel` | `POST /api/system/models/jobs/{jobId}/cancel` | json → json | F-MODEL-04 | pending | pending | pending | pending |
| 161 | `get_api_system_models_material_runtime` | `GET /api/system/models/material-runtime` | none → json | F-MODEL-02 | pending | pending | pending | pending |
| 162 | `put_api_system_models_material_runtime` | `PUT /api/system/models/material-runtime` | json → json | F-MODEL-02 | pending | pending | pending | pending |
| 163 | `post_api_system_models_material_runtime_load` | `POST /api/system/models/material-runtime/load` | json → json | F-MODEL-04 | pending | pending | pending | pending |
| 164 | `get_api_system_models_material_runtime_models` | `GET /api/system/models/material-runtime/models` | none → json | F-MODEL-02 | pending | pending | pending | pending |
| 165 | `post_api_system_models_material_runtime_pull` | `POST /api/system/models/material-runtime/pull` | json → json | F-MODEL-04 | pending | pending | pending | pending |
| 166 | `post_api_system_models_material_runtime_test` | `POST /api/system/models/material-runtime/test` | json → json | F-MODEL-02 | pending | pending | pending | pending |
| 167 | `post_api_system_models_material_runtime_test_inference` | `POST /api/system/models/material-runtime/test-inference` | json → json | F-MODEL-02 | pending | pending | pending | pending |
| 168 | `post_api_system_models_material_runtime_unload` | `POST /api/system/models/material-runtime/unload` | json → json | F-MODEL-04 | pending | pending | pending | pending |
| 169 | `get_api_system_models_monitor` | `GET /api/system/models/monitor` | none → json | F-MODEL-04 | pending | pending | pending | pending |
| 170 | `post_api_mindos_voice_transcribe` | `POST /api/mindos/voice/transcribe` | multipart → json | F-VOICE-01 | pending | pending | pending | pending |

## 8. 完成门槛与最终结论回填

只有上述可达功能逐项有三层证据、关键故障/隔离/重启通过、部署版本对应、残留验收数据按记录清理，才可报告完整产品验收通过。实时语音等限制以及缺少第二账号、在线模型凭据、可用模型或样本的项目必须在结论中保留，不使用“全部完成”覆盖这些缺项。

| 最终检查 | 状态 |
|---|---|
| 15页/20路由/2重定向实测覆盖 | pending |
| 170个operation逐项对应真实调用与业务结果 | pending |
| 3导入路径、3导出类别、2二进制媒体路径 | pending |
| 本地模型和在线逐任务授权真实执行 | pending |
| 跨主体/客户端/代次、故障、重启和资源回收 | pending |
| 用户主动录音→盒端真实转写→草稿输入通过；连续流式识别限制准确披露 | pending |
| 最终源码、实际部署、测试记录与远程提交一致 | pending |
| 最终结论、剩余缺项和证据索引 | pending |

## 2026-09-06 实机隔离验证补充

实际盒端已通过 5 项能力测试（合成语音、PDF、DOCX、OCR、上传/正文脱敏/真实本地模型摘要与实体）和 10 组 v2 HTTP 验证（含知识 CRUD/搜索、分片与 Owner 代次隔离）。故障、修复及结构化证据见[硬件报告](../reports/FULL-PRODUCT-HARDWARE-0906.md)。这些证据使用专用合成主体与隔离数据目录，**不替代下表正式 Consumer、SDK/P2P、UI 及实际麦克风验收**。170 项目录与逐条 UI PASS 仍须分别核对。

匹配的 Agent/DE/worker/catalog 已正式部署，16 个数据库有一致备份，健康及鉴权拒绝验证通过，详见[部署记录](../reports/FULL-PRODUCT-DEPLOYMENT-0906.md)。正式账号的逐功能 UI、SDK/P2P、实际麦克风验收仍待 Admin 新应用登记上线，不能据此关闭原验收矩阵。
