"""Domain grant receipts; only an explicit verified routing grant can mint DE consent."""
import hashlib
import json
import time

from .capabilities import require, CapabilityError


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sources(preview):
    return sorted(({key: source[key] for key in ("key", "kind", "version", "ref")}
                   for source in preview["sources"]), key=lambda source: source["key"])


def _binding(preview, provider, authorization):
    # Consent cannot silently transfer to another prompt, source version, mode or destination.
    return {"conversationId": preview["conversationId"], "purpose": preview["purpose"],
            "serviceId": provider.service_id, "configurationRevision": provider.configuration_revision,
            "sources": _sources(preview), "request": preview["request"], "mode": preview["mode"],
            "authorization": authorization}


def _db():
    from mindos.stores.ontology_store import OntologyStore
    store = OntologyStore.instance()
    with store._lock, store._connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS workspace_consent_receipts (binding TEXT PRIMARY KEY, "
                   "grant_id TEXT NOT NULL, consent_id TEXT NOT NULL, expires_at REAL NOT NULL, "
                   "conversation_id TEXT NOT NULL, sources_json TEXT NOT NULL)")
    return store


def issue(preview, keys, provider, *, default_policy_revision=None):
    if not provider.external:
        raise CapabilityError("CONSENT_EXTERNAL_SERVICE_REQUIRED", 409)
    # Every source in this exact payload must be visible in the user's current grant action.
    if set(keys) != {source["key"] for source in preview["sources"]}:
        raise CapabilityError("CONSENT_ALL_SOURCES_REQUIRED", 409)
    authorization = ({"kind": "default", "policyRevision": default_policy_revision}
                     if default_policy_revision is not None else {"kind": "explicit"})
    dto = {"previewRevision": preview["revision"], "conversationId": preview["conversationId"],
           "purpose": preview["purpose"], "serviceId": provider.service_id,
           "configurationRevision": provider.configuration_revision, "selectedSources": _sources(preview),
           "authorization": authorization}
    grant_id = hashlib.sha256(canonical(dto).encode()).hexdigest()
    response = require().call("models.consent.issue", {**dto, "grantId": grant_id})
    if (type(response) is not dict or not isinstance(response.get("consentId"), str)
            or type(response.get("expiresAt")) not in (int, float) or response["expiresAt"] <= time.time()):
        raise CapabilityError("CAPABILITY_CONSENT_CONTRACT", 502)
    binding = hashlib.sha256(canonical(_binding(preview, provider, authorization)).encode()).hexdigest()
    store = _db()
    with store._lock, store._connect() as db:
        db.execute("DELETE FROM workspace_consent_receipts WHERE expires_at<=?", (time.time(),))
        db.execute("INSERT OR REPLACE INTO workspace_consent_receipts VALUES(?,?,?,?,?,?)",
                   (binding, grant_id, response["consentId"], response["expiresAt"],
                    preview["conversationId"], canonical(_sources(preview))))
    return response


def receipt(preview, provider):
    store = _db()
    authorizations = [{"kind": "explicit"}]
    policy = preview.get("defaultAuthorization") or {}
    if policy.get("applies") and type(policy.get("revision")) is int:
        authorizations.append({"kind": "default", "policyRevision": policy["revision"]})
    with store._connect() as db:
        row = next((found for authorization in authorizations if (found := db.execute(
            "SELECT grant_id,consent_id FROM workspace_consent_receipts WHERE binding=? AND expires_at>?",
            (hashlib.sha256(canonical(_binding(preview, provider, authorization)).encode()).hexdigest(), time.time())).fetchone())), None)
    if not row:
        raise CapabilityError("MODEL_EGRESS_CONSENT_REQUIRED", 409)
    return {"grantId": row[0], "consentId": row[1]}


def revoke(key=None):
    store = _db()
    with store._lock, store._connect() as db:
        rows = db.execute("SELECT binding,consent_id,sources_json FROM workspace_consent_receipts").fetchall()
        selected = [row for row in rows if key is None or any(s["key"] == key for s in json.loads(row[2]))]
        # Remove local permission first; a failing remote revoke never leaves this worker able to reuse it.
        db.executemany("DELETE FROM workspace_consent_receipts WHERE binding=?", [(row[0],) for row in selected])
    for row in selected:
        require().call("models.consent.revoke", {"consentId": row[1]})
