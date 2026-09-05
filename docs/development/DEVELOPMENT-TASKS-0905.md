# 知君 SDK / data-engine 集成开发任务清单

日期：2026-09-06（文件名沿用 0905 集成基线）。规划代码基线：知君 `140dd34`，产品源 `22dc9a3`。任务清单最初仅作规划；2026-09-06 已实施首批 M0-L，实际起点 `ee8cd96`。**独立桌面本地合同和模拟流程已通过，正式 SDK/身份桥/真实盒子 M0-R 尚未实现或验收。** 当前状态见第3节及各任务交付差额，不用既有产品回归或模拟结果代替真实业务集成。

任务依据：[架构](ARCHITECTURE-0905.md)、[集成方案](INTEGRATION-0905.md)、[桌面接口合同](DESKTOP-CONTRACT-0905.md)、[领域迁移规格](DOMAIN-INTEGRATION-0905.md)、[工作包](INTEGRATION-WORKPACKAGES-0905.md)。发生冲突时先修订合同再实施，不由单个任务私自改变身份、部署或传输协议。

## 1. 任务编制计划与交付标准（历史记录）

- [x] 核对当前分支、既有规格和工作包，确认前端/宿主入口及相邻仓库状态。
- [x] 按桌面、服务端、连接与发布三条主线细化任务，列出文件、步骤、依赖和验收。
- [x] 汇总首批任务、跨仓交付、共享文件归属及里程碑完成条件。
- [x] 校验编号、依赖、链接、任务覆盖和未实现能力描述；结果见第7节。

交付分支为 `dev/first-integrate-check-0905`；本文随任务明细统一提交，远程同步状态以 Git 记录为准。

以下为任务编制阶段的影响范围与验收口径，实际开发范围及证据见[实施记录](M0-IMPLEMENTATION-0906.md)。编制阶段影响文件：本文、`tasks/` 下三份明细及 README/架构/集成/工作包导航。验收标准是任务能够直接分配给开发者，依赖可追踪，实施步骤与正负向验收具体，未知配置有责任方，模拟测试和真机测试分别记录。编制阶段未创建远程 Issue、未改应用代码、依赖、相邻仓库或运行数据；该说明不描述后续 M0-L 实施状态。

## 2. 使用方式与任务字段

| 文档 | 任务范围 | 主要负责人 |
| --- | --- | --- |
| 本文 BASE 系列 | 版本、身份、身份桥合同、资料范围和验证环境 | 集成负责人、控制面、后端、测试 |
| [桌面任务](tasks/DESKTOP-TASKS-0905.md) | 新宿主、窄 IPC、登录连接、只读资料、M0 验收、全域 transport/缓存 | Electron、Vue、测试 |
| [服务端任务](tasks/SERVER-TASKS-0905.md) | Agent 可信桥、业务 gate、资料投影、领域与数据迁移 | Agent、data-engine、领域后端 |
| [连接与交付任务](tasks/CONNECTION-DELIVERY-TASKS-0905.md) | 流协议、上传、媒体、打包升级及可选配网 | SDK/Core、Agent、后端、前端、发布 |

每项包含任务 ID、所属 WP、目标里程碑、优先级、负责角色、估算、依赖、涉及文件、实施清单、验收和证据。角色尚未落实到具体人员；派发时补负责人、执行分支及 PR 链接。标为“拟新增”的路径需要实施者落地，不能把文档路径当成现有 API 或测试脚本。

优先级用于安排开发：P0 为首条 M0 链路所需，P1 为完整知君业务所需，P2 为范围批准后的增强项；与集成方案的阶段 P0–P7 不是同一概念。工时按熟悉相关技术的一名工程师估算：S=0.5–1 人日、M=1–2 人日、L=2–4 人日，包含编码、自测和一次评审修订；超过 L 的项在正式派发前继续拆分。跨仓联合任务估算的是合计工作量，不能把多人各投入一天算作一天。

估算不含正式应用注册、硬件到位、外部合同讨论等待、签名账号审批及未知兼容问题；不是日期承诺。先完成版本与合同核验，再根据人员和实际原型结果重估。任务人日可累加为工作量，不能简单除以人数当日历排期。

