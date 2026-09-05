"""Conversation attachments, durable import batches and version/service-bound grants.

Uses the conversation database so creating a batch and its user message is atomic.
Material privacy records deliberately survive deletion of a conversation.
"""
from __future__ import annotations

import json
import uuid

from .conversation_store import ConversationStore, utc_now


class ChatImportStore:
    def __init__(self, conversations: ConversationStore | None = None):
        self.conversations = conversations or ConversationStore.instance()
        with self.conversations._lock, self.conversations._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS chat_import_batches (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    message_id TEXT NOT NULL, request_key TEXT NOT NULL, content TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'uploading', local_only INTEGER NOT NULL DEFAULT 0,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(conversation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS chat_import_files (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES chat_import_batches(id) ON DELETE CASCADE,
                    name TEXT NOT NULL, size INTEGER NOT NULL, material_id TEXT, version INTEGER,
                    state TEXT NOT NULL DEFAULT 'pending', error TEXT, UNIQUE(batch_id, id)
                );
                CREATE TABLE IF NOT EXISTS chat_material_privacy (
                    material_id TEXT PRIMARY KEY, device_scope TEXT NOT NULL, sha256 TEXT,
                    UNIQUE(device_scope, sha256)
                );
                CREATE TABLE IF NOT EXISTS chat_material_grants (
                    material_id TEXT NOT NULL, version INTEGER NOT NULL, service TEXT NOT NULL,
                    created_at TEXT NOT NULL, snapshot_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(material_id, version, service)
                );
                CREATE TABLE IF NOT EXISTS chat_reference_selection (
                    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                    refs_json TEXT NOT NULL DEFAULT '[]', local_only INTEGER NOT NULL DEFAULT 0
                );
            """)
            if "snapshot_id" not in {r[1] for r in db.execute("PRAGMA table_info(chat_material_grants)")}:
                db.execute("ALTER TABLE chat_material_grants ADD COLUMN snapshot_id TEXT NOT NULL DEFAULT ''")

    def scope(self, conversation_id: str) -> str | None:
        with self.conversations._connect() as db:
            row = db.execute("SELECT device_scope FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            return row[0] if row else None

    def create(self, conversation_id: str, key: str, content: str, files: list[dict], local_only: bool = False, *, input_meta=None) -> dict:
        with self.conversations._lock, self.conversations._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM chat_import_batches WHERE conversation_id=? AND request_key=?", (conversation_id, key)).fetchone()
            if existing:
                batch_id = existing[0]
            else:
                batch_id = "imp_" + uuid.uuid4().hex[:16]
                message_id = "msg_" + uuid.uuid4().hex[:16]
                now = utc_now()
                seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
                db.execute("INSERT INTO messages(id,conversation_id,seq,role,content,meta_json,created_at) VALUES(?,?,?,'user',?,?,?)",
                           (message_id, conversation_id, seq, content, json.dumps({"kind": "file_import", "importId": batch_id, **(input_meta or {})}), now))
                db.execute("UPDATE conversations SET message_count=message_count+1,updated_at=?,last_message_at=?,title=CASE WHEN title='' THEN ? ELSE title END, "
                           "metadata_revision=metadata_revision+CASE WHEN status='archived' THEN 1 ELSE 0 END,status='active' WHERE id=?",
                           (now, now, (content or files[0]["name"])[:30], conversation_id))
                db.execute("INSERT INTO chat_import_batches(id,conversation_id,message_id,request_key,content,local_only,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           (batch_id, conversation_id, message_id, key, content, int(local_only), now, now))
                for item in files:
                    db.execute("INSERT INTO chat_import_files(id,batch_id,name,size,material_id,version,state) VALUES(?,?,?,?,?,?,?)",
                               (item["id"], batch_id, item["name"], item.get("size", 0), item.get("materialId"), item.get("version"), "saved" if item.get("materialId") else "pending"))
            db.commit()
        return self.get(batch_id)

    def get(self, batch_id: str) -> dict | None:
        with self.conversations._connect() as db:
            row = db.execute("SELECT * FROM chat_import_batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                return None
            batch = dict(row)
            batch["files"] = [dict(r) for r in db.execute("SELECT * FROM chat_import_files WHERE batch_id=? ORDER BY rowid", (batch_id,))]
            return batch

    def batches(self, conversation_id: str | None = None) -> list[dict]:
        with self.conversations._connect() as db:
            rows = db.execute("SELECT id FROM chat_import_batches" + (" WHERE conversation_id=?" if conversation_id else "") + " ORDER BY created_at", (conversation_id,) if conversation_id else ()).fetchall()
        return [self.get(row[0]) for row in rows]

    def update(self, batch_id: str, state: str, error: str | None = None, *, local_only: bool | None = None):
        with self.conversations._lock, self.conversations._connect() as db:
            db.execute("UPDATE chat_import_batches SET state=?,error=?,updated_at=?,local_only=COALESCE(?,local_only) WHERE id=?",
                       (state, error, utc_now(), int(local_only) if local_only is not None else None, batch_id))

    def file_update(self, file_id: str, state: str, *, material_id: str | None = None, version: int | None = None, error: str | None = None):
        with self.conversations._lock, self.conversations._connect() as db:
            db.execute("UPDATE chat_import_files SET state=?,material_id=COALESCE(?,material_id),version=COALESCE(?,version),error=? WHERE id=?",
                       (state, material_id, version, error, file_id))
            db.execute("UPDATE chat_import_batches SET updated_at=? WHERE id=(SELECT batch_id FROM chat_import_files WHERE id=?)", (utc_now(), file_id))

    def protect(self, material_id: str, scope: str, sha256: str | None = None):
        with self.conversations._lock, self.conversations._connect() as db:
            db.execute("INSERT INTO chat_material_privacy(material_id,device_scope,sha256) VALUES(?,?,?) ON CONFLICT(material_id) DO UPDATE SET sha256=COALESCE(excluded.sha256,chat_material_privacy.sha256)",
                       (material_id, scope, sha256))

    def duplicate(self, scope: str, sha256: str) -> str | None:
        with self.conversations._connect() as db:
            row = db.execute("SELECT material_id FROM chat_material_privacy WHERE device_scope=? AND sha256=?", (scope, sha256)).fetchone()
            return row[0] if row else None

    def protected_ids(self) -> set[str]:
        with self.conversations._connect() as db:
            return {r[0] for r in db.execute("SELECT material_id FROM chat_material_privacy")}

    def forget_hash(self, material_id: str):
        with self.conversations._lock, self.conversations._connect() as db:
            db.execute("UPDATE chat_material_privacy SET sha256=NULL WHERE material_id=?", (material_id,))

    def grant(self, refs: list[dict], service: str):
        from .material_pipeline_store import MaterialPipelineStore
        rows = [(r["materialId"], r["version"], service, utc_now(), (MaterialPipelineStore.instance().current_snapshot(r["materialId"]) or {}).get("snapshot_id", "")) for r in refs]
        with self.conversations._lock, self.conversations._connect() as db:
            db.executemany("INSERT INTO chat_material_grants(material_id,version,service,created_at,snapshot_id) VALUES(?,?,?,?,?) ON CONFLICT(material_id,version,service) DO UPDATE SET snapshot_id=excluded.snapshot_id,created_at=excluded.created_at", rows)

    def allowed(self, ref: dict, service: str, snapshot_id: str | None = None) -> bool:
        from .material_pipeline_store import MaterialPipelineStore
        snapshot = MaterialPipelineStore.instance().current_snapshot(ref["materialId"])
        if not snapshot:
            return False
        with self.conversations._connect() as db:
            return db.execute("SELECT 1 FROM chat_material_grants WHERE material_id=? AND version=? AND service=? AND snapshot_id=?",
                              (ref["materialId"], ref["version"], service, snapshot_id or snapshot["snapshot_id"])).fetchone() is not None

    def refs(self, conversation_id: str) -> list[dict]:
        with self.conversations._connect() as db:
            rows = db.execute("SELECT DISTINCT f.material_id,f.version FROM chat_import_files f JOIN chat_import_batches b ON b.id=f.batch_id WHERE b.conversation_id=? AND f.material_id IS NOT NULL", (conversation_id,)).fetchall()
            selected = self.selection(conversation_id)["refs"]
        return list({(r["materialId"], r["version"]): r for r in ([{"materialId": r[0], "version": r[1]} for r in rows] + selected)}.values())

    def has_imports(self, conversation_id: str) -> bool:
        with self.conversations._connect() as db:
            return db.execute("SELECT 1 FROM chat_import_batches WHERE conversation_id=? LIMIT 1", (conversation_id,)).fetchone() is not None

    def selection(self, conversation_id: str) -> dict:
        with self.conversations._connect() as db:
            row = db.execute("SELECT * FROM chat_reference_selection WHERE conversation_id=?", (conversation_id,)).fetchone()
            return {"refs": json.loads(row["refs_json"]), "localOnly": bool(row["local_only"])} if row else {"refs": [], "localOnly": False}

    def select(self, conversation_id: str, refs: list[dict], local_only: bool):
        with self.conversations._lock, self.conversations._connect() as db:
            db.execute("INSERT INTO chat_reference_selection VALUES(?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET refs_json=excluded.refs_json,local_only=excluded.local_only",
                       (conversation_id, json.dumps(refs), int(local_only)))
