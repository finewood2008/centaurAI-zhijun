"""MindOS 上传/处理状态 SQLite 持久化存储。

对外保持与原有内存实现完全一致的 API，内部使用 SQLite 持久化，
服务重启后材料导入记录不会丢失。

P14-06：原材料目录升级为多级目录树。
- 目录存于 folder_nodes 自关联表（scope 区分 RAW / KNOWLEDGE，本阶段仅使用 RAW）。
- job_records.folder_id 可空外键：NULL 表示「未分类」（根/未归类）。
- job_records.folder 列仅作旧数据兼容读字段，禁止新写入（新关系一律写 folder_id）。
- 首次启动幂等迁移：把旧 folders(name) 单层目录升级为 RAW 根节点并回填 folder_id。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone

from runtime_paths import JOB_STORE_DB_PATH

_INITIALIZED = False
_LOCK = threading.Lock()
_DB_PATH = JOB_STORE_DB_PATH
_DEFAULT_DB_PATH = JOB_STORE_DB_PATH

# 目录 scope：本阶段仅 RAW；KNOWLEDGE 预留（后阶段目录化知识成品）。
SCOPE_RAW = "RAW"
SCOPE_KNOWLEDGE = "KNOWLEDGE"
_UNCATEGORIZED = "未分类"

# 目录名称合法性与长度限制（与旧 create_folder 保持一致）。
_MAX_FOLDER_NAME = 120
_FORBIDDEN_NAME_CHARS = "\\/\x00"


class FolderError(ValueError):
    """目录操作失败（参数非法/目标不合法等），API 层转 400。"""

    status_code = 400


class FolderNotFoundError(FolderError):
    status_code = 404


class FolderNameConflictError(FolderError):
    status_code = 409


# 建表脚本：目录树在 job_records 之前创建（后者引用前者外键）。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS folder_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    parent_id INTEGER NULL REFERENCES folder_nodes(id),
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_folder_nodes_scope_parent_name
    ON folder_nodes(scope, COALESCE(parent_id, 0), name);
CREATE INDEX IF NOT EXISTS idx_folder_nodes_parent ON folder_nodes(parent_id);
CREATE TABLE IF NOT EXISTS job_records (
    material_id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    job_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    folder TEXT NOT NULL DEFAULT '未分类',
    folder_id INTEGER NULL REFERENCES folder_nodes(id),
    canceled INTEGER NOT NULL DEFAULT 0,
    material_family_id TEXT,
    version_number INTEGER,
    supersedes_material_id TEXT,
    superseded_by_material_id TEXT,
    version_note TEXT,
    recycled INTEGER NOT NULL DEFAULT 0,
    device_scope TEXT NOT NULL DEFAULT 'global'
);
CREATE INDEX IF NOT EXISTS idx_job_folder ON job_records(folder_id);
CREATE INDEX IF NOT EXISTS idx_job_folder_legacy ON job_records(folder);
CREATE INDEX IF NOT EXISTS idx_job_created ON job_records(created_at DESC);
CREATE TABLE IF NOT EXISTS index_job_states (
    source_path TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    error TEXT,
    error_code TEXT,
    strategy_id TEXT,
    old_index_preserved INTEGER NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS index_jobs (
    source_path TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    force INTEGER NOT NULL DEFAULT 0,
    strategy_id TEXT,
    submit_wiki INTEGER NOT NULL DEFAULT 1,
    rebuild_session_id TEXT,
    routing_epoch INTEGER,
    target_generation_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until REAL,
    error TEXT,
    error_code TEXT,
    failure_class TEXT,
    next_retry_at REAL,
    auto_retry_count INTEGER NOT NULL DEFAULT 0,
    old_index_preserved INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_index_jobs_state_lease ON index_jobs(state, lease_until);
"""

# 查询时关联目录名：新数据以 folder_id 为唯一事实来源，folder 列仅兜底旧记录。
_RECORD_SELECT = (
    "SELECT j.*, f.name AS folder_name "
    "FROM job_records j LEFT JOIN folder_nodes f ON f.id = j.folder_id"
)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """为旧库补列（幂等）：column 已存在则跳过。"""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate_legacy_folders(conn: sqlite3.Connection) -> None:
    """首次迁移：旧 folders(name) 单层表 → folder_nodes 根节点，并回填 folder_id。

    幂等：每次启动执行一次即可；旧表在迁移后不再写入（仅作只读兼容）。
    - 迁移源 = 旧 folders 表名称 ∪ job_records.folder 中残留的目录名（防止目录名
      只存在于记录、未同步到旧表时被遗漏而误归「未分类」）。
    - 过滤空值/空白与「未分类」；同名根节点已存在则复用（幂等）。
    - 每个名称下所有 folder_id IS NULL 的历史资料：回填 folder_id。
    - 「未分类」保留 NULL（不建节点）。
    """
    legacy: set[str] = set()
    for row in conn.execute("SELECT name FROM folders").fetchall():
        name = (row["name"] or "").strip()
        if name and name != _UNCATEGORIZED:
            legacy.add(name)
    for row in conn.execute("SELECT DISTINCT folder FROM job_records").fetchall():
        name = (row["folder"] or "").strip()
        if name and name != _UNCATEGORIZED:
            legacy.add(name)
    if not legacy:
        return
    now = time.time()
    for name in sorted(legacy):
        node = conn.execute(
            "SELECT id FROM folder_nodes WHERE scope=? AND parent_id IS NULL AND name=?",
            (SCOPE_RAW, name),
        ).fetchone()
        if node is None:
            cur = conn.execute(
                "INSERT INTO folder_nodes (scope, parent_id, name, sort_order, created_at, updated_at) "
                "VALUES (?, NULL, ?, 0, ?, ?)",
                (SCOPE_RAW, name, now, now),
            )
            node_id = cur.lastrowid
        else:
            node_id = node["id"]
        conn.execute(
            "UPDATE job_records SET folder_id=? WHERE folder=? AND folder_id IS NULL",
            (node_id, name),
        )
    conn.commit()


