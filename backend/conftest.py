"""P1-4 测试环境强制隔离（backend 根 conftest，等效于全局启动器）。

任何测试模块 import runtime_paths / config / vector_store 之前，本文件先于
测试收集被 pytest 加载执行（conftest 优先级最高），把可变数据根
CENTAURAI_DATABASE_DATA_ROOT 指向一次性临时目录，使以下路径全部落在临时目录：
- ChromaDB 数据目录（data/chroma_data）
- watch folder
- 各类 SQLite 数据库目录（db/）
- memory / wiki / derived / mcp 等落盘数据

这样测试不会读写/污染生产 data/（方案 §P1-4）。测试结束仅删除该临时目录。

放置位置说明：本文件位于 backend/（而非 backend/tests/）——pytest 会加载测试
文件所在目录的全部祖先 conftest，因此无论 `pytest`（backend 根运行）还是
`pytest backend/tests`（仓库根运行）都强制生效；无需在每个测试目录重复放置。

注意：必须在本文件 import 阶段（而非 fixture）里设置环境变量——因为 runtime_paths
在模块 import 时读取该变量，而 conftest 的模块级代码在测试模块被收集 import 之前执行。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 一次性临时数据根，整个会话共享；永远不落在生产 PROJECT_ROOT/data 下。
_DATA_ROOT = Path(tempfile.mkdtemp(prefix="mindos-test-data-")).resolve()
os.environ["CENTAURAI_DATABASE_DATA_ROOT"] = str(_DATA_ROOT)
# MCP 数据/配置也随数据根隔离（runtime_paths 未显式设置时默认落在 DATA_ROOT 下）。
os.environ.setdefault("CENTAUR_MCP_DATA_DIR", str(_DATA_ROOT / "mcp" / "data"))
os.environ.setdefault("CENTAUR_MCP_CONFIG_DIR", str(_DATA_ROOT / "mcp" / "config"))

# 硬守卫（P1-4「禁止默认根运行测试」的兜底契约）：临时根绝不允许落在仓库内
# ——即使系统 TMP 被指到项目目录，也宁可当场失败，不让测试触碰生产数据。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if _PROJECT_ROOT == _DATA_ROOT or _PROJECT_ROOT in _DATA_ROOT.parents:
    sys.stderr.write(
        f"[conftest] 测试数据根非法地位于项目目录内：{_DATA_ROOT}\n"
        "拒绝在项目目录下运行测试（P1-4 强制隔离）。请检查 TMP/TEMP 环境变量。\n"
    )
    raise SystemExit(2)


def _cleanup_current() -> None:
    shutil.rmtree(_DATA_ROOT, ignore_errors=True)


atexit.register(_cleanup_current)


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    """会话结束删除临时数据根（幂等，便于异常退出后 atexit 兜底）。"""
    _cleanup_current()