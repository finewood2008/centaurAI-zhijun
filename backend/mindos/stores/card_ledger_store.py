"""知识卡片状态账本：卡片事实文件之外的版本、可见性和向量同步状态。"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
import json
import hashlib
from pathlib import Path

from runtime_paths import CARD_LEDGER_DB_PATH
from ..device_context import SCOPE_GLOBAL

_LOCK = threading.RLock()
_PATH = CARD_LEDGER_DB_PATH
_READY = False


def _connect() -> sqlite3.Connection:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _init() -> None:
    global _READY
    with _LOCK:
        if _READY:
            return
        with _connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS card_state (
              knowledge_id TEXT PRIMARY KEY,
              rel_path TEXT NOT NULL UNIQUE,
              mutation_version INTEGER NOT NULL,
              content_revision TEXT NOT NULL,
              active_vector_version INTEGER,
              desired_vector_version INTEGER NOT NULL,
              visibility TEXT NOT NULL,
              vector_sync_state TEXT NOT NULL,
              approval_state TEXT NOT NULL DEFAULT 'draft',
              current_revision TEXT,
              indexed_revision TEXT,
              indexed_vector_version INTEGER,
              index_state TEXT NOT NULL DEFAULT 'none',
              index_error_code TEXT,
              folder_id INTEGER,
              metadata_revision INTEGER NOT NULL DEFAULT 0,
              device_scope TEXT NOT NULL DEFAULT 'global',
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS card_tombstones (
              knowledge_id TEXT NOT NULL,
              max_hidden_vector_version INTEGER NOT NULL,
              reason TEXT NOT NULL,
              created_at REAL NOT NULL,
              PRIMARY KEY (knowledge_id, max_hidden_vector_version)
            );
            CREATE TABLE IF NOT EXISTS card_purge_jobs (
              purge_id TEXT PRIMARY KEY,
              knowledge_id TEXT NOT NULL,
              rel_path TEXT NOT NULL,
              state TEXT NOT NULL,
              dependency_snapshot_json TEXT NOT NULL DEFAULT '{}',
              error_code TEXT,
              error_detail TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS card_vector_jobs (
              job_id TEXT PRIMARY KEY,
              knowledge_id TEXT NOT NULL,
              vector_version INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              state TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              lease_until REAL,
              error_detail TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              UNIQUE(knowledge_id, vector_version)
            );
            CREATE TABLE IF NOT EXISTS card_confirmation_sessions (
              session_id TEXT PRIMARY KEY,
              material_id TEXT,
              knowledge_id TEXT NOT NULL,
              target_revision TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              state TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              lease_until REAL,
              error_code TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              UNIQUE(knowledge_id, target_revision, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS card_edit_drafts (
              knowledge_id TEXT PRIMARY KEY,
              base_revision TEXT NOT NULL,
              draft_revision TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS card_pending_updates (
              knowledge_id TEXT PRIMARY KEY,
              base_revision TEXT NOT NULL,
              target_revision TEXT NOT NULL,
              vector_version INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              state TEXT NOT NULL,
              phase TEXT NOT NULL DEFAULT 'prepared',
              payload_hash TEXT,
              expected_chunk_count INTEGER,
              file_content_hash TEXT,
              owner_id TEXT,
              fencing_token INTEGER NOT NULL DEFAULT 0,
              error_code TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS card_vector_manifests (
              knowledge_id TEXT NOT NULL,
              vector_version INTEGER NOT NULL,
              content_revision TEXT NOT NULL,
              expected_chunk_count INTEGER NOT NULL,
              chunk_ids_hash TEXT NOT NULL,
              body_hash TEXT,
              embedding_model_id TEXT,
              embedding_dimension INTEGER,
              routing_epoch INTEGER,
              state TEXT NOT NULL DEFAULT 'verified',
              verified_at REAL NOT NULL,
              PRIMARY KEY (knowledge_id, vector_version)
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version INTEGER PRIMARY KEY,
              applied_at REAL NOT NULL
            );
            """)
            # Schema-only evolution: deliberately do not backfill or infer approval/index facts
            # from existing files or vectors. Pre-existing rows therefore remain fail-closed.
            for column, definition in (
                ("approval_state", "TEXT NOT NULL DEFAULT 'draft'"),
                ("current_revision", "TEXT"),
                ("indexed_revision", "TEXT"),
                ("indexed_vector_version", "INTEGER"),
                ("index_state", "TEXT NOT NULL DEFAULT 'none'"),
                ("index_error_code", "TEXT"),
                ("folder_id", "INTEGER"),
                ("metadata_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("device_scope", "TEXT NOT NULL DEFAULT 'global'"),
            ):
                _ensure_column(conn, "card_state", column, definition)
            _ensure_column(conn, "card_confirmation_sessions", "material_id", "TEXT")
            for column, definition in (
                ("phase", "TEXT NOT NULL DEFAULT 'prepared'"),
                ("payload_hash", "TEXT"),
                ("expected_chunk_count", "INTEGER"),
                ("file_content_hash", "TEXT"),
                ("owner_id", "TEXT"),
                ("fencing_token", "INTEGER NOT NULL DEFAULT 0"),
            ):
                _ensure_column(conn, "card_pending_updates", column, definition)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (2, time.time()),
            )
            conn.execute("PRAGMA user_version=2")
        _READY = True


def health_check() -> dict:
    """Validate the durable ledger before any vector worker starts."""
    _init()
    with _connect() as conn:
        row = conn.execute("PRAGMA quick_check").fetchone()
        result = str(row[0] if row else "unknown")
        return {"ok": result == "ok", "result": result, "schemaVersion": int(conn.execute("PRAGMA user_version").fetchone()[0])}


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def get(knowledge_id: str, *, device_scope: str = SCOPE_GLOBAL) -> dict | None:
    """按 device_scope 读取卡片账本；跨设备/账号不可见（默认只读全局作用域）。"""
    _init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM card_state WHERE knowledge_id=? AND device_scope=?",
            (knowledge_id, device_scope),
        ).fetchone()
        return _row(row)


def get_many(knowledge_ids: list[str] | set[str], *, device_scope: str = SCOPE_GLOBAL) -> dict[str, dict]:
    """批量读取卡片台账，供材料/知识列表投影避免 N+1 SQLite 查询。"""
    _init()
    ids = sorted({str(knowledge_id) for knowledge_id in knowledge_ids if str(knowledge_id)})
    if not ids:
        return {}
    result: dict[str, dict] = {}
    for start in range(0, len(ids), 900):
        batch = ids[start:start + 900]
        placeholders = ",".join("?" for _ in batch)
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM card_state WHERE device_scope=? "
                f"AND knowledge_id IN ({placeholders})",
                [device_scope, *batch],
            ).fetchall()
        result.update({str(row["knowledge_id"]): dict(row) for row in rows})
    return result


