#!/usr/bin/env python3
"""Migrate legacy project-root runtime data into the unified ``data/`` root.

The script never overwrites an existing destination.  Run without ``--execute``
to preview; with ``--execute`` it moves files (same-volume moves are atomic).
Repeated runs are safe: already-migrated entries are reported and skipped.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Entry:
    source: Path
    destination: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move legacy runtime data from the project root into data/."
    )
    parser.add_argument(
        "--source-root", type=Path, default=PROJECT_ROOT,
        help="legacy project root to inspect (default: this repository root)",
    )
    parser.add_argument(
        "--data-root", type=Path,
        help="target data root (default: <source-root>/data)",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="perform the move; without this flag the script only prints a preview",
    )
    return parser.parse_args()


def _sqlite_files(directory: Path, stem: str) -> list[Path]:
    """Return a database and its SQLite sidecars/backups in deterministic order."""
    return sorted(path for path in directory.glob(f"{stem}*") if path.is_file())


def migration_entries(source_root: Path, data_root: Path) -> list[Entry]:
    db_root = data_root / "db"
    entries: list[Entry] = []

    # Move the Wiki database out before moving wiki/ itself.  Markdown pages stay
    # in data/wiki while SQLite metadata lives together with the other databases.
    for path in _sqlite_files(source_root / "wiki", "wiki.sqlite3"):
        entries.append(Entry(path, db_root / path.name))

    for stem in ("file_center.db", "job_store.db", "derived_content.db", "governance.db"):
        for path in _sqlite_files(source_root, stem):
            entries.append(Entry(path, db_root / path.name))

    for name in (
        "chroma_data", "config", "gbrain_data", "mcp", "memory", "video_frames",
        "video_work", "watch_folder", "wiki", "derived_images", ".trash",
        # 两套上传入口分别使用的暂存目录；迁移它们避免升级后遗留大文件。
        ".upload_staging", ".mindos_upload_staging",
    ):
        path = source_root / name
        if path.exists():
            entries.append(Entry(path, data_root / name))

    for name in (
        "annotations.json", "groups.json", ".lan_config.json", ".mobile_config.json",
        ".context_packs.json", ".personal_context_snapshot.json",
    ):
        path = source_root / name
        if path.exists():
            entries.append(Entry(path, data_root / name))
    return entries


def migrate(source_root: Path, data_root: Path, execute: bool) -> tuple[int, int, int]:
    entries = migration_entries(source_root, data_root)
    moved = skipped = conflicts = 0
    mode = "执行迁移" if execute else "预览"
    print(f"{mode}: {source_root} -> {data_root}")
    if not entries:
        print("未发现需要迁移的旧运行时数据。")
        return moved, skipped, conflicts

    for entry in entries:
        if entry.destination.exists():
            if not entry.source.exists():
                print(f"[已迁移] {entry.destination}")
                skipped += 1
            else:
                print(f"[冲突，不覆盖] {entry.source} -> {entry.destination}")
                conflicts += 1
            continue
        if not execute:
            print(f"[将移动] {entry.source} -> {entry.destination}")
            continue
        entry.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry.source), str(entry.destination))
        print(f"[已移动] {entry.source} -> {entry.destination}")
        moved += 1

    if not execute:
        print("这是预览，未写入任何文件；确认后追加 --execute。")
    print(f"完成：moved={moved}, skipped={skipped}, conflicts={conflicts}")
    return moved, skipped, conflicts


def main() -> int:
    args = parse_args()
    source_root = args.source_root.expanduser().resolve()
    if not source_root.is_dir():
        print(f"迁移失败：源目录不存在：{source_root}", file=sys.stderr)
        return 2
    data_root = (args.data_root or source_root / "data").expanduser().resolve()
    if data_root == source_root:
        print("迁移失败：data root 不能等于 source root。", file=sys.stderr)
        return 2
    _, _, conflicts = migrate(source_root, data_root, args.execute)
    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
