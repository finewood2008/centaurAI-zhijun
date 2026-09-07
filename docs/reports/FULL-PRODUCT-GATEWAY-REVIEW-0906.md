# 完整产品网关独立审核记录（0906）

日期：2026-09-06。审核范围是 DE live worktree 的 `backend/mindos/zhijun_gateway/{manager,router,workers,store,protocol,catalog}.py`、知君 `backend/zhijun_worker/app.py` 与主进程/前端共同合同。审核由 shell 工作流独立只读完成，网关及 worker 修复由主代理和对应代码代理负责。

这是发现与复核账本，不是部署通过报告。初始发现阶段，除 R-01 用原函数进行独立 asyncio 复现外，其余为源码与调用合同交叉检查；不将静态判断或合成测试写成真实用户验收。后续8项问题均已完成本地代码修复与相应回归；下文保留初始发现及修复结果。主代理已记录Gateway完整集合166项通过。盒端Agent/DE/worker/catalog已完成正式匹配部署，Admin生产登记及正式Consumer/SDK/P2P/UI验收仍pending；隔离候选盒端已通过hardware-candidate5五项与gateway-candidate6十项，详见后续复核。

关联：[完整验收清单](../development/FULL-PRODUCT-ACCEPTANCE-0906.md)、[执行计划](../development/FULL-PRODUCT-INTEGRATION-0906.md)、[v2合同](../development/contracts/zhijun-workspace-v2.json)。

## R-01 满队列取消时的流清理死锁（P1）

发现位置：DE `zhijun_gateway/manager.py`，`response_messages.run()` 与 async generator 的 `finally`。

初始实现的 producer 在 `finally` 无条件 `await queue.put(None)`；consumer 停止后队列容量 4 已满，consumer 的清理再 `task.cancel(); await gather(task)`。被取消的 producer 进入 finally 后等待空队列位置，而 consumer 不再消费，形成互相等待。影响取消、会话失效、模型/文件流关闭与服务退出。

复现：从磁盘源码 AST 提取原 `response_messages` 函数，用真实 `asyncio.Queue`，ASGI response 发送 headers 后连续发送 20 个 body；消费 headers、等队列满，再 `wait_for(iterator.aclose(), 0.2)`。输出为 `BUG: aclose blocked with a full queue until wait_for force-cancelled it`。这是函数级真实 asyncio 复现，不是盒端验收。

建议：取消路径不得阻塞投递 sentinel；producer 完成与取消分开处理；consumer 清理有限等待；`_execute` 显式关闭 ASGI/httpx generators，确保异常、取消和正常路径释放响应。

状态：已修复，本地复验通过。`test_zhijun_gateway_manager.py::test_asgi_stream_arrives_before_completion_and_cancellation_cannot_deadlock` 覆盖满队列流关闭；取消清理不再阻塞sentinel。真实慢模型/退出/盒端取消仍pending。对应验收 `X-FAULT-02`、`X-RESTART-02`。
## R-02 DE领域异常丢失原HTTP状态与详情（P1）

发现位置：DE `zhijun_gateway/manager.py::_execute` 调用 `zhijun_capabilities.dispatch` 的异常路径；`zhijun_capabilities/dispatch.py` 中 material/model 校验会直接抛 `HTTPException`。

初始实现把 `HTTPException(409/422/404, detail)` 统一变成 `kind:error` 与 failed，丢失原 HTTP 状态和领域 detail。材料版本变化、删除影响预览过期、知识草稿或设置 revision 冲突等不能由前端原有有限重预览/保留编辑逻辑正确处理。

建议：尚未发 headers 时，将真实领域 HTTPException 转成对应状态与 `{detail: ...}` 的 JSON 响应，走同一 headers/chunks/end 传输；一旦已经发响应头，中途错误只能追加错误终态，不能伪造新的 HTTP 响应或自动重放写入。

状态：已修复，本地复验通过。`test_zhijun_gateway_manager.py::test_canonical_http_conflict_is_preserved_and_text_blob_has_base_mime` 验证409/detail沿headers/chunks/end保留；已经发头后的错误不重写HTTP响应。真实SDK到UI冲突反馈仍pending。对应验收 `F-IMP-02`、`F-KNOW-03`、`F-LIFE-03`、`F-ROUTE-03`。
## R-03 完成上传无法释放导致第9次导入耗尽（P1）

发现位置：DE `zhijun_gateway/store.py::cancel_upload`、router upload cancel、前端 `productClient` 的终态上传清理和 shell `product-session` 的 8 个上传预算。

