"""MindOS 材料处理流水线 SQLite 持久化存储（阶段 A）。

本模块是「原材料处理任务」与「正文快照」的唯一持久化事实来源，替代旧
``index_jobs`` 的「原材料即向量索引」语义。两张表存放于同一 DB
（``material_pipeline.db``），使「任务终态」与「快照当前版本切换」共享事务域，
避免跨库不一致。

表一 material_jobs（原材料处理任务）：
- 状态机：``queued / processing / draft_ready / failed / paused / canceled``。
- 承接旧 index_jobs 的租约（``lease_until``）、``attempts``、``failure_class``、
  ``error_code`` 等机制；按 ``created_at`` FIFO 领取，手动重试可提高优先级。
- 同一 material 同一 ``target_version`` 最多存在一个活动任务（部分唯一索引），
  防止重复投递。

表二 material_content_snapshots（正文快照，派生内容唯一输入）：
- 快照行以 ``storage_state``（``preparing / ready / discarded``）承载 saga 中间态；
  只有 ``ready`` 且未被 ``superseded`` 的行才是当前可见快照。
- 大文本允许落盘到受控目录，SQLite 仅保存受控相对路径 ``rel_path`` 与内容 hash
  ``snapshot_hash``；禁止保存任意用户路径。文件实际写入/fsync/原子 rename 与启动
  孤儿恢复属于阶段 A-A2 的 saga 逻辑，本模块只提供状态/行迁移原语。

任务状态机（§4.1）：
    queued --(claim)--> processing --(ok)--> draft_ready
                                    `--(失败)--> failed
    queued / processing --(启动暂停)--> paused
    queued / paused --(用户取消)--> canceled
    paused --(用户继续)--> queued
    failed --(用户重试)--> queued
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from runtime_paths import MATERIAL_PIPELINE_DB_PATH
from ..device_context import SCOPE_GLOBAL

_INITIALIZED = False
_LOCK = threading.Lock()
_DB_PATH = MATERIAL_PIPELINE_DB_PATH
_DEFAULT_DB_PATH = MATERIAL_PIPELINE_DB_PATH

# ---- 材料处理任务状态（§4.1） ----
ST_QUEUED = "queued"
ST_PROCESSING = "processing"
ST_DRAFT_READY = "draft_ready"
ST_FAILED = "failed"
ST_PAUSED = "paused"
ST_CANCELED = "canceled"
_ACTIVE_STATES = {ST_QUEUED, ST_PROCESSING}
# 终端态：这些状态下的任务不允许再被 worker 领取（用户动作才可转回 queued）。
_TERMINAL_STATES = {ST_DRAFT_READY, ST_FAILED, ST_CANCELED}

# ---- 快照 saga 状态（§5.1） ----
SS_PREPARING = "preparing"
SS_READY = "ready"
SS_DISCARDED = "discarded"

# content_format（§5.1）
FMT_TEXT = "text"
FMT_OCR = "ocr"
FMT_TRANSCRIPT = "transcript"
FMT_MIXED = "mixed"
FMT_EMPTY = "empty"

# parse_status（§5.1）
PARSE_OK = "ok"
PARSE_EMPTY = "empty"
PARSE_FAILED = "failed"

# 启动暂停错误码（§8.2）
ERR_SERVICE_INTERRUPTED = "service_interrupted"

# failure_class（沿用 job_store 语义）
FC_BUSINESS = "business"
FC_INFRASTRUCTURE = "infrastructure"
FC_TRANSIENT = "transient"

# 快照大文件阈值：超过该字节数时正文写盘，SQLite 仅存 rel_path + hash。
_INLINE_TEXT_LIMIT = 256 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS material_jobs (
    job_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    target_version INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    source_hash TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until REAL,
    error_code TEXT,
    error_detail TEXT,
    failure_class TEXT,
    run_epoch TEXT NOT NULL DEFAULT '',
    resume_token TEXT,
    device_scope TEXT NOT NULL DEFAULT 'global',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_material_jobs_active
    ON material_jobs(owner_id, target_version) WHERE state IN ('queued','processing');
CREATE INDEX IF NOT EXISTS idx_material_jobs_claim
    ON material_jobs(state, priority DESC, created_at ASC);
CREATE TABLE IF NOT EXISTS material_content_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    storage_state TEXT NOT NULL,
    text_content TEXT,
    content_format TEXT NOT NULL DEFAULT 'text',
    parse_status TEXT NOT NULL DEFAULT 'ok',
    rel_path TEXT,
    snapshot_hash TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    device_scope TEXT NOT NULL DEFAULT 'global',
    created_at REAL NOT NULL,
    superseded_at REAL,
    UNIQUE(material_id, version)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_material
    ON material_content_snapshots(material_id, version DESC);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """为旧库补列（幂等）：column 已存在则跳过。"""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _failure_class(error_code: str | None) -> str | None:
    """错误码 -> 重试类别（沿用 index_jobs 分类语义）。

    - business：正文/业务校验失败，直接终态，不自动重试；
    - transient：网络/临时存储等，可有限退避重试；
    - infrastructure：存储/索引不可用，需健康恢复后重试。
    """
    if error_code in {"parse_failed", "empty", "source_changed"}:
        return FC_BUSINESS
    if error_code in {"read_failed", "storage_unavailable"}:
        return FC_INFRASTRUCTURE
    if error_code in {"embed_failed", "asr_unavailable", "timeout", "unknown"}:
        return FC_TRANSIENT
    return None


class MaterialJobNotFoundError(KeyError):
    pass


class MaterialJobConflictError(ValueError):
    """同一个 (material, target_version) 已存在活动任务，或 CAS 不匹配。"""

    status_code = 409


class MaterialPipelineStore:
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._ensure()

    @classmethod
    def instance(cls) -> "MaterialPipelineStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = MaterialPipelineStore()
            return cls._instance

    # ---- SQLite helpers ----

    def _connect(self) -> sqlite3.Connection:
        self._ensure()
        conn = sqlite3.connect(str(_DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure(self) -> None:
        global _INITIALIZED
        if _INITIALIZED:
            return
        with _LOCK:
            if _INITIALIZED:
                return
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(_DB_PATH), timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                # 阶段 2：为旧库补 device_scope 列（新库已包含）。
                _ensure_column(
                    conn, "material_jobs", "device_scope",
                    "device_scope TEXT NOT NULL DEFAULT 'global'",
                )
                _ensure_column(
                    conn, "material_content_snapshots", "device_scope",
                    "device_scope TEXT NOT NULL DEFAULT 'global'",
                )
                # 2026-08-29 的错误迁移曾把类型名 ``TEXT`` 误建成列名。
                # 该列不承载业务数据；先确保正确列存在，再在支持 DROP COLUMN
                # 的 SQLite 上幂等清除，避免错误 schema 持续污染新实例。
                columns = {row[1] for row in conn.execute("PRAGMA table_info(material_jobs)")}
                if "TEXT" in columns and "device_scope" in columns:
                    conn.execute('ALTER TABLE material_jobs DROP COLUMN "TEXT"')
                conn.commit()
            finally:
                conn.close()
            _INITIALIZED = True

    # ================= 材料处理任务 =================

    def enqueue_material_job(
        self,
        owner_id: str,
        target_version: int,
        source_path: str,
        *,
        source_hash: str = "",
        priority: int = 0,
        run_epoch: str = "",
        resume_token: str | None = None,
        device_scope: str = SCOPE_GLOBAL,
    ) -> dict:
        """创建原材料处理任务（state=queued）。

        同一 material 同一 target_version 若已存在活动任务（queued/processing），
        抛 ``MaterialJobConflictError``，避免重复入队。device_scope 由上传请求
        的票据身份写入，worker 领取后按任务行继续作用域隔离。
        """
        if not owner_id or target_version < 1:
            raise ValueError("invalid material job params")
        now = time.time()
        job_id = f"mj_{owner_id.removeprefix('mindos_')[:8]}_{target_version}_{int(now * 1000)}"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT job_id FROM material_jobs WHERE owner_id=? AND target_version=? "
                "AND state IN ('queued','processing')",
                (owner_id, target_version),
            ).fetchone()
            if active is not None:
                conn.rollback()
                raise MaterialJobConflictError(
                    f"material {owner_id} version {target_version} already active"
                )
            conn.execute(
                """INSERT INTO material_jobs
                   (job_id, owner_id, target_version, source_path, source_hash, state, priority,
                    attempts, lease_until, error_code, error_detail, failure_class,
                    run_epoch, resume_token, device_scope, created_at, started_at, finished_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,0,NULL,NULL,NULL,NULL,?,?,?,?,NULL,NULL,?)""",
                (
                    job_id, owner_id, target_version, source_path, source_hash, ST_QUEUED,
                    priority, run_epoch, resume_token, device_scope, now, now,
                ),
            )
            conn.commit()
            return self.get_job(job_id)
        finally:
            conn.close()

    def claim_next_material_job(
        self, run_epoch: str = "", lease_seconds: float = 300.0
    ) -> dict | None:
        """原子领取一条 queued 任务（FIFO：priority DESC, created_at ASC）。

        仅领取 ``run_epoch`` 匹配的当前队列任务；历史 ``paused`` 不在此领取。
        ``resume_token`` 非空的历史任务在对应 token 下也可被领取（用户显式继续）。
        领取时 increments ``attempts`` 并写租约 ``lease_until``。
        """
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM material_jobs "
                "WHERE state=? AND (run_epoch=? OR (resume_token IS NOT NULL AND resume_token!='')) "
                "ORDER BY priority DESC, created_at ASC LIMIT 1",
                (ST_QUEUED, run_epoch),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE material_jobs SET state=?, attempts=attempts+1, lease_until=?, "
                "started_at=COALESCE(started_at,?), updated_at=? WHERE job_id=?",
                (ST_PROCESSING, now + lease_seconds, now, now, row["job_id"]),
            )
            conn.commit()
            result = dict(row)
            result.update(
                {"state": ST_PROCESSING, "attempts": int(row["attempts"]) + 1,
                 "lease_until": now + lease_seconds, "started_at": result.get("started_at") or now}
            )
            return result
        finally:
            conn.close()

    def release_stale_leases(self, now: float | None = None) -> int:
        """租约过期但仍处于 processing 的任务释放回 queued（worker 崩溃恢复）。

        注意：此接口用于进程内 worker 崩溃后的回收，与「启动暂停」语义（§8.2）
        不同——启动暂停由 ``pause_pending_jobs`` 统一转 paused，绝不自动续跑。
        """
        now = now or time.time()
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE material_jobs SET state=?, lease_until=NULL, error_code=?, "
                    "error_detail='stale lease released', updated_at=? "
                    "WHERE state=? AND lease_until IS NOT NULL AND lease_until<?",
                    (ST_QUEUED, "stale_lease", now, ST_PROCESSING, now),
                )
                return int(cur.rowcount)
        finally:
            conn.close()

    def finish_material_job(
        self,
        job_id: str,
        state: str,
        *,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> dict:
        """写入任务终态/阶段（draft_ready / failed / paused / canceled）。

        - 清空租约；终端态写 ``finished_at``。
        - ``failed`` 时按 error_code 推断 ``failure_class``；``paused`` 为受控状态
          不自动续跑。
        """
        if state not in (_TERMINAL_STATES | {ST_PAUSED}):
            raise ValueError(f"invalid material job finish state: {state}")
        now = time.time()
        failure_class = _failure_class(error_code) if state == ST_FAILED else None
        terminal = state in _TERMINAL_STATES
        conn = self._connect()
        try:
            with conn:
                # Worker 只能结束自己仍持有的 processing 任务。生命周期删除可能在
                # 解析期间将任务取消；此处绝不能把 canceled 重新写成 draft_ready。
                cur = conn.execute(
                    "UPDATE material_jobs SET state=?, lease_until=NULL, error_code=?, "
                    "error_detail=?, failure_class=?, finished_at=?, updated_at=? "
                    "WHERE job_id=? AND state=?",
                    (state, error_code, error_detail, failure_class,
                     now if terminal else None, now, job_id, ST_PROCESSING),
                )
                if cur.rowcount == 0:
                    current = conn.execute("SELECT * FROM material_jobs WHERE job_id=?", (job_id,)).fetchone()
                    if current is None:
                        raise MaterialJobNotFoundError(job_id)
            return self.get_job(job_id)
        finally:
            conn.close()

    def pause_pending_jobs(self, error_code: str = ERR_SERVICE_INTERRUPTED) -> int:
        """启动恢复（§8.2）：将历史 queued/processing 统一转 paused，绝不自动续跑。

        返回受影响任务数。terminal/paused/draft_ready/failed 原样保留。
        """
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE material_jobs SET state=?, lease_until=NULL, error_code=?, "
                    "error_detail='service interrupted, pending tasks paused on startup', "
                    "failure_class=NULL, updated_at=? "
                    "WHERE state IN ('queued','processing')",
                    (ST_PAUSED, error_code, now),
                )
                return int(cur.rowcount)
        finally:
            conn.close()

    def resume_material_job(
        self, owner_id: str, target_version: int, *, run_epoch: str = ""
    ) -> dict:
        """将 paused 任务转 queued（用户显式「继续处理」）。

        仅允许从 paused 恢复；生成一次性 resume_token 并在给定 run_epoch 下
        可被 worker 领取（§8.2 R20）。
        """
        import uuid

        token = uuid.uuid4().hex
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM material_jobs WHERE owner_id=? AND target_version=?",
                (owner_id, target_version),
            ).fetchone()
            if job is None:
                conn.rollback()
                raise MaterialJobNotFoundError(f"{owner_id}:{target_version}")
            if job["state"] != ST_PAUSED:
                conn.rollback()
                raise MaterialJobConflictError(
                    f"job {job['job_id']} is {job['state']}, only paused can resume"
                )
            conn.execute(
                "UPDATE material_jobs SET state=?, run_epoch=?, resume_token=?, "
                "error_code=NULL, error_detail=NULL, finished_at=NULL, updated_at=? WHERE job_id=?",
                (ST_QUEUED, run_epoch, token, now, job["job_id"]),
            )
            conn.commit()
            return self.get_job(job["job_id"])
        finally:
            conn.close()

    def cancel_material_job(self, owner_id: str, target_version: int) -> dict:
        """用户取消：将 queued/paused 转 canceled。"""
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM material_jobs WHERE owner_id=? AND target_version=?",
                (owner_id, target_version),
            ).fetchone()
            if job is None:
                conn.rollback()
                raise MaterialJobNotFoundError(f"{owner_id}:{target_version}")
            if job["state"] not in (ST_QUEUED, ST_PAUSED):
                conn.rollback()
                raise MaterialJobConflictError(f"job {job['job_id']} cannot be canceled in {job['state']}")
            conn.execute(
                "UPDATE material_jobs SET state=?, finished_at=?, updated_at=? WHERE job_id=?",
                (ST_CANCELED, now, now, job["job_id"]),
            )
            conn.commit()
            return self.get_job(job["job_id"])
        finally:
            conn.close()

    def cancel_for_lifecycle(self, owner_id: str, target_version: int) -> bool:
        """删除/回收前取消活动任务，阻止 worker 再提交结果。

        允许取消 processing 是有意为之：worker 在快照和派生提交边界都会复核该
        状态，因而不会把已删除资料重新写回可见数据。
        """
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE material_jobs SET state=?, lease_until=NULL, error_code=?, "
                    "error_detail=?, finished_at=?, updated_at=? "
                    "WHERE owner_id=? AND target_version=? AND state IN (?, ?)",
                    (ST_CANCELED, "lifecycle_cancelled", "cancelled by material lifecycle", now, now,
                     owner_id, target_version, ST_QUEUED, ST_PROCESSING),
                )
                return bool(cur.rowcount)
        finally:
            conn.close()

    def retry_material_job(
        self,
        owner_id: str,
        target_version: int,
        *,
        priority: int = 10,
        run_epoch: str = "",
        source_hash: str | None = None,
    ) -> dict:
        """用户重试（§9.1 POST /retry）：从 failed/canceled 创建新的 queued 任务。

        CAS 由调用方以 ``expectedSnapshotVersion`` 校验（scheme §9.1）；
        本方法仅保证失败终态可安全重新入队。``source_hash`` 提供时重绑完成栅栏
        指纹（例如失败后源文件已替换），否则沿用原值。
        """
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM material_jobs WHERE owner_id=? AND target_version=?",
                (owner_id, target_version),
            ).fetchone()
            if job is None:
                conn.rollback()
                raise MaterialJobNotFoundError(f"{owner_id}:{target_version}")
            if job["state"] in _ACTIVE_STATES:
                conn.rollback()
                raise MaterialJobConflictError(
                    f"job {job['job_id']} is still active ({job['state']})"
                )
            if source_hash is None:
                source_hash = job["source_hash"]
            conn.execute(
                "UPDATE material_jobs SET state=?, priority=?, attempts=0, lease_until=NULL, "
                "error_code=NULL, error_detail=NULL, failure_class=NULL, run_epoch=?, "
                "resume_token=NULL, started_at=NULL, finished_at=NULL, source_hash=?, "
                "updated_at=? WHERE job_id=?",
                (ST_QUEUED, priority, run_epoch, source_hash, now, job["job_id"]),
            )
            conn.commit()
            return self.get_job(job["job_id"])
        finally:
            conn.close()

    def get_job(self, job_id: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM material_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise MaterialJobNotFoundError(job_id)
            return self._row_to_dict(row)
        finally:
            conn.close()

    def material_job(self, owner_id: str, target_version: int | None = None) -> dict | None:
        """返回某材料的最新（可指定版本）任务；无则 None。"""
        conn = self._connect()
        try:
            if target_version is not None:
                row = conn.execute(
                    "SELECT * FROM material_jobs WHERE owner_id=? AND target_version=?",
                    (owner_id, target_version),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM material_jobs WHERE owner_id=? ORDER BY updated_at DESC LIMIT 1",
                    (owner_id,),
                ).fetchone()
            return self._row_to_dict(row) if row is not None else None
        finally:
            conn.close()

    def list_jobs(self, owner_id: str | None = None, limit: int = 200) -> list[dict]:
        conn = self._connect()
        try:
            if owner_id is not None:
                rows = conn.execute(
                    "SELECT * FROM material_jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                    (owner_id, max(1, min(limit, 1000))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM material_jobs ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(limit, 1000)),),
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def remove_material(self, material_id: str) -> None:
        """移除未完成导入的任务及快照行（调用方已校验状态并清理源文件）。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM material_content_snapshots WHERE material_id=?", (material_id,))
            conn.execute("DELETE FROM material_jobs WHERE owner_id=?", (material_id,))
            conn.commit()
        finally:
            conn.close()

    def queue_summary(self) -> dict:
        """队列摘要：按 state 计数，供 /monitor 复用只读事实。"""
        counts = {s: 0 for s in (ST_QUEUED, ST_PROCESSING, ST_DRAFT_READY, ST_FAILED, ST_PAUSED, ST_CANCELED)}
        conn = self._connect()
        try:
            rows = conn.execute("SELECT state, COUNT(*) AS n FROM material_jobs GROUP BY state").fetchall()
        finally:
            conn.close()
        for row in rows:
            counts[row["state"]] = row["n"]
        counts["active"] = counts[ST_QUEUED] + counts[ST_PROCESSING]
        counts["total"] = sum(counts[k] for k in counts if k not in ("active", "total"))
        return counts

    # ================= 正文快照 =================

    def begin_snapshot(
        self,
        material_id: str,
        version: int,
        source_hash: str,
        *,
        job_id: str | None = None,
        content_format: str = FMT_TEXT,
        metadata: str = "{}",
    ) -> dict:
        """插入 preparing 快照行，占住 (material_id, version)。

        saga 第一步：行先落库为 ``preparing``，并**预先持久化目标相对路径**
        ``rel_path``（{material_id}/{version}.json）。这样即使进程在文件写入与
        commit 之间中断，启动恢复也能据该路径定位并清理/隔离孤儿文件——而不是
        读到 NULL 只能丢弃行、无法回滚刚写入的文件（阶段A评审 P1#2）。
        文件内容随后写入（A2）；``preparing`` 行由启动恢复处理，不暴露为当前快照。
        """
        import uuid

        if content_format not in (FMT_TEXT, FMT_OCR, FMT_TRANSCRIPT, FMT_MIXED, FMT_EMPTY):
            raise ValueError(f"invalid content_format: {content_format}")
        now = time.time()
        snapshot_id = f"snp_{material_id.removeprefix('mindos_')[:8]}_{version}_{uuid.uuid4().hex[:8]}"
        rel_path = f"{material_id}/{version}.json"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if job_id is not None:
                job = conn.execute(
                    "SELECT state FROM material_jobs WHERE job_id=? AND owner_id=? AND target_version=?",
                    (job_id, material_id, version),
                ).fetchone()
                if job is None or job["state"] != ST_PROCESSING:
                    conn.rollback()
                    return None
            # 若该 (material, version) 已有一条 preparing 孤儿行（上次进程中断），
            # 先删除，允许本次重试重新占位，避免 UNIQUE 冲突。
            conn.execute(
                "DELETE FROM material_content_snapshots WHERE material_id=? AND version=? "
                "AND storage_state=?",
                (material_id, version, SS_PREPARING),
            )
            # A reparse of the same material version must retain its previous
            # snapshot, not collide with UNIQUE(material_id, version) or overwrite
            # evidence already cited by a conversation. Snapshot revisions advance
            # independently; the job fence above still checks the requested file
            # version. New content therefore also gets a new consent identity.
            latest = conn.execute(
                "SELECT MAX(version) FROM material_content_snapshots WHERE material_id=?", (material_id,)
            ).fetchone()[0]
            if latest is not None and int(latest) >= version:
                version = int(latest) + 1
                snapshot_id = f"snp_{material_id.removeprefix('mindos_')[:8]}_{version}_{uuid.uuid4().hex[:8]}"
                rel_path = f"{material_id}/{version}.json"
            conn.execute(
                """INSERT INTO material_content_snapshots
                   (snapshot_id, material_id, version, source_hash, storage_state,
                    text_content, content_format, parse_status, rel_path, snapshot_hash,
                    metadata_json, created_at, superseded_at)
                   VALUES (?,?,?,?,?,NULL,?,?,?,?,?,?,NULL)""",
                (snapshot_id, material_id, version, source_hash, SS_PREPARING,
                 content_format, PARSE_OK, rel_path, "", metadata, now),
            )
            conn.commit()
            return self.get_snapshot(snapshot_id)
        finally:
            conn.close()

    def commit_snapshot(
        self,
        snapshot_id: str,
        *,
        text_content: str,
        parse_status: str = PARSE_OK,
        snapshot_hash: str = "",
        rel_path: Path | str | None = None,
    ) -> dict:
        """saga 收尾：将 preparing 行切为 ready，并在同一事务内 supersede 旧版本。

        旧版本同版本的已 ready 行被标 ``superseded_at``，成为历史版本；
        调用方在调用本方法前必须先完成文件落盘（fsync + 原子 rename），
        本方法只在 SQLite 事务内校验并切换可见版本（§5.1）。
        """
        if parse_status not in (PARSE_OK, PARSE_EMPTY, PARSE_FAILED):
            raise ValueError(f"invalid parse_status: {parse_status}")
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT material_id, storage_state FROM material_content_snapshots "
                "WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise MaterialJobNotFoundError(snapshot_id)
            if row["storage_state"] != SS_PREPARING:
                conn.rollback()
                raise MaterialJobConflictError(
                    f"snapshot {snapshot_id} is {row['storage_state']}, only preparing can commit"
                )
            material_id = row["material_id"]
            rel = str(rel_path) if rel_path is not None else None
            # 先写新快照为 ready，再在同一事务内 supersede 该材料的历史 ready 版本。
            conn.execute(
                "UPDATE material_content_snapshots SET storage_state=?, text_content=?, "
                "parse_status=?, snapshot_hash=?, rel_path=? WHERE snapshot_id=?",
                (SS_READY, text_content, parse_status, snapshot_hash, rel, snapshot_id),
            )
            conn.execute(
                "UPDATE material_content_snapshots SET superseded_at=? "
                "WHERE material_id=? AND storage_state=? AND snapshot_id!=? AND superseded_at IS NULL",
                (now, material_id, SS_READY, snapshot_id),
            )
            conn.commit()
            return self.get_snapshot(snapshot_id)
        finally:
            conn.close()

    def discard_snapshot(self, snapshot_id: str) -> None:
        """saga 失败/取消：将 preparing 行标记为 discarded（文件由 A2 清理）。"""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE material_content_snapshots SET storage_state=? WHERE snapshot_id=? "
                    "AND storage_state=?",
                    (SS_DISCARDED, snapshot_id, SS_PREPARING),
                )
        finally:
            conn.close()

    def current_snapshot(self, material_id: str) -> dict | None:
        """返回当前可见快照（storage_state=ready 且未被 superseded）。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM material_content_snapshots WHERE material_id=? "
                "AND storage_state=? AND superseded_at IS NULL "
                "ORDER BY version DESC LIMIT 1",
                (material_id, SS_READY),
            ).fetchone()
            return self._row_to_dict(row) if row is not None else None
        finally:
            conn.close()

    def get_snapshot(self, snapshot_id: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM material_content_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise MaterialJobNotFoundError(snapshot_id)
            return self._row_to_dict(row)
        finally:
            conn.close()

    def pending_snapshots(self, storage_state: str = SS_PREPARING) -> list[dict]:
        """列出处于给定 saga 中间态的快照行（供 A2 启动恢复扫描）。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM material_content_snapshots WHERE storage_state=?",
                (storage_state,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def rel_paths_in_use(self) -> set[str]:
        """返回所有仍被引用（非 discarded）的受控相对路径集合。

        A2 孤儿文件清理据此判断文件系统里哪些 ``*.json`` 已不再被任何活行引用。
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT rel_path FROM material_content_snapshots WHERE storage_state!=? AND rel_path IS NOT NULL",
                (SS_DISCARDED,),
            ).fetchall()
            return {r["rel_path"] for r in rows}
        finally:
            conn.close()

    # ================= 行映射 =================

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        row = dict(row)
        row["rel_path"] = row.get("rel_path")
        return row


def reset_for_tests(db_path=None) -> MaterialPipelineStore:
    """测试用：切换到独立 DB 并清空全局实例；无参数时恢复默认库路径。"""
    global _INITIALIZED, _DB_PATH, MaterialPipelineStore
    _INITIALIZED = False
    if db_path is None:
        _DB_PATH = _DEFAULT_DB_PATH
    else:
        _DB_PATH = Path(db_path)
    MaterialPipelineStore._instance = None
    return MaterialPipelineStore.instance()
