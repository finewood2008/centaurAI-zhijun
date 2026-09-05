"""Prospective experience checks owned by the existing ontology database.

This is an audit of a claim against a decision, not a second profile. Predictions
are frozen before outcomes; only explicit user resolution changes a Claim.
"""
from __future__ import annotations

import json
import uuid

from .alignment_store import digest, claim_version, evidence_version
from .ontology_store import OntologyConflictError, OntologyError, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_episodes (
    id TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL, claim_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL, expectation_json TEXT NOT NULL,
    proposal_json TEXT, resolution_json TEXT,
    status TEXT NOT NULL DEFAULT 'watching', revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def decode(row):
    if not row:
        return None
    return {"id": row["id"], "decisionId": row["decision_id"], "conversationId": row["conversation_id"],
            "claimId": row["claim_id"], "snapshot": json.loads(row["snapshot_json"]),
            "expectation": json.loads(row["expectation_json"]),
            "proposal": json.loads(row["proposal_json"] or "null"),
            "resolution": json.loads(row["resolution_json"] or "null"),
            "status": row["status"], "revision": row["revision"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"]}


def claim_token(claim):
    return digest([claim["updatedAt"], claim_version(claim), evidence_version(claim), claim["selfAlignment"]["revision"]])


class LearningStore:
    def __init__(self, ontology):
        self.onto = ontology

    def get(self, decision_id):
        with self.onto._connect() as db:
            return decode(db.execute("SELECT * FROM learning_episodes WHERE decision_id=?", (decision_id,)).fetchone())

    def start(self, decision, claim, conversation_id, expectation):
        with self.onto._lock, self.onto._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = decode(db.execute("SELECT * FROM learning_episodes WHERE decision_id=?", (decision["id"],)).fetchone())
            if old:
                if old["claimId"] == claim["id"] and old["expectation"] == expectation:
                    return old
                raise OntologyConflictError("这次观察已开始；事前预期不能事后覆盖")
            if decision["status"] != "open":
                raise OntologyConflictError("结果已回来，不能补写成事前预测；可在复盘中记录新的认识")
            current = self.onto._fetch_claim(db, claim["id"])
            if not current or current["trustState"] != "confirmed" or current["updatedAt"] != claim["updatedAt"]:
                raise OntologyConflictError("理解已变化，请刷新后重新选择")
            snapshot = {k: claim[k] for k in ("content", "layer", "scope", "selfAlignment", "updatedAt")}
            snapshot.update(claimVersion=claim_version(claim), evidenceVersion=evidence_version(claim),
                            evidenceIds=[e["id"] for e in claim["evidence"]])
            now = utc_now()
            db.execute("INSERT INTO learning_episodes (id,decision_id,conversation_id,claim_id,snapshot_json,expectation_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                       ("learn_" + uuid.uuid4().hex[:12], decision["id"], conversation_id, claim["id"],
                        json.dumps(snapshot, ensure_ascii=False), json.dumps(expectation, ensure_ascii=False), now, now))
            db.commit()
        return self.get(decision["id"])

    def propose(self, episode, decision, proposal):
        if not decision.get("outcome"):
            raise OntologyConflictError("请先记录真实结果")
        current = self.onto.get_claim(episode["claimId"])
        if not current or current["trustState"] != "confirmed":
            raise OntologyConflictError("原理解已撤回或替代；这次观察仍保留，不再据此修改")
        # A proposal cites the result record, never model-generated evidence.
        proposal = {**proposal, "outcomeRecordedAt": decision["outcome"]["recordedAt"],
                    "outcome": decision["outcome"]["result"], "decisionId": decision["id"],
                    "claimUpdatedAt": current["updatedAt"], "claimToken": claim_token(current), "createdAt": utc_now()}
        with self.onto._lock, self.onto._connect() as db:
            changed = db.execute("UPDATE learning_episodes SET proposal_json=?,status='proposed',revision=revision+1,updated_at=? WHERE id=? AND revision=? AND status IN ('watching','proposed')",
                                (json.dumps(proposal, ensure_ascii=False), utc_now(), episode["id"], episode["revision"]))
            if changed.rowcount != 1:
                raise OntologyConflictError("观察已更新或已处理，请刷新；旧提议不会覆盖新修改")
        return self.get(decision["id"])

    def resolve(self, episode, payload):
        token = digest(payload)
        if episode.get("resolution"):
            if episode["resolution"].get("token") == token:
                return episode
            raise OntologyConflictError("这次观察已经处理；保留历史，不覆盖你的新选择")
        if episode["revision"] != payload["expectedRevision"]:
            raise OntologyConflictError("提议已更新，请刷新后再确认")
        action = payload["action"]
        if action == "apply":
            if episode["status"] != "proposed" or not episode["proposal"]:
                raise OntologyConflictError("请先比较预期与实际结果")
            text = payload.get("content", "").strip()
            if not text:
                raise OntologyError("请确认修改后的理解")
            self.onto.transition(episode["claimId"], "partial", surface="conversation",
                conversation_id=episode["conversationId"], edited_content=text,
                note="用户根据实际经历明确修订；自我贴合度重新校准",
                learning_resolution={"episodeId": episode["id"], "revision": episode["revision"],
                    "expectedUpdatedAt": episode["proposal"]["claimUpdatedAt"], "token": token,
                    "expectedToken": episode["proposal"]["claimToken"],
                    "situation": episode["expectation"]["situation"], "framing": payload["framing"],
                    "exceptions": payload.get("exceptions", ""), "decisionId": episode["decisionId"],
                    "outcome": episode["proposal"]["outcome"]})
        else:
            status = "kept" if action == "keep" else "deferred"
            with self.onto._lock, self.onto._connect() as db:
                changed = db.execute("UPDATE learning_episodes SET status=?,resolution_json=?,revision=revision+1,updated_at=? WHERE id=? AND revision=? AND status IN ('watching','proposed')",
                    (status, json.dumps({"token": token, "action": action, "note": payload.get("note", "")}, ensure_ascii=False),
                     utc_now(), episode["id"], episode["revision"]))
                if changed.rowcount != 1:
                    raise OntologyConflictError("观察已更新，请刷新后再操作")
        return self.get(episode["decisionId"])
