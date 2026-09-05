"""User-managed ongoing matters and editable work products in the local ontology DB.

These records are not Claims, never alter the charter, and never grant consent.
The action ledger supplies both idempotency and an immutable edit audit.
"""
from __future__ import annotations

import json
import uuid

from .alignment_store import digest
from .ontology_store import OntologyConflictError, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS work_matters (
 id TEXT PRIMARY KEY, device_scope TEXT NOT NULL, title TEXT NOT NULL,
 goal TEXT NOT NULL, context TEXT NOT NULL, next_step TEXT NOT NULL,
 outcome TEXT NOT NULL, status TEXT NOT NULL, decision_id TEXT,
 revision INTEGER NOT NULL, sources_json TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_matters_scope ON work_matters(device_scope,status,updated_at);
CREATE TABLE IF NOT EXISTS work_matter_bindings (
 conversation_id TEXT PRIMARY KEY, device_scope TEXT NOT NULL, matter_id TEXT,
 revision INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_artifacts (
 id TEXT PRIMARY KEY, matter_id TEXT NOT NULL, device_scope TEXT NOT NULL,
 title TEXT NOT NULL, kind TEXT NOT NULL, markdown TEXT NOT NULL,
 user_edited INTEGER NOT NULL, revision INTEGER NOT NULL,
 source_message_id TEXT NOT NULL, source_conversation_id TEXT NOT NULL,
 sources_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_artifacts_matter ON work_artifacts(device_scope,matter_id,updated_at);
CREATE TABLE IF NOT EXISTS work_actions (
 device_scope TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 entity_kind TEXT NOT NULL, entity_id TEXT NOT NULL, revision INTEGER NOT NULL,
 result_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(device_scope,request_id)
);
"""


def matter_text(item):
    labels = (("title", "事项"), ("goal", "希望达成"), ("context", "现状与约束"),
              ("nextStep", "用户记录的下一步"), ("outcome", "用户记录的实际结果"))
    state = {"active": "正在推进", "paused": "已暂停", "completed": "已完成"}[item["status"]]
    return f"这是用户维护的事项记录，不是长期人格；仅用户记录的实际结果代表已发生的结果。\n事项状态：{state}\n" + "\n".join(
        f"{label}：{item[key]}" for key, label in labels if item.get(key))


def source_version(item):
    return digest([item["id"], item["revision"], item.get("sources") or []])


class MattersStore:
    def __init__(self, ontology, conversations=None):
        self.ontology, self.conversations = ontology, conversations
        with ontology._lock, ontology._connect() as db:
            db.executescript(SCHEMA)

    def _matter(self, row, db):
        if not row:
            return None
        latest = None
        if self.conversations:
            for binding in db.execute("SELECT conversation_id FROM work_matter_bindings WHERE matter_id=? AND device_scope=? ORDER BY updated_at DESC,conversation_id", (row["id"], row["device_scope"])):
                with self.conversations._connect() as conversations:
                    conversation = conversations.execute("SELECT device_scope FROM conversations WHERE id=?", (binding[0],)).fetchone()
                if conversation and conversation[0] == row["device_scope"]:
                    latest = binding[0]
                    break
        return {"id": row["id"], "deviceScope": row["device_scope"], "title": row["title"],
                "goal": row["goal"], "context": row["context"], "nextStep": row["next_step"],
                "outcome": row["outcome"], "status": row["status"], "decisionId": row["decision_id"],
                "revision": row["revision"], "sources": json.loads(row["sources_json"]),
                "conversationId": latest, "createdAt": row["created_at"], "updatedAt": row["updated_at"]}

    @staticmethod
    def _artifact(row):
        if not row:
            return None
        return {"id": row["id"], "matterId": row["matter_id"], "deviceScope": row["device_scope"],
                "title": row["title"], "kind": row["kind"], "markdown": row["markdown"],
                "userEdited": bool(row["user_edited"]), "revision": row["revision"],
                "sourceMessageId": row["source_message_id"], "sourceConversationId": row["source_conversation_id"],
                "sources": json.loads(row["sources_json"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"]}

    @staticmethod
    def _prior(db, scope, request_id, fingerprint):
        old = db.execute("SELECT fingerprint,result_json FROM work_actions WHERE device_scope=? AND request_id=?", (scope, request_id)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise OntologyConflictError("这次操作编号已用于其他内容，请重新提交")
            return json.loads(old[1])
        return None

    @staticmethod
    def _record(db, scope, request_id, fingerprint, kind, ident, revision, result):
        db.execute("INSERT INTO work_actions VALUES(?,?,?,?,?,?,?,?)", (scope, request_id, fingerprint, kind, ident, revision,
                   json.dumps(result, ensure_ascii=False), utc_now()))
        return result

    def get(self, ident, scope):
        with self.ontology._connect() as db:
            return self._matter(db.execute("SELECT * FROM work_matters WHERE id=? AND device_scope=?", (ident, scope)).fetchone(), db)

    def list(self, scope, status="active"):
        with self.ontology._connect() as db:
            sql = "SELECT * FROM work_matters WHERE device_scope=?"
            params = [scope]
            if status != "all":
                sql += " AND status=?"
                params.append(status)
            return [self._matter(row, db) for row in db.execute(sql + " ORDER BY updated_at DESC,id", params).fetchall()]

    def binding(self, cid, scope):
        with self.ontology._connect() as db:
            row = db.execute("SELECT * FROM work_matter_bindings WHERE conversation_id=? AND device_scope=?", (cid, scope)).fetchone()
            matter = self._matter(db.execute("SELECT * FROM work_matters WHERE id=? AND device_scope=?", (row["matter_id"], scope)).fetchone(), db) if row and row["matter_id"] else None
        return {"matter": matter, "bindingRevision": row["revision"] if row else 0}

    def create(self, scope, payload, request_id, cid=None):
        fingerprint = digest(["create_matter", payload, cid])
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._prior(db, scope, request_id, fingerprint)
            if previous is not None:
                return previous
            if cid and db.execute("SELECT 1 FROM work_matter_bindings WHERE conversation_id=? AND matter_id IS NOT NULL", (cid,)).fetchone():
                raise OntologyConflictError("这段对话已关联另一件事，请先切换或解除关联")
            ident, now = "matter_" + uuid.uuid4().hex[:12], utc_now()
            db.execute("INSERT INTO work_matters VALUES(?,?,?,?,?,?,?,?,NULL,1,'[]',?,?)", (ident, scope, payload["title"],
                payload.get("goal", ""), payload.get("context", ""), payload.get("nextStep", ""), "", "active", now, now))
            if cid:
                db.execute("INSERT INTO work_matter_bindings VALUES(?,?,?,1,?) ON CONFLICT(conversation_id) DO UPDATE SET matter_id=excluded.matter_id,revision=work_matter_bindings.revision+1,updated_at=excluded.updated_at", (cid, scope, ident, now))
            item = self._matter(db.execute("SELECT * FROM work_matters WHERE id=?", (ident,)).fetchone(), db)
            return self._record(db, scope, request_id, fingerprint, "matter", ident, 1, item)

    def update(self, ident, scope, changes, revision, request_id, sources=None):
        fingerprint = digest(["edit_matter", ident, changes, revision])
        mapping = {"title": "title", "goal": "goal", "context": "context", "nextStep": "next_step", "outcome": "outcome", "status": "status", "decisionId": "decision_id"}
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._prior(db, scope, request_id, fingerprint)
            if previous is not None:
                return previous
            row = db.execute("SELECT * FROM work_matters WHERE id=? AND device_scope=?", (ident, scope)).fetchone()
            if not row or row["revision"] != revision:
                raise OntologyConflictError("这件事已更新，请刷新后再保存；你的输入仍保留")
            parents = list({digest(r): r for r in [*json.loads(row["sources_json"]), *(sources or [])]}.values())
            sets = [f"{mapping[key]}=?" for key in changes]
            db.execute("UPDATE work_matters SET " + ",".join([*sets, "sources_json=?", "revision=revision+1", "updated_at=?"]) + " WHERE id=? AND device_scope=?", [*changes.values(), json.dumps(parents, ensure_ascii=False), utc_now(), ident, scope])
            item = self._matter(db.execute("SELECT * FROM work_matters WHERE id=?", (ident,)).fetchone(), db)
            return self._record(db, scope, request_id, fingerprint, "matter", ident, item["revision"], item)

    def bind(self, cid, scope, ident, revision, request_id):
        fingerprint = digest(["bind_matter", cid, ident, revision])
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._prior(db, scope, request_id, fingerprint)
            if previous is not None:
                return previous
            matter = db.execute("SELECT * FROM work_matters WHERE id=? AND device_scope=?", (ident, scope)).fetchone() if ident else None
            if ident and not matter:
                raise OntologyConflictError("这件事不存在或不属于当前设备")
            old = db.execute("SELECT * FROM work_matter_bindings WHERE conversation_id=? AND device_scope=?", (cid, scope)).fetchone()
            if (old["revision"] if old else 0) != revision:
                raise OntologyConflictError("对话关联已变化，请刷新后再选择")
            db.execute("INSERT INTO work_matter_bindings VALUES(?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET matter_id=excluded.matter_id,revision=excluded.revision,updated_at=excluded.updated_at", (cid, scope, ident, revision + 1, utc_now()))
            result = {"matter": self._matter(matter, db), "bindingRevision": revision + 1}
            return self._record(db, scope, request_id, fingerprint, "binding", cid, revision + 1, result)

    def artifact(self, ident, scope):
        with self.ontology._connect() as db:
            return self._artifact(db.execute("SELECT * FROM work_artifacts WHERE id=? AND device_scope=?", (ident, scope)).fetchone())

    def artifacts(self, ident, scope):
        with self.ontology._connect() as db:
            return [self._artifact(row) for row in db.execute("SELECT * FROM work_artifacts WHERE matter_id=? AND device_scope=? ORDER BY updated_at DESC,id", (ident, scope))]

    def save_artifact(self, ident, scope, payload, message, source, request_id):
        fingerprint = digest(["save_artifact", ident, payload, message["id"]])
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._prior(db, scope, request_id, fingerprint)
            if previous is not None:
                return previous
            aid, now = "artifact_" + uuid.uuid4().hex[:12], utc_now()
            db.execute("INSERT INTO work_artifacts VALUES(?,?,?,?,?,?,0,1,?,?,?,?,?)", (aid, ident, scope, payload["title"], payload["kind"], payload["markdown"], message["id"], message["conversationId"], json.dumps([source], ensure_ascii=False), now, now))
            item = self._artifact(db.execute("SELECT * FROM work_artifacts WHERE id=?", (aid,)).fetchone())
            return self._record(db, scope, request_id, fingerprint, "artifact", aid, 1, item)

    def edit_artifact(self, ident, scope, changes, revision, request_id):
        fingerprint = digest(["edit_artifact", ident, changes, revision])
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._prior(db, scope, request_id, fingerprint)
            if previous is not None:
                return previous
            row = db.execute("SELECT * FROM work_artifacts WHERE id=? AND device_scope=?", (ident, scope)).fetchone()
            if not row or row["revision"] != revision:
                raise OntologyConflictError("这份成果已更新，请刷新后再保存；你的修改仍保留")
            # Editing text never detaches its original privacy ancestry.
            edited = bool(row["user_edited"]) or ("markdown" in changes and changes["markdown"] != row["markdown"])
            db.execute("UPDATE work_artifacts SET " + ",".join([*[f"{key}=?" for key in changes], "user_edited=?", "revision=revision+1", "updated_at=?"]) + " WHERE id=? AND device_scope=?", [*changes.values(), int(edited), utc_now(), ident, scope])
            item = self._artifact(db.execute("SELECT * FROM work_artifacts WHERE id=?", (ident,)).fetchone())
            return self._record(db, scope, request_id, fingerprint, "artifact", ident, item["revision"], item)

    def history(self, kind, ident, scope):
        with self.ontology._connect() as db:
            return [{"revision": row["revision"], "at": row["created_at"], "record": json.loads(row["result_json"])} for row in db.execute("SELECT * FROM work_actions WHERE device_scope=? AND entity_kind=? AND entity_id=? ORDER BY revision", (scope, kind, ident))]
