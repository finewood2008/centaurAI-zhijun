"""阶段 2 发布守卫：禁止 Mock 私钥 / devOnlyCode / __mock / consumer_api 进入生产制品。

在打生产 runtime 包之前运行，违规即退出码 1：
- 源码层：backend/server.py 与 backend/mindos/*（生产入口）不得 import consumer_api；
- Electron 主进程层：随包的 frontend/main.js / preload.js / consumer-client.js /
  backend-rpc.js / package.json 不得携带 Mock OTP（devOnlyCode / 本机 Mock 地址 /
  固定验证码）；Mock OTP 联调模块仅按需加载，绝不被随包文件引用到成品中；
- 制品层：扫描 release/ 或指定目录中的全部文本文件，发现
  TEST_PRIVATE_KEY_PEM / "BEGIN PRIVATE KEY" / devOnlyCode / "__mock" / consumer_api
  即失败；
- 数据层：testdata/（含票据测试向量与私钥）不得进入制品扫描目录。

用法：
    .venv\\Scripts\\python.exe scripts/check_release_guard.py [--scan PATH ...]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_MARKERS = (
    "BEGIN PRIVATE KEY",
    "TEST_PRIVATE_KEY_PEM",
    "devOnlyCode",
    "__mock",
    "consumer_api",
    # 阶段 2 生产制品不得携带 Mock OTP 本机默认地址或固定验证码。
    "127.0.0.1:8801",
    '"123456"',
)

PRODUCTION_ENTRY_POINTS = (
    PROJECT_ROOT / "backend" / "server.py",
)

# 阶段 2 Electron 主进程产物：随桌面客户端分发，构建期已彻底分离 Mock OTP
# （consumer-mock-otp.js 仅联调加载），此处扫描生产随包的宿主侧文件；
# 阶段 3（WP M/N）安全窗口与 BLE Adapter 骨架（Gate/安全存储/Setup 窗口/
# IPC 通道/分片/ClaimCoordinator 契约）一并纳入。
ELECTRON_MAIN_PROCESS_FILES = (
    "frontend/main.js",
    "frontend/preload.js",
    "frontend/consumer-client.js",
    "frontend/backend-rpc.js",
    "frontend/feature-gates.js",
    "frontend/ipc-channels.js",
    "frontend/provisioning-crypto-provider.js",
    "frontend/secure-store.js",
    "frontend/setup-window.js",
    "frontend/ble-contracts.js",
    "frontend/command-framing.js",
    "frontend/claim-coordinator.js",
    "frontend/ble-adapter.js",
    "frontend/discovery-contracts.js",
    "frontend/setup-view-state.js",
    "frontend/setup-ui-model.js",
    "frontend/package.json",
)


def _scan_file(path: Path) -> list[str]:
    """返回文件中命中的禁用标记行（[path:line: marker]）；二进制/不可解码文件跳过。"""
    hits: list[str] = []
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return hits
    for line_number, line in enumerate(text.splitlines(), start=1):
        for marker in FORBIDDEN_MARKERS:
            if marker in line:
                hits.append(f"{path}:{line_number}: {marker}")
    return hits


def check_source(source_root: Path) -> list[str]:
    problems: list[str] = []
    for entry in PRODUCTION_ENTRY_POINTS:
        if not entry.is_file():
            continue
        text = entry.read_text(encoding="utf-8", errors="ignore")
        if "consumer_api" in text:
            problems.append(f"{entry.relative_to(source_root)}: 生产入口不得 import consumer_api（Mock 包）")
    mindos_dir = source_root / "backend" / "mindos"
    if mindos_dir.is_dir():
        for py in mindos_dir.rglob("*.py"):
            text = py.read_text(encoding="utf-8", errors="ignore")
            if "consumer_api" in text:
                problems.append(f"{py.relative_to(source_root)}: 生产业务模块不得 import consumer_api（Mock 包）")
    return problems


def check_tree(roots: list[Path]) -> list[str]:
    problems: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() in {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".woff", ".woff2", ".map"}:
                continue
            problems.extend(_scan_file(path))
    return problems


def check_files(files: list[Path]) -> list[str]:
    """扫描显式文件列表（用于随包的 Electron 主进程产物）。"""
    problems: list[str] = []
    for path in files:
        if not path.is_file():
            continue
        if path.suffix.lower() in {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".woff", ".woff2", ".map"}:
            continue
        problems.extend(_scan_file(path))
    return problems


def check_electron_main_process(source_root: Path) -> list[str]:
    """扫描 Electron 主进程随包产物，防止 Mock OTP / 本机 Mock 地址 / 固定验证码泄漏。"""
    return check_files([source_root / rel for rel in ELECTRON_MAIN_PROCESS_FILES])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", nargs="*", default=None,
                        help="额外扫描目录（默认：source + release/ 若存在）")
    args = parser.parse_args()

    source_root = PROJECT_ROOT
    problems: list[str] = check_source(source_root)
    problems.extend(check_electron_main_process(source_root))

    scan_roots: list[Path] = []
    if args.scan:
        scan_roots = [Path(p) for p in args.scan]
    else:
        release = PROJECT_ROOT / "release"
        if release.is_dir():
            scan_roots.append(release)
    if scan_roots:
        problems.extend(check_tree(scan_roots))

    if problems:
        print(f"release guard FAILED: {len(problems)} violation(s)", file=sys.stderr)
        for problem in sorted(set(problems)):
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("release guard OK: 未发现 Mock 私钥 / devOnlyCode / __mock / consumer_api")
    return 0


if __name__ == "__main__":
    sys.exit(main())