def _migrate_material_versions(conn: sqlite3.Connection) -> None:
    """为 P15 前的原材料幂等补齐单成员版本家族。"""
    conn.execute(
        "UPDATE job_records SET material_family_id=material_id "
        "WHERE material_family_id IS NULL OR trim(material_family_id)=''"
    )
    conn.execute(
        "UPDATE job_records SET version_number=1 "
        "WHERE version_number IS NULL OR version_number < 1"
    )
    conn.commit()


class JobStore:
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._ensure()

    @classmethod
    def instance(cls) -> "JobStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = JobStore()
            return cls._instance

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
        with _LOCK:
            if _INITIALIZED:
                return
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(_DB_PATH), timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                # 旧库升级：job_records 不存在 folder_id 列时补列（默认 NULL）。
                _ensure_column(
                    conn,
                    "job_records",
                    "folder_id",
                    "folder_id INTEGER REFERENCES folder_nodes(id)",
                )
                _ensure_column(conn, "job_records", "material_family_id", "material_family_id TEXT")
                _ensure_column(conn, "job_records", "version_number", "version_number INTEGER")
                _ensure_column(conn, "job_records", "supersedes_material_id", "supersedes_material_id TEXT")
                _ensure_column(conn, "job_records", "superseded_by_material_id", "superseded_by_material_id TEXT")
                _ensure_column(conn, "job_records", "version_note", "version_note TEXT")
                _ensure_column(conn, "job_records", "recycled", "recycled INTEGER NOT NULL DEFAULT 0")
                # 阶段 2：业务数据按真实 device_id 作用域隔离（旧库补列，默认全局作用域）。
                _ensure_column(conn, "job_records", "device_scope", "device_scope TEXT NOT NULL DEFAULT 'global'")
                # Do not put this index in _SCHEMA: SQLite evaluates CREATE INDEX
                # against an existing legacy table before the ALTER above runs.
                conn.execute("CREATE INDEX IF NOT EXISTS idx_job_scope ON job_records(device_scope)")
                # P0-4：索引任务终态补列（旧库幂等 ALTER）。
                _ensure_column(conn, "index_job_states", "old_index_preserved", "old_index_preserved INTEGER NOT NULL DEFAULT 0")
                # P0-4：稳定错误码补列（后端重启后可据此精确重试/展示，不依赖异常原文）。
                _ensure_column(conn, "index_job_states", "error_code", "error_code TEXT")
                _ensure_column(conn, "index_jobs", "failure_class", "failure_class TEXT")
                _ensure_column(conn, "index_jobs", "next_retry_at", "next_retry_at REAL")
                _ensure_column(conn, "index_jobs", "auto_retry_count", "auto_retry_count INTEGER NOT NULL DEFAULT 0")
                # Existing failed rows predate failure_class.  Preserve their
                # history while making known HNSW/read failures recoverable as
                # soon as this version starts; unknown rows intentionally stay
                # failed and require an explicit retry instead of broad replay.
                conn.execute(
                    "UPDATE index_jobs SET failure_class=CASE error_code "
                    "WHEN 'parse_failed' THEN 'business' WHEN 'empty' THEN 'business' "
                    "WHEN 'write_failed' THEN 'infrastructure' WHEN 'read_failed' THEN 'infrastructure' "
                    "WHEN 'index_corrupted' THEN 'infrastructure' "
                    "WHEN 'embed_failed' THEN 'transient' WHEN 'asr_unavailable' THEN 'transient' "
                    "WHEN 'unknown' THEN 'transient' END "
                    "WHERE state='failed' AND failure_class IS NULL"
                )
                # 旧 folders(name) 单层目录 → folder_nodes 树（幂等，仅首次生效）。
                _migrate_legacy_folders(conn)
                _migrate_material_versions(conn)
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_job_records_family_version "
                    "ON job_records(material_family_id, version_number)"
                )
                conn.commit()
            finally:
                conn.close()
            _INITIALIZED = True

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        # folder_name 来自 folder_nodes 关联（新数据唯一事实来源）；旧记录回退 folder 列。
        folder_name = row["folder_name"] if "folder_name" in row.keys() else None
        folder_name = folder_name or row["folder"] or _UNCATEGORIZED
        return {
            "material_id": row["material_id"],
            "file_name": row["file_name"],
            "file_type": row["file_type"],
            "source_path": row["source_path"],
            "job_id": row["job_id"],
            "created_at": row["created_at"],
            "folder": folder_name,
            "folder_id": row["folder_id"] if "folder_id" in row.keys() else None,
            "canceled": bool(row["canceled"]),
            "material_family_id": row["material_family_id"] if "material_family_id" in row.keys() else row["material_id"],
            "version_number": row["version_number"] if "version_number" in row.keys() else 1,
            "supersedes_material_id": row["supersedes_material_id"] if "supersedes_material_id" in row.keys() else None,
            "superseded_by_material_id": row["superseded_by_material_id"] if "superseded_by_material_id" in row.keys() else None,
            "version_note": row["version_note"] if "version_note" in row.keys() else None,
            "recycled": bool(row["recycled"]) if "recycled" in row.keys() else False,
            "device_scope": row["device_scope"] if "device_scope" in row.keys() else SCOPE_GLOBAL,
        }

    def _node_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "scope": row["scope"],
            "parentId": row["parent_id"],
            "name": row["name"],
            "sortOrder": row["sort_order"],
            "materialCount": row["material_count"] if "material_count" in row.keys() else 0,
            "createdAt": datetime.fromtimestamp(row["created_at"], tz=timezone.utc).isoformat(),
            "updatedAt": datetime.fromtimestamp(row["updated_at"], tz=timezone.utc).isoformat(),
        }

    # ---- 索引任务终态（watcher._JOBS 为纯内存，重启丢失；终态在此落盘） ----

    def enqueue_index_job(
        self, source_path: str, *, force: bool = False, strategy_id: str | None = None,
        submit_wiki: bool = True, rebuild_session_id: str | None = None,
        routing_epoch: int | None = None, target_generation_id: str | None = None,
    ) -> bool:
        """持久化入队。活动任务去重，已终态任务可安全重新提交。"""
        now = time.time()
        conn = self._connect()
        try:
            row = conn.execute("SELECT state FROM index_jobs WHERE source_path=?", (source_path,)).fetchone()
            if row and row["state"] in ("queued", "processing", "validating"):
                return False
            conn.execute(
                "INSERT INTO index_jobs(source_path,state,force,strategy_id,submit_wiki,rebuild_session_id,"
                "routing_epoch,target_generation_id,attempts,lease_until,error,error_code,old_index_preserved,created_at,updated_at,finished_at) "
                "VALUES(?,?,?,?,?,?,?,?,0,NULL,NULL,NULL,0,?,?,NULL) "
                "ON CONFLICT(source_path) DO UPDATE SET state='queued',force=excluded.force,"
                "strategy_id=excluded.strategy_id,submit_wiki=excluded.submit_wiki,"
                "rebuild_session_id=excluded.rebuild_session_id,routing_epoch=excluded.routing_epoch,"
                "target_generation_id=excluded.target_generation_id,lease_until=NULL,error=NULL,error_code=NULL,"
                "failure_class=NULL,next_retry_at=NULL,auto_retry_count=0,old_index_preserved=0,"
                "updated_at=excluded.updated_at,finished_at=NULL",
                (source_path, "queued", int(force), strategy_id, int(submit_wiki), rebuild_session_id,
                 routing_epoch, target_generation_id, now, now),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def claim_index_job(self, source_path: str, lease_seconds: float = 900.0) -> dict | None:
        """原子领取 queued/retryable 任务，避免重放和线程池重复执行。"""
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM index_jobs WHERE source_path=? AND state IN ('queued','retryable')",
                (source_path,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE index_jobs SET state='processing',attempts=attempts+1,lease_until=?,updated_at=? "
                "WHERE source_path=?", (now + lease_seconds, now, source_path),
            )
            conn.commit()
            result = dict(row)
            result.update({"state": "processing", "attempts": int(row["attempts"]) + 1,
                           "lease_until": now + lease_seconds})
            return result
        finally:
            conn.close()

    @staticmethod
    def _failure_class(error_code: str | None) -> str | None:
        if error_code in {"parse_failed", "empty"}:
            return "business"
        if error_code in {"write_failed", "read_failed", "index_corrupted"}:
            return "infrastructure"
        if error_code in {"embed_failed", "asr_unavailable", "unknown"}:
            return "transient"
        return None

    def finish_index_job(self, source_path: str, state: str, *, error: str | None = None,
                         error_code: str | None = None, old_index_preserved: bool = False) -> None:
        if state not in ("done", "failed", "retryable", "validating"):
            raise ValueError(f"invalid index job state: {state}")
        now = time.time()
        terminal = state in ("done", "failed")
        conn = self._connect()
        try:
            row = conn.execute("SELECT auto_retry_count FROM index_jobs WHERE source_path=?", (source_path,)).fetchone()
            failure_class = self._failure_class(error_code) if state == "failed" else None
            retry_count = int(row["auto_retry_count"] or 0) if row else 0
            next_retry = None
            if state == "failed" and failure_class == "transient" and retry_count < 4:
                retry_count += 1
                # 1 / 5 / 15 / 60 minute exponential-ish backoff.
                next_retry = now + (60, 300, 900, 3600)[retry_count - 1]
            elif state == "done":
                # A later successful run closes the retry episode.  Do not let
                # an old transient-failure counter constrain a future incident.
                retry_count = 0
            conn.execute(
                "UPDATE index_jobs SET state=?,lease_until=NULL,error=?,error_code=?,failure_class=?,next_retry_at=?,"
                "auto_retry_count=?,old_index_preserved=?,updated_at=?,finished_at=? WHERE source_path=?",
                (state, error, error_code, failure_class, next_retry, retry_count, int(old_index_preserved),
                 now, now if terminal else None, source_path),
            )
            conn.commit()
        finally:
            conn.close()

    def recover_index_jobs(self) -> list[dict]:
        """启动恢复：过期/被中断的 running 任务重置为 queued 并返回全部待投递任务。"""
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE index_jobs SET state='queued',lease_until=NULL,updated_at=? "
                "WHERE state IN ('processing','validating')", (now,),
            )
            rows = conn.execute(
                "SELECT * FROM index_jobs WHERE state IN ('queued','retryable') ORDER BY updated_at"
            ).fetchall()
            conn.commit()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def requeue_recoverable_failures(self, *, infrastructure_only: bool = False,
                                     failure_classes: tuple[str, ...] | None = None,
                                     limit: int = 32) -> list[dict]:
        """Requeue only classified infrastructure/transient failures after health recovery."""
        now = time.time()
        classes = failure_classes or (("infrastructure",) if infrastructure_only else ("infrastructure", "transient"))
        placeholders = ",".join("?" for _ in classes)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM index_jobs WHERE state='failed' AND failure_class IN ({placeholders}) "
                "AND (failure_class != 'transient' OR auto_retry_count < 4) "
                "AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY updated_at LIMIT ?",
                (*classes, now, max(1, min(limit, 128))),
            ).fetchall()
            paths = [row["source_path"] for row in rows]
            for path in paths:
                conn.execute("UPDATE index_jobs SET state='queued',lease_until=NULL,error=NULL,error_code=NULL,"
                             "next_retry_at=NULL,updated_at=?,finished_at=NULL WHERE source_path=?", (now, path))
            conn.commit()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def requeue_done_index_jobs_for_generation(self, generation_id: str) -> list[dict]:
        """只重放写入指定故障 delta 的已完成材料任务。

        ``target_generation_id`` 在任务入队时由当前写路由快照写入。delta 损坏后，
        它是无需读取故障 HNSW 文件即可确定受影响材料的持久化账本。绝不能把其他
        健康 delta 或 base 中已完成的任务一并回退，否则一次局部损坏会造成全量重建。
        """
        if not generation_id:
            return []
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE index_jobs SET state='queued',lease_until=NULL,error=NULL,error_code=NULL,"
                "updated_at=?,finished_at=NULL WHERE state='done' AND target_generation_id=?",
                (now, generation_id),
            )
            rows = conn.execute(
                "SELECT * FROM index_jobs WHERE state='queued' AND target_generation_id=? ORDER BY updated_at",
                (generation_id,),
            ).fetchall()
            conn.commit()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_index_job(self, source_path: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM index_jobs WHERE source_path=?", (source_path,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_index_jobs(self, include_done: bool = False) -> list[dict]:
        conn = self._connect()
        try:
            states = "('queued','retryable','processing','validating','failed','done')" if include_done else "('queued','retryable','processing','validating','failed')"
            return [dict(row) for row in conn.execute(
                f"SELECT * FROM index_jobs WHERE state IN {states} ORDER BY updated_at DESC"
            ).fetchall()]
        finally:
            conn.close()

    def save_index_outcome(
        self,
        source_path: str,
        state: str,
        error: str | None = None,
        strategy_id: str | None = None,
        error_code: str | None = None,
        old_index_preserved: bool = False,
        finished_at: float | None = None,
    ) -> None:
        """持久化索引任务终态（done/failed），供后端重启后恢复材料状态。

        error_code：稳定错误码（P0-4，如 parse_failed/embed_failed），重启后
        可据此精确重试或展示，不依赖异常原文。
        old_index_preserved：写入失败时旧索引是否仍被保留（P0-4 可观测）。
        """
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO index_job_states"
                "(source_path, state, error, error_code, strategy_id, old_index_preserved, finished_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    source_path,
                    state,
                    error,
                    error_code,
                    strategy_id,
                    1 if old_index_preserved else 0,
                    finished_at if finished_at is not None else time.time(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def index_outcome(self, source_path: str) -> dict | None:
        """读取持久化索引任务终态；无记录时返回 None。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state, error, error_code, strategy_id, old_index_preserved, finished_at "
                "FROM index_job_states WHERE source_path=?",
                (source_path,),
            ).fetchone()
            if row is None:
                return None
            rec = dict(row)
            rec["old_index_preserved"] = bool(rec.get("old_index_preserved"))
            return rec
        finally:
            conn.close()

    def queue_summary(self) -> dict:
        """索引队列摘要（P2 §4.4）：按 index_jobs.state 计数，供 /monitor 复用只读事实。"""
        counts = {
            "queued": 0, "retryable": 0, "processing": 0,
            "validating": 0, "failed": 0, "done": 0,
        }
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM index_jobs GROUP BY state"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            s = row["state"]
            if s in counts:
                counts[s] = row["n"]
        counts["active"] = counts["queued"] + counts["retryable"] + counts["processing"] + counts["validating"]
        counts["total"] = sum(counts[k] for k in counts if k not in ("active", "total"))
        return counts

    # ---- Public API (与原有内存实现完全一致) ----

    def register(
        self,
        material_id: str,
        file_name: str,
        file_type: str,
        source_path: str,
        folder: str = "",
        folder_id: int | None = None,
        material_family_id: str | None = None,
        supersedes_material_id: str | None = None,
        version_note: str | None = None,
        device_scope: str = "global",
    ) -> dict:
        """登记一条 MindOS 资料记录。

        P14-06 起新写入一律使用 folder_id（NULL = 未分类）；folder 参数仅保留
        兼容旧调用，等价于 folder_id=对应 RAW 根节点（若存在）。
        阶段 2：device_scope 由上传请求的票据身份决定，跨设备/账号资料互不可见
        （本机调试模式固定为 global）。
        """
        legacy_name = (folder or "").strip() or _UNCATEGORIZED
        folder_id_value = folder_id
        if folder_id_value is not None:
            conn0 = self._connect()
            try:
                node = conn0.execute(
                    "SELECT id, name FROM folder_nodes WHERE id=? AND scope=?",
                    (folder_id_value, SCOPE_RAW),
                ).fetchone()
            finally:
                conn0.close()
            if node is None:
                folder_id_value = None
                legacy_name = _UNCATEGORIZED
            else:
                legacy_name = node["name"]
        family_id = (material_family_id or material_id).strip()
        if not family_id:
            family_id = material_id
        record = {
            "material_id": material_id,
            "file_name": file_name,
            "file_type": file_type,
            "source_path": source_path,
            "job_id": f"job_{material_id.removeprefix('mindos_')[:8]}",
            "created_at": time.time(),
            "folder": legacy_name,
            "folder_id": folder_id_value,
            "material_family_id": family_id,
            "supersedes_material_id": supersedes_material_id,
            "superseded_by_material_id": None,
            "version_note": (version_note or "").strip()[:500] or None,
            "device_scope": device_scope or "global",
        }
        conn = self._connect()
        try:
            # 同一家族的版本号必须串行分配，避免并发上传得到重复版本号。
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(version_number), 0) AS version_number "
                "FROM job_records WHERE material_family_id=?",
                (family_id,),
            ).fetchone()
            record["version_number"] = int(row["version_number"] or 0) + 1
            conn.execute(
                """INSERT INTO job_records
                   (material_id, file_name, file_type, source_path, job_id, created_at, folder, folder_id,
                    material_family_id, version_number, supersedes_material_id,
                    superseded_by_material_id, version_note, device_scope)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["material_id"], record["file_name"], record["file_type"],
                    record["source_path"], record["job_id"], record["created_at"],
                    record["folder"], record["folder_id"], record["material_family_id"],
                    record["version_number"], record["supersedes_material_id"],
                    record["superseded_by_material_id"], record["version_note"],
                    record["device_scope"],
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return dict(record)

    def list_versions(self, material_id: str) -> list[dict] | None:
        """返回资料所属家族的全部版本，按版本号倒序。"""
        current = self.get(material_id)
        if current is None:
            return None
        conn = self._connect()
        try:
            rows = conn.execute(
                f"{_RECORD_SELECT} WHERE j.material_family_id=? ORDER BY j.version_number DESC",
                (current["material_family_id"],),
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def finalize_version_link(self, material_id: str) -> None:
        """新版本可用后，才在其直接前代写入 superseded_by 冗余指针。"""
        record = self.get(material_id)
        if record is None or not record.get("supersedes_material_id"):
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE job_records SET superseded_by_material_id=? "
                    "WHERE material_id=? AND superseded_by_material_id IS NULL",
                    (material_id, record["supersedes_material_id"]),
                )
        finally:
            conn.close()

    def get(self, material_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                f"{_RECORD_SELECT} WHERE j.material_id=?", (material_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def update_folder(self, material_id: str, folder: str) -> dict | None:
        """[DEPRECATED] 按字符串文件夹名移动资料（仅旧接口使用，新调用请用 ID 接口）。"""
        folder_name = folder.strip() or _UNCATEGORIZED
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE job_records SET folder=? WHERE material_id=?",
                    (folder_name, material_id),
                )
                if cursor.rowcount == 0:
                    return None
            row = conn.execute(
                f"{_RECORD_SELECT} WHERE j.material_id=?", (material_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def rename_folder(self, old_name: str, new_name: str) -> int:
        """[DEPRECATED] 字符串文件夹重命名（仅旧接口使用）。"""
        old_name = old_name.strip()
        new_name = new_name.strip() or _UNCATEGORIZED
        if old_name == new_name:
            return 0
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE job_records SET folder=? WHERE folder=?",
                    (new_name, old_name),
                )
                return cursor.rowcount
        finally:
            conn.close()

    def mark_canceled(self, material_id: str) -> bool:
        """标记任务已取消（对外呈现为失败状态，前端停止轮询并保留重试入口）。"""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE job_records SET canceled=1 WHERE material_id=?",
                    (material_id,),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()

    def is_canceled(self, material_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT canceled FROM job_records WHERE material_id=?", (material_id,)
            ).fetchone()
            return bool(row and row["canceled"])
        finally:
            conn.close()

    # ---- P15-05：回收站（受控回收，软状态 + 原文件进入回收目录，可恢复） ----

    def set_recycled(self, material_id: str, recycled: bool) -> bool:
        """标记材料是否已回收（回收 = 移出活跃索引，但记录保留可恢复）。"""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE job_records SET recycled=? WHERE material_id=?",
                    (1 if recycled else 0, material_id),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()

    def recycled_ids(self, device_scope: str | None = None) -> set[str]:
        """当前设备作用域内已回收材料 ID；device_scope 为 None 时不限作用域（运维用）。"""
        conn = self._connect()
        try:
            if device_scope is not None:
                rows = conn.execute(
                    "SELECT material_id FROM job_records WHERE recycled=1 AND device_scope=?",
                    (device_scope,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT material_id FROM job_records WHERE recycled=1"
                ).fetchall()
            return {r["material_id"] for r in rows}
        finally:
            conn.close()

    def is_recycled(self, material_id: str, device_scope: str | None = None) -> bool:
        conn = self._connect()
        try:
            if device_scope is not None:
                row = conn.execute(
                    "SELECT recycled FROM job_records WHERE material_id=? AND device_scope=?",
                    (material_id, device_scope),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT recycled FROM job_records WHERE material_id=?", (material_id,)
                ).fetchone()
            return bool(row and row["recycled"])
        finally:
            conn.close()

    def delete(self, material_id: str) -> bool:
        """永久清除：删除材料记录本身（物理文件与派生数据由调用方先行清理）。"""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM job_records WHERE material_id=?", (material_id,)
                )
                return cursor.rowcount > 0
        finally:
            conn.close()

    def list(self, device_scope: str | None = None) -> list[dict]:
        """返回资料记录副本；调用方负责生成公开状态。

        阶段 2：传入 device_scope 时仅返回该作用域的资料（跨设备/账号互不可见）；
        None 表示不限作用域（迁移/运维查询用）。
        """
        conn = self._connect()
        try:
            if device_scope is None:
                rows = conn.execute(
                    f"{_RECORD_SELECT} ORDER BY j.created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    f"{_RECORD_SELECT} WHERE j.device_scope=? ORDER BY j.created_at DESC",
                    (device_scope,),
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # ---- 文件夹管理（旧字符串 API，DEPRECATED） ----

    def create_folder(self, name: str) -> bool:
        """[DEPRECATED] 字符串文件夹创建（旧接口兼容；新调用请用 create_folder_node）。"""
        name = name.strip()
        if not name or name == _UNCATEGORIZED or len(name) > _MAX_FOLDER_NAME:
            return False
        if any(char in name for char in _FORBIDDEN_NAME_CHARS):
            return False
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO folders(name) VALUES (?)", (name,)
                )
                return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_folder(self, name: str) -> int:
        """[DEPRECATED] 字符串文件夹删除（旧接口兼容；新调用请用 delete_folder_node）。"""
        name = name.strip()
        if not name or name == _UNCATEGORIZED:
            return 0
        conn = self._connect()
        try:
            with conn:
                moved = conn.execute(
                    "UPDATE job_records SET folder=? WHERE folder=? AND folder_id IS NULL",
                    (_UNCATEGORIZED, name),
                ).rowcount
                conn.execute("DELETE FROM folders WHERE name=?", (name,))
                return moved
        finally:
            conn.close()

    def list_folders(self) -> list[str]:
        """[DEPRECATED] 返回字符串文件夹名（目录树 + 旧 folder 列 + 显式旧表 的去重合集）。

        新调用方应使用 list_folder_nodes（ID + parent 树结构）。
        """
        conn = self._connect()
        try:
            node_names = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM folder_nodes WHERE scope=?", (SCOPE_RAW,)
                ).fetchall()
            }
            explicit = {r["name"] for r in conn.execute(
                "SELECT name FROM folders"
            ).fetchall()}
            legacy_material = {
                r["folder"] for r in conn.execute(
                    "SELECT DISTINCT folder FROM job_records"
                ).fetchall() if r["folder"]
            }
            names = (node_names | explicit | legacy_material) - {_UNCATEGORIZED}
            return [_UNCATEGORIZED] + sorted(names)
        finally:
            conn.close()

    # ---- 目录树（P14-06） ----

    def _node(self, conn: sqlite3.Connection, folder_id: int) -> sqlite3.Row | None:
        return conn.execute(
            """SELECT f.*,
                      (SELECT COUNT(*) FROM job_records j WHERE j.folder_id = f.id) AS material_count
               FROM folder_nodes f WHERE f.id=?""",
            (folder_id,),
        ).fetchone()

    def _is_descendant(self, conn: sqlite3.Connection, node_id: int, ancestor_id: int) -> bool:
        """node_id 是否位于 ancestor_id 的子树内（沿 parent 链向上可达）。"""
        row = conn.execute(
            "SELECT parent_id FROM folder_nodes WHERE id=?", (node_id,)
        ).fetchone()
        seen: set[int] = set()
        while row is not None and row["parent_id"] is not None:
            parent = row["parent_id"]
            if parent == ancestor_id:
                return True
            if parent in seen:
                return False  # 防御：环路不应存在
            seen.add(parent)
            row = conn.execute(
                "SELECT parent_id FROM folder_nodes WHERE id=?", (parent,)
            ).fetchone()
        return False

    def _subtree_ids(self, conn: sqlite3.Connection, folder_id: int) -> set[int]:
        """返回目录自身及其全部后代的 id 集合。"""
        ids = {folder_id}
        frontier = [folder_id]
        while frontier:
            rows = conn.execute(
                f"SELECT id FROM folder_nodes WHERE parent_id IN ({','.join('?' * len(frontier))})",
                frontier,
            ).fetchall()
            frontier = [r["id"] for r in rows if r["id"] not in ids]
            ids.update(frontier)
        return ids

    def create_folder_node(
        self, scope: str, name: str, parent_id: int | None = None
    ) -> dict:
        """创建目录节点。同 scope + parent 下名称必须唯一；返回新节点 dict。

        仅接受白名单 scope；子目录必须与父节点同 scope，禁止跨作用域创建
        （否则会在 RAW 树中生出一个父级不可见的孤儿节点）。
        """
        scope = scope or SCOPE_RAW
        if scope not in (SCOPE_RAW, SCOPE_KNOWLEDGE):
            raise FolderError("不支持的目录 scope")
        name = name.strip()
        if not name or name == _UNCATEGORIZED or len(name) > _MAX_FOLDER_NAME:
            raise FolderError("目录名称不合法")
        if any(char in name for char in _FORBIDDEN_NAME_CHARS):
            raise FolderError("目录名称不能包含 / \\ 或控制字符")
        conn = self._connect()
        try:
            with conn:
                if parent_id is not None:
                    parent = self._node(conn, parent_id)
                    if parent is None:
                        raise FolderNotFoundError("父目录不存在")
                    if parent["scope"] != scope:
                        raise FolderError("父目录 scope 不一致，不能跨作用域创建子目录")
                dup = conn.execute(
                    "SELECT id FROM folder_nodes WHERE scope=? AND COALESCE(parent_id,0)=? AND name=?",
                    (scope, parent_id or 0, name),
                ).fetchone()
                if dup is not None:
                    raise FolderNameConflictError("同级目录下已存在同名目录")
                now = time.time()
                cur = conn.execute(
                    "INSERT INTO folder_nodes (scope, parent_id, name, sort_order, created_at, updated_at) "
                    "VALUES (?, ?, ?, 0, ?, ?)",
                    (scope, parent_id, name, now, now),
                )
                node_id = cur.lastrowid
            return self._node_to_dict(self._node(conn, node_id))
        finally:
            conn.close()

    def rename_folder_node(self, folder_id: int, new_name: str) -> dict:
        """重命名目录（仅改名称，不移动层级）。"""
        new_name = new_name.strip()
        if not new_name or new_name == _UNCATEGORIZED or len(new_name) > _MAX_FOLDER_NAME:
            raise FolderError("目录名称不合法")
        if any(char in new_name for char in _FORBIDDEN_NAME_CHARS):
            raise FolderError("目录名称不能包含 / \\ 或控制字符")
        conn = self._connect()
        try:
            with conn:
                node = self._node(conn, folder_id)
                if node is None:
                    raise FolderNotFoundError("目录不存在")
                dup = conn.execute(
                    "SELECT id FROM folder_nodes WHERE id!=? AND scope=? AND COALESCE(parent_id,0)=? AND name=?",
                    (folder_id, node["scope"], node["parent_id"] or 0, new_name),
                ).fetchone()
                if dup is not None:
                    raise FolderNameConflictError("同级目录下已存在同名目录")
                conn.execute(
                    "UPDATE folder_nodes SET name=?, updated_at=? WHERE id=?",
                    (new_name, time.time(), folder_id),
                )
            return self._node_to_dict(self._node(conn, folder_id))
        finally:
            conn.close()

    def move_folder_node(self, folder_id: int, parent_id: int | None) -> dict:
        """移动目录到新父级（parent_id=None 移回根）。禁止移动到自身或后代。"""
        conn = self._connect()
        try:
            with conn:
                node = self._node(conn, folder_id)
                if node is None:
                    raise FolderNotFoundError("目录不存在")
                if node["parent_id"] == parent_id:
                    return self._node_to_dict(node)
                if parent_id is not None:
                    if parent_id == folder_id:
                        raise FolderError("不能移动目录到其自身")
                    parent = self._node(conn, parent_id)
                    if parent is None:
                        raise FolderNotFoundError("目标父目录不存在")
                    if parent["scope"] != node["scope"]:
                        raise FolderError("目标父目录 scope 不一致")
                    if self._is_descendant(conn, parent_id, folder_id):
                        raise FolderError("不能移动目录到其自身的后代")
                dup = conn.execute(
                    "SELECT id FROM folder_nodes WHERE id!=? AND scope=? AND COALESCE(parent_id,0)=? AND name=?",
                    (folder_id, node["scope"], parent_id or 0, node["name"]),
                ).fetchone()
                if dup is not None:
                    raise FolderNameConflictError("目标位置已存在同名目录")
                conn.execute(
                    "UPDATE folder_nodes SET parent_id=?, updated_at=? WHERE id=?",
                    (parent_id, time.time(), folder_id),
                )
            return self._node_to_dict(self._node(conn, folder_id))
        finally:
            conn.close()

    def delete_folder_node(
        self,
        folder_id: int,
        target_folder_id: int | None = None,
        move_to_root: bool = False,
    ) -> dict:
        """删除目录。

        - 必须指定迁移目标：targetFolderId（同 scope 的目录）或 move_to_root=true（移回根）。
        - 目录自身直接归类的资料迁往目标；直接子目录整体提升到目标下，深层结构保持不变。
        - 删除目录不触碰任何原材料文件。
        """
        conn = self._connect()
        try:
            with conn:
                node = self._node(conn, folder_id)
                if node is None:
                    raise FolderNotFoundError("目录不存在")
                if target_folder_id is None and not move_to_root:
                    raise FolderError("删除目录必须指定迁移目标（targetFolderId 或 moveToRoot）")
                target = None
                if target_folder_id is not None:
                    if target_folder_id == folder_id:
                        raise FolderError("迁移目标不能是目录自身")
                    target = self._node(conn, target_folder_id)
                    if target is None:
                        raise FolderNotFoundError("迁移目标目录不存在")
                    if target["scope"] != node["scope"]:
                        raise FolderError("迁移目标 scope 不一致")
                    if self._is_descendant(conn, target_folder_id, folder_id):
                        raise FolderError("迁移目标不能是目录自身的后代")
                # 预检：直接子目录提升到目标位置时，不得与目标下已有同名节点冲突。
                # 必须在任何资料/目录变更前完成，抛错即回滚，保证原子性。
                anchor = target_folder_id if target_folder_id is not None else 0
                children = conn.execute(
                    "SELECT id, name FROM folder_nodes WHERE parent_id=?", (folder_id,)
                ).fetchall()
                for child in children:
                    dup = conn.execute(
                        "SELECT id FROM folder_nodes "
                        "WHERE id!=? AND scope=? AND COALESCE(parent_id,0)=? AND name=?",
                        (child["id"], node["scope"], anchor, child["name"]),
                    ).fetchone()
                    if dup is not None:
                        raise FolderNameConflictError(
                            f"迁移目标下已存在同名子目录：{child['name']}"
                        )
                # 本目录直接归类的资料迁往目标；子目录（及其内容）整体保留并提升到目标下。
                # 同步维护兼容列 folder（目标目录名 /「未分类」）：否则残留被删目录名会在
                # 重启时被 _migrate_legacy_folders 重新收集，导致已删除目录「复活」并回填资料。
                legacy_folder = target["name"] if target is not None else _UNCATEGORIZED
                moved_materials = conn.execute(
                    "UPDATE job_records SET folder_id=?, folder=? WHERE folder_id=?",
                    (target_folder_id, legacy_folder, folder_id),
                ).rowcount
                reparented = conn.execute(
                    "UPDATE folder_nodes SET parent_id=?, updated_at=? WHERE parent_id=?",
                    (target_folder_id, time.time(), folder_id),
                ).rowcount
                conn.execute("DELETE FROM folder_nodes WHERE id=?", (folder_id,))
                return {"movedMaterials": moved_materials, "reparentedFolders": reparented}
        finally:
            conn.close()

    def update_material_folder_id(self, material_id: str, folder_id: int | None) -> dict | None:
        """移动资料到目录（folder_id=None = 未分类）。返回更新后记录或 None。"""
        conn = self._connect()
        try:
            with conn:
                if folder_id is not None:
                    node = self._node(conn, folder_id)
                    if node is None:
                        raise FolderNotFoundError("目标目录不存在")
                    if node["scope"] != SCOPE_RAW:
                        raise FolderError("原材料只能归入 RAW 目录")
                    legacy_folder = node["name"]
                else:
                    legacy_folder = _UNCATEGORIZED
                cursor = conn.execute(
                    "UPDATE job_records SET folder_id=?, folder=? WHERE material_id=?",
                    (folder_id, legacy_folder, material_id),
                )
                if cursor.rowcount == 0:
                    return None
            return self.get(material_id)
        finally:
            conn.close()

    def list_folder_nodes(self, scope: str | None = None) -> list[dict]:
        """返回目录节点扁平数组（含 materialCount 与 subtreeMaterialCount）。"""
        conn = self._connect()
        try:
            if scope is not None:
                rows = conn.execute(
                    """SELECT f.*,
                              (SELECT COUNT(*) FROM job_records j WHERE j.folder_id = f.id) AS material_count
                       FROM folder_nodes f WHERE f.scope=?
                       ORDER BY f.sort_order, f.name COLLATE NOCASE""",
                    (scope,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT f.*,
                              (SELECT COUNT(*) FROM job_records j WHERE j.folder_id = f.id) AS material_count
                       FROM folder_nodes f
                       ORDER BY f.scope, f.sort_order, f.name COLLATE NOCASE"""
                ).fetchall()
            nodes = [self._node_to_dict(r) for r in rows]
            return _attach_subtree_counts(nodes)
        finally:
            conn.close()

    def folder_descendants(self, folder_id: int) -> set[int]:
        """返回目录自身及其全部后代的 id 集合（用于子树筛选）。"""
        conn = self._connect()
        try:
            if self._node(conn, folder_id) is None:
                return set()
            return self._subtree_ids(conn, folder_id)
        finally:
            conn.close()

    def folder_path(self, folder_id: int | None) -> str:
        """返回目录路径（根到节点的名称以 / 连接）；None/缺失/已删除返回空串。"""
        if folder_id is None:
            return ""
        conn = self._connect()
        try:
            parts: list[str] = []
            cur = folder_id
            seen: set[int] = set()
            while cur is not None and cur not in seen:
                seen.add(cur)
                row = conn.execute(
                    "SELECT id, parent_id, name FROM folder_nodes WHERE id=?", (cur,)
                ).fetchone()
                if row is None:
                    break
                parts.append(row["name"])
                cur = row["parent_id"]
            return "/".join(reversed(parts))
        finally:
            conn.close()

    def folder_node(self, folder_id: int) -> dict | None:
        """返回单节点 dict（含 materialCount）；不存在返回 None。供知识卡片按 ID 校验目录。"""
        conn = self._connect()
        try:
            node = self._node(conn, folder_id)
            return self._node_to_dict(node) if node is not None else None
        finally:
            conn.close()


def _attach_subtree_counts(nodes: list[dict]) -> list[dict]:
    """为每个节点附加 subtreeMaterialCount（自身 + 全部后代直接归类材料数）。"""
    if not nodes:
        return nodes
    children: dict[int | None, list[dict]] = {}
    for n in nodes:
        children.setdefault(n["parentId"], []).append(n)
    # 从根向下递归：先累计子节点，再累加到父节点。
    def _sum(node: dict) -> int:
        total = node["materialCount"]
        for child in children.get(node["id"], []):
            total += _sum(child)
        node["subtreeMaterialCount"] = total
        return total

    for root in children.get(None, []):
        _sum(root)
    # 若存在异常悬空节点（指向上不存在的父级），单独累加。
    for n in nodes:
        if "subtreeMaterialCount" not in n:
            n["subtreeMaterialCount"] = n["materialCount"]
    return nodes


def reset_for_tests(db_path=None) -> JobStore:
    """测试用：切换到独立 DB 并清空全局实例；无参数时恢复默认 JobStore 库路径。"""
    global _INITIALIZED, _DB_PATH, JobStore
    _INITIALIZED = False
    if db_path is None:
        _DB_PATH = _DEFAULT_DB_PATH
    else:
        from pathlib import Path
        _DB_PATH = Path(db_path)
    JobStore._instance = None
    return JobStore.instance()