初始 store 对 `state == complete` 返回 `UPLOAD_ALREADY_COMPLETE(409)`。前端在业务 end/失败/取消后调用 uploadCancel，主进程只在服务返回 cancelled 时释放引用，因此成功上传也永久占用本次连接预算；连续 8 次普通文件/附件导入后，第 9 次会 RESOURCE_EXHAUSTED。

建议：服务端提供真实、可验证的完成上传释放语义；存在执行中或后台引用时不能提前删字节；终态释放后返回明确状态并让主进程归还预算。不能仅吞 409、扩大额度或把本地忘记句柄说成远端删除成功。

状态：已修复，本地复验通过。store允许complete→cancelled并保留tombstone，router检查活跃引用；`test_zhijun_gateway_routes.py::test_complete_upload_releases_all_bytes_and_other_client_cannot_reuse` 与store取消崩溃恢复测试通过。连续10次真实导入与后台引用现场行为仍pending。对应验收 `F-IMP-03`、`F-IMP-05`、`X-FAULT-02`。
## R-04 临时资源缺少client/session绑定（P1）

发现位置：DE store `create_job/create_upload/create_blob` 与 manager/router 的 poll/cancel/upload/blob 访问。

初始实现只以 workspaceId 查持久资源。相同账号/盒子/ownershipEpoch 下另一已授权 client 如获得资源 ID 或复用 requestId，可读、取消或拼接原 client 的任务/上传。主进程 generation 只能限制本机投递，不能替代盒端校验；与“完成上传可由同 client 新 session 继续使用”的约定不符。

建议：持久记录和幂等 ID 纳入 ownerClientId；任务记录创建 session 并定义明确恢复动作；blob 继承产生它的 job 绑定；同 client 新 session 仅按明确流程重新授权，另一 client 拒绝。重启 tombstone 也必须保留绑定，不能重连重置配额/身份隔离。

状态：已修复，本地复验通过。store资源绑定client/session，HMAC幂等ID纳入client；`test_zhijun_gateway_routes.py::test_jobs_bind_client_and_new_session_needs_identical_explicit_resume` 及store重启/tombstone owner测试通过。新session显式恢复不重启旧写任务；真实多client/重启矩阵仍pending。对应验收 `X-AUTH-02`、`X-RESTART-02`。
## R-05 预览缓存长期增长（P2）

发现位置：DE `GatewayManager.previews`、`_register_preview`、`_validate_consent`、`_validate_sources` 和 `_watch`。

初始实现每个 requestId 缓存完整 preview（含 sources/request），虽然使用时检查 30 秒有效期，watcher 不删除预览、任务终态也不回收。连续正常预览可持续增加主进程内存，违反任务/资源有界要求；不能把“已过期但仍占内存”当作回收。

建议：统一缓存写入口，按有效期、workspace/全局条数与字节预算限制，执行终态或后台授权结束时回收。`_validate_consent` 的直接赋值同样必须走预算，不能绕过；无执行的 semaphore 等辅助字典需随生命周期回收。

状态：已修复，本地复验通过。preview统一经有界`_cache_preview`入口，watcher/任务终态清理；consent合同回归覆盖来源验证、后台结束和授权撤销，辅助恢复集合随租约清理。完整Gateway166项集合通过；真实长期负载内存观察仍pending。对应验收 `X-FAULT-02`。
## R-06 worker慢启动阻塞快速任务提交（P2）

发现位置：DE `GatewayManager.worker()` 与 `start()` 初始共用 `self.lock`。

worker_factory 最多等待 12 秒启动，旧 worker.close 最多等待 5 秒，均在共同锁内；此时其他 operation start 无法快速返回 queued，恰好可能撞主进程 12 秒请求 deadline。用户得到 unknown 写入结果，而服务器随后才接受动作，扩大不确定结果窗口。

建议：worker池创建使用独立锁或每workspace的启动 future；任务持久排队仅使用短锁。保证 worker 慢启动期间，其他合法 start 仍能在请求预算内返回，不以放大客户端超时掩盖问题。

状态：已修复，本地复验通过。独立`worker_lock`隔离慢启动；`test_zhijun_gateway_manager.py::test_slow_worker_start_does_not_hold_operation_submission_lock`验证并发提交不会被worker启动长期占锁。正式多workspace容量及启动时延仍pending。对应验收 `X-FAULT-02`、`X-RESTART-02`。
## R-07 worker初始化前半段失败遗留私钥目录（P2）

发现位置：DE `workers.py::start_worker`。