任务状态只允许：未开始、实施中、本地通过待外部验收、已验收；另记录阻塞原因、责任方和影响范围。依赖任务的合同可先以显式草案支持原型；依赖的真实验收未完成时不能把合流任务标为已验收。

## 3. 里程碑与范围

| 里程碑 | 完成条件 | 不能提前计入的能力 |
| --- | --- | --- |
| M0-L：本地合同通过 | 独立桌面入口、明确标记的 fake adapter、窄 IPC、代次/取消/资料投影/错误用例通过；Web 构建边界保持 | 真实登录、业务身份桥、盒端权限 |
| M0-R：正式只读闭环 | 固定版本，正式登录→选已绑定盒子→Direct→业务身份桥→资料分页→断开；两轮成功及跨设备/账号拒绝用例通过 | 聊天、事项写入、上传、BLE、全应用缓存隔离 |
| M1：知君领域与聊天 | 全域 transport/缓存、领域迁移、来源授权与流协议合流；建档→对话→判断→回访，事项/成果独立读写及清除合同通过 | 未完成的附件、媒体或其他平台 |
| M2：附件与媒体 | 冻结的上传四层合同、版本/保护/续传、chat-import 与消息闭环通过 | 未测试的大文件预算、其他固件及平台 |
| R：指定范围发布 | 按 M0-R、M1 或 M2 选定一个发布范围，完成该范围的固定产物、签名安装、升级/回退、退出验收 | 不要求 BLE 作为所有发布的前置；不推断未测 OS/CPU 已通过 |

M0-L/M0-R 是对既有 M0 的验收分层，不是两套产品实现。发布负责人记录实际包含的能力与合同版本；每个平台单独验收。

### 2026-09-06 实施状态与证据边界

| 任务 / 里程碑 | 当前状态 | 已交付与尚缺内容 |
| --- | --- | --- |
| M0-L；DESK-01/02/03/09/10/11/12/13/14 | 本地通过待外部验收 | 独立宿主与 Vue 入口、安全 IPC、代次、资料投影/调度/取消及合成流程已实现；逐任务未覆盖验收仍保留，见桌面明细 |
| BASE-01、BASE-05 | 本地通过待外部验收（子交付） | 开发版本/哈希审计、临时 userData、合成 adapter 与测试入口已建立；正式产物来源、身份、安全存储和真机环境未齐 |
| BASE-04 | 实施中（草案/fixture） | 五态、分页、256 KiB 与最小投影已有本地策略/夹具；跨团队归属、服务端裁剪与授权尚未冻结 |
| DESK-08 | 实施中（本地清理） | 有界 close、退出抢占、待决结算、单实例及自建宿主退出已覆盖；正式凭据/refresh/native SDK/sidecar 回收未验 |
| BASE-02/03；DESK-04–07、15–23；SERV-01–20；CONN-01–18 | 未开始 | 正式认证/身份桥/真机、全域业务迁移、流/上传和发布继续按依赖推进 |
| M0-R、M1、M2、R | 未验收 | 没有正式应用注册、真实 SDK 会话、盒子资料读取或安装包验收证据 |

实际路径以[桌面明细](tasks/DESKTOP-TASKS-0905.md)映射为准，开发输入见 [integration-release-baseline.json](integration-release-baseline.json)。当前宿主使用 `frontend/shell/runtime/`，前端使用 `src/desktop/` 单页，原设计中的多级 electron 目录和 desktop router 未照搬。

L1 为策略/调度/状态机/controller 单元与合同测试；L2 为真实 Electron 的真实 preload/main/页面搭配显式模拟 adapter。最终合流验证为 shell 45 项、前端 47 项（37 个原有测试文件项 + 10 个 desktop controller 子测试）、L2 3 项及 Web/Desktop 双构建通过。L3 的正式 SDK/盒子和 L4 安装包均未验，不能把 L2 的 Electron 进程视作 L3 真机。

