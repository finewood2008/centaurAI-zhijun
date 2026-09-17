"""Evidence-linked observations in ontology.db; never confirmed Claims."""
from __future__ import annotations

import json
import uuid

from .alignment_store import digest
from .ontology_store import OntologyConflictError, OntologyError, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS reflections (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, conversation_id TEXT NOT NULL,
 message_id TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 surfaced_at TEXT, feedback_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS reflections_scope ON reflections(scope,created_at DESC);
CREATE TABLE IF NOT EXISTS reflection_reviews (
 reflection_id TEXT NOT NULL, request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(reflection_id,request_id)
);
"""


def decode(row):
    if not row:
        return None
    return {**json.loads(row["payload_json"]), "id": row["id"], "scope": row["scope"],
            "conversationId": row["conversation_id"], "messageId": row["message_id"],
            "status": row["status"], "revision": row["revision"], "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "lastSurfacedAt": row["surfaced_at"],
            "feedback": json.loads(row["feedback_json"])}


class ReflectionStore:
    def __init__(self, ontology):
        self.ontology = ontology
        with ontology._lock, ontology._connect() as db:
            db.executescript(SCHEMA)

    def get(self, ident):
        with self.ontology._connect() as db:
            return decode(db.execute("SELECT * FROM reflections WHERE id=?", (ident,)).fetchone())

    def list(self, scope, limit=100):
        with self.ontology._connect() as db:
            return [decode(row) for row in db.execute(
                "SELECT * FROM reflections WHERE scope=? ORDER BY created_at DESC,id LIMIT ?", (scope, limit))]

    def known_topics(self, scope):
        # Keep the suppression ledger beyond the bounded display/history page.
        with self.ontology._connect() as db:
            return [r[0] for r in db.execute(
                "SELECT json_extract(payload_json,'$.topic') FROM reflections WHERE scope=?", (scope,))]

    def create(self, scope, cid, message_id, payload):
        ident, now = "refl_" + uuid.uuid4().hex[:16], utc_now()
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("INSERT INTO reflections(id,scope,conversation_id,message_id,payload_json,status,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,'candidate',?,?)", (ident, scope, cid, message_id, json.dumps(payload, ensure_ascii=False), now, now))
        return self.get(ident)

    def surface(self, ident):
        with self.ontology._lock, self.ontology._connect() as db:
            now = utc_now()
            db.execute("UPDATE reflections SET status='surfaced',surfaced_at=?,updated_at=? "
                       "WHERE id=? AND status='candidate'", (now, now, ident))
        return self.get(ident)

    def review(self, ident, scope, payload):
        action, note = payload["action"], payload.get("note", "").strip()
        if action not in ("accepted", "contextual", "rejected", "observing", "retired"):
            raise OntologyError("请选择如何理解这条照见")
        if action == "contextual" and not note:
            raise OntologyError("请补充适用的情境，或直接说说哪里需要修正")
        hashed = digest(payload)
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reflections WHERE id=? AND scope=?", (ident, scope)).fetchone()
            if not row:
                raise OntologyError("照见不存在")
            old = db.execute("SELECT payload_hash FROM reflection_reviews WHERE reflection_id=? AND request_id=?",
                             (ident, payload["requestId"])).fetchone()
            if old:
                if old[0] != hashed:
                    raise OntologyConflictError("这次反馈已经处理，请刷新后再修改")
                return decode(row)
            if row["revision"] != payload["expectedRevision"]:
                raise OntologyConflictError("照见已更新，请先核对最新内容")
            if row["status"] == "retired":
                raise OntologyConflictError("这条照见已撤回")
            now = utc_now()
            feedback = {"action": action, "note": note, "at": now}
            db.execute("UPDATE reflections SET status=?,feedback_json=?,revision=revision+1,updated_at=?,"
                       "surfaced_at=COALESCE(surfaced_at,?) WHERE id=?",
                       (action, json.dumps(feedback, ensure_ascii=False), now, now, ident))
            db.execute("INSERT INTO reflection_reviews VALUES(?,?,?,?,?)",
                       (ident, payload["requestId"], hashed, json.dumps(feedback, ensure_ascii=False), now))
        return self.get(ident)

    def history(self, ident):
        with self.ontology._connect() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT payload_json FROM reflection_reviews WHERE reflection_id=? ORDER BY created_at, rowid", (ident,))]
