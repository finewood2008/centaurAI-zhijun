# 知君敏感规则管理 V2 接入计划

## 验收标准

- 使用知君后端保存的同一 Data Agent App 凭据访问 `/v1/agent/apps/sensitive-delivery/*`；浏览器只访问知君代理，且功能依据实际 `/capabilities` 授权显示。
- 自定义规则保留新增、编辑、启停、删除；单条读取与写入保留服务端原始 ETag，修改和删除使用 `If-Match`，创建重试复用幂等键。
- 内置规则依据服务端 `editableFields`、`systemConstraints` 编辑和启停，可恢复系统默认；支持相似规则二次确认，并显示覆盖待复核。
- 识别语义变化后显示高层扫描状态；只有用户明确确认且具有独立授权时才启动或重试历史扫描，目标修订来自最新 `/rollout/status`。
- 权限不足、冲突、修订变化、容量超限与服务不可用时保持安全状态；检索主流程不改。

## 分工与依赖

1. 后端客户端：`backend/zhijun_worker/data_agent_rag_v2.py` 与专属测试，扩展响应验证、ETag、内置规则和扫描 API。
2. 知君代理：`backend/mindos/sensitive_rule_routes.py` 与路由测试，提供严格的页面 DTO、权限和错误边界；主代理负责。
3. 前端：`frontend/mindos-web/src/components/settings/SensitiveRulesPanel.vue`、`src/services/api.ts`、`src/services/sensitiveRuleStatus.ts` 与前端测试，依据代理 DTO 实现管理 UI。
4. 产品目录：`frontend/shared/product-operations.json` 及传输合同测试，登记新代理操作；主代理负责。

依赖顺序：客户端/页面可并行；代理与产品目录使用约定 DTO 接线；最后统一测试与集成审查。
不替管理员扩大现有 App 能力，也不自动触发扫描或真实规则变更。

## 验证步骤

- 客户端和代理单测：身份、能力、ETag/If-Match、幂等、相似确认、扫描状态与错误脱敏。
- 前端测试、类型检查、构建、产品操作传输测试。
- 对照 Data Agent OpenAPI 和真实服务只读能力/目录状态；若现场能力尚未配置，界面应给出清楚提示。

## 现场约束（192.168.0.7）

工作区长期 App 凭据已经存在，实际能力为 policyWrite=true、builtinWrite=false、rolloutManage=false。
盒端现运行 Data Agent 为本功能上线前版本，尚无内置编辑和 rollout 接口；检测库与规则库还缺三张新增表。
盒端预识别与历史扫描开关目前均关闭。因此本次接线不能视作已批准扩大 App 能力，也不能自动启动扫描。
上线须把新客户端、代理、catalog、桌面界面和 Data Agent 的 17 个相关生产文件一同更新，并先备份数据、在隔离副本上验证启动迁移；旧容器与备份保留回退。

## 2026-09-17 交付验证

- 知君后端定向测试 38 项、前端定向测试 41 项、前端类型检查和桌面端构建通过。macOS arm64 生产安装包 0.1.36 的验包与启动冒烟测试通过。
- 公司盒采用精确旧镜像的 20 文件覆盖镜像。隔离迁移验证新增 3 张空表，并确认原有 11 张表的结构、行数、逐行哈希不变；切换后同样的只读校验通过。
- 生产容器切换后健康，旧容器和完整状态备份保留。在线只读接口校验确认：规则列表、内置规则详情的 `editableFields`、`systemConstraints`、`etag` 均可读取。
- 当前 App 仍仅有 `policyWrite`；`builtinWrite`、`rolloutManage` 为 false，扫描开关关闭。因此界面依据实际能力限制内置编辑和历史扫描，未扩大授权或自动启动扫描。
