"""模型任务与问答诊断持久化存储（P2 §7 / §7.1）。

- `model_jobs` 与索引任务同库（`JOB_STORE_DB_PATH`），保存模型操作元数据与安全状态，
  不存原始流响应、完整请求文本或密钥；
- 所有状态迁移均带 `owner + state` 条件（旧 worker / 重启后 worker 不得覆盖他人任务）。

单写者：SQLite 依赖连接级原子条件更新，配合 `busy_timeout` 防并发冲突。
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from runtime_paths import JOB_STORE_DB_PATH

_INITIALIZED = False
_INIT_LOCK = threading.Lock()
_DB_PATH = JOB_STORE_DB_PATH

_LOCK_CLEAR_SECONDS = 60

# 状态机：queued -> running -> succeeded / failed / cancel_requested(-> cancelled)
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCEL_REQUESTED = "cancel_requested"
STATE_CANCELLED = "cancelled"

_ACTIVE_STATES = (STATE_QUEUED, STATE_RUNNING, STATE_CANCEL_REQUESTED)
_TERMINAL_STATES = (STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED)

# 类型与最大尝试次数（首次执行计入 attempts）。
MAX_ATTEMPTS = {"pull": 3, "load": 2, "unload": 2}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS model_jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    target_model TEXT NOT NULL,
    state TEXT NOT NULL,
    progress_current INTEGER,
    progress_total INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message_safe TEXT,
    config_revision INTEGER,
    owner TEXT,
    cancel_requested_at TEXT,
    local_base_url TEXT,
    local_timeout_seconds INTEGER,
    local_keep_alive INTEGER,
    local_context_window INTEGER
);
CREATE INDEX IF NOT EXISTS idx_model_jobs_state_created ON model_jobs(state, created_at);
CREATE INDEX IF NOT EXISTS idx_model_jobs_model_active ON model_jobs(target_model, state);
"""

# P2 首版已创建过 `model_jobs` 的部署需要无损升级到任务快照字段。
# SQLite 的 CREATE TABLE IF NOT EXISTS 不会补列，必须显式执行幂等迁移。
_MODEL_JOB_COLUMN_MIGRATIONS = {
    "local_base_url": "TEXT",
    "local_timeout_seconds": "INTEGER",
    "local_keep_alive": "INTEGER",
    "local_context_window": "INTEGER",
}


class ModelJobNotFoundError(LookupError):
    """任务不存在（404）。"""


class ModelJobTerminalError(RuntimeError):
    """对终态或不可取消任务执行非法操作（409）。"""


