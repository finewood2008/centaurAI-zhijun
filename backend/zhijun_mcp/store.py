"""Box-local grants and receipts. No query, result body, OAuth token or DE secret in audit."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .models import AccessError, GrantSpec, Principal, Subject


def opaque(prefix):
    return prefix + secrets.token_hex(20)


class AccessStore:
    def __init__(self, path: Path, subject: Subject, audience: str, *, clock=time.time):
        self.path, self.subject, self.audience, self.clock = Path(path), subject, audience, clock
        self.lock = threading.RLock()
        if not self.path.is_absolute() or any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError("MCP_STORE_PATH_INVALID")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if self.path.stat().st_mode & 0o077:
            raise ValueError("MCP_STORE_PERMISSIONS_INVALID")
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, agent TEXT NOT NULL,
                    spec TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS legacy (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS denials (id TEXT PRIMARY KEY, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence_refs (id TEXT PRIMARY KEY, grant_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, grant_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (id TEXT PRIMARY KEY, agent TEXT NOT NULL,
                    grant_id TEXT NOT NULL, operation TEXT NOT NULL, resources TEXT NOT NULL,
                    delivery TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL);
            """)
            identity = json.dumps(subject.model_dump(), sort_keys=True)
            row = db.execute("SELECT value FROM meta WHERE key='subject'").fetchone()
            if row and row[0] != identity:
                raise ValueError("MCP_WORKSPACE_MISMATCH")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('subject',?)", (identity,))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('enabled','false')")

    @contextmanager
    def db(self):
        with self.lock:
            conn = sqlite3.connect(self.path, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=FULL")
            try:
                with conn:
                    yield conn
            except sqlite3.Error:
                raise AccessError("ACCESS_STORAGE_UNAVAILABLE", 503) from None
            finally:
                conn.close()

    def enabled(self):
        with self.db() as db:
            return db.execute("SELECT value FROM meta WHERE key='enabled'").fetchone()[0] == "true"

    def set_enabled(self, enabled):
        with self.db() as db:
            db.execute("UPDATE meta SET value=? WHERE key='enabled'", (json.dumps(enabled),))
            if not enabled:
                db.execute("DELETE FROM evidence_refs")
                db.execute("UPDATE requests SET state='cancelled',payload='{}'")

    def initialize_legacy(self, claim_ids):
        with self.db() as db:
            if db.execute("SELECT 1 FROM meta WHERE key='legacy_initialized'").fetchone():
                return
            db.executemany("INSERT OR IGNORE INTO legacy VALUES (?)", ((cid,) for cid in claim_ids))
            db.execute("INSERT INTO meta VALUES ('legacy_initialized','true')")

    def legacy_ids(self):
        with self.db() as db:
            return {r[0] for r in db.execute("SELECT id FROM legacy")}

    def preserve_denial(self, claim_id):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO denials VALUES (?,?)", (claim_id, self.clock()))

    def denied_ids(self):
        with self.db() as db:
            return {row[0] for row in db.execute("SELECT id FROM denials")}

    @staticmethod
    def _grant(row):
        return {**json.loads(row["spec"]), "id": row["id"], "state": row["state"],
                "revision": row["revision"], "createdAt": row["created"], "expiresAt": row["expires"]}

    def grants(self):
        with self.db() as db:
            return [self._grant(r) for r in db.execute("SELECT * FROM grants ORDER BY created DESC LIMIT 200")]

    def grant(self, grant_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
            if not row:
                raise AccessError()
            return self._grant(row)

    def save_grant(self, spec: GrantSpec, grant_id=None, expected_revision=None):
        with self.db() as db:
            now = self.clock()
            if grant_id:
                old = self.grant(grant_id)
                if old["revision"] != expected_revision:
                    raise AccessError("GRANT_CHANGED", 409)
                if old["agentId"] != spec.agentId or old["state"] == "revoked":
                    raise AccessError()
                # Changing the scope does not silently renew the term.
                expires = min(old["expiresAt"], now + spec.days * 86400)
                db.execute("UPDATE grants SET spec=?, revision=revision+1, expires=? WHERE id=?",
                           (spec.model_dump_json(), expires, grant_id))
                db.execute("DELETE FROM evidence_refs WHERE grant_id=?", (grant_id,))
                db.execute("UPDATE requests SET state='cancelled',payload='{}' WHERE grant_id=?", (grant_id,))
            else:
                grant_id = opaque("gr_")
                db.execute("INSERT INTO grants VALUES (?,?,?,'active',1,?,?)",
                           (grant_id, spec.agentId, spec.model_dump_json(), now, now + spec.days * 86400))
        return self.grant(grant_id)

    def change_state(self, grant_id, revision, state):
        with self.db() as db:
            grant = self.grant(grant_id)
            if grant["revision"] != revision:
                raise AccessError("GRANT_CHANGED", 409)
            if grant["state"] == "revoked" or grant["expiresAt"] <= self.clock():
                raise AccessError("GRANT_INACTIVE")
            db.execute("UPDATE grants SET state=?,revision=revision+1 WHERE id=?", (state, grant_id))
            db.execute("DELETE FROM evidence_refs WHERE grant_id=?", (grant_id,))
            db.execute("UPDATE requests SET state='cancelled',payload='{}' WHERE grant_id=?", (grant_id,))
        return self.grant(grant_id)

    def check(self, principal: Principal, revision=None):
        if (any(getattr(principal, k) != v for k, v in self.subject.model_dump().items())
                or principal.audience != self.audience or principal.expiresAt <= self.clock()):
            raise AccessError()
        if not self.enabled():
            raise AccessError("MCP_DISABLED")
        grant = self.grant(principal.grantId)
        if (grant["agentId"] != principal.agentId or grant["state"] != "active"
                or grant["expiresAt"] <= self.clock()
                or (revision is not None and grant["revision"] != revision)):
            raise AccessError("GRANT_INACTIVE")
        return grant

    def receipt(self, principal, operation, resources, *, result="delivered", delivery="bounded"):
        # Explicit columns/allowlisted values: never serialize arguments, exceptions or response bodies.
        receipt = opaque("rc_")
        with self.db() as db:
            db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)", (receipt, principal.agentId,
                principal.grantId, operation, json.dumps(resources, separators=(",", ":")),
                delivery, result, self.clock()))
        return receipt

    def audits(self):
        with self.db() as db:
            return [{**dict(r), "resources": json.loads(r["resources"])} for r in
                    db.execute("SELECT * FROM audit ORDER BY created DESC LIMIT 100")]

    def put_record(self, table, grant, payload, *, expires=None, state="pending"):
        assert table in {"evidence_refs", "requests"}
        record_id = opaque("ev_" if table == "evidence_refs" else "rq_")
        expires = min(expires or self.clock() + 600, grant["expiresAt"])
        with self.db() as db:
            db.execute(f"DELETE FROM {table} WHERE expires<=?", (self.clock(),))
            count = db.execute(f"SELECT count(*) FROM {table} WHERE grant_id=?", (grant["id"],)).fetchone()[0]
            if count >= 1000:
                raise AccessError("REQUEST_LIMIT", 429)
            values = (record_id, grant["id"], grant["revision"], expires)
            if table == "requests":
                values += (state,)
            values += (json.dumps(payload),)
            db.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in values)})", values)
        return record_id

    def record(self, table, record_id, grant):
        assert table in {"evidence_refs", "requests"}
        with self.db() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE id=? AND grant_id=? AND revision=?",
                             (record_id, grant["id"], grant["revision"])).fetchone()
            if not row:
                raise AccessError("REFERENCE_UNAVAILABLE", 404)
            if row["expires"] <= self.clock():
                raise AccessError("REFERENCE_EXPIRED", 410)
            return {**dict(row), "payload": json.loads(row["payload"])}

    def update_request(self, request_id, state, payload):
        with self.db() as db:
            db.execute("UPDATE requests SET state=?,payload=? WHERE id=?", (state, json.dumps(payload), request_id))
