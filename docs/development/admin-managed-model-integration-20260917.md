# 公司盒 Admin 托管模型接入

## 目标与验收

将公司盒 `192.168.0.7` 的知君后端接入已经运行的系统 Provider Broker，
支持用户只在 Admin 配置服务凭据、在知君选择获准的 Provider/模型，无需再次填写 Key。
保留当前自配模型选择、本地模型和资料出域授权，不自动选目录第一项。

- [x] 核对现场 Broker socket、专用 UID/组、产品映射、租约及容器配置。
- [x] 补齐可持久复用的部署接入配置，建立必要回归；不扩展产品权限或暴露 Key。
- [x] 完成隔离验证：无自配凭据时可以选择平台模型；无 Admin 配置/过期/停用时明确拒绝；不自动换服务。
- [x] 备份并定向更新公司盒容器，保留回退入口，验证现有数据及配置选择。
- [x] 从真实知君/DE目录链路读取两个 Provider，执行不含用户数据的最小推理验收；允许模型目录另由真实产品链路隔离测试覆盖。

## 范围与依赖

当前公司盒已部署此前检索修复镜像 `sha256:1969cdfde0fe70481db5554c18545517b0e17e77979114f18ca72424a9a22741`。
Admin/OS Broker已具备下发及独立服务身份；缺少Data Engine容器socket挂载、环境变量和受限组接入。
影响文件待调查收敛到OS/DE正式部署入口及接入测试，必要时修复知君列表读取体验。
不改Admin的Provider配置/Key，不运行全量OS安装，不覆盖相邻仓库未提交改动。

主代理负责现场、方案、整合、发布与最终验证；并行只读子任务分别核对Broker协议/身份约束及持久部署入口。
涉及部署变更时先检查空闲任务并保存原容器配置；失败仅回退服务配置，不回灌业务数据。
线上推理仅使用固定合成提示，不读取/发送用户会话、文件或本体。

## 已确认的起点

初查DE独立模型库显示旧自配配置，但它不是当时产品使用的权威配置。后续真实GUI与Worker核对确认，实际选择为 `ext_2874411bc577401b`、`deepseek-flash`（显示名deepseek），迁移以Worker为准。
实际后端PID1没有`CENTAUR_PROVIDER_BROKER_SOCKET`，不能把代码支持或Admin历史加载ACK当作接入成功。
系统服务`centauros-provider-broker.service`正在运行，实际socket为`/run/centauros/model-providers/model-broker.sock`。

## 现场验收发现的第二缺口与修订计划

产品catalog的9个chat配置操作目前归属domain，且真正workspace工厂直接使用Worker配置；DE托管能力虽已实现，却尚未接入真实产品入口。此前DE独立模型库并非当前对话的有效选择，不能据其推断实际在线服务。真实界面当前选择deepseek/deepseek-flash。

因此扩大到必需的接线修复：

- 模型设置9条操作归属models；workspace对话与本地工厂统一使用DE capability。
- 接通已有preview、显式/默认授权、撤销及后台凭据链路，维持原有出域边界。
- 迁移Worker自配profiles/当前选择到DE，同工作区身份检查、先备份、显式冲突处理、保留原库；不迁移历史资料授权到新服务。
- 发布仅本次明确文件的增量镜像，保留先前资料检索修复及其他配置。
- 独立代理分别负责工厂接线、授权桥、迁移工具；主代理负责catalog、整合、上线和GUI验收。

验收增加真实产品入口→DE托管推理的合成用例，不能以仅模型底层单测通过替代。

## 升级与回退合同

公司盒是独立容器，不经过GPU Manager生成器。后续重建必须复用DE `scripts/zhijun_broker_container.py`：从原容器inspect深拷贝，仅加固定socket环境、専用目录只读Mounts和宿主实际Broker数字GID，拒绝宽挂载/冲突/错误UID。不导出平台Key，也不挂Admin配置。