E2E 的网络零请求断言仅覆盖窗口创建并安装 `request` 监听之后；启动更早阶段未被该监听捕获，另以入口依赖审查、受限 CSP 和 Electron session 阻断核对边界。详细命令、差额及环境以[本轮实施记录](M0-IMPLEMENTATION-0906.md)为准。

## 4. 公共前置任务

<a id="base-01"></a>

### BASE-01：固定跨仓源码与构建输入

**归属：** WP-00 / M0-L、M0-R；P0；集成负责人 + SDK/后端维护者；M（1–2 人日）。**依赖：** 无，可立即开始。

**当前状态：本地通过待外部验收（本地子交付）。** 已记录 `integration-release-baseline.json` 中各仓本地 SHA、dirty 状态、SDK tgz 哈希和 sidecar 审计，并将 shell 锁到 Electron 37.10.3。未安装运行时 SDK，未取得 data-engine dirty 变更的获批固定交付，sidecar dirty 输入尚未干净重建，正式版本门禁和平台兼容仍待验；下面跨仓完整步骤保持未勾选。

**文件与交付：** 当前各仓 `package.json`、lockfile、SDK release manifest；已新增 `docs/development/integration-release-baseline.json`，跨仓完整验收仍待完成。记录源码 SHA、相关 dirty 改动的可追溯交付、SDK tgz/sidecar 哈希、协议与 OS/CPU 范围，不写凭据。

- [ ] 对比知君、SDK、OS/Agent、data-engine 的本地及远程基线，登记实际依赖文件。
- [ ] 与维护者核对 data-engine 未提交 Pocket/上传等变更，取得固定提交或可校验归档；不代替维护者提交整个工作区。
- [ ] 核验 sidecar manifest 的构建输入与当前源码关系，选择可重建的 D05 开发组合；生产签名由发布任务完成。
- [ ] 验证现已升级的 shell Electron 37.10.3 与 SDK 组合及目标平台兼容，不仅按版本号推断可用。

**验收：** 在隔离检出中可定位依赖版本；缺产物、哈希不匹配或未知 dirty 输入使真实集成版本检查失败；本地模拟开发有单独标记的依赖清单。**证据：** 版本矩阵、哈希核验及兼容试验结果；真实组合未齐备则只记本地准备通过。

<a id="base-02"></a>

### BASE-02：落实知君正式应用身份

**归属：** WP-00 / M0-R；P0；控制面 + 桌面认证负责人；M（1–2 人日）。**依赖：** BASE-01 的版本清单；维护方提供合法配置与测试身份。

**文件与交付：** 拟新增配置 schema/无敏感信息示例及注册记录；确切 Consumer/Admin 服务仓库与注册入口由维护方登记。

- [ ] 固定 D02 applicationId、purpose、scopes、受信 Consumer/Gateway/JWKS 地址及登录回调合同。
- [ ] 确定 clientId 生命周期、安全存储 namespace、账号退出与 refresh 旋转行为。
- [ ] 准备至少两台已绑定设备及同盒不同账号的授权测试矩阵；不把设备在线提示当作授权。
- [ ] 明确配置由主进程读取，缺项返回配置未就绪；示例配置不能自动启用真实连接。

**验收：** 合法身份和无权限身份均有可重现预期；伪应用/错误 audience 或 scope 被拒；renderer 和日志无票据。**证据：** 脱敏配置版本、维护者责任记录、合法测试资源清单。模拟认证代码可在此项完成前编写。

<a id="base-03"></a>

### BASE-03：冻结连接到业务身份的可信桥合同

**归属：** WP-00、WP-03 / M0-R；P0；Agent + data-engine + 控制面 + SDK；L（2–4 人日）。**依赖：** BASE-01、BASE-02 的身份字段；并行准备主体传递草案。

**文件与交付：** `DESKTOP-CONTRACT-0905.md` D03、拟版本化桥合同与双方共享测试向量；Agent manifest、服务端 gate 的实施由 SERV-01/SERV-02 独占。

