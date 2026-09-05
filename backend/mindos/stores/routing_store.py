"""Local consent ledger. No migration infers consent from legacy privacy flags."""
from __future__ import annotations

import json
import time

from .alignment_store import digest
from .ontology_store import utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS context_lookup_stages (
 conversation_id TEXT NOT NULL, request_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(conversation_id,request_id)
);
CREATE TABLE IF NOT EXISTS routing_modes (
 owner TEXT PRIMARY KEY, mode TEXT NOT NULL, service TEXT NOT NULL DEFAULT '',
 cutoff INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS routing_grants (
 scope TEXT NOT NULL, source_key TEXT NOT NULL, version TEXT NOT NULL,
 service TEXT NOT NULL, purpose TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(scope,source_key,version,service,purpose)
);
CREATE TABLE IF NOT EXISTS routing_previews (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, payload_json TEXT NOT NULL,
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS routing_audits (
 id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
 purpose TEXT NOT NULL, preview_id TEXT NOT NULL, sources_json TEXT NOT NULL,
 provider TEXT NOT NULL, model TEXT NOT NULL, external INTEGER NOT NULL,
 state TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, usage_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routing_pending (
 conversation_id TEXT NOT NULL, task_key TEXT NOT NULL, preview_id TEXT NOT NULL,
 detail TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(conversation_id,task_key)
);
CREATE TABLE IF NOT EXISTS routing_auto_consent (
 scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL, service TEXT NOT NULL,
 service_name TEXT NOT NULL, include_files INTEGER NOT NULL,
 purposes_json TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routing_auto_exclusions (
 scope TEXT NOT NULL, service TEXT NOT NULL, source_key TEXT NOT NULL,
 PRIMARY KEY(scope,service,source_key)
);
CREATE TABLE IF NOT EXISTS routing_auto_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
 policy_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routing_handling (
 scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL, action TEXT NOT NULL,
 service TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routing_tasks (
 conversation_id TEXT PRIMARY KEY, task TEXT NOT NULL
);
"""


class RoutingStore:
    def __init__(self, ontology):
        self.ontology = ontology
        with ontology._lock, ontology._connect() as db:
            db.executescript(SCHEMA)
            if "include_charter" not in {row[1] for row in db.execute("PRAGMA table_info(routing_auto_consent)")}:
                db.execute("ALTER TABLE routing_auto_consent ADD COLUMN include_charter INTEGER NOT NULL DEFAULT 0")

    def mode(self, owner):
        with self.ontology._connect() as db:
            row = db.execute("SELECT * FROM routing_modes WHERE owner=?", (owner,)).fetchone()
            return dict(row) if row else {"owner": owner, "mode": "legacy", "service": "", "cutoff": 0, "revision": 0}

    def task(self, conversation_id):
        with self.ontology._connect() as db:
            row = db.execute("SELECT task FROM routing_tasks WHERE conversation_id=?", (conversation_id,)).fetchone()
        return row[0] if row else ""

    def set_task(self, conversation_id, task):
        # Only an explicit UI entry creates this control; never reconstruct it
        # from protected history. No personal text or authorization lives here.
        if task != "charter":
            raise ValueError("未知对话任务")
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("INSERT OR REPLACE INTO routing_tasks VALUES(?,?)", (conversation_id, task))

    def set_mode(self, owner, mode, service, cutoff=0):
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("INSERT INTO routing_modes(owner,mode,service,cutoff) VALUES(?,?,?,?) "
                       "ON CONFLICT(owner) DO UPDATE SET mode=excluded.mode,service=excluded.service,"
                       "cutoff=excluded.cutoff,revision=routing_modes.revision+1", (owner, mode, service, cutoff))
        return self.mode(owner)

    def granted(self, scope, source, service, purpose):
        with self.ontology._connect() as db:
            return db.execute("SELECT 1 FROM routing_grants WHERE scope=? AND source_key=? AND version=? AND service=? AND purpose=?",
                              (scope, source["key"], source["version"], service, purpose)).fetchone() is not None

    def handling(self, scope):
        with self.ontology._connect() as db:
            row = db.execute("SELECT * FROM routing_handling WHERE scope=?", (scope,)).fetchone()
        return {"enabled": bool(row["enabled"]), "action": row["action"], "service": row["service"],
                "revision": row["revision"]} if row else {"enabled": False, "action": "omit", "service": "", "revision": 0}

    def set_handling(self, scope, *, enabled, action, service, expected_revision):
        if action not in ("omit", "local"):
            raise ValueError("未知默认处理方式")
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM routing_handling WHERE scope=?", (scope,)).fetchone()
            if (row[0] if row else 0) != expected_revision:
                raise ValueError("默认处理方式已变化，请刷新后再保存")
            db.execute("INSERT OR REPLACE INTO routing_handling VALUES(?,?,?,?,?,?)",
                       (scope, int(enabled), action, service, expected_revision + 1, utc_now()))
        return self.handling(scope)

    def grant(self, scope, sources, service, purpose):
        with self.ontology._lock, self.ontology._connect() as db:
            db.executemany("INSERT OR IGNORE INTO routing_grants VALUES(?,?,?,?,?,?)",
                           [(scope, s["key"], s["version"], service, purpose, utc_now()) for s in sources])

    def revoke(self, scope, key=None):
        with self.ontology._lock, self.ontology._connect() as db:
            if key:
                db.execute("DELETE FROM routing_grants WHERE scope=? AND source_key=?", (scope, key))
                db.execute("INSERT OR IGNORE INTO routing_auto_exclusions SELECT scope,service,? FROM routing_auto_consent WHERE scope=?", (key, scope))
            else:
                db.execute("DELETE FROM routing_grants WHERE scope=?", (scope,))
                db.execute("UPDATE routing_auto_consent SET enabled=0,revision=revision+1,updated_at=? WHERE scope=?", (utc_now(), scope))
            self._policy_audit(db, scope)

    def policy(self, scope):
        with self.ontology._connect() as db:
            row = db.execute("SELECT * FROM routing_auto_consent WHERE scope=?", (scope,)).fetchone()
            if not row:
                return {"enabled": False, "service": "", "serviceName": "", "includeFiles": False, "includeCharter": False,
                        "purposes": [], "revision": 0, "exclusions": []}
            return {"enabled": bool(row["enabled"]), "service": row["service"], "serviceName": row["service_name"],
                    "includeFiles": bool(row["include_files"]), "includeCharter": bool(row["include_charter"]), "purposes": json.loads(row["purposes_json"]),
                    "revision": row["revision"], "updatedAt": row["updated_at"],
                    "exclusions": [r[0] for r in db.execute("SELECT source_key FROM routing_auto_exclusions WHERE scope=? AND service=?", (scope, row["service"]))]}

    @staticmethod
    def _policy_audit(db, scope):
        row = db.execute("SELECT * FROM routing_auto_consent WHERE scope=?", (scope,)).fetchone()
        if row:
            snapshot = dict(row)
            snapshot["exclusions"] = [r[0] for r in db.execute("SELECT source_key FROM routing_auto_exclusions WHERE scope=? AND service=?", (scope, row["service"]))]
            db.execute("INSERT INTO routing_auto_history(scope,policy_json,created_at) VALUES(?,?,?)", (scope, json.dumps(snapshot), utc_now()))

    def set_policy(self, scope, *, enabled, service, service_name, include_files, purposes, expected_revision, include_charter=False):
        # A stale settings tab must never silently re-enable a revoked policy.
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM routing_auto_consent WHERE scope=?", (scope,)).fetchone()
            revision = row[0] if row else 0
            if revision != expected_revision:
                raise ValueError("默认授权设置已变化，请刷新后核对")
            db.execute("INSERT OR REPLACE INTO routing_auto_consent(scope,enabled,service,service_name,include_files,purposes_json,revision,updated_at,include_charter) VALUES(?,?,?,?,?,?,?,?,?)",
                       (scope, int(enabled), service, service_name, int(include_files), json.dumps(purposes), revision + 1, utc_now(), int(include_charter)))
            self._policy_audit(db, scope)
        return self.policy(scope)

    def preview(self, payload):
        token = digest(payload)
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("INSERT OR REPLACE INTO routing_previews VALUES(?,?,?,?)",
                       (token, payload["conversationId"], json.dumps(payload, ensure_ascii=False), time.time()))
            db.execute("DELETE FROM routing_previews WHERE created_at<?", (time.time() - 86400,))
        return {**payload, "revision": token}

    def get_preview(self, token, conversation_id, *, include_expired=False):
        with self.ontology._connect() as db:
            row = db.execute("SELECT payload_json FROM routing_previews WHERE id=? AND conversation_id=? AND created_at>?",
                             (token, conversation_id, 0 if include_expired else time.time() - 3600)).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def _conversation_jobs(db, conversation_id, task=None):
        # Scope before ranking, never take a global latest-N slice. A newer
        # completed/active attempt supersedes an older pause for that owner.
        where = "json_valid(payload_json) AND json_extract(payload_json,'$.conversationId')=?"
        args = [conversation_id]
        if conversation_id.startswith("scope:"):
            where = "json_valid(payload_json) AND kind IN ('home_brief','consolidate') AND COALESCE(json_extract(payload_json,'$.scope'),'global')=?"
            args = [conversation_id[6:]]
        if task:
            where += " AND kind=?"
            args.append(task)
        return db.execute("SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY kind,owner_id ORDER BY created_at DESC,rowid DESC) AS attempt_rank FROM ontology_jobs WHERE " + where + ") WHERE attempt_rank=1 ORDER BY created_at,job_id", args).fetchall()

    def conversation_jobs(self, conversation_id, task=None):
        with self.ontology._connect() as db:
            return [self.ontology._job(row) for row in self._conversation_jobs(db, conversation_id, task)]

    def paused_jobs(self, conversation_id, task=None):
        return [job for job in self.conversation_jobs(conversation_id, task)
                if job["state"] == "done" and (job.get("result") or {}).get("state") == "paused"]

    def resume_jobs(self, conversation_id, task, *, local_only=False):
        """Requeue every current pause once, retaining its original owner/job ID.

        This does not grant anything: the worker reconstructs and checks the
        current request again. The active-owner unique index and transaction
        keep concurrent clicks from creating another generation for one turn.
        """
        now, resumed = time.time(), []
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in self._conversation_jobs(db, conversation_id, task):
                job = self.ontology._job(row)
                if job["state"] != "done" or (job.get("result") or {}).get("state") != "paused":
                    continue
                if db.execute("SELECT 1 FROM ontology_jobs WHERE kind=? AND owner_id=? AND state IN ('queued','running')", (task, job["ownerId"])).fetchone():
                    continue
                payload = {**job["payload"], "localOnly": bool(local_only)}
                changed = db.execute("UPDATE ontology_jobs SET state='queued',payload_json=?,priority=MAX(priority,8),attempts=0,lease_owner=NULL,lease_until=NULL,finished_at=NULL,updated_at=? WHERE job_id=? AND state='done'",
                                     (json.dumps(payload, ensure_ascii=False), now, job["jobId"]))
                if changed.rowcount:
                    resumed.append(job["jobId"])
            if resumed:
                db.execute("DELETE FROM routing_pending WHERE conversation_id=? AND task_key=?", (conversation_id, task))
        return resumed

    def pending(self, conversation_id, key=None, preview=None, detail=""):
        with self.ontology._lock, self.ontology._connect() as db:
            if key is not None:
                if preview:
                    db.execute("INSERT OR REPLACE INTO routing_pending VALUES(?,?,?,?,?)",
                               (conversation_id, key, preview, detail, utc_now()))
                else:
                    db.execute("DELETE FROM routing_pending WHERE conversation_id=? AND task_key=?", (conversation_id, key))
            return [dict(r) for r in db.execute("SELECT * FROM routing_pending WHERE conversation_id=?", (conversation_id,))]

    def audit(self, preview, provider, state, elapsed, usage=None):
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("INSERT INTO routing_audits(conversation_id,purpose,preview_id,sources_json,provider,model,external,state,elapsed_ms,usage_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (preview["conversationId"], preview["purpose"], preview["revision"],
                 json.dumps(preview["sources"], ensure_ascii=False), provider.name, provider.model, int(provider.external),
                 state, int(elapsed * 1000), json.dumps(usage), utc_now()))
