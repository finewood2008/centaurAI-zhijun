# 知君工程迁移

## 来源与范围

- 来源：`https://github.com/finewood2008/centaurAI-zhijun`
- 源分支：`main`
- 源提交：`67161ed46296d7299e26cca522594d9a29c2bd82`
- 目标：Codeup `nexusaos/nexusaos-centuarai-zhijun`，`master` 分支。
- 迁入源仓库的 684 个受版本管理文件，包含 Vue 前端、Electron 壳、FastAPI 后端、测试、启动/部署脚本及产品文档。前端调用自带后端 API，生产构建由后端在 `/mindos/` 提供。
- 保留目标仓库 Git 历史及 `.aliyun/pull_request_template.md`。不迁入源 Git 历史、本机依赖、运行数据或模型缓存。

## 执行计划与验收

1. [x] 确认源版本、目标仓库及远程，检查本机路径依赖。
2. [x] 迁移受版本管理文件，保留相对目录关系。涉及 `frontend/`、`backend/`、`scripts/`、`docs/`、部署配置及根启动脚本。
3. [x] 补齐 shell 启动脚本可执行权限，忽略后续自动生成的浏览器截图，并从 Git 和 Docker 上下文排除本地密钥目录，明确 Node 测试版本及后端测试入口。
4. [x] 安装依赖，执行前端类型检查、生产构建及现有回归测试；验证后端核心流程。
5. [x] 核对文件完整性、提交内容及 Git 差异检查。
6. 本记录随迁移提交推送至目标 `master`；最终提交号与推送状态以远程 Git 记录为准。

验收要求：源码完整；前端构建和核心回归检查通过，记录其他检查的失败与范围；本机依赖、运行时测试数据与构建产物不入库（保留源测试夹具和界面基线）；提交成功推送并与远程分支一致。

## 本机运行

按根 README 安装依赖并构建前端，然后执行 `./start-backend.sh`，访问 `http://127.0.0.1:8618/mindos/`。开发前端可另执行 `./start-web.sh`。真实对话需要配置可用模型；`ZHIJUN_PROVIDER=fake ./start-backend.sh` 可在开发环境演示与联调。

## 验证记录

- 已通过：`npm ci`、`npm run typecheck`、`npm run build`。
- 已通过：`npm run test:p14-frontend` 及 `node --experimental-strip-types --test tests/*.test.mjs`（全部 32 个测试文件）。
- 已通过：`bash scripts/check-web-no-electron.sh`、shell 启动脚本语法检查、Electron 薄壳 JavaScript 语法检查。
- 已通过：18 个后端核心测试模块，共 150 项测试，无失败或跳过。使用 `scripts/run_isolated_qa.py --workers 2 backend/tests/test_zhijun*.py backend/tests/test_data_root_contract.py backend/tests/test_instance_lock.py backend/tests/test_mindos_local_web_debug.py` 分模块隔离运行。
- 未通过：源工程原有 `scripts/e2e_zhijun_phase1.py`。后端启动、演示模型和首轮 SSE 成功；随后旧断言期望至少 1 条已确认理解，实际为 1 条待确认、0 条已确认，后续步骤未执行。相关业务代码与源提交保持一致，此次迁移未修改确认规则或放宽断言。
- 已通过：真实后端服务 + Chrome 浏览器启动检查，1440px 与 390px 两档宽度。新数据目录访问首页、对话、本体、判断、资料入口均按首次使用流程重定向到「第一次认识」建档页，页面正常渲染，无未捕获 JavaScript 异常；本次未在浏览器完成建档后的各页面交互。检查使用临时数据、临时密钥目录及独立端口 18618，并仅在检查进程中关闭外部 Agent 记忆自动导入。
- 已核对：684 个源文件无缺失；仅迁移文档、README、忽略规则、脚本权限及一处 Markdown 行尾空格有调整。保留源仓库已跟踪的 7 张界面基线图。
- 本机验证环境：Node 23.11.0、Python 3.11.12；后端依赖根据 `requirements.txt` 安装，未改动源依赖锁文件。
- 验证边界：真实模型效果、OCR/语音模型、移动原生包及 Docker 镜像不属于此次迁移运行验证。
