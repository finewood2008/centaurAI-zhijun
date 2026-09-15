# 最近功能集中回归（2026-09-15）

## 范围与计划

目标：验证当前工作区的最近修复，包括测试汇总 A1–A4、B1–B6、回答后整理故障 S1，以及登录恢复、连接恢复、请求调度、容量错误、设备扫描配网。

- 主代理：Shell 全量测试、桌面构建、可用 Electron 合成端到端测试、结果汇总。
- 后端回归代理：本体候选/整理队列/权限、流式收尾、事情绑定隔离、检索和材料生命周期，按仓库隔离测试入口执行。
- 前端回归代理：前端全量测试，输入框和原材料页面的合成接口浏览器交互。

依赖与文件：复用现有 tests 和 scripts，不调整业务代码、不重打发行包、不部署或写入真实盒端/账号数据。允许校正过时测试定位与补充回归断言；测试生成目录与构建输出不是安装包发布。

验收：记录每个测试命令及真实结果；失败定位后明确报告，不通过弱化断言掩盖问题；本体核对、材料外发确认、会话与事情隔离不得放宽。合成模型/接口结果与真实模型、真实多设备、蓝牙硬件验证分开列出。

## 进度

- [x] 核对当前改动与测试团队问题清单。
- [x] Shell、前端与后端自动化回归。
- [x] 构建和可用界面交互回归。
- [x] 汇总失败、覆盖和真机验证边界。

## 主代理执行结果

使用 Node 22，命令在各自项目目录执行（shell 命令经 RTK 代理）。

| 目录 / 命令 | 结果 |
| --- | --- |
| `frontend/shell` / `npm test` | 335 通过，0 失败，1 项 Windows 专用测试跳过 |
| `frontend/mindos-web` / `npm run build:desktop` | 类型检查及 Vite 构建通过；保留 taskRouting 混合静态/动态导入的分块提示 |
| `frontend/shell` / `npm run test:e2e` | 4/4 通过：真实 Electron 协议、preload、权限隔离、合成登录/切盒/退出 |
| `frontend/shell` / `node tests/product-navigation.e2e.cjs` | 首次旧测试定位失败；校正后 1/1 通过，详见下文 |
| `frontend/mindos-web` / `node tests/secure-connection.e2e.mjs` | 原有安全连接矩阵及补充登录恢复 DOM 回归全部通过 |

### 回归发现与处理

`product-navigation.e2e.cjs` 仍要求连接失败页面显示旧账号 banner。新版 `SecureConnectionProgress` 已取代该 banner，导致定位失败，但诊断中无 renderer error、console error、网络请求或错误 toast。校正测试为：通过真实 preload snapshot 确认账号/failed 状态仍保留，页面显示安全连接失败卡、不出现登录表单；重新选择盒子后账号文字和刷新设备入口可见。身份与恢复断言没有删除或放宽。重跑通过，本轮不涉及业务逻辑修改。

### 已复看的截图

安全连接 360px 截图：`/var/folders/77/yz4bycbs0pvbjtkwx2hcn0cr0000gn/T/zhijun-secure-connection-e2e-wOeffI/connecting-360.png`。步骤竖排、取消按钮和顶栏均可见，无横向溢出；该目录还包含 768px/1440px 和中继就绪截图。均为合成 IPC 场景。

## 前端与登录恢复交互

- `npm run test:all`：220/220 通过，0 失败、0 跳过。包含真实 Composer SFC 合成浏览器验证（桌面语音/Web Speech × 1280/390px）。
- `node tests/raw-materials-layout.e2e.mjs`：1440、1200、1199、1101、1100、768、760、390px 八种宽度通过；覆盖分页、长文件名、操作按钮、软回收预览/取消/确认、返回 `/data`，无非预期请求或页面异常。
- `node tests/product-navigation.e2e.mjs`：主导航、15 个业务页面、故障断连恢复、过期回登录通过。
- 扩展 `secure-connection.e2e.mjs` 后验证实际 DOM：账号服务故障保留账号、不误显示无绑定；推进 30 秒无自动重试/退出/业务请求；一次点击只触发一次重试，等待时禁用刷新，成功后展示设备；点击“重新登录”只退出一次；独立的 `SESSION_EXPIRED` 状态直接展示登录表单，不触发额外退出循环。

