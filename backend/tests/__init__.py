"""以 package 模式运行 unittest 时的导入前置隔离。

适用于 ``python -m unittest discover -s tests -t .``。不带 ``-t .`` 的裸 discover
会被 runtime_paths 的硬保护拒绝；项目正式测试入口仍是 scripts/run_tests.py。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path


if not os.environ.get("CENTAURAI_DATABASE_DATA_ROOT"):
    _DATA_ROOT = Path(tempfile.mkdtemp(prefix="mindos-test-data-")).resolve()
    os.environ["CENTAURAI_DATABASE_DATA_ROOT"] = str(_DATA_ROOT)
    os.environ.setdefault("CENTAUR_MCP_DATA_DIR", str(_DATA_ROOT / "mcp" / "data"))
    os.environ.setdefault("CENTAUR_MCP_CONFIG_DIR", str(_DATA_ROOT / "mcp" / "config"))
    atexit.register(lambda: shutil.rmtree(_DATA_ROOT, ignore_errors=True))
