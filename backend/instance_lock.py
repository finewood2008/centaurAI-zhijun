"""数据根目录级 OS 独占锁（索引可靠性方案 P0-1：单实例与跨进程互斥）。

互斥语义：
- 锁对象是「数据根目录」（data/.mindos-backend.lock），不是端口——后端、
  回填/重建/compact 等任何直接访问 ChromaDB 的进程都必须先拿这把锁；
- 用操作系统级文件锁实现（Windows msvcrt.locking / Unix fcntl.flock），
  进程崩溃/被杀时由 OS 自动释放，天然免疫 stale lock（文件残留但无进程
  持锁时，下一个进程可直接加锁成功并覆写持有者信息）；
- 禁止「检查文件存在再创建」的竞态写法——文件存在不代表被持有，
  唯一真相是「能否成功加锁」；
- 持有者信息（pid/role/started_at/data_root）写在锁区域之后（offset 1 起），
  加锁失败的一方仍能读取并给出明确提示。

使用约定（方案要求）：锁必须在打开任何 ChromaDB 连接之前获取（锁先于连接）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from runtime_paths import DATA_ROOT

logger = logging.getLogger(__name__)

LOCK_NAME = ".mindos-backend.lock"
# 锁定文件前 1 字节；持有者 JSON 从 offset 1 写起（锁外区域，他进程可读）
_LOCK_BYTES = 1
_HOLDER_MAX_BYTES = 4096


def lock_path() -> Path:
    """锁文件路径（随 CENTAURAI_DATABASE_DATA_ROOT 环境变量解析）。"""
    return DATA_ROOT / LOCK_NAME


def _try_lock_fd(fd: int) -> bool:
    """对已打开的 fd 尝试非阻塞独占锁；成功 True，被占用/失败 False。"""
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_BYTES)
            return True
        except OSError:
            return False
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


class InstanceLock:
    """持有的 OS 锁对象。必须保持引用直到进程退出（防 fd 被 GC 关闭）。"""

    def __init__(self, fd: int, path: Path, role: str):
        self._fd = fd
        self.path = path
        self.role = role

    def release(self) -> None:
        """主动释放（进程退出时 OS 也会自动释放，这里用于脚本正常结束）。"""
        fd, self._fd = self._fd, -1
        if fd < 0:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_BYTES)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass  # 进程即将退出，释放失败由 OS 兜底
        try:
            os.close(fd)
        except OSError:
            pass

    def __del__(self):  # pragma: no cover - 兜底路径
        try:
            self.release()
        except Exception:
            pass


def _read_holder(path: Path) -> dict | None:
    """读锁文件锁外区域（offset 1 起）的持有者信息；读不到返回 None。"""
    try:
        with open(path, "rb") as f:
            f.seek(_LOCK_BYTES)
            raw = f.read(_HOLDER_MAX_BYTES)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def holder_hint(holder: dict | None) -> str:
    """把持有者信息格式化为一行人类可读提示（无信息时给出兜底文案）。"""
    if not holder:
        return "无法读取持有者信息"
    return (
        f"PID={holder.get('pid')}, role={holder.get('role')}, "
        f"started_at={holder.get('started_at')}, data_root={holder.get('data_root')}"
    )


def acquire(role: str = "backend") -> tuple[InstanceLock | None, dict | None]:
    """尝试获取数据根目录独占锁。

    返回 (lock, holder)：
    - lock 非 None → 成功持有（holder 恒为 None），用完调 release() 或持有到进程退出；
    - lock 为 None → 已被其他进程持有，holder 为读到的持有者信息（可能为 None）。
    """
    path = lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # 数据根目录不可创建属于部署故障，直接失败
        logger.error("无法创建数据根目录 %s: %s", path.parent, exc)
        return None, None

    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    if not _try_lock_fd(fd):
        holder = _read_holder(path)
        os.close(fd)
        return None, holder

    # 加锁成功：覆写持有者信息到锁外区域并截断旧残留
    info = {
        "pid": os.getpid(),
        "role": role,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data_root": str(DATA_ROOT),
    }
    try:
        payload = json.dumps(info, ensure_ascii=False).encode("utf-8")
        os.lseek(fd, _LOCK_BYTES, os.SEEK_SET)
        os.write(fd, payload)
        os.ftruncate(fd, _LOCK_BYTES + len(payload))
    except OSError:
        pass  # 持有者信息写失败不影响互斥语义
    return InstanceLock(fd, path, role), None