最终恢复截图目录：`/var/folders/77/yz4bycbs0pvbjtkwx2hcn0cr0000gn/T/zhijun-secure-connection-e2e-lTw5ck`，主代理已复看 `account-service-unavailable.png` 和 `session-expired-login.png`。

其他截图目录：Composer 为 `/var/folders/77/yz4bycbs0pvbjtkwx2hcn0cr0000gn/T/zhijun-composer-Ji4l03`；原材料为 `/var/folders/77/yz4bycbs0pvbjtkwx2hcn0cr0000gn/T/zhijun-materials-layout-ofSyBU`。回归代理已复看 Composer 390px 和原材料 760px，无遮挡。原材料 390px 已测但未保存截图。

本轮仅修改两个测试脚本及本报告，未修改业务实现。`git diff --check` 通过。A2/B3 的部分断言仍为抽取函数和模板契约测试；不能将此记录理解为全部业务已完成真实盒端端到端验收。

## 后端核心回归

24/24 模块通过：388 个测试 + 117 个子测试，JUnit 合计 505；失败、错误、跳过均为 0。使用根目录 `backend/.venv/bin/python scripts/run_tests.py --isolated-modules` 加下列模块路径及 `-- -q --tb=short`，每个模块独立临时数据根。

模块名均为 `tests/test_<名称>.py`：

```
memory_followup memory_admission memory_queue memory_policy
memory_background_consent zhijun_stream_completion_diagnostics
matters matters_independent retrieval_tools retrieval_chat_integration
rag_material_review mindos_p15_02_03_lifecycle store_connection_lifecycle
zhijun_worker_ports workspace_routing_configuration workspace_model_settings
workspace_gpu_deployment runtime_config_provider zhijun_extract context_plan
memory_context_routing file_center_recycle task_routing zhijun_turn_sse
```

主要断言：

- 本体承接用户原话、生成待核对候选、去重和跨会话拒绝；不是把模型口头回复当作已保存。
- 正文完成后后台登记正常、429 拒绝或契约错误，保持相应 SSE 收尾；失败整理不自动执行，重放不重复。
- 事情换题暂停、绑定修订与并发冲突、成果来源及权限。
- 文件名/项目检索、仅选中片段交付，过期证据和授权/服务错误不伪装为无结果。
- 软回收/恢复、数据库连接释放、工作区与模型配置、GPU 跨仓库请求契约（实际运行未跳过）。

仅有 10 条 Starlette/AnyIO `BlockingPortal` 弃用警告，不影响本次结果。模型和检索采用合成实现，S1 回调为本地 HTTP，并未调用真实中转站或改动盒子资料。

### 补充接口兼容组

另外 6/6 模块通过：67 个测试 + 42 个子测试，JUnit 合计 109，失败/错误/跳过均为 0（与核心组分开记录）。模块为 `zhijun_provider`、`retrieval_only_boundary`、`sensitive_rule_status`、`sensitive_rule_routes`、`conversation_management_compat`、`mindos_material_card_projection`。

覆盖模型调用与检索边界、敏感规则状态新旧契约与 Retry-After、规则写保护、历史对话兼容及原材料卡片状态；仅 4 条同类依赖弃用警告。

## 尚不能由本轮证明的事项

- 未使用用户真实账号执行云端登录、短信注册/重置密码或真实中转站调用，不能据合成测试确认现场网络故障已消失。
- 未在真实盒子写入本体、资料或事情；真实 RAG 索引状态、召回质量及实际模型回答仍需安装后验收。
- 蓝牙扫描结果增删、取消、过期设备禁选和安全配网流程通过自动化，但未操作蓝牙硬件、输入 Wi-Fi 或认领真实设备。
- 多账号/多会话和并发绑定在隔离测试中覆盖，不等于两台实体电脑同时操作的验收；外网打洞成功率与连接耗时也不由本轮合成测试代表。
- Windows 原生 PowerShell 权限测试需在 Windows 上执行；本次 macOS 跳过该项。
- 当前测试针对最新源码。未生成新 DMG、未更新已安装客户端或盒端。0.1.26 安装包仍不包含上一轮登录恢复源码修复。