Worker旧模型库与DE模型库具有不同选择，不能直接改catalog后上线。使用DE `scripts/migrate_zhijun_worker_models.py` 先dry-run、审核单工作区planDigest，再停服务备份和应用。工具保留Worker原库、两个profile的ID/revision与当前选择；只在内存解密后重新加密导入DE。旧DE独立授权policy备份后删除，防其revision序列阻挡重新授权；不迁移资料授权。原会话模式遇到新serviceId会走既有重新确认流程。

后台默认授权使用独立 `models.consent.background`，必须已登记任务、当前执行身份、对应purpose、未过期租约、当前DE明确启用autoEgress的默认策略与新鲜精确预览；前台授权入口保持原限制。后台receipt和grant绑定任务身份，不能被其他任务或前台请求复用。

桌面已有托管Provider展示/选择UI；RPC仅传operationId，服务端按升级后的catalog确定能力归属，因此同协议的现有桌面无需重新输入Key。可在设置里“重新读取”，选择标记为平台托管的Provider并保存允许模型。

## 验证记录（发布前）

- 实际产品链路隔离测试6项：真实catalog、Gateway、独立UDS Worker、反向签名能力和Broker合成响应；没有自行构造预览/授权回执替代产品路径。
- 前端transport32项通过，覆盖managed Provider参数/revision和9条设置操作统一归属models。
- 容器配置59项、配置迁移20项通过；本地跳过的2项root身份用例另在隔离Linux容器通过。迁移包括WAL、锁、密钥重加密、事务失败回滚、旧policy revision冲突恢复及新WAL/SHM归属修复。
- 工厂、legacy适配器与后台相关111项+12subtests通过。已存在的两个非workspace章程测试在HEAD基线复现失败，未将其算作本次通过项。
- 最终Linux候选镜像隔离回归44项通过，包含真实产品入口、后台授权和网关合同；不挂线上状态且禁用网络。

## 公司盒发布与验收结果

2026-09-17已更新 `192.168.0.7` 的 `zhijun-integration-0907`，健康检查通过。
镜像 `zhijun-data-engine:company248-admin-broker-20260917`，不可变ID：
`sha256:580458738259c89620077bca59bbfb3948781c97bb75c64fd15dbf704e0e7768`。

仅覆盖7个知君生产文件及2个DE生产文件，保留原容器只读根文件系统、UID10001、安全选项、网络与业务挂载。
增加Broker目录只读挂载、固定socket环境和宿主实际GID968；验证实际进程身份与root Broker对端。
停服务前确认活动任务为零，迁移后4个domain数据库的受保护业务表行数/内容哈希一致。
迁移保留两个自配profile的ID、revision及当前模型选择，未迁移历史资料授权。

私有回退目录 `/home/user/zhijun-admin-broker-20260917` 保存原容器配置、模型库备份与状态归档。
状态归档14,263,399字节，SHA256：`5d30c41398c2bcbea2d5fab230bcdd5c18e4c9cea935576ed3f194173132204d`。
原容器 `zhijun-integration-0907-before-admin-broker-20260917` 保持停止且不自动重启。
备份含敏感状态，不作为公开发布附件。

真实桌面重新连接公司盒后，显示Admin下发的「企数token」「企数token-gpt」，并显示「平台托管，无需配置密钥」。
临时选择「企数token / deepseek-flash」，点击「测试当前通道」：界面返回「连接成功 · openai / deepseek-flash · 1002ms」。
服务端此测试仅发送固定 `ping`、最多16个输出token，未读取或发送用户会话、文件、本体。
验收后供应商恢复为原有「deepseek / deepseek-flash」；未自动开启资料来源默认授权。

用户可在「设置 → 在线供应商与默认模型」选择平台供应商及允许模型，再保存启用，无需填写Key。
Admin配置只提供可用目录，不替用户选择模型或授予资料出域权限；缺少配置、停用或过期时不自动回退到其他服务。
切换服务或升级后服务身份变化时，既有资料授权需要用户重新确认。
