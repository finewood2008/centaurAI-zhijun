# 知君桌面产品接口与远程传输合同

更新：2026-09-06。当前公开类型以 [desktop-contract.ts](../../frontend/shared/desktop-contract.ts)、[product-contract.ts](../../frontend/shared/product-contract.ts) 为准。文件名沿用0905，内容已覆盖完整产品v2接入。**新应用正式盒子/UI验收仍待完成，Admin生产发布路径未提供。**

配套：[架构](ARCHITECTURE-0905.md)、[集成方案](INTEGRATION-0905.md)、[领域规格](DOMAIN-INTEGRATION-0905.md)、[机器合同](contracts/zhijun-workspace-v2.json)、[真实验收](REAL-ACCEPTANCE-0906.md)。

## 1. 范围与版本

正式桌面现装配原15个页面组件、20条页面路由和2条重定向，使用原产品导航、独立hash router和单一连接provider。共享catalog为170项：95领域、54资料、21模型；包含原JSON CRUD、聊天SSE、附件/版本、下载/预览、设置和语音转写端口。范围覆盖不表示每项业务均已实测成功。

保留旧M0资料只读接口和v1应用兼容。新完整产品使用 `zhijun-desktop / zhijun.workspace`，不能把 `mindos-person-data-pc / person-data.read` 升权。preload仍是 `protocolVersion:1`，产品操作envelope `version:1`，签名桥是 `v2`；三者层次不同。

SDK1.2.0提供整响应request/close。本文的start/poll/cancel/uploads/blob是知君主进程与盒端Gateway合同，不是SDK已经导出的原生stream API。

## 2. Renderer → preload → main

所有调用携带 `CallContext {callId,expectedGeneration}`，返回 `Result<T>`，包含当前generation或受控错误。preload去除Electron事件对象，只暴露业务类型；main核验真实发送frame、参数、代次、ready状态及权限。renderer不得传URL、任意header、可信身份、磁盘路径或模型Key。

| 接口组 | 当前方法 | 职责 |
| --- | --- | --- |
| 会话 | getSnapshot/subscribe/beginSignIn/signInWithPassword/listDevices/connect/disconnect/signOut | main持有登录态、设备选择与SDK；公开快照不含票据 |
| 旧资料 | materials.list / cancelRead | v1最小公开投影；cancelRead只抑制本地投递 |
| 产品任务 | product.start / poll / cancel | catalog操作、有界事件与显式服务端取消 |
| 上传 | uploadCreate / uploadChunk / uploadComplete / uploadStatus / uploadCancel | 主进程计算分块/整文件SHA，校验所属资源 |
| 二进制 | blobRead / save / openMedia / closeMedia | 有界读取、用户选择保存路径、受控媒体协议句柄 |
| 麦克风 | requestMicrophone | 明确录音按钮后的短时audio许可，不自行采集 |

`ProductOperationRequest` 固定字段为version/requestId/operationId/params/query/body。业务路径由catalog在main与DE分别展开；pathParams/query白名单、字节预算、multipart引用都必须匹配。JSON、SSE和bytes响应按catalog类型分别处理，不使用万能HTTP IPC。

## 3. 状态、代次与身份

状态为 signed_out/authenticating/selecting_device/connecting/authorizing/ready/disconnecting/failed。快照有generation和单调sequence；未ready不开放产品操作。context在同一SDK会话验证account/client/device/application及workspace/capability后才ready。simulation持续标记合成环境，不能证明真实盒子已连接。

断开、退出、设备或账户切换会使旧generation失效。晚到结果被拒绝，页面数据、媒体句柄、录音和临时资源随主体切换清理；草稿不能自动搬给新主体。app capability缺失或桥未就绪直接报错，不回退PC本机HTTP或旧读应用。

DE workspace绑定account/device/ownershipEpoch；job/upload/blob还绑定clientId，job保留创建session。新session仅允许同client显式相同start恢复已知结果，不重放旧运行任务。所有poll、cancel、chunk和blob读都重新验权；本地对象ID不等于远程授权。

## 4. 流与取消

start返回 `{id,state,cursor}`；poll返回 `{id,state,events,cursor,hasMore}`。事件序号单调：headers带原HTTP状态和受控头，chunk在网络中为base64、preload中为Uint8Array，blob带文件描述，end/error终结事件流。renderer适配为ReadableStream，原SSE消费方保留跨块UTF-8与事件语义。

poll最多等待8秒、32事件/256KiB；SDK收完每次短响应后返回。单任务上限600秒、累计事件16MiB并受更小catalog限制。缓冲与消费按页推进，不能无限累积，也不能把“长聊天”变成SDK超时后自动重试POST。

产品cancel会请求服务端取消，区别于旧cancelRead的本地投递抑制；取消与写入提交可能竞争，不能承诺回滚。任务状态为queued/running/succeeded/failed/cancelled/interrupted。重启/失联导致写结果不确定时保留原requestId，提示用户核对；不自动换ID重建。

