"""Conversation attachments, durable import batches and version/service-bound grants.

Uses the conversation database so creating a batch and its user message is atomic.
Material privacy records deliberately survive deletion of a conversation.
"""
from __future__ import annotations

import json
import os
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
                    error TEXT, rag_prompt_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(conversation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS chat_import_files (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES chat_import_batches(id) ON DELETE CASCADE,
                    name TEXT NOT NULL, size INTEGER NOT NULL, material_id TEXT, version INTEGER,
                    state TEXT NOT NULL DEFAULT 'pending', error TEXT, job_id TEXT, status_url TEXT,
                    UNIQUE(batch_id, id)
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
            file_columns = {r[1] for r in db.execute("PRAGMA table_info(chat_import_files)")}
            if "job_id" not in file_columns:
                db.execute("ALTER TABLE chat_import_files ADD COLUMN job_id TEXT")
            if "status_url" not in file_columns:
                db.execute("ALTER TABLE chat_import_files ADD COLUMN status_url TEXT")
            batch_columns = {r[1] for r in db.execute("PRAGMA table_info(chat_import_batches)")}
            if "rag_prompt_json" not in batch_columns:
                db.execute("ALTER TABLE chat_import_batches ADD COLUMN rag_prompt_json TEXT")

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

    def update(self, batch_id: str, state: str, error: str | None = None, *,
               local_only: bool | None = None, rag_prompt: dict | None = None):
        if state == "queued":
            from zhijun_worker.background import register
            register(batch_id, "chat")
        with self.conversations._lock, self.conversations._connect() as db:
            db.execute("UPDATE chat_import_batches SET state=?,error=?,rag_prompt_json=?,updated_at=?,"
                       "local_only=COALESCE(?,local_only) WHERE id=?",
                       (state, error, json.dumps(rag_prompt, ensure_ascii=False) if rag_prompt else None,
                        utc_now(), int(local_only) if local_only is not None else None, batch_id))

    def file_update(self, file_id: str, state: str, *, material_id: str | None = None,
                    version: int | None = None, error: str | None = None,
                    job_id: str | None = None, status_url: str | None = None):
        with self.conversations._lock, self.conversations._connect() as db:
            db.execute("UPDATE chat_import_files SET state=?,material_id=COALESCE(?,material_id),"
                       "version=COALESCE(?,version),error=?,job_id=COALESCE(?,job_id),"
                       "status_url=COALESCE(?,status_url) WHERE id=?",
                       (state, material_id, version, error, job_id, status_url, file_id))
            db.execute("UPDATE chat_import_batches SET updated_at=? WHERE id=(SELECT batch_id FROM chat_import_files WHERE id=?)", (utc_now(), file_id))

    def material(self, material_id: str, scope: str) -> dict | None:
        """Return only Zhijun-owned metadata; material content stays in RAG V2."""
        with self.conversations._connect() as db:
            row = db.execute(
                "SELECT f.* FROM chat_import_files f JOIN chat_import_batches b ON b.id=f.batch_id "
                "JOIN conversations c ON c.id=b.conversation_id "
                "WHERE f.material_id=? AND c.device_scope=? ORDER BY f.rowid DESC LIMIT 1",
                (material_id, scope),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        return {"materialId": value["material_id"], "versionNumber": value["version"],
                "fileName": value["name"], "status": value["state"],
                "jobId": value.get("job_id"), "statusUrl": value.get("status_url")}

    def protect(self, material_id: str, scope: str, sha256: str | None = None):
        with self.conversations._lock, self.conversations._connect() as db:
            db.execute("INSERT INTO chat_material_privacy(material_id,device_scope,sha256) VALUES(?,?,?) ON CONFLICT(material_id) DO UPDATE SET sha256=COALESCE(excluded.sha256,chat_material_privacy.sha256)",
                       (material_id, scope, sha256))

    def duplicate(self, scope: str, sha256: str) -> str | None:
        with self.conversations._connect() as db:
            row = db.execute("SELECT material_id FROM chat_material_privacy WHERE device_scope=? AND sha256=?", (scope, sha256)).fetchone()
            return row[0] if row else None

    def protected_ids(self, scope: str | None = None) -> set[str]:
        with self.conversations._connect() as db:
            if scope is None:
                rows = db.execute("SELECT material_id FROM chat_material_privacy")
            else:
                rows = db.execute(
                    "SELECT material_id FROM chat_material_privacy WHERE device_scope=?",
                    (scope,),
                )
            return {r[0] for r in rows}

    def forget_hash(self, material_id: str):
        with self.conversations._lock, self.conversations._connect() as db:
            db.execute("UPDATE chat_material_privacy SET sha256=NULL WHERE material_id=?", (material_id,))

    def _snapshot(self, ref):
        if os.environ.get("ZHIJUN_WORKSPACE_ID"):
            with self.conversations._connect() as db:
                row = db.execute("SELECT device_scope FROM chat_material_privacy WHERE material_id=?", (ref["materialId"],)).fetchone()
            value = self.material(ref["materialId"], row[0]) if row else None
            if not value or value["versionNumber"] != ref["version"]:
                return None
            # Online-model egress consent is separate from Data Agent's
            # sensitive-delivery confirmation. The evidenceRef is revalidated
            # immediately before final prompt assembly.
            return {"snapshot_id": f"rag-v2:{ref['materialId']}:{ref['version']}"}
        from .material_pipeline_store import MaterialPipelineStore
        return MaterialPipelineStore.instance().current_snapshot(ref["materialId"])

    def grant(self, refs: list[dict], service: str):
        rows = []
        for ref in refs:
            snapshot = self._snapshot(ref)
            if os.environ.get("ZHIJUN_WORKSPACE_ID") and not snapshot:
                from fastapi import HTTPException
                raise HTTPException(409, {"code": "ATTACHMENT_VERSION_CHANGED", "detail": "文件正文或版本已变化"})
            rows.append((ref["materialId"], ref["version"], service, utc_now(), (snapshot or {}).get("snapshot_id", "")))
        with self.conversations._lock, self.conversations._connect() as db:
            db.executemany("INSERT INTO chat_material_grants(material_id,version,service,created_at,snapshot_id) VALUES(?,?,?,?,?) ON CONFLICT(material_id,version,service) DO UPDATE SET snapshot_id=excluded.snapshot_id,created_at=excluded.created_at", rows)

    def allowed(self, ref: dict, service: str, snapshot_id: str | None = None) -> bool:
        snapshot = self._snapshot(ref)
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