初始代码先创建随机 runtime 目录、key、subject，再校验 domain 目录并 create_subprocess_exec；现有 try/cleanup 从构造 Worker 后才开始。domain_root 权限错误、进程创建失败等前半段异常可能残留私有 runtime 目录与密钥文件，重复失败会积累。

建议：将整个临时初始化放在统一异常清理范围；如已产生子进程必须终止并有限等待；只删除本次专属 runtime 文件，不能删 domain 持久目录。进程恰好退出的 ProcessLookupError 和残留socket也要纳入清理测试。

状态：已修复，本地复验通过。整个初始化过程纳入清理；`test_zhijun_gateway_manager.py::test_worker_exec_failure_removes_only_runtime_key_files`验证失败只清本代runtime key/subject，保留持久domain。实际生产故障和runtime权限仍pending。对应验收 `X-RESTART-02`。
## R-08 worker未使用显式catalog部署配置（P2）

发现位置：知君 `backend/zhijun_worker/app.py::_catalog` 与 DE `workers.py` 的 `ZHIJUN_PRODUCT_CATALOG` 环境设置。

初始 worker 忽略该环境变量，从源码相对路径 `frontend/shared/product-operations.json` 读取；DE则读取 GatewayConfig.catalog。若只部署 backend 目录，worker会启动找不到文件；即使完整复制仓库，两份清单也可能漂移而出现路由缺失或行为不一致。

建议：使用明确、受权限校验的配置清单或明确部署同一固定产物；启动核验 SHA 与 schemaVersion；worker和DE不能各自默默选择不同路径。生产发布还须核对真实进程 cwd 与加载的版本，防止验到旧实例。

状态：已修复，本地复验通过。worker读取明确的`ZHIJUN_PRODUCT_CATALOG`并检查路径/权限/清单及实际domain路由；DE实际UDS worker CRUD测试使用显式部署catalog。正式匹配发布已核对363文件与已验candidate一致，live健康与未签名拒绝通过；完整Consumer/SDK路径、生产重启与逐功能验收仍待完成，不把本地路径测试当成全部生产验收。对应验收 `X-RESTART-03`。
## 补充合同核对

- MIME：manager 最初直接把 `text/plain; charset=utf-8` 交给仅接受基础MIME的 blob store，会使文本原件下载失败。已向主代理反馈，后续源码已见基础MIME提取/校验。文本原件、PDF、图片、音频和子文件的真实下载/预览仍需 `F-MEDIA-01/02` 和 `F-EXP-03` 实测。
- 分片：store 已严格要求非末块为 524288 字节，因此按固定块偏移读取没有发现该项问题。shell 已同步前置校验，并把测试改成 524288+1 字节的两块上传、相同hash重复ACK及非法短块拒绝；16 项 product 测试复验通过。此项是自动化证据，不能填写真实上传通过。
- 写取消：主代理已明确采用保守语义：domain mutating任务断开HTTPX不能证明同步handler停止，需 `interrupted / WORKSPACE_WRITE_RESULT_UNKNOWN`，即使 cancelRequested=true 也不能声称取消已消除副作用。shell已支持interrupted和未知写结果；真实故障核对仍pending。
- 本轮未发现可替代真实执行的隐式mock可以作为验收证据；这不等于所有模块已完成审计，更不等于全功能真机通过。

## 追加生命周期与consent复核

- consent ledger原600秒早于canonical receipt的1800秒过期，已改为1860秒保存，覆盖签发先后差；真实授权仍由receipt期限、当前来源/配置与执行校验控制，不因ledger保留而延长。
- Gateway取消会使已启动的反向模型投递停止；后台来源结束/失败或撤销后，原任务不能继续用旧executionRequestId调用模型或签发前台consent。`test_zhijun_gateway_consent_contract.py` 共7项相关合同用例通过。
- 无活租约且无活跃执行的idle worker现在会回收，保留domain持久数据；`test_expired_workspace_worker_is_reaped_without_removing_domain_data`通过。
- 这些增量与protocol/store/routes/manager/reverse一起包含于主代理报告的166项Gateway集合，不另加到166上。

## 最终复核回填