- [ ] 冻结验证者、传递载体和 account/client/device/application/scope/purpose 绑定，明确每层信任来源。
- [ ] 选择盒内可信交换或经批准的专用票据方案，写明签发/验签、期限、防重放、撤销、续期及重启行为。
- [ ] 明确错误码和安全公开字段；若选 JWT，验证真实票据长度满足当前 Bearer 上限。
- [ ] 定义 ready 的证据及缺桥拒绝行为，禁止依赖 loopback 或 renderer 信任头绕过 gate。

**验收：** 同一组有效、过期、撤销、错误设备/应用、伪主体向量可交给双方实现；未冻结字段保持显式未实现；不把 API 名称草案写成已部署端点。**证据：** 带版本的字段/时序/拒绝矩阵和双方负责人。

<a id="base-04"></a>

### BASE-04：冻结 M0 资料范围、归属与预算

**归属：** WP-00、WP-04 / M0-R；P0；后端 + 产品 + 桌面负责人；M（1–2 人日）。**依赖：** BASE-03 的主体定义；可先按桌面草案制作合成夹具。

**当前状态：实施中（草案/fixture）。** `frontend/shell/runtime/materials.cjs`、`runtime/adapters.cjs` 及策略测试已覆盖本地最小字段、五态和 256 KiB 边界。未冻结同盒多账号归属、服务端 folders 裁剪或跨层共享夹具；客户端拒绝/隐藏不替代服务端隔离，原正式步骤继续保留。

**文件与交付：** 桌面合同第5节、资料列表请求/响应 fixture（拟）；data-engine `backend/mindos/uploads.py`、`services/ingestion.py` 及主进程策略由后续任务修改。

- [ ] 明确盒级资料的共享/私有规则及同盒不同账号、设备转让后可见范围；列表 total 与条目使用同一过滤范围。
- [ ] 冻结分页、五种状态（含 queued）、最小字段及 256 KiB 原始响应预算。
- [ ] 确定服务端共享 folders 的裁剪方案与版本协商位置；客户端拒绝超限不能被当作完整服务端隔离。
- [ ] 覆盖空页、末页、变动数据、未知状态、超限和无权访问的共享 fixture。

**验收：** 桌面、Agent 与服务端对同一夹具有一致结论，响应不泄露其他主体目录或计数；没有人为扩大允许范围。**证据：** 字段/状态/归属矩阵及预算边界报告。资料规则不自动决定个人 Claim/会话归属，后者由领域任务冻结。

<a id="base-05"></a>

### BASE-05：建立隔离测试环境与交付证据规范

**归属：** WP-00、WP-05 / M0-L、M0-R；P0；测试 + 集成负责人；M（1–2 人日）。**依赖：** 无，可立即开始；真实环境部分依赖 BASE-02。

**当前状态：本地通过待外部验收（本地子交付）。** shell E2E 创建并清除临时 userData，仅用显式模拟 adapter 和合成内容，不启动 Python 或加载运行库，并检查本次宿主退出；实际入口为 `frontend/shell/tests/`、`frontend/mindos-web/tests/desktop-ui.test.mjs`。正式 credential store、后端模块路径覆盖拒绝与 native/sidecar 残留尚未验，不将本轮不加载后端写成已完成后端隔离故障测试。

**文件与交付：** [隔离运行说明](local-runtime.md)、既有 `scripts/run_tests.py`；拟新增 M0 fixture/测试辅助及验收记录模板，具体文件与 runner 在实现 PR 中登记。

- [ ] 父进程设置独立临时数据根与 secret store，清除 metadata/gbrain/MCP 等路径覆盖，再加载应用模块。
- [ ] fake adapter 明确启用并隔离网络出口；生成合成账号、设备和资料，不复制用户运行库。
- [ ] 区分 L1 单元、L2 组件、L3 真机、L4 安装包证据；只关闭本次创建的进程。
- [ ] 建立版本、场景、输入、期望/实际、关联 ID、通过/未验及清理结果的记录格式。

**验收：** 故意配置外部数据路径时测试入口拒绝或强制隔离；测试结束无自建进程残留；无真实数据/密钥进入证据。**证据：** 隔离入口检查和一份合成例子的报告；不把默认开发 supervisor 当测试包装。

