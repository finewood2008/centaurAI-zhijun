"""Local candidate snapshots, separate from messages and formal personal records."""
import json

from .conversation_store import ConversationStore


class ReplyAssistStore:
    def __init__(self, conversations=None):
        self.convs = conversations or ConversationStore.instance()
        with self.convs._lock, self.convs._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS reply_assist_batches (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                request_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                UNIQUE(conversation_id, request_id))""")

    def get(self, ident):
        with self.convs._connect() as db:
            row = db.execute("SELECT payload_json FROM reply_assist_batches WHERE id=?", (ident,)).fetchone()
            return json.loads(row[0]) if row else None

    def latest(self, cid):
        with self.convs._connect() as db:
            row = db.execute("SELECT payload_json FROM reply_assist_batches WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1", (cid,)).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, batch):
        with self.convs._lock, self.convs._connect() as db:
            db.execute("INSERT OR IGNORE INTO reply_assist_batches VALUES(?,?,?,?)",
                       (batch["id"], batch["conversationId"], batch["requestId"], json.dumps(batch, ensure_ascii=False)))
        return self.get(batch["id"])
