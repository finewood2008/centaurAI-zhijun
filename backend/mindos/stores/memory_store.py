"""Local memory admission drafts and interruption budget, not a second profile.

No migration rewrites claims or grants consent. Drafts are never automatically
included in prompts, exports, or USER.md. Their source restrictions survive save.
"""
from __future__ import annotations

import json
import uuid

from .ontology_store import OntologyConflictError, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_policy (
 scope TEXT PRIMARY KEY, mode TEXT NOT NULL, revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_admissions (
 claim_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, topic_id TEXT NOT NULL,
 message_id TEXT NOT NULL, explicit INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_attention (
 conversation_id TEXT NOT NULL, topic_id TEXT NOT NULL, kind TEXT NOT NULL,
 target_id TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
 shown_message_seq INTEGER NOT NULL DEFAULT 0, shown_user_turn INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(conversation_id,topic_id)
);
CREATE TABLE IF NOT EXISTS memory_drafts (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, topic_id TEXT NOT NULL,
 revision INTEGER NOT NULL, entries_json TEXT NOT NULL, status TEXT NOT NULL,
 claim_id TEXT, updated_at TEXT NOT NULL,
 UNIQUE(conversation_id,topic_id)
);
"""


class MemoryStore:
    def __init__(self, ontology):
        self.ontology = ontology
        with ontology._lock, ontology._connect() as db:
            db.executescript(SCHEMA)
            columns = {r[1] for r in db.execute("PRAGMA table_info(memory_attention)")}
            for column in ("shown_message_seq", "shown_user_turn"):
                if column not in columns:
                    db.execute(f"ALTER TABLE memory_attention ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")

    def policy(self, scope):
        with self.ontology._connect() as db:
            row = db.execute("SELECT mode,revision FROM memory_policy WHERE scope=?", (scope,)).fetchone()
        return dict(row) if row else {"mode": "important", "revision": 0}

    def set_policy(self, scope, mode, expected_revision):
        if mode not in ("important", "manual"):
            raise ValueError("未知记忆整理方式")
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT mode,revision FROM memory_policy WHERE scope=?", (scope,)).fetchone()
            revision = row["revision"] if row else 0
            if row and row["mode"] == mode and revision == expected_revision + 1:
                return dict(row)  # same retry, not another policy revision
            if revision != expected_revision:
                raise OntologyConflictError("记忆设置已变化，请刷新后再保存")
            db.execute("INSERT OR REPLACE INTO memory_policy VALUES(?,?,?)", (scope, mode, revision + 1))
        return self.policy(scope)

    def register(self, claim_id, cid, topic_id, message_id, explicit):
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("INSERT OR IGNORE INTO memory_admissions VALUES(?,?,?,?,?,?)",
                       (claim_id, cid, topic_id, message_id, int(explicit), utc_now()))

    def admissions(self, cid, topic_id=None):
        with self.ontology._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM memory_admissions WHERE conversation_id=?"
                + (" AND topic_id=?" if topic_id is not None else "") + " ORDER BY created_at,claim_id",
                (cid, topic_id) if topic_id is not None else (cid,))]

    def slot(self, cid, topic_id):
        with self.ontology._connect() as db:
            row = db.execute("SELECT * FROM memory_attention WHERE conversation_id=? AND topic_id=?", (cid, topic_id)).fetchone()
        return dict(row) if row else None

    def reserve(self, cid, topic_id, kind, target_id, *, message_seq=0, user_turn=0):
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("INSERT OR IGNORE INTO memory_attention "
                       "(conversation_id,topic_id,kind,target_id,consumed,shown_message_seq,shown_user_turn) VALUES(?,?,?,?,0,?,?)",
                       (cid, topic_id, kind, target_id, message_seq, user_turn))
        return self.slot(cid, topic_id)

    def renew(self, cid, topic_id, previous, kind, target_id, *, message_seq, user_turn):
        """Replace only the consumed slot observed by the caller, never another tab's card."""
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("UPDATE memory_attention SET kind=?,target_id=?,consumed=0,shown_message_seq=?,shown_user_turn=? "
                       "WHERE conversation_id=? AND topic_id=? AND consumed=1 AND kind=? AND target_id=?",
                       (kind, target_id, message_seq, user_turn, cid, topic_id, previous["kind"], previous["target_id"]))
        return self.slot(cid, topic_id)

    def initialize_clock(self, cid, topic_id, *, message_seq, user_turn):
        # Old one-slot rows have no timing. Start a cooldown, not an immediate
        # second reminder from previously unseen historical candidates.
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("UPDATE memory_attention SET shown_message_seq=?,shown_user_turn=? "
                       "WHERE conversation_id=? AND topic_id=? AND shown_user_turn=0",
                       (message_seq, user_turn, cid, topic_id))
        return self.slot(cid, topic_id)

    def consume(self, cid, topic_id, kind, target_id):
        with self.ontology._lock, self.ontology._connect() as db:
            cursor = db.execute("UPDATE memory_attention SET consumed=1 WHERE conversation_id=? AND topic_id=? AND kind=? AND target_id=?",
                                (cid, topic_id, kind, target_id))
            if not cursor.rowcount:
                raise OntologyConflictError("这条提醒已变化，请重新查看")

    @staticmethod
    def _draft(row):
        if not row:
            return None
        entries = json.loads(row["entries_json"])
        # A transparent extractive outline, never an invented combined interpretation.
        outline = [entries[i]["content"] for i in sorted({0, *range(max(0, len(entries) - 2), len(entries))})] if entries else []
        return {"id": row["id"], "conversationId": row["conversation_id"], "topicId": row["topic_id"],
                "revision": row["revision"], "status": row["status"], "entries": entries,
                "summary": "；".join(outline), "claimId": row["claim_id"]}

    def draft(self, cid, topic_id=None, draft_id=None):
        with self.ontology._connect() as db:
            if draft_id:
                row = db.execute("SELECT * FROM memory_drafts WHERE conversation_id=? AND id=?", (cid, draft_id)).fetchone()
            else:
                row = db.execute("SELECT * FROM memory_drafts WHERE conversation_id=? AND topic_id=?", (cid, topic_id)).fetchone()
        return self._draft(row)

    def merge_draft(self, cid, topic_id, entries):
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM memory_drafts WHERE conversation_id=? AND topic_id=?", (cid, topic_id)).fetchone()
            if row and row["status"] != "draft":
                return self._draft(row)  # saved/dismissed topic never silently resumes
            previous = json.loads(row["entries_json"]) if row else []
            seen = {(e["messageId"], e["content"]) for e in previous}
            added = [e for e in entries if (e["messageId"], e["content"]) not in seen]
            if row and not added:
                return self._draft(row)
            combined = previous + added
            if len(combined) > 8:
                combined = [combined[0], *combined[-7:]]
            db.execute("INSERT OR REPLACE INTO memory_drafts VALUES(?,?,?,?,?,'draft',NULL,?)",
                       (row["id"] if row else "mem_" + uuid.uuid4().hex[:12], cid, topic_id,
                        row["revision"] + 1 if row else 1, json.dumps(combined, ensure_ascii=False), utc_now()))
        return self.draft(cid, topic_id)

    def finish_draft(self, cid, draft_id, expected_revision, status, claim_id=None):
        with self.ontology._lock, self.ontology._connect() as db:
            cursor = db.execute("UPDATE memory_drafts SET status=?,claim_id=?,revision=revision+1,updated_at=? "
                                "WHERE id=? AND conversation_id=? AND revision=? AND status='draft'",
                                (status, claim_id, utc_now(), draft_id, cid, expected_revision))
            if not cursor.rowcount:
                raise OntologyConflictError("小结已经变化，请重新核对")
        return self.draft(cid, draft_id=draft_id)

    def remove_conversation(self, cid):
        # Draft text has the conversation's lifecycle; formal claims are retained.
        with self.ontology._lock, self.ontology._connect() as db:
            for table in ("memory_drafts", "memory_admissions", "memory_attention"):
                db.execute(f"DELETE FROM {table} WHERE conversation_id=?", (cid,))
