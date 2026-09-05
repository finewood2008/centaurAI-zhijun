"""MindOS 治理待办持久化（P11）。

治理候选（重复 / 可能过时 / 待确认关联）与人工仲裁结果统一存放在 SQLite，
与 Chroma 向量集合和 Wiki 存储解耦。仲裁仅改变状态或标记，绝不改写
知识卡片内容、原材料文件或向量库。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from runtime_paths import GOVERNANCE_DB_PATH

# 候选类型
KIND_DUPLICATE = "duplicate"   # 疑似重复
KIND_OUTDATED = "outdated"     # 可能过时
KIND_RELATION = "relation"     # 待确认关联
KIND_CONFLICT = "conflict"     # 观点冲突

# 仲裁状态
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"  # 两阶段提交的中间态：已抢占、实体操作进行中
STATUS_IGNORED = "ignored"
STATUS_MERGED = "merged"
STATUS_ARCHIVED = "archived"

_ALL_STATUSES = {STATUS_PENDING, STATUS_PROCESSING, STATUS_IGNORED, STATUS_MERGED, STATUS_ARCHIVED}
_ALL_KINDS = {KIND_DUPLICATE, KIND_OUTDATED, KIND_RELATION, KIND_CONFLICT}

# processing 租约安全超时：明显大于合并/归档等实体操作的最大预期耗时，
# 避免扫描误回收仍在执行的仲裁。可通过环境变量 MINDOS_GOV_RECOVER_TIMEOUT 覆盖。
RECOVER_TIMEOUT_SECONDS = int(os.environ.get("MINDOS_GOV_RECOVER_TIMEOUT", "300"))

_LOCK = threading.RLock()
_INITIALIZED = False
_DB_PATH = GOVERNANCE_DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GovernanceStore:
    """SQLite-backed governance item store."""

    def __init__(self, db_path):
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        self._ensure()
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure(self) -> None:
        global _INITIALIZED
        with _LOCK:
            if _INITIALIZED:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), timeout=30)
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS governance_items (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        title TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        snippet TEXT NOT NULL DEFAULT '',
                        source_knowledge_id TEXT NOT NULL DEFAULT '',
                        target_knowledge_id TEXT NOT NULL DEFAULT '',
                        material_id TEXT NOT NULL DEFAULT '',
                        score REAL NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'pending',
                        note TEXT NOT NULL DEFAULT '',
                        fingerprint TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        resolved_at TEXT,
                        processing_started_at TEXT,
                        claim_token TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_gov_status ON governance_items(status, kind);
                    """
                )
                # 旧库迁移：补充租约与 claim token 字段，并把无租约的遗留 processing 恢复为 pending。
                with conn:
                    cols = [r[1] for r in conn.execute("PRAGMA table_info(governance_items)")]
                    if "processing_started_at" not in cols:
                        conn.execute("ALTER TABLE governance_items ADD COLUMN processing_started_at TEXT")
                    if "claim_token" not in cols:
                        conn.execute("ALTER TABLE governance_items ADD COLUMN claim_token TEXT")
                    # 早期版本在 fingerprint 上使用了表级 UNIQUE，导致已处理的候选也会
                    # 永久阻止同一问题再次进入待办。重建表以移除该约束，并改为仅限制
                    # pending / processing 中的活动候选；历史仲裁记录则保留供审计。
                    table_sql_row = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name='governance_items'"
                    ).fetchone()
                    table_sql = " ".join((table_sql_row[0] or "").lower().split()) if table_sql_row else ""
                    if "fingerprint text not null unique" in table_sql:
                        conn.executescript(
                            """
                            ALTER TABLE governance_items RENAME TO governance_items_legacy;
                            CREATE TABLE governance_items (
                                id TEXT PRIMARY KEY,
                                kind TEXT NOT NULL,
                                title TEXT NOT NULL,
                                reason TEXT NOT NULL,
                                snippet TEXT NOT NULL DEFAULT '',
                                source_knowledge_id TEXT NOT NULL DEFAULT '',
                                target_knowledge_id TEXT NOT NULL DEFAULT '',
                                material_id TEXT NOT NULL DEFAULT '',
                                score REAL NOT NULL DEFAULT 0,
                                status TEXT NOT NULL DEFAULT 'pending',
                                note TEXT NOT NULL DEFAULT '',
                                fingerprint TEXT NOT NULL,
                                created_at TEXT NOT NULL,
                                resolved_at TEXT,
                                processing_started_at TEXT,
                                claim_token TEXT
                            );
                            INSERT INTO governance_items
                                (id, kind, title, reason, snippet, source_knowledge_id,
                                 target_knowledge_id, material_id, score, status, note,
                                 fingerprint, created_at, resolved_at, processing_started_at, claim_token)
                            SELECT id, kind, title, reason, snippet, source_knowledge_id,
                                   target_knowledge_id, material_id, score, status, note,
                                   fingerprint, created_at, resolved_at, processing_started_at, claim_token
                            FROM governance_items_legacy;
                            DROP TABLE governance_items_legacy;
                            """
                        )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_gov_status ON governance_items(status, kind)"
                    )
                    conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_gov_active_fingerprint "
                        "ON governance_items(fingerprint) WHERE status IN ('pending', 'processing')"
                    )
                    conn.execute(
                        "UPDATE governance_items SET status='pending', resolved_at=NULL, claim_token=NULL "
                        "WHERE status='processing' AND processing_started_at IS NULL"
                    )
            finally:
                conn.close()
            _INITIALIZED = True

    def _row(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "title": row["title"],
            "reason": row["reason"],
            "snippet": row["snippet"],
            "sourceKnowledgeId": row["source_knowledge_id"] or None,
            "targetKnowledgeId": row["target_knowledge_id"] or None,
            "materialId": row["material_id"] or None,
            "score": row["score"],
            "status": row["status"],
            "note": row["note"],
            "createdAt": row["created_at"],
            "resolvedAt": row["resolved_at"] or None,
        }

    def list(self, status: str | None = None, kind: str | None = None, limit: int = 500) -> list[dict]:
        clauses = []
        params: list = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM governance_items {where} ORDER BY created_at DESC, score DESC LIMIT ?",
                (*params, min(limit, 1000)),
            ).fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    def get(self, item_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM governance_items WHERE id=?", (item_id,)).fetchone()
            return self._row(row) if row else None
        finally:
            conn.close()

    def create(self, items: list[dict]) -> int:
        """插入新候选；同 fingerprint 的活动项（pending / processing）忽略（幂等）。

        已忽略、已合并和已归档项保留为历史审计记录，不阻止后续扫描重新提审。
        """
        if not items:
            return 0
        conn = self._connect()
        created = 0
        try:
            with conn:
                for item in items:
                    fingerprint = item["fingerprint"]
                    cursor = conn.execute(
                        """INSERT OR IGNORE INTO governance_items
                           (id, kind, title, reason, snippet, source_knowledge_id,
                            target_knowledge_id, material_id, score, status, note,
                            fingerprint, created_at, resolved_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL)""",
                        (
                            str(uuid.uuid4().hex[:16]),
                            item["kind"],
                            item["title"],
                            item["reason"],
                            item.get("snippet", ""),
                            item.get("source_knowledge_id", ""),
                            item.get("target_knowledge_id", ""),
                            item.get("material_id", ""),
                            float(item.get("score", 0.0)),
                            item.get("note", ""),
                            fingerprint,
                            _now(),
                        ),
                    )
                    created += cursor.rowcount
        finally:
            conn.close()
        return created

    def resolve(self, item_id: str, action: str, note: str = "", from_status: str | None = STATUS_PENDING,
                claim_token: str | None = None, force: bool = False) -> dict | None:
        """更新仲裁状态（ignore/merge/archive 由调用方先执行实际语义操作）。

        - 抢占：action='processing' 且带新 claim_token，仅 pending 可转；rowcount==0 返回 None。
        - 完成 / 回滚：action 为最终状态或 pending，须带原 claim_token，仅 status='processing' 且 token 匹配才生效，
          避免旧请求的完成/回滚覆盖新一次抢占。
        - force=True：无条件更新（测试 / 特殊用途），清空租约与 token。
        """
        if action not in _ALL_STATUSES:
            raise ValueError(f"不支持的仲裁动作: {action}")
        now = _now()
        conn = self._connect()
        try:
            with conn:
                if force:
                    cursor = conn.execute(
                        "UPDATE governance_items SET status=?, note=?, resolved_at=?, processing_started_at=NULL, claim_token=NULL WHERE id=?",
                        (action, note, now, item_id),
                    )
                elif action == STATUS_PROCESSING:
                    cursor = conn.execute(
                        "UPDATE governance_items SET status=?, note=?, resolved_at=?, processing_started_at=?, claim_token=? WHERE id=? AND status=?",
                        (action, note, now, now, claim_token or "", item_id, from_status),
                    )
                elif claim_token:
                    cursor = conn.execute(
                        "UPDATE governance_items SET status=?, note=?, resolved_at=?, processing_started_at=NULL, claim_token=NULL "
                        "WHERE id=? AND status='processing' AND claim_token=?",
                        (action, note, now, item_id, claim_token),
                    )
                else:
                    cursor = conn.execute(
                        "UPDATE governance_items SET status=?, note=?, resolved_at=?, processing_started_at=NULL WHERE id=? AND status=?",
                        (action, note, now, item_id, from_status),
                    )
                if cursor.rowcount == 0:
                    return None
            row = conn.execute("SELECT * FROM governance_items WHERE id=?", (item_id,)).fetchone()
            return self._row(row) if row else None
        finally:
            conn.close()

    def recover_processing(self, timeout_seconds: int | None = None) -> int:
        """把超过安全超时仍停留在 processing 的中间态恢复为 pending。

        仅清理租约过期（进程崩溃遗留）的记录，不触碰仍在执行的仲裁，并清除对应 claim token。
        timeout_seconds 默认使用模块级 RECOVER_TIMEOUT_SECONDS（可配置）。
        """
        from datetime import timedelta
        timeout = RECOVER_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout)).isoformat()
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE governance_items SET status='pending', note='', resolved_at=NULL, processing_started_at=NULL, claim_token=NULL "
                    "WHERE status='processing' AND processing_started_at IS NOT NULL AND processing_started_at < ?",
                    (cutoff,),
                )
                return cursor.rowcount
        finally:
            conn.close()

    def current_claim(self, item_id: str) -> str | None:
        """返回记录当前的 claim token（实体操作前的二次校验用）。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT claim_token FROM governance_items WHERE id=?", (item_id,)
            ).fetchone()
            return row["claim_token"] if row else None
        finally:
            conn.close()

    # ---- P15-05：永久清除时清理治理待办（先处理治理再删实体，避免孤儿待办） ----

    def purge_material_items(self, material_id: str) -> int:
        """删除全部关联某原材料的治理待办（含历史仲裁记录），返回受影响行数。"""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM governance_items WHERE material_id=?", (material_id,)
                )
                return cursor.rowcount
        finally:
            conn.close()

    def purge_knowledge_items(self, knowledge_id: str) -> int:
        """删除全部关联某知识卡片的治理待办（作为 source 或 target），返回受影响行数。"""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM governance_items "
                    "WHERE source_knowledge_id=? OR target_knowledge_id=?",
                    (knowledge_id, knowledge_id),
                )
                return cursor.rowcount
        finally:
            conn.close()


_instance: GovernanceStore | None = None


def instance() -> GovernanceStore:
    global _instance
    if _instance is None:
        _instance = GovernanceStore(_DB_PATH)
    return _instance


def reset_for_tests(db_path) -> GovernanceStore:
    """测试用：切换到独立 DB 并清空全局实例。"""
    global _instance, _INITIALIZED, _DB_PATH
    _INITIALIZED = False
    _DB_PATH = db_path
    _instance = None
    return instance()
