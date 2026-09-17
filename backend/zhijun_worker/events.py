"""Authenticated system events, transactionally deduplicated inside this private owner database."""
import hashlib
import json
import os
import re
import threading
import time

from .auth import strict_json
from .capabilities import execution, require, CapabilityError
from .background import register

_lock = threading.RLock()
TYPES = {"material.ready", "material.recycled", "material.purged", "material.version_superseded", "domain.tick"}


def parse(raw):
    event = strict_json(raw)
    if (type(event) is not dict or set(event) != {"eventId", "type", "payload", "executionRequestId", "operationId"}
            or not isinstance(event["eventId"], str) or not re.fullmatch("[a-f0-9]{32}", event["eventId"])
            or not isinstance(event["type"], str) or event["type"] not in TYPES or event["operationId"] != "system_material_event"
            or not isinstance(event["executionRequestId"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", event["executionRequestId"])):
        raise CapabilityError("WORKER_EVENT_INVALID", 400)
    value = event["payload"]
    if event["type"] == "domain.tick":
        valid = (type(value) is dict and set(value) == {"scheduledAt"}
                 and type(value["scheduledAt"]) is int and value["scheduledAt"] >= 0
                 and abs(value["scheduledAt"] - int(time.time()) // 60) <= 2)
    else:
        valid = (type(value) is dict and set(value) == {"materialId", "version"}
                 and isinstance(value["materialId"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value["materialId"])
                 and type(value["version"]) is int and value["version"] >= 1)
    if not valid:
        raise CapabilityError("WORKER_EVENT_INVALID", 400)
    return event


def _detach_conversations(material_id):
    from mindos.stores.chat_import_store import ChatImportStore
    store = ChatImportStore()
    with store.conversations._lock, store.conversations._connect() as db:
        db.execute("DELETE FROM chat_material_grants WHERE material_id=?", (material_id,))
        db.execute("UPDATE chat_material_privacy SET sha256=NULL WHERE material_id=?", (material_id,))
        db.execute("UPDATE chat_import_files SET state='unavailable',error='资料已删除或版本已变化' WHERE material_id=?", (material_id,))
        for row in db.execute("SELECT conversation_id,refs_json FROM chat_reference_selection").fetchall():
            refs = json.loads(row[1])
            kept = [ref for ref in refs if ref.get("materialId") != material_id]
            if len(kept) != len(refs):
                db.execute("UPDATE chat_reference_selection SET refs_json=? WHERE conversation_id=?", (json.dumps(kept), row[0]))


def _detach_ontology(store, db, material_id, reason):
    from mindos.stores.ontology_store import utc_now
    now = utc_now()
    rows = db.execute("SELECT DISTINCT c.id,c.trust_state,c.content FROM claims c JOIN claim_evidence e ON e.claim_id=c.id WHERE e.material_id=?", (material_id,)).fetchall()
    for row in rows:
        other = db.execute("SELECT 1 FROM claim_evidence WHERE claim_id=? AND (material_id IS NULL OR material_id!=?) LIMIT 1", (row["id"], material_id)).fetchone()
        if not other and row["trust_state"] == "working":
            db.execute("UPDATE claims SET trust_state='retracted',retracted_at=?,retraction_reason=?,updated_at=? WHERE id=?", (now, reason, now, row["id"]))
            store._insert_review_event(db, target_type="claim", target_id=row["id"], action="retract", actor="system", surface="system",
                before={"trustState": "working", "content": row["content"]}, after={"trustState": "retracted", "reason": reason}, note="资料生命周期已变化")
    db.execute("DELETE FROM claim_evidence WHERE material_id=?", (material_id,))
    # Existing source references are retained for audit, but no longer grant access or surface as usable evidence.
    db.execute("DELETE FROM routing_grants WHERE source_key=?", ("material:" + material_id,))
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_consent_receipts'").fetchone():
        for row in db.execute("SELECT binding,sources_json FROM workspace_consent_receipts").fetchall():
            if any(source["kind"] == "material" and source["ref"].get("id") == material_id for source in json.loads(row[1])):
                db.execute("DELETE FROM workspace_consent_receipts WHERE binding=?", (row[0],))
    if rows:
        store._bump_revision(db)


def handle(event):
    from mindos.stores.ontology_store import OntologyStore
    from mindos.stores.routing_store import RoutingStore
    from mindos.zhijun import consolidate
    store = OntologyStore.instance()
    RoutingStore(store)
    fingerprint = hashlib.sha256(json.dumps({"type": event["type"], "payload": event["payload"]}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    token = execution.set({"requestId": event["executionRequestId"], "operationId": event["operationId"]})
    try:
        with _lock:
            with store._lock, store._connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS worker_events (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL)")
                row = db.execute("SELECT fingerprint,result_json FROM worker_events WHERE id=?", (event["eventId"],)).fetchone()
                if row:
                    if row[0] != fingerprint:
                        raise CapabilityError("WORKER_EVENT_CONFLICT", 409)
                    return {**json.loads(row[1]), "duplicate": True}
            kind, value = event["type"], event["payload"]
            stale = False
            if kind.startswith("material."):
                with store._lock, store._connect() as db:
                    db.execute("CREATE TABLE IF NOT EXISTS worker_material_heads (material_id TEXT PRIMARY KEY, version INTEGER NOT NULL)")
                    head = db.execute("SELECT version FROM worker_material_heads WHERE material_id=?", (value["materialId"],)).fetchone()
                    stale = bool(head and head[0] > value["version"])
            jobs = []
            retrieval_only = bool(os.environ.get("ZHIJUN_WORKSPACE_ID"))
            if kind == "material.ready" and not stale and not retrieval_only:
                try:
                    material = require().call("materials.get", {"materialId": value["materialId"]})
                except CapabilityError as exc:
                    if exc.status not in {404, 409, 410}:
                        raise
                    material = None
                if material and material.get("versionNumber") == value["version"] and material.get("status") == "available":
                    jobs.append(("extract_material", value["materialId"] + ":" + str(value["version"]), value))
            elif kind == "domain.tick":
                hour = value["scheduledAt"] // 60
                if int(store.meta_get("workspace_tick_hour", "-1")) < hour:
                    jobs.append(("nudge_scan", "hourly", {}))
                    jobs.append(("proactive_scan", "hourly", {"scope": "global"}))
                    if consolidate.should_run(store):
                        jobs.append(("consolidate", "nightly", {}))
            elif kind.startswith("material.") and kind != "material.ready" and not stale:
                _detach_conversations(value["materialId"])
            registered = []
            for job_kind, owner, payload in jobs:
                job_id = "ojob_event_" + hashlib.sha256((event["eventId"] + job_kind).encode()).hexdigest()[:24]
                register(job_id, job_kind)
                registered.append((job_id, job_kind, owner, payload))
            result = {"eventId": event["eventId"], "accepted": True, "jobIds": [], "stale": stale}
            if kind == "material.ready" and retrieval_only:
                result.update(state="skipped", code="RAG_RETRIEVAL_ONLY",
                              message="知君不自动读取材料正文；请在对话中检索并确认使用。")
            with store._lock, store._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if kind.startswith("material.") and not stale:
                    db.execute("INSERT INTO worker_material_heads VALUES(?,?) ON CONFLICT(material_id) DO UPDATE SET version=MAX(version,excluded.version)", (value["materialId"], value["version"]))
                if kind.startswith("material.") and kind != "material.ready" and not stale:
                    _detach_ontology(store, db, value["materialId"], kind)
                if kind == "domain.tick":
                    db.execute("INSERT INTO ontology_meta(key,value) VALUES('workspace_tick_hour',?) ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(ontology_meta.value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)", (str(value["scheduledAt"] // 60),))
                for job_id, job_kind, owner, payload in registered:
                    now = time.time()
                    inserted = db.execute("INSERT OR IGNORE INTO ontology_jobs(job_id,kind,owner_id,state,priority,attempts,input_hash,payload_json,created_at,updated_at) VALUES(?,?,?,'queued',2,0,'',?,?,?)", (job_id, job_kind, owner, json.dumps(payload), now, now))
                    if inserted.rowcount:
                        result["jobIds"].append(job_id)
                db.execute("INSERT INTO worker_events VALUES(?,?,?)", (event["eventId"], fingerprint, json.dumps(result)))
            from .background import finish
            for job_id, _, _, _ in registered:
                if job_id not in result["jobIds"]:
                    finish(job_id)
            return result
    finally:
        execution.reset(token)
