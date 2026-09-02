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

CREATE TABLE IF NOT EXISTS decision_drafts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','confirmed','discarded')),
    decision_id TEXT,
    fields_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_conversation ON decision_drafts(conversation_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS nudge_policies (
    key TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    max_per_day INTEGER NOT NULL DEFAULT 3,
    silenced_refs_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nudge_events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('review_due','commitment_due','checkin','principle_tension')),
    trigger_key TEXT NOT NULL,
    trigger_ref_json TEXT NOT NULL,
    why_now TEXT NOT NULL CHECK(length(why_now) > 0),
    message TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','shown','acted','dismissed','silenced')),
    scheduled_for TEXT NOT NULL,
    shown_at TEXT,
    acted_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nudges_status ON nudge_events(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_nudges_trigger ON nudge_events(trigger_key, created_at DESC);

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
                # P2：回访会话绑定判断（旧库补列）。
                columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
                if "decision_id" not in columns:
                    conn.execute("ALTER TABLE conversations ADD COLUMN decision_id TEXT")
                # P3：提醒类型增加 principle_tension（P2 建的库 CHECK 不含它，需重建表）。
                ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'nudge_events'").fetchone()
                if ddl and "principle_tension" not in (ddl[0] or ""):
                    conn.executescript(
                        """
                        ALTER TABLE nudge_events RENAME TO nudge_events_old;
                        CREATE TABLE nudge_events (
                            id TEXT PRIMARY KEY,
                            kind TEXT NOT NULL CHECK(kind IN ('review_due','commitment_due','checkin','principle_tension')),
                            trigger_key TEXT NOT NULL,
                            trigger_ref_json TEXT NOT NULL,
                            why_now TEXT NOT NULL CHECK(length(why_now) > 0),
                            message TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(status IN ('pending','shown','acted','dismissed','silenced')),
                            scheduled_for TEXT NOT NULL,
                            shown_at TEXT,
                            acted_at TEXT,
                            created_at TEXT NOT NULL
                        );
                        INSERT INTO nudge_events SELECT * FROM nudge_events_old;
                        DROP TABLE nudge_events_old;
                        CREATE INDEX IF NOT EXISTS idx_nudges_status ON nudge_events(status, scheduled_for);
                        CREATE INDEX IF NOT EXISTS idx_nudges_trigger ON nudge_events(trigger_key, created_at DESC);
                        """
                    )
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
        keys = set(row.keys())
        return {
            "id": row["id"],
            "title": row["title"] or "",
            "mode": row["mode"],
            "status": row["status"],
            "decisionId": row["decision_id"] if "decision_id" in keys else None,
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
    def create_conversation(
        self, *, mode: str = "chat", title: str = "", device_scope: str = "global", decision_id: str | None = None
    ) -> dict:
        if mode not in MODES:
            raise ConversationError(f"mode 不合法：{mode}")
        title = (title or "").strip()[:80]
        conversation_id = f"conv_{uuid.uuid4().hex[:12]}"
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, mode, status, device_scope, message_count, created_at, updated_at, decision_id) "
                "VALUES (?, ?, ?, 'active', ?, 0, ?, ?, ?)",
                (conversation_id, title, mode, device_scope, now, now, decision_id),
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


    # ------------------------------------------------------------------ 判断草稿（P2）
    @staticmethod
    def _draft(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "conversationId": row["conversation_id"],
            "messageId": row["message_id"],
            "revision": int(row["revision"]),
            "status": row["status"],
            "decisionId": row["decision_id"],
            "fields": _load(row["fields_json"], {}),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def get_draft(self, conversation_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM decision_drafts WHERE conversation_id = ? ORDER BY updated_at DESC, revision DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return self._draft(row)

    def get_draft_by_id(self, draft_id: str) -> dict | None:
        with self._connect() as conn:
            return self._draft(conn.execute("SELECT * FROM decision_drafts WHERE id = ?", (draft_id,)).fetchone())

    def upsert_draft(self, conversation_id: str, fields: dict, *, message_id: str | None = None) -> dict:
        """同一会话只维护一份进行中的草稿；已确认 / 已丢弃后再商量则另起一份。"""
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone() is None:
                    raise ConversationNotFoundError("会话不存在")
                row = conn.execute(
                    "SELECT * FROM decision_drafts WHERE conversation_id = ? ORDER BY updated_at DESC, revision DESC LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if row is not None and row["status"] == "draft":
                    draft_id = row["id"]
                    conn.execute(
                        "UPDATE decision_drafts SET fields_json = ?, revision = revision + 1, message_id = COALESCE(?, message_id), updated_at = ? WHERE id = ?",
                        (_json(fields), message_id, now, draft_id),
                    )
                else:
                    draft_id = f"draft_{uuid.uuid4().hex[:12]}"
                    conn.execute(
                        "INSERT INTO decision_drafts (id, conversation_id, message_id, revision, status, fields_json, created_at, updated_at) "
                        "VALUES (?, ?, ?, 1, 'draft', ?, ?, ?)",
                        (draft_id, conversation_id, message_id, _json(fields), now, now),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return self._draft(conn.execute("SELECT * FROM decision_drafts WHERE id = ?", (draft_id,)).fetchone())  # type: ignore[return-value]

    def set_draft_status(self, draft_id: str, status: str, *, decision_id: str | None = None, fields: dict | None = None) -> dict | None:
        if status not in ("draft", "confirmed", "discarded"):
            raise ConversationError(f"草稿状态不合法：{status}")
        with self._lock, self._connect() as conn:
            if fields is not None:
                conn.execute(
                    "UPDATE decision_drafts SET status = ?, decision_id = COALESCE(?, decision_id), fields_json = ?, updated_at = ? WHERE id = ?",
                    (status, decision_id, _json(fields), utc_now(), draft_id),
                )
            else:
                conn.execute(
                    "UPDATE decision_drafts SET status = ?, decision_id = COALESCE(?, decision_id), updated_at = ? WHERE id = ?",
                    (status, decision_id, utc_now(), draft_id),
                )
            return self._draft(conn.execute("SELECT * FROM decision_drafts WHERE id = ?", (draft_id,)).fetchone())

    # ------------------------------------------------------------------ 提醒（P2）
    @staticmethod
    def _nudge(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "triggerKey": row["trigger_key"],
            "triggerRef": _load(row["trigger_ref_json"], {}),
            "whyNow": row["why_now"],
            "message": row["message"],
            "status": row["status"],
            "scheduledFor": row["scheduled_for"],
            "shownAt": row["shown_at"],
            "actedAt": row["acted_at"],
            "createdAt": row["created_at"],
        }

    def nudge_policy(self) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM nudge_policies WHERE key = 'default'").fetchone()
        if row is None:
            return {"enabled": True, "maxPerDay": 3, "silencedRefs": []}
        return {
            "enabled": bool(row["enabled"]),
            "maxPerDay": int(row["max_per_day"]),
            "silencedRefs": _load(row["silenced_refs_json"], []),
        }

    def save_nudge_policy(self, *, enabled: bool | None = None, max_per_day: int | None = None, silenced_refs: list[str] | None = None) -> dict:
        current = self.nudge_policy()
        enabled = current["enabled"] if enabled is None else bool(enabled)
        max_per_day = current["maxPerDay"] if max_per_day is None else max(1, min(10, int(max_per_day)))
        silenced = current["silencedRefs"] if silenced_refs is None else sorted({str(s) for s in silenced_refs if str(s).strip()})
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO nudge_policies (key, enabled, max_per_day, silenced_refs_json, updated_at) VALUES ('default', ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET enabled = excluded.enabled, max_per_day = excluded.max_per_day, "
                "silenced_refs_json = excluded.silenced_refs_json, updated_at = excluded.updated_at",
                (1 if enabled else 0, max_per_day, _json(silenced), utc_now()),
            )
        return self.nudge_policy()

    def create_nudge(
        self,
        *,
        kind: str,
        trigger_key: str,
        trigger_ref: dict,
        why_now: str,
        message: str,
        scheduled_for: str,
        dedupe_days: int = 3,
        now: str | None = None,
    ) -> dict | None:
        """写入一条提醒；同一 trigger_key 在去重窗口内已有记录、或已被静默 → 返回 None。

        ``now`` 允许调用方传入扫描时刻（测试与补扫用），去重窗口与创建时间都以它为准。
        """
        if kind not in ("review_due", "commitment_due", "checkin", "principle_tension"):
            raise ConversationError(f"提醒类型不合法：{kind}")
        if not (why_now or "").strip():
            raise ConversationError("why_now 不能为空")
        if trigger_key in self.nudge_policy()["silencedRefs"]:
            return None
        now = now or utc_now()
        with self._lock, self._connect() as conn:
            recent = conn.execute(
                "SELECT 1 FROM nudge_events WHERE trigger_key = ? AND julianday(created_at) > julianday(?) - ? LIMIT 1",
                (trigger_key, now, int(dedupe_days)),
            ).fetchone()
            if recent is not None:
                return None
            nudge_id = f"ndg_{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO nudge_events (id, kind, trigger_key, trigger_ref_json, why_now, message, status, scheduled_for, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (nudge_id, kind, trigger_key, _json(trigger_ref), why_now.strip(), message.strip(), scheduled_for, now),
            )
            return self._nudge(conn.execute("SELECT * FROM nudge_events WHERE id = ?", (nudge_id,)).fetchone())

    def today_nudges(self, *, now: str | None = None, max_per_day: int | None = None) -> list[dict]:
        """今日可展示的提醒（pending/shown，按计划时间），最多 max_per_day 条；返回时把 pending 标为 shown。"""
        policy = self.nudge_policy()
        if not policy["enabled"]:
            return []
        limit = max_per_day or policy["maxPerDay"]
        current = now or utc_now()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM nudge_events WHERE status IN ('pending','shown') AND scheduled_for <= ? "
                "ORDER BY scheduled_for ASC, created_at ASC LIMIT ?",
                (current, int(limit)),
            ).fetchall()
            items = [self._nudge(r) for r in rows]
            for item in items:
                if item and item["status"] == "pending":
                    conn.execute("UPDATE nudge_events SET status = 'shown', shown_at = ? WHERE id = ?", (current, item["id"]))
                    item["status"] = "shown"
                    item["shownAt"] = current
        return [i for i in items if i]

    def get_nudge(self, nudge_id: str) -> dict | None:
        with self._connect() as conn:
            return self._nudge(conn.execute("SELECT * FROM nudge_events WHERE id = ?", (nudge_id,)).fetchone())

    def set_nudge_status(self, nudge_id: str, status: str) -> dict | None:
        if status not in ("pending", "shown", "acted", "dismissed", "silenced"):
            raise ConversationError(f"提醒状态不合法：{status}")
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE nudge_events SET status = ?, acted_at = CASE WHEN ? = 'acted' THEN ? ELSE acted_at END WHERE id = ?",
                (status, status, now, nudge_id),
            )
            return self._nudge(conn.execute("SELECT * FROM nudge_events WHERE id = ?", (nudge_id,)).fetchone())

    def act_nudges(self, trigger_key: str) -> int:
        now = utc_now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE nudge_events SET status = 'acted', acted_at = ? WHERE trigger_key = ? AND status IN ('pending','shown')",
                (now, trigger_key),
            )
            return int(cur.rowcount)

    def silence_trigger(self, trigger_key: str) -> dict:
        policy = self.nudge_policy()
        refs = sorted(set(policy["silencedRefs"]) | {trigger_key})
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE nudge_events SET status = 'silenced' WHERE trigger_key = ? AND status IN ('pending','shown')",
                (trigger_key,),
            )
        return self.save_nudge_policy(silenced_refs=refs)

    def purge_all(self) -> dict:
        """全量删除对话层（不可恢复）：会话、消息、摘要、回执、草稿、提醒；策略保留。"""
        with self._lock, self._connect() as conn:
            counts = {
                "conversations": conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"],
                "messages": conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"],
            }
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table in ("turn_receipts", "decision_drafts", "conversation_summaries", "messages", "nudge_events", "conversations"):
                    conn.execute(f"DELETE FROM {table}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {k: int(v) for k, v in counts.items()}

    def list_nudges(self, *, statuses: tuple[str, ...] = ("pending", "shown", "acted", "dismissed", "silenced"), limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM nudge_events WHERE status IN (%s) ORDER BY created_at DESC LIMIT ?" % ",".join("?" for _ in statuses),
                (*statuses, int(limit)),
            ).fetchall()
        return [self._nudge(r) for r in rows]  # type: ignore[misc]


def reset_for_tests(db_path: str | Path | None = None) -> ConversationStore:
    with ConversationStore._instance_lock:
        ConversationStore._instance = ConversationStore(db_path)
        return ConversationStore._instance
