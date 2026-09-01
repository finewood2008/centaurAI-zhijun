"""MindOS 的唯一受支持测试入口：强制 pytest 及其数据隔离 conftest。

请勿直接使用 ``python -m unittest discover``：unittest 不加载根 conftest.py，无法
保证在业务模块导入前隔离 ``CENTAURAI_DATABASE_DATA_ROOT``。

仓库根目录运行：
    .venv\\Scripts\\python.exe scripts/run_tests.py
    .venv\\Scripts\\python.exe scripts/run_tests.py tests/test_mindos_review2_reliability.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"


def main() -> int:
    command = [sys.executable, "-m", "pytest", *sys.argv[1:]]
    return subprocess.run(command, cwd=BACKEND_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
