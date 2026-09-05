"""在隔离临时目录校验 MindOS 数据快照并执行 Chroma 读取探测。

该脚本只复制备份内容到临时目录，绝不覆盖当前数据根。先验证 manifest 的大小与
SHA-256，再启动子进程执行受管 collection 的健康检查，作为恢复演练的可归档证据。

仓库根目录运行：
    .venv\\Scripts\\python.exe scripts/verify_runtime_backup.py --backup backups\\mindos-data-YYYYMMDD-HHMMSS
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校验 MindOS 数据备份并在隔离副本上演练读取。")
    parser.add_argument("--backup", type=Path, required=True, help="backup_runtime_data.py 产生的快照目录")
    parser.add_argument("--keep-copy", action="store_true", help="保留临时恢复副本用于人工诊断")
    return parser.parse_args()


def _validate(snapshot: Path, manifest: dict) -> list[str]:
    errors = []
    for entry in manifest.get("files") or []:
        path = snapshot / str(entry.get("path") or "")
        if not path.is_file():
            errors.append(f"缺少文件: {entry.get('path')}")
            continue
        if path.stat().st_size != entry.get("bytes"):
            errors.append(f"文件大小不一致: {entry.get('path')}")
            continue
        if _sha256(path) != entry.get("sha256"):
            errors.append(f"SHA-256 不一致: {entry.get('path')}")
    return errors


def main() -> int:
    args = parse_args()
    backup = args.backup.expanduser().resolve()
    snapshot = backup / "data"
    manifest_path = backup / "manifest.json"
    if not snapshot.is_dir() or not manifest_path.is_file():
        print("错误：不是有效的 MindOS 数据快照（需要 data/ 与 manifest.json）。", file=sys.stderr)
        return 2
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"错误：无法读取 manifest.json：{exc}", file=sys.stderr)
        return 2
    errors = _validate(snapshot, manifest)
    if errors:
        print("备份校验失败：", file=sys.stderr)
        print("\n".join(errors[:20]), file=sys.stderr)
        return 1

    restored = Path(tempfile.mkdtemp(prefix="mindos-restore-drill-")).resolve()
    try:
        shutil.copytree(snapshot, restored / "data")
        code = (
            "import json,sys;"
            f"sys.path.insert(0,{str(PROJECT_ROOT / 'backend')!r});"
            "import vector_store as v;"
            "print(json.dumps(v.verify_chroma_health(), ensure_ascii=False))"
        )
        env = dict(os.environ)
        env["CENTAURAI_DATABASE_DATA_ROOT"] = str(restored / "data")
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=str(PROJECT_ROOT), env=env,
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            print(result.stderr[-4000:], file=sys.stderr)
            return 1
        health = json.loads(result.stdout.strip().splitlines()[-1])
        print(json.dumps({"backup": str(backup), "health": health}, ensure_ascii=False, indent=2))
        return 0 if health.get("ok") else 1
    finally:
        if args.keep_copy:
            print(f"保留恢复演练副本：{restored}")
        else:
            shutil.rmtree(restored, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
