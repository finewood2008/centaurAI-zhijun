#!/usr/bin/env python3
"""Build an offline CentaurOS runtime archive from a provisioned source tree."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import struct
import sys
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path, PurePosixPath

import check_release_guard as release_guard


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET_MACHINES = {
    "linux-x86_64": 62,
    "linux-riscv64": 243,
}
MUTABLE_ROOTS = {
    "data",
    "chroma_data",
    "config",
    "gbrain_data",
    "mcp",
    "memory",
    "video_frames",
    "video_work",
    "watch_folder",
    "wiki",
}
MUTABLE_FILES = {
    ".context_packs.json",
    ".lan_config.json",
    ".mobile_config.json",
    ".personal_context_snapshot.json",
    "annotations.json",
    "file_center.db",
    "groups.json",
}
DEV_ROOTS = {".git", ".pytest_cache", ".ruff_cache", ".mypy_cache", "release"}
DEV_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
RUNTIME_TOP_LEVEL = {"run.sh", "backend", "frontend", "scripts"}
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 8 * 1024 * 1024 * 1024
MAX_MEMBERS = 100_000


def fail(message: str) -> None:
    raise SystemExit(f"runtime packaging failed: {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Package a provisioned CentaurAI Database tree without installing dependencies."
    )
    parser.add_argument("--target", choices=sorted(TARGET_MACHINES), required=True)
    parser.add_argument("--version", help="Artifact version; defaults to frontend/package.json")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "release")
    parser.add_argument("--source-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    return parser.parse_args()


def runtime_version(source_root: Path, explicit: str | None) -> str:
    value = explicit
    if not value:
        try:
            value = str(json.loads((source_root / "frontend" / "package.json").read_text())["version"])
        except (OSError, KeyError, TypeError, ValueError):
            fail("--version is required when frontend/package.json has no valid version")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+-]*", value or ""):
        fail(f"invalid version: {value!r}")
    return value


def elf_machine(path: Path) -> int:
    try:
        header = path.resolve(strict=True).read_bytes()[:20]
    except OSError as exc:
        fail(f"cannot read runtime Python: {exc}")
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2:
        fail(f"runtime Python is not a 64-bit Linux ELF executable: {path}")
    endian = "<" if header[5] == 1 else ">" if header[5] == 2 else ""
    if not endian:
        fail(f"runtime Python has an invalid ELF byte order: {path}")
    return struct.unpack(f"{endian}H", header[18:20])[0]


def has_model_weight(path: Path) -> bool:
    names = {"model.safetensors", "pytorch_model.bin"}
    return any(candidate.is_file() and candidate.name in names for candidate in path.rglob("*"))


def validate_source(source_root: Path, target: str) -> None:
    required_files = [
        source_root / "run.sh",
        source_root / "backend" / "server.py",
        source_root / "backend" / "runtime_paths.py",
        source_root / "frontend" / "package.json",
        source_root / "frontend" / "mindos-web" / "dist" / "index.html",
    ]
    missing = [str(path.relative_to(source_root)) for path in required_files if not path.is_file()]
    if missing:
        fail(f"source tree is incomplete; missing: {', '.join(missing)}")

    python_bin = source_root / "backend" / ".venv" / "bin" / "python"
    if not python_bin.exists() or not os.access(python_bin, os.X_OK):
        fail("backend/.venv/bin/python is missing or not executable; provision dependencies first")
    machine = elf_machine(python_bin)
    if machine != TARGET_MACHINES[target]:
        fail(f"runtime Python architecture does not match {target}")

    site_packages = list((source_root / "backend" / ".venv" / "lib").glob("python*/site-packages"))
    if not site_packages or not any(any(path.glob("*.dist-info")) for path in site_packages):
        fail("backend virtualenv has no installed distributions")

    text_model = source_root / "backend" / "models_cache" / "BAAI" / "bge-small-zh-v1.5"
    if not (text_model / "config.json").is_file() or not has_model_weight(text_model):
        fail("required BGE text model is not preinstalled under backend/models_cache")


def excluded(relative: PurePosixPath) -> bool:
    if not relative.parts:
        return False
    if relative.parts[0] in MUTABLE_ROOTS | DEV_ROOTS:
        return True
    if relative.parts[0] not in RUNTIME_TOP_LEVEL:
        return True
    if relative.parts[0] == "scripts" and relative != PurePosixPath("scripts/sync_agent_memories.py"):
        return len(relative.parts) > 1
    if relative.parts[0] == "frontend" and len(relative.parts) > 1:
        allowed = (
            PurePosixPath("frontend/assets"),
            PurePosixPath("frontend/mobile"),
            PurePosixPath("frontend/mindos-web/dist"),
            PurePosixPath("frontend/renderer/lan_import.html"),
            PurePosixPath("frontend/package.json"),
        )
        if not any(relative == path or path in relative.parents for path in allowed):
            return True
    if len(relative.parts) > 1 and relative.parts[:2] == ("backend", "venv"):
        return True
    # 阶段 2：Consumer API Mock（含测试私钥/固定验证码/__mock 管理面）仅联调用，
    # 禁止进入生产 runtime 包；构建守卫（check_release_guard）再兜底复核。
    if relative.parts[:2] == ("backend", "consumer_api"):
        return True
    # 阶段 2：Mock OTP Consumer Client 仅联调按需加载，构建期彻底从生产制品分离。
    if relative.parts[:2] == ("frontend", "consumer-mock-otp.js"):
        return True
    if any(part in DEV_DIRS for part in relative.parts):
        return True
    name = relative.name
    if name in MUTABLE_FILES or name.startswith("file_center.db-"):
        return True
    if name == ".DS_Store" or name.endswith((".pyc", ".pyo", ".log")):
        return True
    if len(relative.parts) == 2 and relative.parts[0] == "backend" and name.startswith("test_") and name.endswith(".py"):
        return True
    return False


def normalized_filter(top_level: str, epoch: int):
    prefix = f"{top_level}/"

    def apply(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if info.name == top_level:
            relative = PurePosixPath()
        elif info.name.startswith(prefix):
            relative = PurePosixPath(info.name[len(prefix):])
        else:
            fail(f"unexpected archive path: {info.name}")
        if excluded(relative):
            return None
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        info.mtime = epoch
        if info.isdir():
            info.mode = 0o755
        elif info.isfile():
            info.mode = 0o755 if info.mode & 0o111 else 0o644
        return info

    return apply


def build_archive(source_root: Path, output_dir: Path, version: str, target: str) -> Path:
    top_level = f"centaurai-database-{version}-{target}-runtime"
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = (output_dir / f"{top_level}.tar.gz").resolve()
    try:
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    except ValueError:
        fail("SOURCE_DATE_EPOCH must be an integer")
    if epoch < 0:
        fail("SOURCE_DATE_EPOCH must not be negative")

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    archive.dereference = True
                    archive.add(
                        source_root,
                        arcname=top_level,
                        recursive=True,
                        filter=normalized_filter(top_level, epoch),
                    )
                    version_data = f"{version}\n".encode("ascii")
                    version_info = tarfile.TarInfo(f"{top_level}/VERSION")
                    version_info.size = len(version_data)
                    version_info.mode = 0o644
                    version_info.uid = version_info.gid = 0
                    version_info.uname = version_info.gname = "root"
                    version_info.mtime = epoch
                    archive.addfile(version_info, BytesIO(version_data))
        verify_archive(temporary, top_level)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def verify_archive(path: Path, top_level: str) -> None:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        fail("runtime archive exceeds the 4 GiB compressed size limit")
    count = 0
    expanded = 0
    seen: set[str] = set()
    top_info: tarfile.TarInfo | None = None
    run_info: tarfile.TarInfo | None = None
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            count += 1
            if count > MAX_MEMBERS:
                fail("runtime archive exceeds the 100000 member limit")
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != top_level:
                fail("runtime archive must contain exactly one top-level directory")
            if member.name in seen:
                fail(f"runtime archive contains a duplicate member: {member.name}")
            seen.add(member.name)
            if member.name == top_level:
                top_info = member
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                fail(f"runtime archive contains an unsupported member: {member.name}")
            if member.mode & 0o6000:
                fail(f"runtime archive contains setuid/setgid content: {member.name}")
            if member.isfile():
                expanded += member.size
                if expanded > MAX_EXPANDED_BYTES:
                    fail("runtime archive exceeds the 8 GiB expanded size limit")
            if member.name == f"{top_level}/run.sh":
                run_info = member
    if top_info is None or not top_info.isdir():
        fail("runtime archive top-level entry must be a directory")
    if run_info is None or not run_info.isfile() or run_info.mode & 0o111 == 0:
        fail("runtime archive is missing an executable run.sh")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    source_root = args.source_root.expanduser().resolve()
    if not source_root.is_dir():
        fail(f"source root does not exist: {source_root}")
    version = runtime_version(source_root, args.version)
    validate_source(source_root, args.target)
    # 阶段 2：发布守卫——禁止 Mock 私钥 / devOnlyCode / __mock / consumer_api 及前端
    # Mock/Nexus 逻辑进入生产制品（源码层 + frontend/mindos-web/dist + Electron 主进程产物）。
    guard_problems = list(release_guard.check_source(source_root))
    guard_problems.extend(release_guard.check_electron_main_process(source_root))
    web_dist = source_root / "frontend" / "mindos-web" / "dist"
    if web_dist.is_dir():
        guard_problems.extend(release_guard.check_tree([web_dist]))
    if guard_problems:
        fail("release guard violations:\n  " + "\n  ".join(sorted(set(guard_problems))))
    archive = build_archive(source_root, args.output_dir.expanduser().resolve(), version, args.target)
    verify_archive(archive, archive.name.removesuffix(".tar.gz"))
    digest = sha256(archive)
    print(f"runtime archive: {archive}")
    print(f"sha256: {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