## 5. 任务总览与派发顺序

共 **66 项**：公共前置5项、桌面与前端23项、服务端20项、连接与发布18项（其中2项为可选BLE）。以下索引链接到具体步骤；当前状态见第3节与任务正文，负责人和依赖保留原规划。

### 公共前置

| 任务 | 开发内容 | 目标 | 估算 |
| --- | --- | --- | --- |
| [BASE-01](#base-01) | 固定跨仓源码与构建输入 | M0-L / M0-R | M |
| [BASE-02](#base-02) | 落实知君正式应用身份 | M0-R | M |
| [BASE-03](#base-03) | 冻结连接到业务身份的可信桥合同 | M0-R | L |
| [BASE-04](#base-04) | 冻结 M0 资料范围、归属与预算 | M0-R | M |
| [BASE-05](#base-05) | 建立隔离测试环境与交付证据规范 | M0-L / M0-R | M |

### 桌面与前端

| 任务 | 开发内容 | 目标 | 估算 |
| --- | --- | --- | --- |
| [DESK-01](tasks/DESKTOP-TASKS-0905.md#desk-01) | 建立独立 desktop 构建和启动链 | M0-L | M |
| [DESK-02](tasks/DESKTOP-TASKS-0905.md#desk-02) | 实现窄 preload 和 IPC 安全边界 | M0-L | M |
| [DESK-03](tasks/DESKTOP-TASKS-0905.md#desk-03) | 实现主进程连接状态与代次控制 | M0-L | L |
| [DESK-04](tasks/DESKTOP-TASKS-0905.md#desk-04) | 实现正式登录适配和系统凭据存储 | M0-R | L |
| [DESK-05](tasks/DESKTOP-TASKS-0905.md#desk-05) | 实现共享刷新与认证撤销竞态 | M0-R | M |
| [DESK-06](tasks/DESKTOP-TASKS-0905.md#desk-06) | 实现授权设备列表与 ticket provider | M0-R | M |
| [DESK-07](tasks/DESKTOP-TASKS-0905.md#desk-07) | 装配 Direct SDK 与业务桥客户端端口 | M0-R | L |
| [DESK-08](tasks/DESKTOP-TASKS-0905.md#desk-08) | 实现断开、退出与宿主进程回收 | M0-R | M |
| [DESK-09](tasks/DESKTOP-TASKS-0905.md#desk-09) | 实现资料 query 校验和操作映射 | M0-L | M |
| [DESK-10](tasks/DESKTOP-TASKS-0905.md#desk-10) | 实现资料响应投影与安全错误映射 | M0-L | M |
| [DESK-11](tasks/DESKTOP-TASKS-0905.md#desk-11) | 实现读调度、去重和取消结算 | M0-L | L |
| [DESK-12](tasks/DESKTOP-TASKS-0905.md#desk-12) | 实现独立入口、状态页和快照订阅 | M0-L | M |
| [DESK-13](tasks/DESKTOP-TASKS-0905.md#desk-13) | 实现 M0 资料列表与分页状态 | M0-L | M |
| [DESK-14](tasks/DESKTOP-TASKS-0905.md#desk-14) | 建立 M0 隔离合同与故障回归集 | M0-L | L |
| [DESK-15](tasks/DESKTOP-TASKS-0905.md#desk-15) | 执行正式 M0 跨仓联调与验收 | M0-R | L |
| [DESK-16](tasks/DESKTOP-TASKS-0905.md#desk-16) | 收敛普通 RPC 与路由任务网络入口 | M1 | L |
| [DESK-17](tasks/DESKTOP-TASKS-0905.md#desk-17) | 收敛 SSE/聊天入口与能力门控 | M1 | M |
| [DESK-18](tasks/DESKTOP-TASKS-0905.md#desk-18) | 建立全域缓存归属与生命周期基础设施 | M1 | M |
| [DESK-19](tasks/DESKTOP-TASKS-0905.md#desk-19) | 隔离聊天草稿、失败恢复与待处理对话 | M1 | L |
| [DESK-20](tasks/DESKTOP-TASKS-0905.md#desk-20) | 隔离事项与章程编辑状态和操作键 | M1 | L |
| [DESK-21](tasks/DESKTOP-TASKS-0905.md#desk-21) | 接入事项、成果与历史分页和单版读取 | M1 | L |
| [DESK-22](tasks/DESKTOP-TASKS-0905.md#desk-22) | 呈现事项写幂等、revision 冲突与不确定结果 | M1 | M |
| [DESK-23](tasks/DESKTOP-TASKS-0905.md#desk-23) | 接入清除预览、显式确认和进度清理 | M1 | L |

### 服务端与领域

| 任务 | 开发内容 | 目标 | 估算 |
| --- | --- | --- | --- |
| [SERV-01](tasks/SERVER-TASKS-0905.md#serv-01) | Agent 可信身份桥适配 | M0-R | L |
| [SERV-02](tasks/SERVER-TASKS-0905.md#serv-02) | data-engine 业务 gate 与会话生命周期 | M0-R | L |
| [SERV-03](tasks/SERVER-TASKS-0905.md#serv-03) | 资料归属及有界响应投影 | M0-R | L |
| [SERV-04](tasks/SERVER-TASKS-0905.md#serv-04) | 建立完整依赖闭包与迁移清单 | M1 | M |
| [SERV-05](tasks/SERVER-TASKS-0905.md#serv-05) | 冻结领域部署、owner 与 Claim 规则 | M1 | M |
| [SERV-06](tasks/SERVER-TASKS-0905.md#serv-06) | 单一领域装配入口与基础服务适配 | M1 | L |
| [SERV-07](tasks/SERVER-TASKS-0905.md#serv-07) | 会话及附件领域模块与 schema 迁移 | M1 | L |
| [SERV-08](tasks/SERVER-TASKS-0905.md#serv-08) | 本体、路由、alignment 与事项存储迁移 | M1 | L |
| [SERV-09](tasks/SERVER-TASKS-0905.md#serv-09) | 成长、章程及工作区模块迁移 | M1 | L |
| [SERV-10](tasks/SERVER-TASKS-0905.md#serv-10) | 同库事务与跨库故障恢复 | M1 | L |
| [SERV-11](tasks/SERVER-TASKS-0905.md#serv-11) | 唯一 worker、启动恢复与有界停机 | M1 | L |
| [SERV-12](tasks/SERVER-TASKS-0905.md#serv-12) | 受保护资料与版本摄取策略 | M1 | L |
| [SERV-13](tasks/SERVER-TASKS-0905.md#serv-13) | 领域路由装配、权限及错误合同 | M1 | L |
| [SERV-14](tasks/SERVER-TASKS-0905.md#serv-14) | 事项列表、成果及历史有界分页 | M1 | L |
| [SERV-15](tasks/SERVER-TASKS-0905.md#serv-15) | 稳定客户端输入幂等与 revision 冲突 | M1 | L |
| [SERV-16](tasks/SERVER-TASKS-0905.md#serv-16) | 冻结清除范围、回放保留与恢复规则 | M1 | M |
| [SERV-17](tasks/SERVER-TASKS-0905.md#serv-17) | 可恢复清除与回放防复活 | M1 | L |
| [SERV-18](tasks/SERVER-TASKS-0905.md#serv-18) | 设备投影与 Claim 事实源隔离 | M1 | L |
| [SERV-19](tasks/SERVER-TASKS-0905.md#serv-19) | 整组迁移、故障注入与恢复演练 | M1 | L |
| [SERV-20](tasks/SERVER-TASKS-0905.md#serv-20) | 服务端合同合流与发布前交接 | M1 | M |

### 连接与发布

| 任务 | 开发内容 | 目标 | 估算 |
| --- | --- | --- | --- |
| [CONN-01](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-01) | 冻结长请求协议与失败语义 | M1 | M |
| [CONN-02](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-02) | 实现 Core 与 Agent 的有界增量通道 | M1 | L |
| [CONN-03](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-03) | 扩展 sidecar 与 Electron SDK 流 API | M1 | L |
| [CONN-04](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-04) | 接入宿主窄流 IPC 与共享调度 | M1 | M |
| [CONN-05](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-05) | 保留前端 SSE 解析与有界重预览行为 | M1 | L |
| [CONN-06](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-06) | 闭合远端停止、落库与重启恢复 | M1 | L |
| [CONN-07](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-07) | 完成聊天与领域端到端验收 | M1 | M |
| [CONN-08](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-08) | 冻结四层上传合同与可追溯服务端来源 | M2 | M |
| [CONN-09](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-09) | 补齐后端上传一致性与 Agent 授权 | M2 | L |
| [CONN-10](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-10) | 实现宿主分片上传与额度管理 | M2 | L |
| [CONN-11](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-11) | 实现上传 UI、同主体续传与状态恢复 | M2 | L |
| [CONN-12](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-12) | 接入版本上传与受保护 chat-import | M2 | L |
| [CONN-13](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-13) | 实现受控媒体预览与对象 URL 回收 | M2 | M |
| [CONN-14](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-14) | 建立独立桌面打包与 sidecar 完整性验证 | R | M |
| [CONN-15](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-15) | 建立跨仓版本检查与发布能力门禁 | R | M |
| [CONN-16](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-16) | 验证签名安装包、升级与退出回收 | R | L |
| [CONN-17](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-17) | 冻结可选 BLE 的平台、固件与安全合同 | R | M |
| [CONN-18](tasks/CONNECTION-DELIVERY-TASKS-0905.md#conn-18) | 实现隔离 BLE 适配与真机验收 | R | L |

### 后续派发与合流顺序

| 顺序 | 可派发任务 | 输出 / 转入下一批条件 |
| --- | --- | --- |
| 本地前置已交子集 | BASE-01、BASE-05；BASE-04草案fixture | 本地输入和隔离已可复现；正式版本、环境及归属继续补齐，SERV-04调查未开始 |
| 本地桌面基础已通过 | DESK-01/02/03/09/10/12 | 已实现独立入口、窄IPC、状态机和草案资料策略；按逐项差额补充验收 |
| 本地资料合流已通过 | DESK-11/13/14；DESK-08本地子集 | M0-L通过；真实清理另验。DESK-18仍未开始，可在合同对齐后独立推进 |
| 同期外部链路 | BASE-02→03→04；SERV-01→02→03；DESK-04→05→06→07→08 | 正式应用、可信桥、资料归属与预算交付 |
| 正式M0 | DESK-15 | M0-R实际资料读取与拒绝矩阵通过，可进入M0范围发布 |
| 完整业务三线并行 | DESK-16–23、SERV-04–20、CONN-01–06按各自依赖接力 | 全域端口/缓存/页面、独立领域合同、流与持久恢复 |
| M1合流 | CONN-07 | 真实领域与聊天主链；SERV-20独立交接，不反依赖此项 |
| 附件与媒体 | CONN-08–13 | M2包含处理→授权引用→回复→版本恢复，不只验上传API |
| 指定范围发布 | CONN-14→15→16；CONN-17→18仅在批准BLE后启动 | R-M0/R-M1/R-M2逐级累计，平台/产物单独验收 |

箭头表示合流依赖，不表示只能一人串行工作。BASE-01本地版本、BASE-04草案fixture及BASE-05隔离子交付可先满足M0-L；正式版本和真实权限须在DESK-15前完成。共享文件按第6节归属串行合入。

### 原始工作量估算汇总（不是当前剩余工时）

| 范围 | 任务集合 | 初步合计（人日） |
| --- | --- | --- |
| 首条正式M0 | BASE-01–05 + DESK-01–15 + SERV-01–03 | 33–66 |
| M1增量 | DESK-16–23 + SERV-04–20 + CONN-01–07 | 54–108 |
| M2增量 | CONN-08–13 | 10–20 |
| 指定范围发布 | CONN-14–16 | 4–8 |
| 可选BLE增量 | CONN-17–18 | 3–6 |

完整M0→M1→M2加指定范围发布暂估 **101–202人日**，包含可选BLE为 **104–208人日**。这是分层开发/验收任务的工作量相加，不是日历工期或外部承诺。只交付M0时取M0及所选发布任务；完成BASE与最初原型后据实缩小区间，不把未知等待计入开发工作量。

## 6. 开发协作与合流规则

公共合同和跨仓基线由集成负责人维护。一个任务以一个可评审 PR 为目标；跨仓任务分别提交，并在任务记录中关联成套版本，任何一仓合并不能单独宣称端到端完成。测试文件随其被测模块一起归属，不能让多人同时重写入口、schema 或策略表。

| 共享改动位置 | 主要归属 | 合流规则 |
| --- | --- | --- |
| 知君 shell main/preload、安全配置、根桌面启动器 | 桌面任务负责人 | 其他主线提出接口变更，由宿主负责人接入；不复制第二套入口 |
| 主进程 consumer/会话状态机、generation、refresh coordinator | 桌面生命周期负责人 | 流/上传复用主体与代次，不能各自持独立登录态 |
| 主进程 request/response policy 与调度 | 桌面策略负责人 | M0先落最小操作；流/上传按冻结版本增加独立处理器，统一审核策略表 |
| 前端统一 transport/缓存、`api.ts` | 前端适配负责人 | 先交付端口，再接聊天与上传；现有Web行为保留独立验证 |
| `sse.ts`、`chatStream.ts`、聊天恢复UI | 聊天连接任务负责人 | 在统一transport端口上串行集成；前端基础任务不并行重写同一文件 |
| data-engine 服务装配、schema、共享连接与worker | 服务端领域负责人 | schema/生命周期有唯一合流人，各表族迁移不自行开启第二个worker |
| data-engine uploads/QA 与资料保护 | 服务端保护负责人 | 上传协议负责人提交协议变更，保护与版本规则审查后串行合流 |
| Agent manifest/HTTP通道、Core/SDK合同 | 各仓连接负责人 | 身份桥、流、上传分别实现；同一manifest/合同版本由仓库负责人统一发布 |

若采用多人并行，建议按宿主、Vue、服务端领域、连接平台、测试/发布分工；控制面和产品为指定协作角色。角色可以由同一人承担，此时按依赖顺序串行推进；不假定团队已有固定人数。

派发前填写：`任务ID / 负责人 / 执行仓库与分支 / 输入版本 / 可开始的子步骤 / 未决输入 / 预计投入 / 目标验收层级`。合流时填写：`PR与提交 / 实际文件 / 实际测试命令及结果 / 未验场景 / 回退方式 / 后继任务可用版本`。

新配置、新路由、状态枚举、响应预算、schema 或流协议若改变既有规格，必须同一轮更新合同和对应测试。任务 ID 保持稳定；拆分时保留父任务及新子任务映射。修复不会自动把后续全部工作包标为完成。

## 7. 任务编制验证（历史）与开发验证入口

原任务编制阶段仅检查文档链接、任务 ID 唯一性、依赖可解析与无环、WP-00–WP-10 覆盖、实施/验收字段和当前事实一致性，不运行产品服务或业务回归。后续开发按任务增量验证；现有命令与隔离要求见工作包第4节，不能把尚不存在的测试脚本写成可执行入口。

2026-09-06 文档验证结果：66个唯一任务ID、262个实施步骤、240处显式任务依赖引用无环，全部任务具备实施/验收/证据要求；8份Markdown的220处本地链接及66个任务锚点有效，WP-00–WP-10全部覆盖，估算总计与明细一致。检查同时人工区分了草案子交付、正式验收与按发布范围选择的依赖；图源码未改变，保留既有4张Mermaid图与SVG。

交叉审核补齐了普通领域RPC的main侧适配、M1路由/建档守卫、新分页/幂等/清除的前端任务，以及附件到回复的M2关闭条件。当时所有开发复选框未勾选，该历史记录仅说明任务文档通过检查；本轮 M0-L 的实际进度和运行验证已在第3节、桌面明细及实施记录回填。
