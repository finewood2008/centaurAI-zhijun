"""User-owned self alignment. Model proposals never write an accepted level.

Lives in ontology.db; optimistic revisions, source fingerprints and review events
keep calibration independent of extraction confidence and trust transitions.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from .ontology_store import OntologyConflictError, OntologyError, OntologyNotFoundError, utc_now

LEVELS = ("不代表我", "较少代表", "部分代表", "比较代表", "很能代表")
FRAMES = ("long_term", "context_only", "aspirational")
SCHEMA = """
CREATE TABLE IF NOT EXISTS alignment_requests (
    claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL, payload_hash TEXT NOT NULL, PRIMARY KEY(claim_id, request_id)
);
CREATE TABLE IF NOT EXISTS alignment_grants (
    claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL, service TEXT NOT NULL, created_at TEXT NOT NULL,
    current_token TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(claim_id, fingerprint, service)
);
CREATE TABLE IF NOT EXISTS alignment_conversations (
    conversation_id TEXT PRIMARY KEY, local_only INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT ''
);
"""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def claim_version(claim: dict) -> str:
    # New supporting evidence doesn't erase a user's calibration. Content,
    # framing, applicability and lifecycle changes do invalidate it.
    data = {k: claim.get(k) for k in ("id", "content", "layer", "scope", "contextRef", "trustState")}
    if claim.get("contextual"):
        data["contextual"] = claim["contextual"]
    return digest(data)


def evidence_version(claim: dict) -> str:
    return digest(sorted((e for e in claim.get("evidence", [])), key=lambda e: e["id"]))


def view(claim: dict, stored: dict | None) -> dict:
    value = dict(stored or {})
    version = claim_version(claim)
    valid = value.get("claimVersion") == version
    proposal = value.get("proposal") if valid else None
    if proposal and proposal.get("evidenceVersion") != evidence_version(claim):
        proposal = None
    return {
        "level": value.get("level") if valid else None,
        "framing": value.get("framing", "context_only" if claim.get("scope") == "context_only" else "aspirational" if claim.get("layer") == "aspirational" else "long_term"),
        "reason": value.get("reason", "") if valid else "",
        "evidenceIds": value.get("evidenceIds", []) if valid else [],
        "proposal": proposal,
        "revision": value.get("revision", 0),
        "claimVersion": version,
        "evidenceVersion": evidence_version(claim),
        "calibratedAt": value.get("calibratedAt") if valid else None,
        "needsRecalibration": bool(stored) and not valid,
        "lastConsideredEvidence": value.get("lastConsideredEvidence") if valid else None,
        "history": value.get("history", [])[-20:],
    }


class AlignmentStore:
    def __init__(self, ontology):
        self.ontology = ontology

    def _claim(self, db, claim_id):
        claim = self.ontology._fetch_claim(db, claim_id)
        if not claim:
            raise OntologyNotFoundError("理解不存在")
        if claim["trustState"] not in ("confirmed", "working"):
            raise OntologyConflictError("理解已撤回或被替代，请刷新")
        return claim

    def _save(self, db, claim, value, action, *, actor, conversation_id=None, note=""):
        old = claim["selfAlignment"]
        value["revision"] = old["revision"] + 1
        value["claimVersion"] = claim_version(claim)
        event = {"at": utc_now(), "action": action, "actor": actor,
                 "level": value.get("level"), "framing": value.get("framing"), "note": note}
        value["history"] = (old.get("history", []) + [event])[-20:]
        db.execute("UPDATE claims SET self_alignment_json = ? WHERE id = ?",
                   (json.dumps(value, ensure_ascii=False), claim["id"]))
        self.ontology._insert_review_event(db, target_type="claim", target_id=claim["id"],
            action=action, actor=actor, surface="conversation" if conversation_id else "ontology_page",
            conversation_id=conversation_id, before=old, after=value, note=note)
        self.ontology._bump_revision(db)

    def add_user_evidence(self, claim_id, expected, message):
        """Attach a locally selected real user span, without confirming any trait."""
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            claim = self._claim(db, claim_id)
            current = claim["selfAlignment"]
            if any(current[k] != expected[k] for k in ("revision", "claimVersion", "evidenceVersion")):
                raise OntologyConflictError("理解已变化，请使用新的证据重新判断")
            if message["role"] != "user":
                raise OntologyError("不能把模型输出作为新的用户证据")
            if not any(e.get("messageId") == message["id"] for e in claim["evidence"]):
                self.ontology._insert_evidence(db, claim_id, [{"kind": "conversation_turn",
                    "conversation_id": message["conversationId"], "message_id": message["id"],
                    "quote": message["content"][:500], "stance": "background"}])
        return self.ontology.get_claim(claim_id)

    def propose(self, claim_id: str, *, expected_revision: int, version: str, level: int,
                framing: str, reason: str, evidence_ids: list[str], conversation_id: str,
                message_id: str, evidence_digest: str) -> dict:
        if type(level) is not int or level not in range(5) or framing not in FRAMES:
            raise OntologyError("贴合度提议不合法")
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            claim = self._claim(db, claim_id)
            value = claim["selfAlignment"]
            if value["revision"] != expected_revision or version != claim_version(claim) or evidence_digest != evidence_version(claim):
                raise OntologyConflictError("理解或证据已变化，旧提议已丢弃")
            if not evidence_ids or not set(evidence_ids) <= {e["id"] for e in claim["evidence"]}:
                raise OntologyError("提议必须引用这条理解的有效证据")
            if value.get("lastConsideredEvidence") == evidence_digest:
                return claim
            value = dict(value)
            value["proposal"] = {"id": "alp_" + uuid.uuid4().hex[:16], "level": level,
                "framing": framing, "reason": reason[:500], "evidenceIds": evidence_ids,
                "evidenceVersion": evidence_digest, "conversationId": conversation_id,
                "messageId": message_id, "createdAt": utc_now()}
            value["lastConsideredEvidence"] = evidence_digest
            self._save(db, claim, value, "alignment_propose", actor="model", conversation_id=conversation_id, note=reason[:500])
        return self.ontology.get_claim(claim_id)

    def review(self, claim_id: str, payload: dict) -> dict:
        action = payload["action"]
        if action not in ("calibrate", "defer", "clear"):
            raise OntologyError("校准操作不合法")
        if action == "calibrate" and (type(payload.get("level")) is not int or payload["level"] not in range(5)):
            raise OntologyError("请选择五档贴合度之一")
        if payload.get("framing", "long_term") not in FRAMES:
            raise OntologyError("适用情境不合法")
        payload_hash = digest(payload)
        with self.ontology._lock, self.ontology._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            claim = self._claim(db, claim_id)
            duplicate = db.execute("SELECT payload_hash FROM alignment_requests WHERE claim_id=? AND request_id=?",
                                   (claim_id, payload["requestId"])).fetchone()
            if duplicate:
                if duplicate[0] != payload_hash:
                    raise OntologyConflictError("重复请求的内容不一致")
                return claim
            value = claim["selfAlignment"]
            if value["revision"] != payload["expectedRevision"] or value["claimVersion"] != payload["claimVersion"] or value["evidenceVersion"] != payload["evidenceVersion"]:
                raise OntologyConflictError("理解、证据或校准已更新，请刷新后确认")
            proposal = value.get("proposal")
            if payload.get("proposalId") and (not proposal or proposal["id"] != payload["proposalId"]):
                raise OntologyConflictError("这个提议已失效，请刷新")
            if action == "calibrate" and claim["trustState"] != "confirmed":
                raise OntologyConflictError("请先确认理解内容，再校准它有多代表你")
            value = dict(value)
            value["proposal"] = None
            value["lastConsideredEvidence"] = value["evidenceVersion"]
            if action == "calibrate":
                frame = payload.get("framing", "long_term")
                # Context-limited claims cannot silently become lifelong traits.
                if claim["scope"] == "context_only":
                    frame = "context_only"
                elif claim["layer"] == "aspirational" and frame == "long_term":
                    frame = "aspirational"
                if frame == "context_only":
                    claim["scope"] = "context_only"
                    claim["contextRef"] = payload.get("conversationId") or claim.get("contextRef")
                    db.execute("UPDATE claims SET scope='context_only',context_ref=? WHERE id=?", (claim["contextRef"], claim_id))
                elif frame == "aspirational":
                    claim["layer"] = "aspirational"
                    db.execute("UPDATE claims SET self_model_layer='aspirational' WHERE id=?", (claim_id,))
                value.update(level=payload["level"], framing=frame, reason=payload.get("note", "")[:500],
                             evidenceIds=[e["id"] for e in claim["evidence"]], calibratedAt=utc_now())
            elif action == "clear":
                value.update(level=None, calibratedAt=None, reason="", evidenceIds=[])
            self._save(db, claim, value, "alignment_" + action, actor="user",
                       conversation_id=payload.get("conversationId"), note=payload.get("note", "")[:500])
            db.execute("INSERT INTO alignment_requests VALUES(?,?,?)", (claim_id, payload["requestId"], payload_hash))
            # Any user edit revokes old grants, including grants for derived history.
            db.execute("DELETE FROM alignment_grants WHERE claim_id=?", (claim_id,))
        return self.ontology.get_claim(claim_id)

    def status(self, conversation_id: str, *, status=None, detail="", local_only=None) -> dict:
        with self.ontology._connect() as db:
            if status is not None or local_only is not None:
                db.execute("INSERT OR IGNORE INTO alignment_conversations(conversation_id) VALUES(?)", (conversation_id,))
                if status is not None:
                    db.execute("UPDATE alignment_conversations SET status=?,detail=? WHERE conversation_id=?", (status, detail[:300], conversation_id))
                if local_only is not None:
                    db.execute("UPDATE alignment_conversations SET local_only=? WHERE conversation_id=?", (int(local_only), conversation_id))
            row = db.execute("SELECT * FROM alignment_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            return dict(row) if row else {"local_only": False, "status": "", "detail": ""}

    def granted(self, ref: dict, service: str, current_token: str | None = None) -> bool:
        with self.ontology._connect() as db:
            return db.execute("SELECT 1 FROM alignment_grants WHERE claim_id=? AND fingerprint=? AND service=? AND current_token=?",
                              (ref["claimId"], ref["fingerprint"], service, current_token or ref["fingerprint"])).fetchone() is not None

    def grant(self, refs: list[dict], service: str, current_tokens: dict | None = None):
        with self.ontology._connect() as db:
            db.executemany("INSERT OR REPLACE INTO alignment_grants(claim_id,fingerprint,service,created_at,current_token) VALUES(?,?,?,?,?)",
                           [(r["claimId"], r["fingerprint"], service, utc_now(), (current_tokens or {}).get(r["claimId"], r["fingerprint"])) for r in refs])

    def revoke(self, claim_id: str):
        with self.ontology._connect() as db:
            db.execute("DELETE FROM alignment_grants WHERE claim_id=?", (claim_id,))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='routing_grants'").fetchone():
                db.execute("DELETE FROM routing_grants WHERE source_key=?", ("claim:" + claim_id,))
