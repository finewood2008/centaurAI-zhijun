"""DE receipts bound to explicit grants or independently verified standing policies."""
import hashlib
import json
import time

from .capabilities import require, CapabilityError


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sources(preview):
    return sorted(({key: source[key] for key in ("key", "kind", "version", "ref")}
                   for source in preview["sources"]), key=lambda source: source["key"])


def _binding(preview, provider, authorization, background=None):
    # Consent cannot silently transfer to another prompt, source version, mode or destination.
    return {"conversationId": preview["conversationId"], "purpose": preview["purpose"],
            "serviceId": provider.service_id, "configurationRevision": provider.configuration_revision,
            "sources": _sources(preview), "request": preview["request"], "mode": preview["mode"],
            "authorization": authorization, **({"background": background} if background else {})}


def _background_binding(ident):
    from .capabilities import execution
    origin = execution.get() or {}
    return {"id": ident, "executionRequestId": origin.get("requestId"),
            "operationId": origin.get("operationId")}


def _db():
    from mindos.stores.ontology_store import OntologyStore
    store = OntologyStore.instance()
    with store._lock, store._connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS workspace_consent_receipts (binding TEXT PRIMARY KEY, "
                   "grant_id TEXT NOT NULL, consent_id TEXT NOT NULL, expires_at REAL NOT NULL, "
                   "conversation_id TEXT NOT NULL, sources_json TEXT NOT NULL)")
    return store


def issue(preview, keys, provider, *, default_policy_revision=None, _background_id=None):
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
    background = _background_binding(_background_id) if _background_id is not None else None
    if background is not None:
        dto["background"] = background
    grant_id = hashlib.sha256(canonical(dto).encode()).hexdigest()
    grant = {**dto, "grantId": grant_id}
    response = (require().call("models.consent.background", {"backgroundId": _background_id, "grant": grant})
                if _background_id is not None else require().call("models.consent.issue", grant))
    if (type(response) is not dict or not isinstance(response.get("consentId"), str)
            or type(response.get("expiresAt")) not in (int, float) or response["expiresAt"] <= time.time()):
        raise CapabilityError("CAPABILITY_CONSENT_CONTRACT", 502)
    binding = hashlib.sha256(canonical(_binding(preview, provider, authorization, background)).encode()).hexdigest()
    store = _db()
    with store._lock, store._connect() as db:
        db.execute("DELETE FROM workspace_consent_receipts WHERE expires_at<=?", (time.time(),))
        db.execute("INSERT OR REPLACE INTO workspace_consent_receipts VALUES(?,?,?,?,?,?)",
                   (binding, grant_id, response["consentId"], response["expiresAt"],
                    preview["conversationId"], canonical(_sources(preview))))
    return response


def authorize_background(preview, provider):
    """Redeem a standing policy for a registered task, never a user grant."""
    from .background import active_task
    ident = active_task.get()
    policy = preview.get("defaultAuthorization") or {}
    if (not ident or not provider.external or not policy.get("applies") or not policy.get("autoEgress")
            or type(policy.get("revision")) is not int):
        return False
    try:
        issue(preview, [source["key"] for source in preview["sources"]], provider,
              default_policy_revision=policy["revision"], _background_id=ident)
    except CapabilityError as exc:
        if exc.status in {403, 409}:
            # An old local policy, a changed source or an expired registration
            # cannot substitute for DE's independently verified standing policy.
            return False
        raise
    return True


def find_receipt(preview, provider):
    store = _db()
    authorizations = [{"kind": "explicit"}]
    policy = preview.get("defaultAuthorization") or {}
    if policy.get("applies") and type(policy.get("revision")) is int:
        authorizations.append({"kind": "default", "policyRevision": policy["revision"]})
    bindings = [_binding(preview, provider, authorization) for authorization in authorizations]
    from .background import active_task
    ident = active_task.get()
    if ident:
        # A task receipt cannot become a foreground grant or migrate to a
        # different task/origin after the original DE registration is finished.
        bindings.extend(_binding(preview, provider, authorization, _background_binding(ident))
                        for authorization in authorizations if authorization["kind"] == "default")
    with store._connect() as db:
        row = next((found for binding in bindings if (found := db.execute(
            "SELECT grant_id,consent_id FROM workspace_consent_receipts WHERE binding=? AND expires_at>?",
            (hashlib.sha256(canonical(binding).encode()).hexdigest(), time.time())).fetchone())), None)
    return {"grantId": row[0], "consentId": row[1]} if row else None


def receipt(preview, provider):
    result = find_receipt(preview, provider)
    if result is None:
        raise CapabilityError("MODEL_EGRESS_CONSENT_REQUIRED", 409)
    return result


def register_preview(preview, provider):
    response = require().call("domain.preview.register", {
        "preview": preview, "configurationRevision": provider.configuration_revision})
    if type(response) is not dict or response.get("registered") is not True:
        raise CapabilityError("CAPABILITY_CONSENT_CONTRACT", 502)


def register_policy(policy):
    payload = {"action": "disable", "policyRevision": policy["revision"]}
    if policy["enabled"]:
        payload.update(action="enable", serviceId=policy["service"],
                       configurationRevision=policy["configurationRevision"],
                       purposes=policy["purposes"], includeFiles=policy["includeFiles"],
                       includeCharter=policy["includeCharter"], autoEgress=policy["autoEgress"])
    response = require().call("domain.consent-policy.register", payload)
    if (type(response) is not dict or response.get("revision") != policy["revision"]
            or (policy["enabled"] and response.get("registered") is not True)):
        raise CapabilityError("CAPABILITY_CONSENT_CONTRACT", 502)


def revoke_policy(key, revision):
    response = require().call("domain.consent-policy.register", {
        "action": "revoke", "key": key, "policyRevision": revision})
    if type(response) is not dict or response.get("revision") != revision:
        raise CapabilityError("CAPABILITY_CONSENT_CONTRACT", 502)


def revoke(key=None):
    store = _db()
    with store._lock, store._connect() as db:
        rows = db.execute("SELECT binding,consent_id,sources_json FROM workspace_consent_receipts").fetchall()
        selected = [row for row in rows if key is None or any(s["key"] == key for s in json.loads(row[2]))]
        # Remove local permission first; a failing remote revoke never leaves this worker able to reuse it.
        db.executemany("DELETE FROM workspace_consent_receipts WHERE binding=?", [(row[0],) for row in selected])
    for row in selected:
        require().call("models.consent.revoke", {"consentId": row[1]})