## 5. 附件、预览、保存与语音

上传状态open/complete/cancelled/failed，received为原始字节数，nextIndex为0-based。每块≤512KiB，非末块必须恰好512KiB；SHA256匹配、顺序正确才ACK。同index同内容重传ACK，不同内容拒绝；整文件摘要确认后才能作为multipart的uploadId引用。完成态仅表示暂存就绪，业务导入成功还需相应operation终态。

单文件≤200MiB；会话传输1GiB含base64开销，workspace磁盘1GiB含加密元数据/墓碑。完成上传无活跃任务引用后可显式释放；重复DELETE幂等，不重置原requestId。v2暂存协议不等于DE Pocket的1MiB/1-based业务协议。

blobRead最多512KiB，save在主进程打开原生保存对话框并逐块写文件。openMedia返回短生命周期 `zhijun-media:` 句柄，主进程限制类型、范围、主体与8MiB预览预算；页面不直接打开盒子previewUrl，也不能把任意文件路径交给宿主。

语音流程为明确录音按钮 → requestMicrophone → OS授权/可信audio frame → getUserMedia → 停止 → 16kHz PCM16 mono WAV → 同一受控上传/转写操作 → 文字填入草稿。录音最长120秒，断开/切换/销毁时停止tracks；不自动发消息。macOS需有效 `NSMicrophoneUsageDescription`，可信frame短许可15秒，camera/display capture拒绝。DE WAV转写最多120秒/20MiB，不注册资料，不隐式改用外部模型。盒端voice API已用合成WAV返回40字符、0资料、2个指定短语均匹配；设备麦克风权限与正式SDK/UI录音链路仍待验收。

## 6. 错误与预算

公开错误保留受控 `code/message/httpStatus/remoteCode/traceId/recovery`；不暴露票据、proof、路径或原始异常。409/422及业务preview必须保留语义，不能全部归为“网络失败”。`WRITE_OUTCOME_UNKNOWN` 不能被自动重试吞掉，STALE_GENERATION结果不覆盖新页面。

Agent新应用：1MiB请求/响应、8并发、120rpm、1GiB会话预算；Core单会话1024请求ID保持不变。main统一调度心跳、业务轮询和上传，预留上传请求数与编码后字节；仅明确未派发的限流拒绝允许同字节有限重试，已派发写入不自动重放。预算耗尽不得自动重连刷新。Gateway每workspace4运行/8排队，每盒4worker、30秒租约和10秒心跳。配置缺失、超额、过期、撤销与身份不匹配均fail closed。

## 7. 历史证据与正式验收

历史M0-L、v1桥及真实空资料页/刷新/一次重连见[实施记录](M0-IMPLEMENTATION-0906.md)、[盒端部署](BOX-DEPLOYMENT-0906.md)。这些证据不证明非空历史资料已迁移，也不证明v2 product capability、完整跨主体矩阵或新Admin应用已生产发布。

正式验收需覆盖原15页真实操作与所有170项清单映射，尤其聊天长流/断流/取消、文件导入版本/保护/释放、模型授权拒绝与来源失效、媒体保存、语音权限、跨账号/client/device/session、重启和预算失败。安装包还需平台产物、签名/公证/权限声明验证；BLE独立可选。局部测试数量不可相加成完整产品“通过数”。

当前Admin发布前置与逐功能状态以[完整产品执行计划](FULL-PRODUCT-INTEGRATION-0906.md)和[真实验收记录](REAL-ACCEPTANCE-0906.md)为准。连接FD热修已单独恢复现有服务，见[故障报告](../reports/CONNECTIVITY-FD-HOTFIX-0906.md)，此为历史独立热修。后续Agent/DE/worker/catalog已正式匹配部署，仍不代表Admin登记、桌面安装包或正式Consumer/SDK/P2P/UI验收通过。

最新本地验证：shell117项Node、vue-tsc通过；独立15项配额用例包含在117内。200MiB/400块经真实JS模块和内存严格Agent配额，在243秒虚拟时间完成，滚动60秒最多101请求；尚未做真实SDK/P2P大文件验证。隔离盒端hardware-candidate5为5/5（safe material正文82、摘要43字符、实体2、关系0），gateway-candidate6为10/10（60请求/21个completed操作，含知识CRUD/confirm/search/purge）。均为合成主体/输入，非正式Consumer/UI；不能按170项catalog或隔离硬件结果关闭完整UI验收。证据见[硬件报告](../reports/FULL-PRODUCT-HARDWARE-0906.md)。

正式盒端匹配部署的版本、备份、文件哈希、健康与拒绝检查见[部署回执](../reports/FULL-PRODUCT-DEPLOYMENT-0906.md)；Admin发布及正式Consumer/SDK/P2P/UI验收保持独立待办。