def is_rag_eligible(
    state: dict | None, revision: str | None = None, vector_version: int | None = None,
) -> bool:
    """唯一的卡片 RAG 准入判断。账本缺失或任一状态不匹配一律拒绝。"""
    if not state:
        return False
    current = str(state.get("current_revision") or "")
    indexed = str(state.get("indexed_revision") or "")
    return bool(
        state.get("visibility") == "active"
        and state.get("approval_state") == "confirmed"
        and state.get("index_state") == "indexed"
        and current
        and current == indexed
        and (revision is None or revision == indexed)
        and (vector_version is None or int(state.get("indexed_vector_version") or -1) == vector_version)
    )


def can_index(state: dict | None, target_revision: str) -> bool:
    """索引消费者闸门：仅确认中的当前 revision 可写入向量。"""
    return bool(
        state
        and state.get("visibility") == "active"
        and state.get("approval_state") == "confirmed"
        and str(state.get("current_revision") or "") == target_revision
        and state.get("index_state") == "indexing"
    )


def edit_as_draft(knowledge_id: str, expected_revision: str) -> dict:
    """撤销已确认 revision 的准入，并记录旧向量 tombstone。"""
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if row is None or row["approval_state"] != "confirmed":
            conn.rollback()
            raise ConfirmationConflict("card is not confirmed")
        if str(row["current_revision"] or "") != expected_revision:
            conn.rollback()
            raise ConfirmationConflict("card revision conflict")
        if row["active_vector_version"] is not None:
            conn.execute("INSERT OR IGNORE INTO card_tombstones VALUES(?,?,?,?)",
                         (knowledge_id, int(row["active_vector_version"]), "edit_as_draft", now))
        conn.execute("""UPDATE card_state SET approval_state='draft', index_state='none',
            indexed_revision=NULL, indexed_vector_version=NULL, active_vector_version=NULL,
            vector_sync_state='pending', mutation_version=mutation_version+1, updated_at=? WHERE knowledge_id=?""",
                     (now, knowledge_id))
        conn.commit()
    return get(knowledge_id) or {}


def update_draft_revision(knowledge_id: str, rel_path: str, revision: str) -> None:
    """草稿正文保存后更新台账内容 revision；未确认状态绝不创建 outbox。"""
    _init()
    with _connect() as conn:
        conn.execute("""UPDATE card_state SET rel_path=?, content_revision=?, current_revision=?,
            mutation_version=mutation_version+1, updated_at=? WHERE knowledge_id=? AND approval_state='draft'""",
                     (rel_path, revision, revision, time.time(), knowledge_id))


def mark_needs_reconfirmation(knowledge_id: str, reason: str = "external_content_changed") -> dict | None:
    """Fail closed when a confirmed Wiki file changes outside the draft flow."""
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if row is None or row["approval_state"] != "confirmed":
            conn.rollback()
            return _row(row)
        if row["active_vector_version"] is not None:
            conn.execute("INSERT OR IGNORE INTO card_tombstones VALUES(?,?,?,?)",
                         (knowledge_id, int(row["active_vector_version"]), reason, now))
        conn.execute("""UPDATE card_state SET approval_state='draft', index_state='none',
            indexed_revision=NULL, indexed_vector_version=NULL, active_vector_version=NULL,
            vector_sync_state='pending', index_error_code=?, mutation_version=mutation_version+1,
            updated_at=? WHERE knowledge_id=?""", (reason, now, knowledge_id))
        conn.commit()
    return get(knowledge_id)


def retry_index(knowledge_id: str, expected_revision: str, payload: dict) -> dict:
    """仅允许当前已确认 revision 手动重试，复用同一 vector_version 唯一任务。"""
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if state is None or state["approval_state"] != "confirmed":
            conn.rollback()
            raise ConfirmationConflict("card is not confirmed")
        if str(state["current_revision"] or "") != expected_revision:
            conn.rollback()
            raise ConfirmationConflict("card revision conflict")
        version = int(state["desired_vector_version"] or 1)
        job_payload = {**payload, "target_revision": expected_revision, "vector_version": version,
                       "rel_path": state["rel_path"]}
        job_id = uuid.uuid4().hex
        conn.execute("""INSERT INTO card_vector_jobs
            (job_id,knowledge_id,vector_version,payload_json,state,attempts,lease_until,error_detail,created_at,updated_at)
            VALUES(?,?,?,?, 'queued',0,NULL,NULL,?,?)
            ON CONFLICT(knowledge_id,vector_version) DO UPDATE SET payload_json=excluded.payload_json,
              state='queued', lease_until=NULL, error_detail=NULL, updated_at=excluded.updated_at""",
                     (job_id, knowledge_id, version, json.dumps(job_payload, ensure_ascii=False), now, now))
        conn.execute("""UPDATE card_state SET index_state='indexing', index_error_code=NULL,
            vector_sync_state='pending', updated_at=? WHERE knowledge_id=?""", (now, knowledge_id))
        job = conn.execute("SELECT * FROM card_vector_jobs WHERE knowledge_id=? AND vector_version=?", (knowledge_id, version)).fetchone()
        conn.commit()
    return _row(job) or {}


class ConfirmationConflict(RuntimeError):
    pass


def _draft_revision(payload: dict) -> str:
    """Stable revision for an unconfirmed edit payload, used as a CAS token."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "edit_" + __import__("hashlib").sha256(encoded.encode("utf-8")).hexdigest()[:16]


def get_edit_draft(knowledge_id: str) -> dict | None:
    _init()
    with _connect() as conn:
        return _row(conn.execute("SELECT * FROM card_edit_drafts WHERE knowledge_id=?", (knowledge_id,)).fetchone())


def discard_edit_draft(knowledge_id: str) -> bool:
    _init()
    with _LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM card_edit_drafts WHERE knowledge_id=?", (knowledge_id,))
        return cur.rowcount == 1


def restore_edit_draft(row: dict) -> None:
    _init()
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO card_edit_drafts
               (knowledge_id,base_revision,draft_revision,payload_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (row["knowledge_id"], row["base_revision"], row["draft_revision"], row["payload_json"],
             row["created_at"], row["updated_at"]),
        )


def begin_edit_draft(knowledge_id: str, expected_revision: str, payload: dict) -> dict:
    """Create a durable working copy without changing the confirmed card state."""
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if state is None or state["approval_state"] != "confirmed" or state["visibility"] != "active":
            conn.rollback()
            raise ConfirmationConflict("card is not an active confirmed card")
        if str(state["current_revision"] or "") != expected_revision:
            conn.rollback()
            raise ConfirmationConflict("card revision conflict")
        existing = conn.execute("SELECT * FROM card_edit_drafts WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if existing is None:
            revision = _draft_revision(payload)
            conn.execute("""INSERT INTO card_edit_drafts
                (knowledge_id,base_revision,draft_revision,payload_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?)""", (knowledge_id, expected_revision, revision,
                                             json.dumps(payload, ensure_ascii=False), now, now))
            existing = conn.execute("SELECT * FROM card_edit_drafts WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        elif str(existing["base_revision"]) != expected_revision:
            conn.rollback()
            raise ConfirmationConflict("working draft is based on an older card revision")
        conn.commit()
        return _row(existing) or {}


def save_edit_draft(knowledge_id: str, expected_draft_revision: str, payload: dict) -> dict:
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM card_edit_drafts WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if row is None or str(row["draft_revision"]) != expected_draft_revision:
            conn.rollback()
            raise ConfirmationConflict("working draft revision conflict")
        revision = _draft_revision(payload)
        conn.execute("""UPDATE card_edit_drafts SET draft_revision=?, payload_json=?, updated_at=?
                        WHERE knowledge_id=?""", (revision, json.dumps(payload, ensure_ascii=False), now, knowledge_id))
        saved = conn.execute("SELECT * FROM card_edit_drafts WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        conn.commit()
        return _row(saved) or {}


def begin_pending_update(knowledge_id: str, expected_draft_revision: str, target_revision: str, payload: dict) -> dict:
    """Queue a new vector version while preserving the old confirmed version."""
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        draft = conn.execute("SELECT * FROM card_edit_drafts WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        state = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if draft is None or str(draft["draft_revision"]) != expected_draft_revision:
            conn.rollback()
            raise ConfirmationConflict("working draft revision conflict")
        if state is None or state["approval_state"] != "confirmed" or state["visibility"] != "active" or str(state["current_revision"] or "") != str(draft["base_revision"]):
            conn.rollback()
            raise ConfirmationConflict("confirmed card changed; create a new working draft")
        pending = conn.execute("SELECT * FROM card_pending_updates WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if pending is not None:
            conn.commit()
            return {"pending": _row(pending), "job": None, "idempotent": True}
        version = int(state["desired_vector_version"] or 0) + 1
        job_payload = {**payload, "target_revision": target_revision, "vector_version": version,
                       "pending_edit_update": True}
        encoded_payload = json.dumps(job_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(encoded_payload.encode("utf-8")).hexdigest()
        conn.execute("""INSERT INTO card_pending_updates
            (knowledge_id,base_revision,target_revision,vector_version,payload_json,state,phase,payload_hash,
             error_code,created_at,updated_at)
            VALUES(?,?,?,?,?,'indexing','prepared',?,NULL,?,?)""",
            (knowledge_id, draft["base_revision"], target_revision, version,
             json.dumps(job_payload, ensure_ascii=False), payload_hash, now, now))
        conn.execute("UPDATE card_state SET desired_vector_version=?, updated_at=? WHERE knowledge_id=?",
                     (version, now, knowledge_id))
        job_id = uuid.uuid4().hex
        conn.execute("""INSERT INTO card_vector_jobs
            (job_id,knowledge_id,vector_version,payload_json,state,attempts,created_at,updated_at)
            VALUES(?,?,?,?, 'queued',0,?,?)""",
            (job_id, knowledge_id, version, json.dumps(job_payload, ensure_ascii=False), now, now))
        pending = conn.execute("SELECT * FROM card_pending_updates WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        job = conn.execute("SELECT * FROM card_vector_jobs WHERE job_id=?", (job_id,)).fetchone()
        conn.commit()
        return {"pending": _row(pending), "job": _row(job), "idempotent": False}


def pending_update_can_index(knowledge_id: str, target_revision: str, vector_version: int) -> bool:
    _init()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM card_pending_updates WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        return bool(row and row["state"] in {"indexing", "recovering"} and str(row["target_revision"]) == target_revision
                    and int(row["vector_version"]) == int(vector_version))


def get_pending_update(knowledge_id: str) -> dict | None:
    _init()
    with _connect() as conn:
        return _row(conn.execute("SELECT * FROM card_pending_updates WHERE knowledge_id=?", (knowledge_id,)).fetchone())


def fail_pending_update(knowledge_id: str, error_code: str) -> None:
    _init()
    with _connect() as conn:
        conn.execute("""UPDATE card_pending_updates SET state='index_failed', phase='index_failed',
                        owner_id=NULL, error_code=?, updated_at=?
                        WHERE knowledge_id=? AND state IN ('indexing','recovering')""",
                     (str(error_code or "index_failed")[:120], time.time(), knowledge_id))


def mark_pending_vector_written(
    knowledge_id: str, target_revision: str, vector_version: int, expected_chunk_count: int,
) -> bool:
    _init()
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE card_pending_updates SET phase='vector_written', expected_chunk_count=?, updated_at=?
               WHERE knowledge_id=? AND target_revision=? AND vector_version=?
               AND state IN ('indexing','recovering')""",
            (expected_chunk_count, time.time(), knowledge_id, target_revision, vector_version),
        )
        return cur.rowcount == 1


def mark_pending_file_committed(
    knowledge_id: str, target_revision: str, vector_version: int, file_content_hash: str,
) -> bool:
    _init()
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE card_pending_updates SET phase='file_committed', file_content_hash=?, updated_at=?
               WHERE knowledge_id=? AND target_revision=? AND vector_version=?
               AND state IN ('indexing','recovering') AND phase IN ('prepared','vector_written','file_committed')""",
            (file_content_hash, time.time(), knowledge_id, target_revision, vector_version),
        )
        return cur.rowcount == 1


def retry_pending_update(knowledge_id: str) -> dict:
    """Retry only the unpublished update; the current searchable version is untouched."""
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute("SELECT * FROM card_pending_updates WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if pending is None or pending["state"] != "index_failed":
            conn.rollback()
            raise ConfirmationConflict("no failed pending update")
        job_id = uuid.uuid4().hex
        conn.execute("""INSERT INTO card_vector_jobs
            (job_id,knowledge_id,vector_version,payload_json,state,attempts,lease_until,error_detail,created_at,updated_at)
            VALUES(?,?,?,?, 'queued',0,NULL,NULL,?,?)
            ON CONFLICT(knowledge_id,vector_version) DO UPDATE SET job_id=excluded.job_id, payload_json=excluded.payload_json,
                state='queued', attempts=0, lease_until=NULL, error_detail=NULL, updated_at=excluded.updated_at""",
            (job_id, knowledge_id, pending["vector_version"], pending["payload_json"], now, now))
        conn.execute("""UPDATE card_pending_updates SET state='indexing', phase='prepared', owner_id=NULL,
                        fencing_token=fencing_token+1, error_code=NULL, updated_at=? WHERE knowledge_id=?""",
                     (now, knowledge_id))
        job = conn.execute("SELECT * FROM card_vector_jobs WHERE knowledge_id=? AND vector_version=?",
                           (knowledge_id, pending["vector_version"])).fetchone()
        conn.commit()
        return _row(job) or {}


def activate_pending_update(knowledge_id: str, target_revision: str, vector_version: int) -> bool:
    """Atomically make a pre-written version current and retire the former vector."""
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        pending = conn.execute("SELECT * FROM card_pending_updates WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if (row is None or pending is None or pending["state"] not in {"indexing", "recovering"}
                or str(pending["target_revision"]) != target_revision
                or int(pending["vector_version"]) != int(vector_version)
                or str(pending["phase"] or "") != "file_committed"):
            conn.rollback()
            return False
        old = row["active_vector_version"]
        if old is not None and int(old) != int(vector_version):
            conn.execute("INSERT OR IGNORE INTO card_tombstones VALUES(?,?,?,?)",
                         (knowledge_id, int(old), "confirmed_update", now))
        conn.execute("""UPDATE card_state SET content_revision=?, current_revision=?, active_vector_version=?,
            indexed_revision=?, indexed_vector_version=?, vector_sync_state='clean', index_state='indexed',
            index_error_code=NULL, mutation_version=mutation_version+1, updated_at=? WHERE knowledge_id=?""",
            (target_revision, target_revision, vector_version, target_revision, vector_version, now, knowledge_id))
        conn.execute("DELETE FROM card_pending_updates WHERE knowledge_id=?", (knowledge_id,))
        conn.execute("DELETE FROM card_edit_drafts WHERE knowledge_id=?", (knowledge_id,))
        conn.commit()
    return True


def begin_material_confirmation(material_id: str, target_revision: str, idempotency_key: str, payload: dict) -> dict:
    """持久化材料草稿确认会话；相同 key 的重试返回同一会话。"""
    _init()
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    now = time.time()
    with _LOCK, _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM card_confirmation_sessions WHERE material_id=? AND target_revision=? AND idempotency_key=?",
            (material_id, target_revision, idempotency_key),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        active = conn.execute(
            "SELECT * FROM card_confirmation_sessions WHERE material_id=? AND target_revision=? AND state IN ('preparing','file_committed','ledger_committed')",
            (material_id, target_revision),
        ).fetchone()
        if active is not None:
            raise ConfirmationConflict("confirmation already in progress")
        session_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO card_confirmation_sessions
               (session_id,material_id,knowledge_id,target_revision,idempotency_key,state,payload_json,lease_until,created_at,updated_at)
               VALUES(?,?, '', ?, ?, 'preparing', ?, ?, ?, ?)""",
            (session_id, material_id, target_revision, idempotency_key,
             json.dumps(payload, ensure_ascii=False), now + 300, now, now),
        )
        return dict(conn.execute("SELECT * FROM card_confirmation_sessions WHERE session_id=?", (session_id,)).fetchone())


def begin_card_confirmation(knowledge_id: str, target_revision: str, idempotency_key: str, payload: dict) -> dict:
    """Start the durable confirmation saga for an existing Wiki draft."""
    _init()
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    now = time.time()
    with _LOCK, _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM card_confirmation_sessions WHERE knowledge_id=? AND target_revision=? AND idempotency_key=?",
            (knowledge_id, target_revision, idempotency_key),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        active = conn.execute(
            "SELECT 1 FROM card_confirmation_sessions WHERE knowledge_id=? AND target_revision=? "
            "AND state IN ('preparing','file_committed','ledger_committed')",
            (knowledge_id, target_revision),
        ).fetchone()
        if active is not None:
            raise ConfirmationConflict("confirmation already in progress")
        state = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if state is not None and state["approval_state"] == "confirmed" and state["current_revision"] == target_revision:
            raise ConfirmationConflict("revision already confirmed")
        session_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO card_confirmation_sessions
               (session_id,material_id,knowledge_id,target_revision,idempotency_key,state,payload_json,lease_until,created_at,updated_at)
               VALUES(?,NULL,?,?,?,'preparing',?,?,?,?)""",
            (session_id, knowledge_id, target_revision, idempotency_key,
             json.dumps(payload, ensure_ascii=False), now + 300, now, now),
        )
        return dict(conn.execute("SELECT * FROM card_confirmation_sessions WHERE session_id=?", (session_id,)).fetchone())


def finalize_material_confirmation(session_id: str, knowledge_id: str, rel_path: str, draft_revision: str, payload: dict) -> dict:
    """文件已原子提交后，原子写确认状态和 outbox。"""
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute("SELECT * FROM card_confirmation_sessions WHERE session_id=?", (session_id,)).fetchone()
        if session is None:
            conn.rollback()
            raise ConfirmationConflict("confirmation session not found")
        if session["state"] == "ledger_committed":
            session_payload = json.loads(session["payload_json"] or "{}")
            job = conn.execute(
                "SELECT * FROM card_vector_jobs WHERE knowledge_id=? AND vector_version=?",
                (knowledge_id, session_payload.get("vector_version")),
            ).fetchone()
            conn.commit()
            return {"session": dict(session), "job": _row(job), "idempotent": True}
        if session["target_revision"] != draft_revision:
            conn.rollback()
            raise ConfirmationConflict("confirmation revision mismatch")
        card_revision = str(payload.get("content_revision") or "")
        if not card_revision:
            conn.rollback()
            raise ConfirmationConflict("confirmed card revision is required")
        existing = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        reuse_confirming_version = bool(
            existing is not None
            and existing["approval_state"] == "confirming"
            and str(existing["current_revision"] or "") == card_revision
        )
        desired = (
            int(existing["desired_vector_version"] or 1)
            if reuse_confirming_version else 1 if existing is None else int(existing["desired_vector_version"] or 0) + 1
        )
        if existing is None:
            conn.execute(
                """INSERT INTO card_state
                   (knowledge_id,rel_path,mutation_version,content_revision,active_vector_version,desired_vector_version,
                    visibility,vector_sync_state,approval_state,current_revision,index_state,updated_at)
                   VALUES(?,?,1,?,NULL,?,'active','pending','confirmed',?,'indexing',?)""",
                (knowledge_id, rel_path, card_revision, desired, card_revision, now),
            )
        else:
            conn.execute("""UPDATE card_state SET rel_path=?, content_revision=?, desired_vector_version=?,
                approval_state='confirmed', current_revision=?, indexed_revision=NULL, indexed_vector_version=NULL,
                index_state='indexing', index_error_code=NULL, vector_sync_state='pending', updated_at=? WHERE knowledge_id=?""",
                (rel_path, card_revision, desired, card_revision, now, knowledge_id))
        job_payload = {**payload, "target_revision": card_revision, "rel_path": rel_path, "vector_version": desired}
        job_id = uuid.uuid4().hex
        conn.execute("""INSERT INTO card_vector_jobs
            (job_id,knowledge_id,vector_version,payload_json,state,attempts,created_at,updated_at)
            VALUES(?,?,?,?, 'queued',0,?,?) ON CONFLICT(knowledge_id,vector_version) DO NOTHING""",
            (job_id, knowledge_id, desired, json.dumps(job_payload, ensure_ascii=False), now, now))
        conn.execute("""UPDATE card_confirmation_sessions SET knowledge_id=?, state='ledger_committed',
            payload_json=?, lease_until=NULL, updated_at=? WHERE session_id=?""",
            (knowledge_id, json.dumps(job_payload, ensure_ascii=False), now, session_id))
        job = conn.execute("SELECT * FROM card_vector_jobs WHERE knowledge_id=? AND vector_version=?", (knowledge_id, desired)).fetchone()
        conn.commit()
        return {"session": {"session_id": session_id, "state": "ledger_committed"}, "job": _row(job), "idempotent": False}


def mark_confirmation_file_committed(session_id: str, knowledge_id: str, payload: dict) -> None:
    """记录已原子发布的文件，并持久化卡片的 confirming 中间态。"""
    _init()
    revision = str(payload.get("content_revision") or "")
    rel_path = str(payload.get("rel_path") or "")
    if not revision or not rel_path:
        raise ConfirmationConflict("confirmation file payload is incomplete")
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if existing is None:
            conn.execute("""INSERT INTO card_state
                (knowledge_id,rel_path,mutation_version,content_revision,active_vector_version,
                 desired_vector_version,visibility,vector_sync_state,approval_state,current_revision,index_state,updated_at)
                VALUES(?,?,1,?,NULL,1,'active','pending','confirming',?,'none',?)""",
                (knowledge_id, rel_path, revision, revision, now))
        elif existing["approval_state"] != "confirmed":
            conn.execute("""UPDATE card_state SET rel_path=?, content_revision=?, current_revision=?,
                approval_state='confirming', index_state='none', index_error_code=NULL,
                vector_sync_state='pending', updated_at=? WHERE knowledge_id=?""",
                (rel_path, revision, revision, now, knowledge_id))
        conn.execute("""UPDATE card_confirmation_sessions SET knowledge_id=?, state='file_committed',
            payload_json=?, updated_at=? WHERE session_id=? AND state='preparing'""",
            (knowledge_id, json.dumps(payload, ensure_ascii=False), now, session_id))
        conn.commit()


def pending_material_confirmations() -> list[dict]:
    """仅列出本阶段遗留会话；不扫描或推断任何历史卡片。"""
    _init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM card_confirmation_sessions WHERE material_id IS NOT NULL AND state IN ('preparing','file_committed')"
        ).fetchall()
        return [dict(row) for row in rows]


def pending_confirmations() -> list[dict]:
    """Return all unfinished confirmation sagas for the controlled recovery loop."""
    _init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM card_confirmation_sessions WHERE state IN ('preparing','file_committed')"
        ).fetchall()
    return [dict(row) for row in rows]


def roll_back_material_confirmation(session_id: str, error_code: str) -> None:
    _init()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute(
            "SELECT knowledge_id FROM card_confirmation_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        conn.execute("""UPDATE card_confirmation_sessions SET state='rolled_back', error_code=?,
            lease_until=NULL, updated_at=? WHERE session_id=? AND state IN ('preparing','file_committed')""",
                     (error_code, time.time(), session_id))
        knowledge_id = str(session["knowledge_id"] or "") if session is not None else ""
        if knowledge_id:
            conn.execute("""UPDATE card_state SET approval_state='draft', index_state='none',
                index_error_code=?, updated_at=? WHERE knowledge_id=? AND approval_state='confirming'""",
                         (error_code, time.time(), knowledge_id))
        conn.commit()


def confirm_and_enqueue(
    knowledge_id: str,
    rel_path: str,
    target_revision: str,
    idempotency_key: str,
    payload: dict,
    lease_seconds: int = 300,
) -> dict:
    """确认卡片并写入向量 outbox，三项状态在同一 SQLite 事务中提交。

    调用方须在进入本函数前完成 Wiki 文件的 hash CAS/原子 rename。这里不做任何
    历史卡片扫描；账本缺失时仅为当前明确确认的卡片创建一行。
    """
    _init()
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    now = time.time()
    payload = {**payload, "target_revision": target_revision, "rel_path": rel_path}
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute(
            "SELECT * FROM card_confirmation_sessions WHERE knowledge_id=? AND target_revision=? AND idempotency_key=?",
            (knowledge_id, target_revision, idempotency_key),
        ).fetchone()
        if session is not None:
            job = conn.execute(
                "SELECT * FROM card_vector_jobs WHERE knowledge_id=? AND vector_version=?",
                (knowledge_id, json.loads(session["payload_json"]).get("vector_version")),
            ).fetchone()
            conn.commit()
            return {"session": dict(session), "job": _row(job), "idempotent": True}
        existing = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if existing is not None and existing["approval_state"] == "confirming":
            conn.rollback()
            raise ConfirmationConflict("confirmation already in progress")
        if existing is not None and existing["approval_state"] == "confirmed" and existing["current_revision"] == target_revision:
            conn.rollback()
            raise ConfirmationConflict("revision already confirmed")
        desired = 1 if existing is None else int(existing["desired_vector_version"] or 0) + 1
        session_id = uuid.uuid4().hex
        session_payload = {**payload, "vector_version": desired}
        if existing is None:
            conn.execute(
                """INSERT INTO card_state
                   (knowledge_id,rel_path,mutation_version,content_revision,active_vector_version,desired_vector_version,
                    visibility,vector_sync_state,approval_state,current_revision,index_state,updated_at)
                   VALUES(?,?,1,?,NULL,?,'active','pending','confirmed',?,'indexing',?)""",
                (knowledge_id, rel_path, target_revision, desired, target_revision, now),
            )
        else:
            conn.execute(
                """UPDATE card_state SET rel_path=?, mutation_version=mutation_version+1, content_revision=?,
                   desired_vector_version=?, approval_state='confirmed', current_revision=?, indexed_revision=NULL,
                   indexed_vector_version=NULL, index_state='indexing', index_error_code=NULL,
                   vector_sync_state='pending', updated_at=? WHERE knowledge_id=?""",
                (rel_path, target_revision, desired, target_revision, now, knowledge_id),
            )
        conn.execute(
            """INSERT INTO card_confirmation_sessions
               (session_id,knowledge_id,target_revision,idempotency_key,state,payload_json,lease_until,created_at,updated_at)
               VALUES(?,?,?,?, 'ledger_committed', ?, ?, ?, ?)""",
            (session_id, knowledge_id, target_revision, idempotency_key,
             json.dumps(session_payload, ensure_ascii=False), now + lease_seconds, now, now),
        )
        job_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO card_vector_jobs
               (job_id,knowledge_id,vector_version,payload_json,state,attempts,created_at,updated_at)
               VALUES(?,?,?,?, 'queued',0,?,?)
               ON CONFLICT(knowledge_id,vector_version) DO NOTHING""",
            (job_id, knowledge_id, desired, json.dumps(session_payload, ensure_ascii=False), now, now),
        )
        job = conn.execute(
            "SELECT * FROM card_vector_jobs WHERE knowledge_id=? AND vector_version=?", (knowledge_id, desired)
        ).fetchone()
        conn.commit()
        return {"session": {"session_id": session_id, "state": "ledger_committed"}, "job": _row(job), "idempotent": False}


def ensure(
    knowledge_id: str,
    rel_path: str,
    content_revision: str,
    visibility: str = "active",
    *,
    device_scope: str = SCOPE_GLOBAL,
) -> dict:
    """建立或更新卡片账本；正文变化才递增期待向量版本。"""
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM card_state WHERE knowledge_id=? AND device_scope=?",
            (knowledge_id, device_scope),
        ).fetchone()
        if existing is None:
            conn.execute("""INSERT INTO card_state
                (knowledge_id,rel_path,mutation_version,content_revision,active_vector_version,
                 desired_vector_version,visibility,vector_sync_state,device_scope,updated_at)
                VALUES(?,?,?,?,NULL,1,?,'pending',?,?)""",
                (knowledge_id, rel_path, 1, content_revision, visibility, device_scope, now))
        else:
            changed = existing["content_revision"] != content_revision
            reactivated = existing["visibility"] != visibility
            desired = int(existing["desired_vector_version"] or 1) + (1 if changed or reactivated else 0)
            sync = "pending" if changed or reactivated else existing["vector_sync_state"]
            conn.execute("""UPDATE card_state SET rel_path=?, mutation_version=?, content_revision=?,
                desired_vector_version=?, visibility=?, vector_sync_state=?, updated_at=? WHERE knowledge_id=?""",
                (rel_path, int(existing["mutation_version"]) + (1 if changed or reactivated else 0),
                 content_revision, desired, visibility, sync, now, knowledge_id))
        conn.commit()
    return get(knowledge_id, device_scope=device_scope) or {}


def mark_visibility(knowledge_id: str, visibility: str, reason: str) -> dict | None:
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        active = row["active_vector_version"]
        if active is not None:
            conn.execute("INSERT OR IGNORE INTO card_tombstones VALUES(?,?,?,?)",
                         (knowledge_id, int(active), reason, now))
        conn.execute("""UPDATE card_state SET visibility=?, active_vector_version=NULL,
            vector_sync_state='clean', mutation_version=mutation_version+1, updated_at=? WHERE knowledge_id=?""",
                     (visibility, now, knowledge_id))
        if visibility != "active":
            # A recycled/purged card must not resurrect an abandoned working copy.
            conn.execute("DELETE FROM card_edit_drafts WHERE knowledge_id=?", (knowledge_id,))
            conn.execute("DELETE FROM card_pending_updates WHERE knowledge_id=?", (knowledge_id,))
        conn.commit()
    return get(knowledge_id)


def transition_lifecycle_visibility(
    knowledge_id: str, visibility: str, content_revision: str,
) -> dict | None:
    """Apply a recycle/restore transition without treating its metadata as an edit.

    Recycling changes frontmatter and therefore the file revision, but it does not
    change the semantic card body.  The normal watcher path must fail closed for
    arbitrary file edits; lifecycle operations are the controlled exception.
    """
    if visibility not in {"recycled", "active"}:
        raise ValueError("unsupported lifecycle visibility")
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if row is None or row["approval_state"] != "confirmed":
            conn.rollback()
            return _row(row)
        if row["active_vector_version"] is not None:
            conn.execute(
                "INSERT OR IGNORE INTO card_tombstones VALUES(?,?,?,?)",
                (knowledge_id, int(row["active_vector_version"]), "recycle" if visibility == "recycled" else "restore", now),
            )
        if visibility == "recycled":
            conn.execute(
                """UPDATE card_state SET visibility='recycled', content_revision=?, current_revision=?,
                   active_vector_version=NULL, indexed_revision=NULL, indexed_vector_version=NULL,
                   index_state='none', index_error_code=NULL, vector_sync_state='clean',
                   mutation_version=mutation_version+1, updated_at=? WHERE knowledge_id=?""",
                (content_revision, content_revision, now, knowledge_id),
            )
            conn.execute("DELETE FROM card_edit_drafts WHERE knowledge_id=?", (knowledge_id,))
            conn.execute("DELETE FROM card_pending_updates WHERE knowledge_id=?", (knowledge_id,))
        else:
            desired = int(row["desired_vector_version"] or 0) + 1
            conn.execute(
                """UPDATE card_state SET visibility='active', content_revision=?, current_revision=?,
                   desired_vector_version=?, active_vector_version=NULL, indexed_revision=NULL,
                   indexed_vector_version=NULL, index_state='indexing', index_error_code=NULL,
                   vector_sync_state='pending', mutation_version=mutation_version+1, updated_at=?
                   WHERE knowledge_id=?""",
                (content_revision, content_revision, desired, now, knowledge_id),
            )
        conn.commit()
    return get(knowledge_id)


def activate_vector(knowledge_id: str, vector_version: int) -> bool:
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if (
            row is None or row["visibility"] != "active"
            or row["approval_state"] != "confirmed"
            or row["index_state"] != "indexing"
            or int(row["desired_vector_version"]) != vector_version
        ):
            conn.rollback()
            return False
        old = row["active_vector_version"]
        if old is not None and int(old) != vector_version:
            conn.execute("INSERT OR IGNORE INTO card_tombstones VALUES(?,?,?,?)",
                         (knowledge_id, int(old), "update", now))
        conn.execute(
            """UPDATE card_state SET active_vector_version=?, vector_sync_state='clean',
               indexed_vector_version=?, indexed_revision=current_revision, index_state='indexed',
               index_error_code=NULL, updated_at=? WHERE knowledge_id=?""",
            (vector_version, vector_version, now, knowledge_id),
        )
        conn.commit()
    return True


def mark_vector_failed(knowledge_id: str, error_code: str = "index_failed") -> None:
    _init()
    with _connect() as conn:
        conn.execute(
            """UPDATE card_state SET vector_sync_state='failed', index_state='index_failed',
               index_error_code=?, updated_at=? WHERE knowledge_id=?
               AND approval_state='confirmed'""",
            (str(error_code or "index_failed")[:120], time.time(), knowledge_id),
        )


def touch_metadata(knowledge_id: str, content_revision: str | None = None) -> None:
    """Record a non-vector mutation (for example source refs) without bumping vectors."""
    _init()
    with _connect() as conn:
        if content_revision is None:
            conn.execute("UPDATE card_state SET mutation_version=mutation_version+1, updated_at=? WHERE knowledge_id=?",
                         (time.time(), knowledge_id))
        else:
            conn.execute("UPDATE card_state SET mutation_version=mutation_version+1, content_revision=?, updated_at=? WHERE knowledge_id=?",
                         (content_revision, time.time(), knowledge_id))


def update_folder_metadata(knowledge_id: str, folder_id: int, expected_revision: str | None = None) -> dict:
    """Move a card as metadata only; semantic/vector revisions stay unchanged."""
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if row is None or row["visibility"] != "active" or row["approval_state"] != "confirmed":
            conn.rollback()
            raise ConfirmationConflict("card is not an active confirmed card")
        if expected_revision and str(row["current_revision"] or "") != expected_revision:
            conn.rollback()
            raise ConfirmationConflict("card revision conflict")
        conn.execute(
            """UPDATE card_state SET folder_id=?, metadata_revision=metadata_revision+1,
               mutation_version=mutation_version+1, updated_at=? WHERE knowledge_id=?""",
            (folder_id, now, knowledge_id),
        )
        conn.commit()
    return get(knowledge_id) or {}


def enqueue_vector_repair(knowledge_id: str, vector_version: int, payload_json: str) -> str:
    _init()
    now = time.time()
    job_id = uuid.uuid4().hex
    with _LOCK, _connect() as conn:
        conn.execute("""INSERT INTO card_vector_jobs
            (job_id,knowledge_id,vector_version,payload_json,state,attempts,created_at,updated_at)
            VALUES(?,?,?,?, 'queued',0,?,?)
            ON CONFLICT(knowledge_id,vector_version) DO UPDATE SET
              updated_at=excluded.updated_at""",
            (job_id, knowledge_id, vector_version, payload_json, now, now))
        row = conn.execute("SELECT job_id FROM card_vector_jobs WHERE knowledge_id=? AND vector_version=?",
                           (knowledge_id, vector_version)).fetchone()
    return str(row["job_id"])


def record_vector_manifest(
    knowledge_id: str,
    vector_version: int,
    content_revision: str,
    expected_chunk_count: int,
    chunk_ids_hash: str,
    *,
    body_hash: str = "",
    embedding_model_id: str = "",
    embedding_dimension: int | None = None,
    routing_epoch: int | None = None,
) -> None:
    _init()
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO card_vector_manifests
               (knowledge_id,vector_version,content_revision,expected_chunk_count,chunk_ids_hash,body_hash,
                embedding_model_id,embedding_dimension,routing_epoch,state,verified_at)
               VALUES(?,?,?,?,?,?,?,?,?,'verified',?)
               ON CONFLICT(knowledge_id,vector_version) DO UPDATE SET
                 content_revision=excluded.content_revision,
                 expected_chunk_count=excluded.expected_chunk_count,
                 chunk_ids_hash=excluded.chunk_ids_hash,
                 body_hash=excluded.body_hash,
                 embedding_model_id=excluded.embedding_model_id,
                 embedding_dimension=excluded.embedding_dimension,
                 routing_epoch=excluded.routing_epoch,
                 state='verified', verified_at=excluded.verified_at""",
            (knowledge_id, vector_version, content_revision, expected_chunk_count, chunk_ids_hash,
             body_hash, embedding_model_id, embedding_dimension, routing_epoch, time.time()),
        )


def get_vector_manifest(knowledge_id: str, vector_version: int) -> dict | None:
    _init()
    with _connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM card_vector_manifests WHERE knowledge_id=? AND vector_version=?",
            (knowledge_id, vector_version),
        ).fetchone())


def mark_vector_manifest_corrupted(knowledge_id: str, vector_version: int) -> None:
    _init()
    with _connect() as conn:
        conn.execute(
            "UPDATE card_vector_manifests SET state='corrupted', verified_at=? WHERE knowledge_id=? AND vector_version=?",
            (time.time(), knowledge_id, vector_version),
        )


def list_indexed_cards() -> list[dict]:
    _init()
    with _connect() as conn:
        return [dict(row) for row in conn.execute(
            """SELECT * FROM card_state WHERE visibility='active' AND approval_state='confirmed'
               AND index_state='indexed' AND indexed_vector_version IS NOT NULL ORDER BY updated_at"""
        ).fetchall()]


def should_preserve_vector(knowledge_id: str, vector_version: int) -> bool:
    """Whether a routed generation switch must retain this physical version."""
    _init()
    with _connect() as conn:
        state = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if state is not None and state["visibility"] == "active" and state["approval_state"] == "confirmed":
            if state["active_vector_version"] is not None and int(state["active_vector_version"]) == int(vector_version):
                return True
            if state["index_state"] == "indexing" and int(state["desired_vector_version"] or -1) == int(vector_version):
                return True
        pending = conn.execute(
            "SELECT 1 FROM card_pending_updates WHERE knowledge_id=? AND vector_version=? AND state IN ('indexing','recovering')",
            (knowledge_id, vector_version),
        ).fetchone()
        return pending is not None


def mark_vector_corrupted(knowledge_id: str, error_code: str = "index_manifest_mismatch") -> None:
    _init()
    with _LOCK, _connect() as conn:
        conn.execute(
            """UPDATE card_state SET vector_sync_state='failed', index_state='index_corrupted',
               index_error_code=?, updated_at=? WHERE knowledge_id=? AND approval_state='confirmed'""",
            (str(error_code)[:120], time.time(), knowledge_id),
        )


def recover_interrupted_vector_jobs() -> dict:
    """Reconcile durable outbox rows after a healthy restart without scanning Wiki."""
    _init()
    recovered = completed = failed = 0
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM card_vector_jobs WHERE state IN ('queued','running','paused') ORDER BY created_at"
        ).fetchall()
        for job in rows:
            state = conn.execute("SELECT * FROM card_state WHERE knowledge_id=?", (job["knowledge_id"],)).fetchone()
            pending = conn.execute(
                "SELECT * FROM card_pending_updates WHERE knowledge_id=? AND vector_version=?",
                (job["knowledge_id"], job["vector_version"]),
            ).fetchone()
            if (state is not None and state["index_state"] == "indexed"
                    and state["active_vector_version"] is not None
                    and int(state["active_vector_version"]) == int(job["vector_version"])):
                conn.execute(
                    "UPDATE card_vector_jobs SET state='done', lease_until=NULL, error_detail=NULL, updated_at=? WHERE job_id=?",
                    (now, job["job_id"]),
                )
                completed += 1
                continue
            if pending is not None:
                if pending["state"] == "index_failed":
                    conn.execute(
                        "UPDATE card_vector_jobs SET state='failed', lease_until=NULL, error_detail=?, updated_at=? WHERE job_id=?",
                        (pending["error_code"] or "index_failed", now, job["job_id"]),
                    )
                    failed += 1
                    continue
                conn.execute(
                    """UPDATE card_pending_updates SET state='recovering', owner_id=NULL,
                       fencing_token=fencing_token+1, error_code=NULL, updated_at=? WHERE knowledge_id=?""",
                    (now, job["knowledge_id"]),
                )
            elif state is None or state["visibility"] != "active" or state["approval_state"] != "confirmed":
                conn.execute(
                    "UPDATE card_vector_jobs SET state='failed', lease_until=NULL, error_detail='stale_target', updated_at=? WHERE job_id=?",
                    (now, job["job_id"]),
                )
                failed += 1
                continue
            else:
                conn.execute(
                    """UPDATE card_state SET index_state='indexing', vector_sync_state='pending',
                       index_error_code=NULL, updated_at=? WHERE knowledge_id=?""",
                    (now, job["knowledge_id"]),
                )
            conn.execute(
                """UPDATE card_vector_jobs SET state='queued', lease_until=NULL,
                   error_detail='service_interrupted_recovering', updated_at=? WHERE job_id=?""",
                (now, job["job_id"]),
            )
            recovered += 1
        conn.commit()
    return {"recovered": recovered, "completed": completed, "failed": failed}


def pause_vector_jobs(reason: str = "service_interrupted") -> int:
    """启动恢复时暂停索引，并把不可检索原因同步到卡片台账。"""
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 兼容本次修复前已经遗留的 paused 行：它们同样必须从卡片详情可见并可重试。
        rows = conn.execute(
            "SELECT DISTINCT knowledge_id FROM card_vector_jobs WHERE state IN ('queued','running','paused')"
        ).fetchall()
        cur = conn.execute("""UPDATE card_vector_jobs SET state='paused', lease_until=NULL,
                              error_detail=?, updated_at=? WHERE state IN ('queued','running')""",
                           (reason[:1000], now))
        if rows:
            conn.executemany(
                """UPDATE card_state SET index_state='index_failed', vector_sync_state='failed',
                   index_error_code=?, updated_at=? WHERE knowledge_id=?
                   AND approval_state='confirmed' AND index_state='indexing'""",
                [(reason[:120], now, row["knowledge_id"]) for row in rows],
            )
        conn.commit()
    return int(cur.rowcount or 0)


def reclaim_vector_jobs(lease_seconds: int = 120) -> int:
    """Compatibility shim: recovery is deliberately pause-only in phase C."""
    return pause_vector_jobs("lease_recovery_paused")


def claim_vector_job(lease_seconds: int = 120) -> dict | None:
    _init()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM card_vector_jobs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            conn.rollback()
            return None
        conn.execute("UPDATE card_vector_jobs SET state='running', attempts=attempts+1, lease_until=?, updated_at=? WHERE job_id=?",
                     (now + lease_seconds, now, row["job_id"]))
        conn.commit()
        result = dict(row)
        result["attempts"] = int(result["attempts"] or 0) + 1
        return result


def finish_vector_job(
    job_id: str, success: bool, error: str = "", *, transient: bool = False, retry_limit: int = 3,
) -> str | None:
    _init()
    with _connect() as conn:
        row = conn.execute("SELECT attempts FROM card_vector_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        state = "done" if success else (
            "queued" if transient and int(row["attempts"] or 0) < retry_limit else "failed"
        )
        conn.execute("UPDATE card_vector_jobs SET state=?, lease_until=NULL, error_detail=?, updated_at=? WHERE job_id=?",
                     (state, error[:1000], time.time(), job_id))
    return state


def list_vector_jobs(limit: int = 100) -> list[dict]:
    _init()
    with _connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM card_vector_jobs ORDER BY updated_at DESC LIMIT ?", (limit,))]


def list_purge_jobs(non_terminal_only: bool = False) -> list[dict]:
    _init()
    sql = "SELECT * FROM card_purge_jobs"
    if non_terminal_only:
        sql += " WHERE state NOT IN ('completed','completed_with_vector_cleanup_pending')"
    sql += " ORDER BY updated_at"
    with _connect() as conn:
        return [dict(row) for row in conn.execute(sql)]


def create_purge_job(purge_id: str, knowledge_id: str, rel_path: str, snapshot: str = "{}") -> None:
    _init()
    now = time.time()
    with _connect() as conn:
        conn.execute("""INSERT OR REPLACE INTO card_purge_jobs
            (purge_id,knowledge_id,rel_path,state,dependency_snapshot_json,created_at,updated_at)
            VALUES(?,?,?,'prepared',?,?,?)""", (purge_id, knowledge_id, rel_path, snapshot, now, now))


def update_purge_job(purge_id: str, state: str, error_code: str | None = None, error_detail: str = "") -> None:
    _init()
    with _connect() as conn:
        conn.execute("""UPDATE card_purge_jobs SET state=?, error_code=?, error_detail=?, updated_at=?
                        WHERE purge_id=?""", (state, error_code, error_detail[:1000], time.time(), purge_id))


def is_active_vector(knowledge_id: str, vector_version: int) -> bool:
    row = get(knowledge_id)
    return bool(row and row["visibility"] == "active" and row["active_vector_version"] == vector_version)


def active_vector_version(knowledge_id: str) -> int | None:
    row = get(knowledge_id)
    return int(row["active_vector_version"]) if row and row["active_vector_version"] is not None else None


def reset_for_tests(path: Path | None = None) -> None:
    global _PATH, _READY
    with _LOCK:
        _PATH = path or CARD_LEDGER_DB_PATH
        _READY = False
