"""知君对话的本地持久化：会话、消息、摘要与出设备回执。

- 会话与消息是「对话记录」层（三层记忆的第一层）：只存原文与生成元数据，不做任何理解。
- ``turn_receipts`` 记录每一轮送出设备（或送入本地模型）的上下文构成，是「可见可审计」的落点。
- 与 ontology.db 互不引用：理解通过 ``claim_evidence.message_id`` 反向指回消息。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import CONVERSATIONS_DB_PATH

MODES = ("chat", "onboarding", "deliberate", "review")
ROLES = ("user", "assistant", "system")
MESSAGE_STATUSES = ("complete", "aborted", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL CHECK(mode IN ('chat','onboarding','deliberate','review')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
    device_scope TEXT NOT NULL DEFAULT 'global',
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_message_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conversations_recent ON conversations(status, last_message_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'complete' CHECK(status IN ('complete','aborted','error')),
    provider TEXT,
    model TEXT,
    external INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, seq);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    up_to_seq INTEGER NOT NULL,
    summary TEXT NOT NULL,
    key_points_json TEXT NOT NULL DEFAULT '[]',
    generated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(conversation_id, revision)
);

CREATE TABLE IF NOT EXISTS turn_receipts (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    external INTEGER NOT NULL,
    confirmed_claim_ids_json TEXT NOT NULL,
    working_claim_ids_json TEXT NOT NULL,
    material_chunk_keys_json TEXT NOT NULL,
    retracted_notice_count INTEGER NOT NULL,
    prompt_chars INTEGER NOT NULL,
    extraction_provider TEXT,
    created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class ConversationError(ValueError):
    """请求不合法（400）。"""


class ConversationNotFoundError(ConversationError):
    """会话或消息不存在（404）。"""


class ConversationStore:
    _instance: "ConversationStore | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else CONVERSATIONS_DB_PATH
        self._ready = False
        self._lock = threading.RLock()
        self._ensure()

    @classmethod
    def instance(cls) -> "ConversationStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _ensure(self) -> None:
        if self._ready and self._db_path.is_file():
            return
        with self._lock:
            if self._ready and self._db_path.is_file():
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            self._ready = True

    def _connect(self) -> sqlite3.Connection:
        self._ensure()
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    # ------------------------------------------------------------------ 序列化
    @staticmethod
    def _conversation(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "title": row["title"] or "",
            "mode": row["mode"],
            "status": row["status"],
            "messageCount": int(row["message_count"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "lastMessageAt": row["last_message_at"],
        }

    @staticmethod
    def _message(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "conversationId": row["conversation_id"],
            "seq": int(row["seq"]),
            "role": row["role"],
            "content": row["content"],
            "status": row["status"],
            "provider": row["provider"],
            "model": row["model"],
            "external": bool(row["external"]),
            "usage": _load(row["usage_json"], None),
            "meta": _load(row["meta_json"], {}),
            "createdAt": row["created_at"],
        }

    # ------------------------------------------------------------------ 会话
    def create_conversation(self, *, mode: str = "chat", title: str = "", device_scope: str = "global") -> dict:
        if mode not in MODES:
            raise ConversationError(f"mode 不合法：{mode}")
        title = (title or "").strip()[:80]
        conversation_id = f"conv_{uuid.uuid4().hex[:12]}"
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, mode, status, device_scope, message_count, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, 0, ?, ?)",
                (conversation_id, title, mode, device_scope, now, now),
            )
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            return self._conversation(row)  # type: ignore[return-value]

    def get_conversation(self, conversation_id: str) -> dict | None:
        with self._connect() as conn:
            return self._conversation(
                conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            )

    def list_conversations(self, *, limit: int = 50, status: str = "active") -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE status = ? "
                "ORDER BY COALESCE(last_message_at, created_at) DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
            return [self._conversation(r) for r in rows]  # type: ignore[misc]

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            return cur.rowcount > 0

    def set_title(self, conversation_id: str, title: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                ((title or "").strip()[:80], utc_now(), conversation_id),
            )

    # ------------------------------------------------------------------ 消息
    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        message_id: str | None = None,
        status: str = "complete",
        provider: str | None = None,
        model: str | None = None,
        external: bool = False,
        usage: dict | None = None,
        meta: dict | None = None,
    ) -> dict:
        if role not in ROLES:
            raise ConversationError(f"role 不合法：{role}")
        if status not in MESSAGE_STATUSES:
            raise ConversationError(f"status 不合法：{status}")
        message_id = message_id or f"msg_{uuid.uuid4().hex[:12]}"
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
                if conv is None:
                    raise ConversationNotFoundError("会话不存在")
                seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE conversation_id = ?", (conversation_id,)
                ).fetchone()
                seq = int(seq_row["m"]) + 1
                conn.execute(
                    """
                    INSERT INTO messages
                        (id, conversation_id, seq, role, content, meta_json, status, provider, model, external, usage_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        conversation_id,
                        seq,
                        role,
                        content or "",
                        _json(meta or {}),
                        status,
                        provider,
                        model,
                        1 if external else 0,
                        _json(usage) if usage is not None else None,
                        now,
                    ),
                )
                title = conv["title"] or ""
                if not title and role == "user":
                    title = (content or "").strip().replace("\n", " ")[:30]
                conn.execute(
                    "UPDATE conversations SET message_count = message_count + 1, updated_at = ?, "
                    "last_message_at = ?, title = ? WHERE id = ?",
                    (now, now, title, conversation_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            return self._message(row)  # type: ignore[return-value]

    def update_message(
        self,
        message_id: str,
        *,
        content: str | None = None,
        status: str | None = None,
        usage: dict | None = None,
        meta: dict | None = None,
    ) -> dict | None:
        if status is not None and status not in MESSAGE_STATUSES:
            raise ConversationError(f"status 不合法：{status}")
        sets: list[str] = []
        params: list = []
        if content is not None:
            sets.append("content = ?")
            params.append(content)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if usage is not None:
            sets.append("usage_json = ?")
            params.append(_json(usage))
        if meta is not None:
            sets.append("meta_json = ?")
            params.append(_json(meta))
        if not sets:
            return self.get_message(message_id)
        params.append(message_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", params)
            return self._message(conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())

    def get_message(self, message_id: str) -> dict | None:
        with self._connect() as conn:
            return self._message(conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())

    def list_messages(self, conversation_id: str, *, limit: int | None = None, before_seq: int | None = None) -> list[dict]:
        with self._connect() as conn:
            query = "SELECT * FROM messages WHERE conversation_id = ?"
            params: list = [conversation_id]
            if before_seq is not None:
                query += " AND seq < ?"
                params.append(int(before_seq))
            query += " ORDER BY seq"
            if limit is not None:
                query += " LIMIT ?"
                params.append(int(limit))
            rows = conn.execute(query, params).fetchall()
            return [self._message(r) for r in rows]  # type: ignore[misc]

    def recent_messages(self, conversation_id: str, n: int = 12) -> list[dict]:
        """最近 n 条（按 seq 升序返回）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq DESC LIMIT ?",
                (conversation_id, int(n)),
            ).fetchall()
        return [self._message(r) for r in reversed(rows)]  # type: ignore[misc]

    def count_messages(self, conversation_id: str, role: str | None = None) -> int:
        with self._connect() as conn:
            if role:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ? AND role = ?",
                    (conversation_id, role),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?", (conversation_id,)
                ).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ 摘要
    def save_summary(
        self,
        conversation_id: str,
        *,
        up_to_seq: int,
        summary: str,
        key_points: list[str] | None = None,
        generated_by: str = "model",
    ) -> dict:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) AS r FROM conversation_summaries WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                revision = int(row["r"]) + 1
                conn.execute(
                    "INSERT INTO conversation_summaries (conversation_id, revision, up_to_seq, summary, key_points_json, generated_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (conversation_id, revision, int(up_to_seq), summary, _json(key_points or []), generated_by, now),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {
            "conversationId": conversation_id,
            "revision": revision,
            "upToSeq": int(up_to_seq),
            "summary": summary,
            "keyPoints": key_points or [],
            "generatedBy": generated_by,
            "createdAt": now,
        }

    def latest_summary(self, conversation_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ? ORDER BY revision DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "conversationId": row["conversation_id"],
            "revision": int(row["revision"]),
            "upToSeq": int(row["up_to_seq"]),
            "summary": row["summary"],
            "keyPoints": _load(row["key_points_json"], []),
            "generatedBy": row["generated_by"],
            "createdAt": row["created_at"],
        }

    # ------------------------------------------------------------------ 回执
    def save_receipt(
        self,
        *,
        message_id: str,
        conversation_id: str,
        provider: str,
        model: str,
        external: bool,
        confirmed_claim_ids: list[str],
        working_claim_ids: list[str],
        material_chunk_keys: list[str],
        retracted_notice_count: int,
        prompt_chars: int,
        extraction_provider: str | None = None,
    ) -> dict:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO turn_receipts
                    (message_id, conversation_id, provider, model, external, confirmed_claim_ids_json,
                     working_claim_ids_json, material_chunk_keys_json, retracted_notice_count, prompt_chars,
                     extraction_provider, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    provider = excluded.provider, model = excluded.model, external = excluded.external,
                    confirmed_claim_ids_json = excluded.confirmed_claim_ids_json,
                    working_claim_ids_json = excluded.working_claim_ids_json,
                    material_chunk_keys_json = excluded.material_chunk_keys_json,
                    retracted_notice_count = excluded.retracted_notice_count,
                    prompt_chars = excluded.prompt_chars, extraction_provider = excluded.extraction_provider
                """,
                (
                    message_id,
                    conversation_id,
                    provider,
                    model,
                    1 if external else 0,
                    _json(list(confirmed_claim_ids)),
                    _json(list(working_claim_ids)),
                    _json(list(material_chunk_keys)),
                    int(retracted_notice_count),
                    int(prompt_chars),
                    extraction_provider,
                    now,
                ),
            )
        return self.get_receipt(message_id)  # type: ignore[return-value]

    def get_receipt(self, message_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM turn_receipts WHERE message_id = ?", (message_id,)).fetchone()
        if row is None:
            return None
        return {
            "messageId": row["message_id"],
            "conversationId": row["conversation_id"],
            "provider": row["provider"],
            "model": row["model"],
            "external": bool(row["external"]),
            "confirmedClaimIds": _load(row["confirmed_claim_ids_json"], []),
            "workingClaimIds": _load(row["working_claim_ids_json"], []),
            "materialChunkKeys": _load(row["material_chunk_keys_json"], []),
            "retractedNoticeCount": int(row["retracted_notice_count"]),
            "promptChars": int(row["prompt_chars"]),
            "extractionProvider": row["extraction_provider"],
            "createdAt": row["created_at"],
        }


def reset_for_tests(db_path: str | Path | None = None) -> ConversationStore:
    with ConversationStore._instance_lock:
        ConversationStore._instance = ConversationStore(db_path)
        return ConversationStore._instance
