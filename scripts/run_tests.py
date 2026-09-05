"""MindOS 的唯一受支持测试入口：强制 pytest 及其数据隔离 conftest。

请勿直接使用 ``python -m unittest discover``：unittest 不加载根 conftest.py，无法
保证在业务模块导入前隔离 ``CENTAURAI_DATABASE_DATA_ROOT``。

仓库根目录运行：
    .venv\\Scripts\\python.exe scripts/run_tests.py
    .venv\\Scripts\\python.exe scripts/run_tests.py tests/test_mindos_review2_reliability.py

隔离全量回归（每个模块一个进程，避免模块级单例/路由配置相互污染）：
    backend/.venv/bin/python scripts/run_tests.py --isolated-modules -- -q
    backend/.venv/bin/python scripts/run_tests.py --isolated-modules tests/test_matters.py -- -q

隔离模式的 pytest 选项放在 ``--`` 后；仍加载 backend/conftest.py。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"


def module_groups(selectors):
    """Resolve only explicit test paths; never import application/test modules."""
    groups = {}
    test_root = (BACKEND_ROOT / "tests").resolve()
    for selector in selectors or ["tests"]:
        path_text, separator, suffix = selector.partition("::")
        path = (BACKEND_ROOT / path_text).resolve()
        if not path.is_relative_to(test_root) or not path.exists():
            raise ValueError(f"测试目标必须是 backend/tests 下的现有路径：{selector}")
        if path.is_dir():
            if separator:
                raise ValueError("目录不能带测试节点选择器")
            paths = sorted(path.rglob("test_*.py"))
        else:
            if path.suffix != ".py":
                raise ValueError(f"不是 Python 测试模块：{selector}")
            paths = [path]
        for module in paths:
            relative = module.relative_to(BACKEND_ROOT).as_posix()
            node = relative + ("::" + suffix if separator else "")
            selected = groups.setdefault(relative, [])
            if node == relative:
                selected[:] = [relative]
            elif relative not in selected and node not in selected:
                selected.append(node)
    return groups


def isolated_modules(arguments):
    split = arguments.index("--") if "--" in arguments else len(arguments)
    selectors, options = arguments[:split], arguments[split + 1:]
    if any(arg.startswith("-") for arg in selectors):
        raise ValueError("隔离模式请把 pytest 选项放在 -- 后，例如 --isolated-modules -- -q")
    if any(arg.startswith(("--junitxml", "--junit-xml")) for arg in options):
        raise ValueError("隔离模式内部按模块收集测试报告，不支持覆盖 --junitxml")
    groups = module_groups(selectors)
    if not groups:
        print("未找到测试模块", flush=True)
        return 5
    results, counts = [], {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    with tempfile.TemporaryDirectory(prefix="zhijun-isolated-tests-") as directory:
        for index, (module, selected) in enumerate(groups.items(), 1):
            print(f"\n[{index}/{len(groups)}] {module}", flush=True)
            report = Path(directory) / f"module-{index}.xml"
            command = [sys.executable, "-m", "pytest", *selected, *options, f"--junitxml={report}"]
            code = subprocess.run(command, cwd=BACKEND_ROOT, check=False).returncode
            results.append((module, code))
            if report.exists():
                try:
                    suites = ET.parse(report).getroot()
                    for suite in [suites] if suites.tag == "testsuite" else suites.findall("testsuite"):
                        for key in counts:
                            counts[key] += int(suite.get(key, "0"))
                except (ET.ParseError, ValueError):
                    results[-1] = (module, code or 3)
                    print("模块报告无法解析；本轮标记失败，不忽略结果。", flush=True)
            elif code == 0:
                results[-1] = (module, 3)
                print("模块未产生报告；本轮标记失败，不宣称通过。", flush=True)
            # Pytest 2 can mean a collection error, not a user interruption.
            if code == 130 or code < 0:
                break
    failures = [(module, code) for module, code in results if code not in (0, 5)]
    print(f"\n隔离回归：{len(results)}/{len(groups)} 个模块；"
          f"通过 {sum(code == 0 for _, code in results)}，"
          f"无匹配测试 {sum(code == 5 for _, code in results)}，失败 {len(failures)}。", flush=True)
    print("JUnit 统计（含子测试）：" + ", ".join(f"{key}={value}" for key, value in counts.items()), flush=True)
    for module, code in failures:
        print(f"FAILED ({code}): {module}", flush=True)
    return failures[0][1] if failures else (0 if any(code == 0 for _, code in results) else 5)


def main() -> int:
    arguments = sys.argv[1:]
    if "--isolated-modules" in arguments:
        arguments.remove("--isolated-modules")
        try:
            return isolated_modules(arguments)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 4
    command = [sys.executable, "-m", "pytest", *arguments]
    return subprocess.run(command, cwd=BACKEND_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
