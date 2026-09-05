"""MindOS 原材料正文快照文件/SQLite saga（阶段 A-A2）。

§5.1 要求文件系统与 SQLite 不能共享事务，因此正文落盘采用可恢复 saga：

    1. ``begin_snapshot`` 先落 `preparing` 行（占住 material_id+version）；
    2. 大文本写入同目录临时文件 ``.tmp-{uuid}``，``fsync`` 后原子 rename 为
       ``{material_id}/{version}.json``（受控相对路径，禁止任意用户路径）；
    3. ``commit_snapshot`` 在**同一 SQLite 事务**内校验并切为 ``ready``、supersede
       历史 ready 版本。

进程在任意步骤中断时允许短暂出现孤儿文件或 ``preparing`` 行；``recover_snapshots``
在启动恢复时按 storage_state、文件存在性和 hash 完成/回滚/隔离，绝不把半成品暴露
为当前快照。``cleanup_orphan_files`` 定期回收无活行引用且超过保留期的旧文件，但会
避开刚中断、仍在恢复保留期内的文件。

正文存取：
- 短文本直接内联到 ``text_content`` 列（``rel_path`` 为空）。
- 大文本落盘：``rel_path`` + ``snapshot_hash`` 存行内，正文经 ``read_snapshot_text``
  按需从文件读取，避免把磁盘占用埋进 SQLite。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from runtime_paths import MATERIAL_SNAPSHOTS_DATA_DIR
from .stores.material_pipeline_store import (
    MaterialPipelineStore,
    PARSE_EMPTY,
    PARSE_FAILED,
    PARSE_OK,
    SS_DISCARDED,
    SS_PREPARING,
)

logger = logging.getLogger(__name__)

# 快照大文件阈值：超过该字节数的正文写盘（与 store 内 _INLINE_TEXT_LIMIT 一致）。
_INLINE_TEXT_LIMIT = 256 * 1024
# 孤儿文件回收保留期（秒）：小于该时间的一律保留，避免与刚中断的 saga 竞争。
_DEFAULT_ORPHAN_RETENTION = 7 * 24 * 3600


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MaterialSnapshotSaga:
    """封装正文快照的「落盘 + 状态切换」与「启动恢复/孤儿清理」。

    线程安全：文件写入需在单 worker 内串行；跨线程并发写同一 material 由
    material_jobs 的活动任务去重 + 本类文件锁兜底。
    """

    def __init__(
        self,
        store: MaterialPipelineStore | None = None,
        snapshot_root: Path | None = None,
    ) -> None:
        self._store = store or MaterialPipelineStore.instance()
        self._snapshot_root = (snapshot_root or MATERIAL_SNAPSHOTS_DATA_DIR).resolve()
        self._file_lock = threading.Lock()

    # ================= 受控路径 =================

    def _resolve(self, rel_path: str) -> Path:
        """把受控相对路径解析为绝对路径，并强制限定在快照根内。

        任何尝试（``..`` / 绝对路径 / 穿越）越出快照根的 rel_path 一律拒绝。
        """
        p = (self._snapshot_root / rel_path).resolve()
        if not str(p).startswith(str(self._snapshot_root) + os.sep):
            raise ValueError(f"unsafe snapshot rel_path: {rel_path!r}")
        return p

    @staticmethod
    def _rel_for(material_id: str, version: int) -> str:
        # Windows 下避免目录名里出现保留字符：material_id 已由调用方规范化，
        # 这里仅按 {material_id}/{version}.json 组装。
        return f"{material_id}/{version}.json"

    # ================= 正文落盘与提交 =================

    def save_and_commit_snapshot(
        self,
        snapshot_id: str,
        material_id: str,
        version: int,
        text: str,
        *,
        content_format: str = "text",
        parse_status: str = PARSE_OK,
        metadata: dict | None = None,
        force_file: bool = False,
    ) -> dict:
        """把 preparing 快照的正文落盘并提交为 ready（saga 收尾一步到位）。

        供 job worker 在线程内串行调用（单 worker 全局唯一，见 §6.1）。
        短文本内联；长文本或 ``force_file=True`` 写文件后仅存 rel_path+hash。
        """
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        text_bytes = text.encode("utf-8") if text else b""
        if not force_file and len(text_bytes) <= _INLINE_TEXT_LIMIT:
            # 内联：无需文件 IO，直接事务提交。
            return self._store.commit_snapshot(
                snapshot_id,
                text_content=text if text else "",
                parse_status=parse_status,
                snapshot_hash="",
                rel_path=None,
            )
        rel_path = self._rel_for(material_id, version)
        snapshot_hash = self._write_snapshot_file(rel_path, text)
        return self._store.commit_snapshot(
            snapshot_id,
            text_content="",  # 大文本存文件，不冗余进列
            parse_status=parse_status,
            snapshot_hash=snapshot_hash,
            rel_path=rel_path,
        )

    def _write_snapshot_file(self, rel_path: str, text: str) -> str:
        """临时文件 -> fsync -> 原子 rename -> 返回内容 hash。

        与 SQLite 的 ``preparing`` 行解耦：若进程在写文件与 commit 之间中断，
        行保持 ``preparing``，文件为孤儿，由启动恢复处理。
        """
        target = self._resolve(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex[:12]}")
        data = text.encode("utf-8")
        snapshot_hash = _sha256(data)
        with self._file_lock:
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                # 原子替换；Windows 下 os.replace 覆盖已存在文件是原子的。
                os.replace(tmp, target)
                self._fsync_dir(target.parent)
            except BaseException:
                # 失败时清理临时半成品，避免泄漏。
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                raise
        return snapshot_hash

    @staticmethod
    def _fsync_dir(dir_path: Path) -> None:
        """fsync 目录以持久化 rename（非关键路径，失败仅告警）。"""
        try:
            fd = os.open(str(dir_path), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            logger.debug("fsync dir failed (non-critical): %s", dir_path)

    def read_snapshot_text(self, snapshot: dict) -> str:
        """返回快照正文：行内 text_content 优先，否则按 rel_path 从文件读取。

        快照行标记了 ``rel_path``（大文本落盘）但文件缺失/损坏时 raise，
        由调用方区分「无快照（迁移期可回退）」与「有快照但读取失败（应上报）」
        ——绝不把缺文件静默当作空正文，否则派生会基于丢失的内容产出。
        """
        inline = snapshot.get("text_content")
        if inline:
            return inline
        rel = snapshot.get("rel_path")
        if rel:
            file = self._resolve(rel)
            if not file.is_file():
                raise OSError(f"snapshot file missing: {rel}")
            with open(file, "rb") as f:
                return f.read().decode("utf-8", errors="replace")
        return ""

    # ================= 启动恢复与孤儿清理 =================

    def recover_pipeline(self) -> dict:
        """启动恢复入口（§8.2）：material_jobs 历史 queued/processing 统一转 paused，
        并回滚遗留的 preparing 快照。绝不自动续跑（上传/继续由用户显式触发）。
        """
        paused = self._store.pause_pending_jobs()
        snapshots = self.recover_snapshots()
        return {
            "paused_jobs": paused,
            "rolled_back": snapshots.get("rolled_back", 0),
            "removed_tmp": snapshots.get("removed_tmp", 0),
        }

    def recover_snapshots(self) -> dict:
        """启动恢复：处理遗留的 ``preparing`` 行，使 DB 回到一致状态（§8.2）。

        判定规则（§5.1「完成、回滚或隔离」）：
        - ``preparing`` 行尚无 rel_path（未开始写文件）或对应文件不完整 → 回滚
          （标 discarded 并清理残留临时/半成品文件）；相应的 material_job 已由
          启动暂停统一转 ``paused``，用户可显式继续后重做。
        - 极少数「文件已写好但 commit 前中断」的孤儿 —— 无法验证作者意图，
          保守回滚并隔离，交由后续重做，绝不直接暴露为当前快照。
        """
        rolled_back = 0
        removed_tmp = 0
        pending = self._store.pending_snapshots(SS_PREPARING)
        for snap in pending:
            self._rollback_preparing(snap)
            rolled_back += 1
        removed_tmp += self._cleanup_tmp_files(self._snapshot_root)
        return {"rolled_back": rolled_back, "removed_tmp": removed_tmp}

    def _rollback_preparing(self, snap: dict) -> None:
        snapshot_id = snap["snapshot_id"]
        rel = snap.get("rel_path")
        if rel:
            try:
                file = self._resolve(rel)
            except ValueError:
                file = None
            if file is not None and file.exists():
                try:
                    file.unlink()
                except OSError as e:
                    logger.warning("orphan snapshot file not removed: %s (%s)", file, e)
        self._store.discard_snapshot(snapshot_id)
        logger.info("rolled back preparing snapshot %s", snapshot_id)

    def cleanup_orphan_files(self, retention_seconds: float = _DEFAULT_ORPHAN_RETENTION) -> int:
        """回收无活行引用且超过恢复保留期的孤儿 ``*.json`` 文件。

        小于保留期的文件保留，避免与刚中断的 saga 竞争（§5.1 末尾）。
        返回删除的文件数。
        """
        in_use = self._store.rel_paths_in_use()
        now = time.time()
        removed = 0
        if not self._snapshot_root.is_dir():
            return 0
        for material_dir in self._snapshot_root.iterdir():
            if not material_dir.is_dir():
                continue
            for json_file in material_dir.glob("*.json"):
                rel = json_file.relative_to(self._snapshot_root).as_posix()
                if rel in in_use:
                    continue
                try:
                    if now - json_file.stat().st_mtime > retention_seconds:
                        json_file.unlink()
                        removed += 1
                except OSError:
                    continue
        removed += self._cleanup_tmp_files(self._snapshot_root)
        return removed

    def _cleanup_tmp_files(self, root: Path) -> int:
        """清理残留的 ``.tmp-*`` 临时文件（非关键，尽力而为）。"""
        removed = 0
        for material_dir in root.iterdir() if root.is_dir() else ():
            if not material_dir.is_dir():
                continue
            for tmp in material_dir.glob("*.tmp-*"):
                try:
                    tmp.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed