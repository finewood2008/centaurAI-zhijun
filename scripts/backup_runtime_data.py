"""创建 MindOS 可恢复的数据根快照。

必须在后端停止后执行。脚本先获取与后端相同的数据根独占锁，再复制完整
``CENTAURAI_DATABASE_DATA_ROOT``，以保证 Chroma 集合、generation 注册表和各 SQLite
数据库来自同一时点。快照写到独立目录，并附带 SHA-256 清单供恢复演练校验。

仓库根目录运行：
    .venv\\Scripts\\python.exe scripts/backup_runtime_data.py
    .venv\\Scripts\\python.exe scripts/backup_runtime_data.py --output-dir D:\\mindos-backups
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(data_root: Path) -> dict:
    files = []
    for path in sorted(data_root.rglob("*")):
        if path.is_file() and path.name != ".mindos-backend.lock":
            files.append({
                "path": path.relative_to(data_root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            })
    return {
        "format": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_data_root": str(data_root),
        "files": files,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="创建完整 MindOS 运行数据快照。")
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "backups",
        help="快照父目录，不能位于数据根内（默认仓库 backups/）。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    from instance_lock import acquire, holder_hint
    from runtime_paths import DATA_ROOT

    data_root = Path(DATA_ROOT).resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir == data_root or data_root in output_dir.parents:
        print("错误：--output-dir 不能是数据根或其子目录，避免备份递归包含自身。", file=sys.stderr)
        return 2

    lock, holder = acquire(role="backup-runtime-data")
    if lock is None:
        print(
            f"错误：数据目录被其他进程占用（{holder_hint(holder)}）。"
            "请先停止后端和维护脚本，再执行一致性备份。",
            file=sys.stderr,
        )
        return 2
    try:
        if not data_root.exists():
            print(f"错误：数据根不存在：{data_root}", file=sys.stderr)
            return 2
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        final = output_dir / f"mindos-data-{stamp}"
        if final.exists():
            print(f"错误：备份目标已存在：{final}", file=sys.stderr)
            return 2
        temporary = Path(tempfile.mkdtemp(prefix=f".{final.name}-", dir=output_dir))
        try:
            snapshot = temporary / "data"
            shutil.copytree(
                data_root, snapshot,
                ignore=shutil.ignore_patterns(".mindos-backend.lock"),
            )
            (temporary / "manifest.json").write_text(
                json.dumps(_manifest(snapshot), ensure_ascii=False, indent=2), encoding="utf-8",
            )
            temporary.replace(final)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        print(f"备份完成：{final}")
        print("下一步：使用 scripts/verify_runtime_backup.py 对该快照执行校验和隔离恢复演练。")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