| 项目 | 当前源码修复 | 隔离回归 | 真实UI/SDK/盒端 | 状态 |
| --- | --- | --- | --- | --- |
| R-01 流取消死锁 | 完成 | 通过 | pending | 本地关闭，生产验收待完成 |
| R-02 HTTP领域语义 | 完成 | 通过 | pending | 本地关闭，生产验收待完成 |
| R-03 上传释放 | 完成 | 通过 | pending | 本地关闭，生产验收待完成 |
| R-04 client/session绑定 | 完成 | 通过 | pending | 本地关闭，生产验收待完成 |
| R-05 缓存预算/回收 | 完成 | 通过 | pending | 本地关闭，生产验收待完成 |
| R-06 启动锁竞争 | 完成 | 通过 | pending | 本地关闭，生产验收待完成 |
| R-07 失败初始化清理 | 完成 | 通过 | pending | 本地关闭，生产验收待完成 |
| R-08 catalog路径/版本 | 完成 | 通过 | pending | 本地关闭，生产验收待完成 |

本地审核结论：8项已修复并完成回归，追加consent期限/反向取消/后台撤销/idle回收已测。Gateway完整集合166项通过；其余前端63、shell最终117项Node与vue-tsc（独立15项包含在117内）及既有4项隔离Electron E2E、worker113与4subtests、资料156、模型99与7subtests及近期针对性40项，分别属于不同验证范围和批次，**不累计为全链路通过数**。

提交与部署：OS `5f5f4c9`、Admin `44a0950`已提交推送；DE连接FD热修 `132b97d`已单独部署，见[故障报告](CONNECTIVITY-FD-HOTFIX-0906.md)。知君完整增量 `58dac31`已提交推送，DE `015c659`已提交推送；后续clean heads知君 `735e341`（代码 `58dac31`）/DE `015c659`/OS `5f5f4c9`已正式匹配部署，363文件与已验candidate一致；该完整发布与此前FD热修分开记录。

生产结论：**pending**。Admin新应用尚缺boss生产发布入口，盒端Agent/DE/worker/catalog已正式匹配部署，健康与未签名拒绝检查通过，完整验收尚未完成；隔离硬件hardware-candidate5为5/5，gateway-candidate6为10/10、60请求/21个completed操作，含知识CRUD/confirm/search/purge。均为合成主体/输入，真实Consumer/UI功能矩阵未关闭。170项catalog不是170项UI实测；原v1空资料页/一次重连证据保留历史含义。

## 后续硬件与配额缺陷复核

以下R-09至R-14是原8项本地修复之后的新增发现，不能因为原8项关闭就省略硬件复测。实际现场环境与逐项输出由[硬件报告](FULL-PRODUCT-HARDWARE-0906.md)维护。

### R-09 业务poll/上传与心跳共用配额但缺少统一调度（P1）

真实JS模块配合严格内存Agent的复现发现，原200MiB上传约59MiB即触发120请求/分钟限制；持续SSE立即poll也可能耗尽窗口。心跳和业务分开计量不足以保证30秒Owner租约，简单放大超时或自动重连会掩盖配额与未知写入问题。

修复：同一会话统一调度请求、心跳优先级及上传请求/编码后字节预留；明确未派发的限流拒绝才可同字节有限重试，派发后不确定写入不重放；Core1024请求和Agent120rpm/1GiB不变。

证据：shell最终117项Node、vue-tsc通过，独立15项配额用例包含在117内，不再累加。真实JS生产模块+内存严格Agent+虚拟时钟下，200MiB/400块完整完成，耗时243秒虚拟时间，滚动60秒最大101请求。此为调度/资源合同验证，**不是实际SDK/P2P吞吐或大文件真机通过**；真实通道验收仍pending。

### R-10 小文档内联快照缺少正文SHA导致隐私源不可用（P1）

真实盒端小文档虽为storage_state=ready、parse_status=ok，旧内联snapshot_hash仍为空，privacy source因缺少可核对摘要返回 `REDACTION_SNAPSHOT_UNAVAILABLE`。仅检查上传或parse成功会错误关闭后续脱敏/摘要链路。

修复在快照提交事务计算并校验内联正文真实UTF-8 SHA256，仅给已有ready内联正文补齐缺失摘要；不伪造外部文件哈希，不改已有非空摘要。相关源码为DE `mindos/material_snapshot_saga.py` 与 `mindos/stores/material_pipeline_store.py`。

证据：该阶段172项本地测试通过、body与summary ready；当时analysis与knowledge仍失败，未将哈希修复直接记为整链路通过。后续分别定位并修复R-12至R-14，最新资料相关范围189项通过，最终hardware-candidate5为5/5；这些是先后批次，不相加。

### R-11 受限一次性ASR进程线程/分配导致推理失败（P1）

初轮硬件ASR在4GiB地址空间限制内遭遇原生线程池和 `mkl_malloc` 分配失败；加载模型的单独成功不足以证明实际推理或voice API成功。推理失败也不能被当作“无语音”正常返回。

