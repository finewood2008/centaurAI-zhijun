# 知君 MVP 实现说明

状态：首个可运行纵向闭环

对应 PRD：`ZHIJUN_PRD_V1.md`

实现底座：现有 `nexusaos-data-engine` 仓库

## 1. 本期交付边界

本期不是对 24 周 PRD 的一次性实现，而是先交付一条真实、可持久化、可回归的产品闭环：

> 人生章程 → 记录关键判断 → 今日跟踪 → 记录真实结果 → 完成成长复盘

现有原材料、知识卡片、本地搜索、证据问答、图谱与治理能力继续作为知君的数据底座。本期 Growth Store 只保存用户明确提交的应用状态，Entity ID 与 EvidenceRef 仅作为不透明引用，不自动创建、确认或改写 Claim，避免形成第二事实源。

## 2. 已实现能力

### 人生章程

- 创建章程并保存为不可变版本。
- 更新章程时生成新版本，不覆盖历史内容。
- 新判断原子绑定创建当时的 `charterId` 和 `charterVersion`。
- 章程读取使用同一 SQLite 快照，避免并发更新时当前版本与历史列表不一致。

### 判断与成长闭环

- 记录主题、背景、选项、最终选择、理由、信心度、预期结果和观察时间。
- 状态只能按 `open → outcome_recorded → reviewed` 前进。
- 结果和复盘不能重复提交或越级提交；冲突返回 HTTP 409，第一次确认的内容不会被覆盖。
- 复盘必须由用户确认反思、至少一条经验和下一步行动。
- 完成后的复盘随判断历史持久化展示，服务重启后仍可读取。

### 今日

- 聚合逾期判断、待复盘结果和未来七天到期判断。
- 固定优先级为“逾期 → 待复盘 → 即将到期”，全局最多展示 3 项。
- 页面展示完整统计，Top 3 截断不会造成数字失真。
- 今日页与原有资料概览独立加载、独立失败，任一接口失败不会拖垮另一部分。

### 本地数据与安全

- `growth.db` 位于 `runtime_paths.GROWTH_DB_PATH`，属于统一 `CENTAURAI_DATABASE_DATA_ROOT` 数据根。
- MVP 明确采用单盒、单 PersonalVault 边界，不跨设备或跨 Vault 混读。
- SQLite 启用 WAL、foreign keys、busy timeout；关键状态变化使用事务。
- 所有 Growth API 经过既有 MindOS Web 访问 Gate；写请求额外要求本机 loopback 与 CSRF 头。
- 本期 Growth 功能不调用云模型，不产生新的出盒数据。

## 3. 主要接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST | `/api/mindos/growth/charter` | 当前章程、版本历史与创建新版本 |
| GET / POST | `/api/mindos/growth/decisions` | 判断列表与创建判断 |
| POST | `/api/mindos/growth/decisions/{id}/outcome` | 记录真实结果 |
| POST | `/api/mindos/growth/reviews` | 完成成长复盘 |
| GET | `/api/mindos/growth/today` | 今日 Top 3、章程摘要、最近复盘与完整统计 |

## 4. 本期不伪装完成的事项

以下能力仍属于后续里程碑：

- 分步式首次建档、章程复审日期与历史版本详情浏览。
- 判断补充、关闭、重新打开及完整修订轨迹。
- 从现有本体只读推荐相关证据、人物、项目和类似历史判断。
- 用户级提醒策略、静默、延后、重复打扰抑制及系统状态提醒。
- 可中途退出并恢复的 `ReviewDraft`。
- `PatternCandidate` 的 hypothesis → confirmed/rejected 生命周期。
- 基于已确认事实和章程、带依据与替代解释的“良师”建议。
- 面向万象和其他 Agent 的最小化 Context Pack。

## 5. 本地运行与验收

```bash
# 后端（按仓库既有安装方式准备 backend/.venv）
./start-backend.sh

# Web 开发服务
./start-web.sh
```

浏览器访问 `http://127.0.0.1:5173/mindos/`，依次完成：

1. 在“成长”创建人生章程 v1，再保存一个新版本。
2. 创建判断并设置观察时间，确认卡片显示所绑定的章程版本。
3. 在判断卡片记录真实结果。
4. 完成复盘并刷新页面，确认反思、经验和下一步仍存在。
5. 回到“今日”，确认主要事项不超过 3 项，统计仍显示完整数量。

专项验证命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/run_tests.py tests/test_mindos_growth.py -q
npm --prefix frontend/mindos-web run test:growth-frontend
npm --prefix frontend/mindos-web run test:p14-frontend
npm --prefix frontend/mindos-web run typecheck
npm --prefix frontend/mindos-web run build
bash scripts/check-web-no-electron.sh
.venv/bin/python scripts/check_release_guard.py
```
