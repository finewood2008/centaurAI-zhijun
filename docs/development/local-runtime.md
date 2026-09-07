# 本机开发：一个入口启动知君

> 产品源：`22dc9a3112058f06a1e4a385c1b2dc3175e39476`，2026-09-05 同步复核。本文补充当前工程的平台与测试隔离边界；本轮实际验证见 [同步记录](UPSTREAM-SYNC-0905.md)。

在仓库根目录运行：

```sh
bash zhijun.sh start
bash zhijun.sh status
bash zhijun.sh stop
```

`start` 复用现有后端与 Web 启动脚本，等待两个服务通过健康检查，再给出应用地址。重复启动和并发启动不会创建第二份服务；已经由其他终端启动且可核对为本项目的健康服务会被复用，不接管。端口存在未知或不健康服务时说明原因，不杀掉占用进程，也不偷偷换端口。

`stop` 只停止这个入口创建、且进程身份仍匹配的服务。原有终端或其他项目启动的进程保持运行。只发正常退出信号，不强制杀进程；若仍在退出则保留记录供下次查询。

日志位于 `data/run/dev/backend.log`、`web.log`，进程记录在同目录 `services.json`，均不提交仓库。服务异常退出后，查看日志并再次 `start`，仅补启动缺失服务；不安装登录项或系统常驻任务，不自动切换模型或修改任何授权。

本入口用于本机开发，依赖现有 Python 虚拟环境与 Node 依赖；不自动安装依赖或构建生产应用。正式部署仍使用部署文档中的系统服务。

该入口依赖 `fcntl`、bash、lsof、ps 与 POSIX 进程组，目前在 macOS 验证。Linux 需确认这些工具和进程检查行为；源测试 `test_dev_runtime.py` 还有 `/private/tmp` 平台路径假设，本次未承诺 Linux 通过。Windows 应继续使用已有 PowerShell/启动脚本，不直接运行该 Python supervisor。

`start` 默认使用项目业务数据，`data/run/dev` 固定相对项目根，即使指定其他业务数据根也不移动运行记录。因此它是开发启动器，不能当作一次性测试沙箱。Vite 绑定 `127.0.0.1:5173` 并启用 strictPort，端口被占用时应报告问题而非换端口。

## 后台整理未完成

对话的统一待处理接口同时返回权限暂停与执行失败。失败不展示原始服务错误，以免暴露请求内容；显示“本次整理未完成，原对话仍保留”。前台可以沿用原恢复接口重试，所有来源、设备、服务和章程检查仍在执行前再次进行。

`pending` 项补充 `state: paused | failed | mixed` 与 `failedCount`。`POST .../routing/resume` 对当前暂停或失败的任务按原任务 ID 重排队；重复点击不重复执行，已成功或被新任务替代的旧失败不再恢复。重新尝试不等于授予权限，可能继续等待用户核对。

回归：`tests.test_dev_runtime` 使用临时目录、随机端口与合成 HTTP 子进程；`tests.test_routing_backlog` 使用隔离数据库及模拟模型，不访问真实用户资料或外部模型。

## 隔离测试入口

全量回归可使用 `backend/.venv/bin/python scripts/run_tests.py --isolated-modules -- -q`。
指定模块时，把路径放在 `--` 前，pytest 选项放在后。每个模块启动新的 pytest
进程并继续加载 `backend/conftest.py` 的临时数据隔离；生产写保护不会被关闭。
这是为了隔离已有测试对模块级服务、路由和单例的配置，不代表这些全局状态问题已修复。
失败模块会汇总并返回非零退出码；没有匹配测试的模块单独列出。JUnit 总数包含子测试，
与 pytest 的普通测试函数数量不同，不能直接相加或混用。

conftest 强制隔离业务数据根，但 MCP 路径使用 setdefault，且 metadata、gbrain 和 secret store 另有路径覆盖；secret store 默认还在项目 `secrets/`。本次测试使用外层临时目录并清除独立路径环境，示例：

```sh
backend/.venv/bin/python - <<'PY'
import os, subprocess, sys, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory(prefix="zhijun-test-env-") as directory:
    env = os.environ.copy()
    for key in ("CENTAUR_METADATA_DB", "CENTAUR_GBRAIN_HOME", "CENTAUR_MCP_DATA_DIR", "CENTAUR_MCP_CONFIG_DIR"):
        env.pop(key, None)
    env["CENTAUR_SECRET_STORE_DIR"] = str(Path(directory) / "secrets")
    env["CENTAURAI_DATABASE_DATA_ROOT"] = str(Path(directory) / "fallback-data")
    result = subprocess.run([sys.executable, "scripts/run_tests.py", "--isolated-modules", "--", "-q", "-p", "no:cacheprovider"], env=env)
raise SystemExit(result.returncode)
PY
```

浏览器 fixture `tests.matters_fixture`（8774）和 `tests.chat_send_fixture`（8775）在业务 import 前自建临时数据根，但仍需应用同样的外层路径环境。先构建前端，检查端口空闲，验证专用 health 身份后运行对应 E2E，只停止本次创建的进程。`today-layout.e2e.mjs` 无需后端；其他历史 E2E 的输出目录不同，例如 context-plan 仍写项目 `data/diagnostics`，应逐个检查后运行。