修复保留4GiB限制，把BLAS/OpenMP和推理线程收敛为单线程、限制一次性解析进程的分配；区分资源耗尽、推理失败、超时与无语音。限制不作用于用户正在运行的主DE服务；运行期禁止下载模型或隐式外发。相关源码为DE `mindos/services/protected_material_extract.py`、`mindos/zhijun_capabilities/voice.py`。

最新实际证据：完整voice API通过，返回40字符、资料新增0，两个指定短语均match。语音输入是合成WAV，**不代表真实麦克风和正式Electron/SDK/P2P录音闭环通过**。PDF46字符、DOCX48字符、OCR110字符亦已真盒通过；analysis/knowledge另经R-12至R-14修复和独立复测，不以解析或语音通过替代。

### R-12 首次lazy import覆盖包公开dispatch函数（P1）

首次真盒knowledge失败并非知识CRUD合同本身缺失：`mindos.zhijun_capabilities.__init__`的公开`dispatch`函数在首次lazy import同名子模块时被Python包属性赋值覆盖，后续调用拿到module。已预热模块的同进程测试容易遗漏这个首次启动路径。

修复：先导入子模块实现并绑定私有别名，再定义稳定的公开包装函数，不在调用时触发同名属性覆盖。`test_zhijun_capability_package.py::test_package_dispatch_remains_callable_in_fresh_interpreter`以fresh subprocess做红绿验证，覆盖实际首次导入顺序。

证据：修复后的隔离真盒`gateway-candidate6`为10/10，60请求、21个completed操作；knowledge CRUD、confirm、search、purge全部通过。使用合成主体与专用数据，未登录正式Consumer，不等于桌面UI或正式SDK/P2P通过。

### R-13 DeletionStore高频范围检查未确定关闭SQLite连接（P1）

材料隐私/生命周期验证反复查询DeletionStore时，SQLite事务上下文退出只提交或回滚，未保证关闭连接；依赖GC会造成文件描述符增长。这个存储的问题独立于先前已上线的connectivity.db热修，不能用旧热修通过替代验证。

修复：`mindos/stores/deletion_store.py`读写和只读连接采用确定性closing，并保留原事务提交/回滚语义及初始化异常清理。专用`test_zhijun_deletion_connections.py`覆盖GC禁用高频查询、正常事务、查询异常回滚和初始化失败关闭。

证据：GC禁用下100次检查，FD从4保持为4。随后真盒材料曾单次跑通正文82、摘要27字符、实体3；该中间结果未代替整脚本复跑，后续另一轮仍出现R-14的`evidence_invalid`。最终结果以hardware-candidate5为准。

### R-14 小模型结构化分析证据偶发无效，需要有界静态纠错（P1）

小Qwen在材料分析整脚本复跑中返回`evidence_invalid`。此前正文/摘要ready或单次分析成功不足以证明这次完整执行成功；保留首次失败记录，不降低证据/隐私校验来消除错误。

修复：`mindos/zhijun_capabilities/safe_derived.py`最多生成**总计2次**，即首次加至多1次纠错，仅对明确结构/证据错误附加预定义静态提示；不将失败模型输出回灌提示词。每次仍验证当前主体权限、来源版本/脱敏状态和模型配置，输出继续走同一严格证据/隐私检查；撤销、配置变化、传输失败或隐私复核不进入纠错。两次均失败则保留失败结果，不无限重试。

证据：最新资料相关范围189项通过，包含纠错上限、禁止回灌和权限/配置/隐私变化负向回归；189不是与既有156/172批次累加。最终实际盒端`hardware-candidate5`五项全部通过：voice 40字符/0资料/2个指定短语匹配，PDF46、DOCX48、OCR110字符，safe material正文82、摘要43字符、实体2、关系0。输入与主体均为合成，实际麦克风和正式Consumer/UI/SDK验收仍待完成。

当前总边界：hardware-candidate5为5/5，safe material正文82、摘要43字符、实体2、关系0；gateway-candidate6为10/10，60请求/21个completed操作，含知识CRUD/confirm/search/purge。全部为实际盒端的合成主体/输入；盒端匹配部署已完成，完整v2正式Consumer/SDK/P2P/UI仍缺Admin生产发布入口与后续端到端验收。170项catalog、117项shell测试或任何局部通过数均不等于170项UI实测。

正式盒端匹配部署的版本、备份、文件哈希、健康与拒绝检查见[部署回执](FULL-PRODUCT-DEPLOYMENT-0906.md)；Admin发布及正式Consumer/SDK/P2P/UI验收保持独立待办。