class ModelJobDuplicateError(RuntimeError):
    """同模型已存在进行中任务，返回既有 jobId 而非创建重复。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    # 安全投影：布尔/数字字段规范化，绝不包含持有者随机 token 之外的可信凭据。
    d["attempts"] = d.get("attempts") or 0
    return d


class ModelJobStore:
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._ensure()

    @classmethod
    def instance(cls) -> "ModelJobStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = ModelJobStore()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """测试隔离：清空单例与表（需在设置 CENTAURAI_DATABASE_DATA_ROOT 后调用）。"""
        global _INITIALIZED
        with cls._instance_lock:
            cls._instance = None
        with _INIT_LOCK:
            _INITIALIZED = False

    # ---- SQLite helpers ----

    def _connect(self) -> sqlite3.Connection:
        self._ensure()
        conn = sqlite3.connect(str(_DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure(self) -> None:
        global _INITIALIZED
        if _INITIALIZED:
            return
        with _INIT_LOCK:
            if _INITIALIZED:
                return
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(_DB_PATH), timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_CREATE_SQL)
                existing_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(model_jobs)")
                }
                for name, sql_type in _MODEL_JOB_COLUMN_MIGRATIONS.items():
                    if name not in existing_columns:
                        conn.execute(f"ALTER TABLE model_jobs ADD COLUMN {name} {sql_type}")
                conn.commit()
            finally:
                conn.close()
            _INITIALIZED = True

    # ---- 创建与去重 ----

    def create_job(
        self,
        *,
        type_: str,
        target_model: str,
        config_revision: int | None,
        local_base_url: str | None = None,
        local_timeout_seconds: int | None = None,
        local_keep_alive: int | None = None,
        local_context_window: int | None = None,
    ) -> dict:
        """创建任务；同 `type_+target_model` 存在 active（queued/running/cancel_requested）时返回既有任务。

        pull/load/unload 均按目标模型去重，避免并发重复操作同一模型；冲突接口依据
        设计 §7 返回既有 `jobId` 而非创建第二个任务。

        `config_revision` 与 `local_*` 在创建时写入：前者为创建时配置版本（审计），
        `local_*` 为该版本对应的可恢复材料快照字段，供 worker 在领取/恢复时据此还原
        不可变快照（§7.0.1 第 3 条），避免配置变更后改用新地址/模型。
        """
        if type_ not in MAX_ATTEMPTS:
            raise ValueError(f"未知模型任务类型: {type_}")
        if not target_model or len(target_model) > 200:
            raise ValueError("模型名为空或超长")

        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM model_jobs WHERE type=? AND target_model=? "
                "AND state IN (?,?,?) ORDER BY created_at LIMIT 1",
                (type_, target_model) + _ACTIVE_STATES,
            )
            row = _row_to_dict(cur.fetchone())
            if row is not None:
                row["duplicate"] = True
                return row

            job = {
                "id": str(uuid.uuid4()),
                "type": type_,
                "target_model": target_model,
                "state": STATE_QUEUED,
                "progress_current": None,
                "progress_total": None,
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "lease_until": None,
                "attempts": 0,
                "error_code": None,
                "error_message_safe": None,
                "config_revision": config_revision,
                "owner": None,
                "cancel_requested_at": None,
                "local_base_url": local_base_url,
                "local_timeout_seconds": local_timeout_seconds,
                "local_keep_alive": local_keep_alive,
                "local_context_window": local_context_window,
            }
            job["duplicate"] = False
            conn.execute(
                "INSERT INTO model_jobs (id,type,target_model,state,progress_current,"
                "progress_total,created_at,started_at,finished_at,lease_until,attempts,"
                "error_code,error_message_safe,config_revision,owner,cancel_requested_at,"
                "local_base_url,local_timeout_seconds,local_keep_alive,local_context_window) "
                "VALUES (:id,:type,:target_model,:state,:progress_current,"
                ":progress_total,:created_at,:started_at,:finished_at,:lease_until,:attempts,"
                ":error_code,:error_message_safe,:config_revision,:owner,:cancel_requested_at,"
                ":local_base_url,:local_timeout_seconds,:local_keep_alive,:local_context_window)",
                job,
            )
            conn.commit()
            return job
        finally:
            conn.close()

    # ---- 领取（单 worker 条件更新）----

    def claim_next(self, worker_id: str, lease_seconds: int = _LOCK_CLEAR_SECONDS) -> dict | None:
        """原子领取最旧 queued 任务：queued -> running，写 owner/started_at/lease_until。"""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM model_jobs WHERE state=? ORDER BY created_at LIMIT 1",
                (STATE_QUEUED,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            job_id = row["id"]
            now = _now()
            lease = (
                datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            ).isoformat()
            cur = conn.execute(
                "UPDATE model_jobs SET state=?, owner=?, started_at=?, lease_until=?,"
                "attempts=attempts+1 WHERE id=? AND state=?",
                (STATE_RUNNING, worker_id, now, lease, job_id, STATE_QUEUED),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            return _row_to_dict(
                conn.execute("SELECT * FROM model_jobs WHERE id=?", (job_id,)).fetchone()
            )
        finally:
            conn.close()

    def renew(self, job_id: str, owner: str, lease_seconds: int = _LOCK_CLEAR_SECONDS) -> bool:
        """续租：仅 owner 且 running 时延长 lease_until。"""
        lease = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat()
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE model_jobs SET lease_until=? WHERE id=? AND owner=? AND state=?",
                (lease, job_id, owner, STATE_RUNNING),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    # ---- 进度 ----

    def update_progress(self, job_id: str, owner: str, current: int, total: int | None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE model_jobs SET progress_current=?, progress_total=? "
                "WHERE id=? AND owner=? AND state=?",
                (current, total, job_id, owner, STATE_RUNNING),
            )
            conn.commit()
        finally:
            conn.close()

    # ---- 终态 ----

    def succeed(self, job_id: str, owner: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE model_jobs SET state=?, finished_at=?, error_code=NULL, "
                "error_message_safe=NULL, lease_until=NULL "
                "WHERE id=? AND owner=? AND state=?",
                (STATE_SUCCEEDED, _now(), job_id, owner, STATE_RUNNING),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    def fail(
        self,
        job_id: str,
        owner: str,
        error_code: str,
        error_message_safe: str = "",
    ) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE model_jobs SET state=?, finished_at=?, error_code=?, "
                "error_message_safe=?, lease_until=NULL "
                "WHERE id=? AND owner=? AND state=?",
                (STATE_FAILED, _now(), error_code, error_message_safe, job_id, owner, STATE_RUNNING),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    def mark_cancelled(self, job_id: str, owner: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE model_jobs SET state=?, finished_at=?, lease_until=NULL "
                "WHERE id=? AND owner=? AND state=?",
                (STATE_CANCELLED, _now(), job_id, owner, STATE_CANCEL_REQUESTED),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    # ---- 取消请求（协作式）----

    def request_cancel(self, job_id: str) -> dict:
        conn = self._connect()
        try:
            row = _row_to_dict(
                conn.execute("SELECT * FROM model_jobs WHERE id=?", (job_id,)).fetchone()
            )
            if row is None:
                raise ModelJobNotFoundError(job_id)
            cur_state = row["state"]
            if cur_state in _TERMINAL_STATES:
                raise ModelJobTerminalError(cur_state)
            if cur_state == STATE_CANCEL_REQUESTED:
                return row
            if cur_state == STATE_QUEUED:
                # 尚未被 worker 领取：直接终态 cancelled（worker 只领取 queued，否则会滞留）。
                conn.execute(
                    "UPDATE model_jobs SET state=?, finished_at=?, cancel_requested_at=? "
                    "WHERE id=? AND state=?",
                    (STATE_CANCELLED, _now(), _now(), job_id, STATE_QUEUED),
                )
            else:
                # running：协作式，worker 在流读取边界写 cancelled。
                conn.execute(
                    "UPDATE model_jobs SET state=?, cancel_requested_at=? WHERE id=? AND state=?",
                    (STATE_CANCEL_REQUESTED, _now(), job_id, cur_state),
                )
            conn.commit()
            return _row_to_dict(
                conn.execute("SELECT * FROM model_jobs WHERE id=?", (job_id,)).fetchone()
            )
        finally:
            conn.close()

    # ---- 恢复（租约过期回收 / 幂等重投）----

    def mark_pull_installed(self, job_id: str) -> bool:
        """§7 规则 2：恢复时核验模型已完整存在，把重投的 queued pull 直接标记 succeeded。

        仅用于 worker 启动恢复，避免下载完成后崩溃重启的 pull 任务再次执行并消耗一次
        attempts。只在 `state=queued` 时才生效（运行中任务不在此列）。
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE model_jobs SET state=?, finished_at=?, owner=NULL, started_at=NULL,"
                "lease_until=NULL, progress_current=NULL, progress_total=NULL,"
                "error_code=NULL, error_message_safe=NULL "
                "WHERE id=? AND state=?",
                (STATE_SUCCEEDED, _now(), job_id, STATE_QUEUED),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    def recover_expired(self, now_utc=None) -> int:
        """恢复过期租约：running 重投；已请求取消的任务收敛为 cancelled。"""
        now = now_utc or datetime.now(timezone.utc)
        conn = self._connect()
        try:
            # 进程可能在用户请求取消后、下一次流读取边界之前被强制结束。
            # 该任务不能再被执行，租约过期后直接完成取消，避免永久停在
            # cancel_requested。
            cancelled = conn.execute(
                "UPDATE model_jobs SET state=?, finished_at=?, owner=NULL, lease_until=NULL "
                "WHERE state=? AND owner IS NOT NULL AND lease_until IS NOT NULL "
                "AND lease_until < ?",
                (STATE_CANCELLED, _now(), STATE_CANCEL_REQUESTED, now.isoformat()),
            )
            requeued = conn.execute(
                "UPDATE model_jobs SET state=?, owner=NULL, started_at=NULL,"
                "lease_until=NULL, progress_current=NULL, progress_total=NULL,"
                "error_code=NULL, error_message_safe=NULL "
                "WHERE state=? AND owner IS NOT NULL AND lease_until IS NOT NULL "
                "AND lease_until < ?",
                (STATE_QUEUED, STATE_RUNNING, now.isoformat()),
            )
            conn.commit()
            return cancelled.rowcount + requeued.rowcount
        finally:
            conn.close()

    def requeue_for_retry(self, job_id: str, error_code: str, error_message_safe: str = "") -> bool:
        """失败后按类型次数重投：低于上限则回 queued 减值尝试；否则置 failed。"""
        conn = self._connect()
        try:
            row = _row_to_dict(
                conn.execute("SELECT * FROM model_jobs WHERE id=?", (job_id,)).fetchone()
            )
            if row is None:
                conn.close()
                return False
            max_attempts = MAX_ATTEMPTS.get(row["type"], 1)
            if row["attempts"] < max_attempts:
                conn.execute(
                    "UPDATE model_jobs SET state=?, owner=NULL, started_at=NULL,"
                    "lease_until=NULL, finished_at=NULL, error_code=?, error_message_safe=? "
                    "WHERE id=? AND state=?",
                    (STATE_QUEUED, error_code, error_message_safe, job_id, STATE_FAILED),
                )
                conn.commit()
                conn.close()
                return True
            conn.close()
            return False
        finally:
            conn.close()

    # ---- 查询 ----

    def get(self, job_id: str) -> dict | None:
        conn = self._connect()
        try:
            return _row_to_dict(
                conn.execute("SELECT * FROM model_jobs WHERE id=?", (job_id,)).fetchone()
            )
        finally:
            conn.close()

    def list_jobs(
        self,
        *,
        state: str | None = None,
        type_: str | None = None,
        limit: int = 50,
        before_created: str | None = None,
    ) -> list[dict]:
        """按 created_at 倒序分页（`before_created` 上一页末条时间，做 keyset 游标）。"""
        limit = max(1, min(int(limit), 100))
        sql = "SELECT * FROM model_jobs WHERE 1=1"
        params: list = []
        if state:
            sql += " AND state=?"
            params.append(state)
        if type_:
            sql += " AND type=?"
            params.append(type_)
        if before_created:
            sql += " AND created_at < ?"
            params.append(before_created)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit + 1)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_dict(r) for r in rows[:limit]]
        finally:
            conn.close()

    def list_jobs_paged(
        self,
        *,
        state: str | None = None,
        type_: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """按 created_at 倒序 keyset 分页，返回 (items, next_cursor)。

        `cursor` 为上一页末条的 `created_at`；`next_cursor` 仅在仍有更多行时返回。
        """
        limit = max(1, min(int(limit), 100))
        sql = "SELECT * FROM model_jobs WHERE 1=1"
        params: list = []
        if state:
            sql += " AND state=?"
            params.append(state)
        if type_:
            sql += " AND type=?"
            params.append(type_)
        if cursor:
            sql += " AND created_at < ?"
            params.append(cursor)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit + 1)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            has_more = len(rows) > limit
            items = [_row_to_dict(r) for r in rows[:limit]]
            next_cursor = items[-1]["created_at"] if (has_more and items) else None
            return items, next_cursor
        finally:
            conn.close()

    def purge_expired_terminal_jobs(
        self, retention_days: int, now_utc: datetime | None = None
    ) -> int:
        """删除超过保留期的终态模型任务元数据。

        仅处理有 ``finished_at`` 的 succeeded/failed/cancelled 行，不会删除运行中、
        排队或取消请求中的任务，也不触碰 Ollama 模型文件、配置与诊断摘要。
        ``retention_days=0`` 由调用方解释为禁用，方法本身同样安全返回 0。
        """
        if retention_days <= 0:
            return 0
        now = now_utc or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=retention_days)).isoformat()
        placeholders = ",".join("?" for _ in _TERMINAL_STATES)
        conn = self._connect()
        try:
            cur = conn.execute(
                f"DELETE FROM model_jobs WHERE state IN ({placeholders}) "
                "AND finished_at IS NOT NULL AND finished_at < ?",
                (*_TERMINAL_STATES, cutoff),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
