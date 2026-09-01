# MindOS 前端视觉截图与回归清单（FE-UI-022）

本目录固化改版后的页面视觉基线，防止后续样式漂移。

## 目录结构

```text
tests/visual/
├── screenshot.mjs   # Playwright 截图脚本
├── shots/           # 截图输出（运行时生成，勿提交）
└── README.md        # 本说明与回归清单
```

## 截图流程

1. 确保后端与前端已启动：FastAPI（8618）与 `npm run dev`（默认 5173）。
2. 安装截图依赖与浏览器（首次）：
   ```bash
   npm i -D playwright
   npx playwright install chromium
   ```
   > 若系统限制写入用户目录（如沙箱环境），可将浏览器下载到项目内：
   > ```bash
   > $env:PLAYWRIGHT_BROWSERS_PATH="$PWD\.pw-browsers"; npx playwright install chromium
   > ```
   > 该目录已加入 `.gitignore`。
3. 运行截图（自定义地址/浏览器路径可用环境变量）：
   ```bash
   npm run screenshots
   ```
   ```bash
   # 示例：指向非默认地址
   $env:MINDOS_BASE_URL="http://localhost:5199/mindos/"
   $env:PLAYWRIGHT_BROWSERS_PATH="...\.pw-browsers"
   npm run screenshots
   ```
4. 产物位于 `tests/visual/shots/<route>-<viewport>.png`，共 8 路由 × 4 视口：
   - desktop：1440 × 900
   - tablet-lg：1024 × 768
   - tablet：768 × 1024
   - mobile：390 × 844
   - 覆盖路由：首页、导入、原材料、知识成品、搜索、问答、图谱、治理。
5. 响应式断言：脚本对每页每个视口校验 `scrollWidth <= innerWidth`（无横向滚动），违规则以退出码 1 失败。

## 回归对比清单

每次改版后按以下维度对比 `shots/` 新旧截图（建议配合 Git 提交查看）。

### 1. 主色与主题
- [ ] 主色为 `#0077ff` / `#1b99ff`，背景白/浅灰，无暖棕色残留。
- [ ] 主按钮为蓝色，危险操作为红色，状态 Badge 文案 + 语义色一致。

### 2. 侧栏与顶栏
- [ ] 桌面 ≥768px 侧栏常驻；768–1199px 折叠为图标栏；<768px 抽屉正常。
- [ ] 当前路由高亮；顶栏标题与全局搜索位置稳定。
- [ ] 移动端关闭抽屉后无遮罩残留、无横向滚动。

### 3. 表格与列表
- [ ] 表头浅灰背景、行 hover 轻蓝背景。
- [ ] 复杂表格（原材料、导入队列）在小屏可横向滚动，不撑破页面。
- [ ] 空状态与错误状态（重试）样式统一。

### 4. 弹窗与反馈
- [ ] ConfirmDialog 居中、可 Escape 取消、焦点进入弹窗。
- [ ] Toast 底部居中，2.5s 自动关闭，错误可手动关闭。

### 5. 响应式（1440 / 1024 / 768 / 390）
- [ ] 无意外横向滚动（`scrollWidth === innerWidth`）。
- [ ] 图谱、详情页小屏不遮挡主操作，检查器/属性栏转为纵向。

### 6. 无障碍基线
- [ ] 图标按钮有 `aria-label`/`title`；Tab 顺序合理、焦点可见。
- [ ] 状态不只依赖颜色（Badge 均有文字）。

## 说明

- `shots/` 为运行产物，建议加入 `.gitignore`；如需提交基线图，保留并按 Git 提交对照。
- 当前后端未就绪时截图会记录错误态（ErrorState），属于有效基线；数据就绪后应补正常态截图。
