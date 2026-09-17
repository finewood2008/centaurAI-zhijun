"""Isolated real SQLite stores, RAG contract double, no user data or cloud services."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mindos.stores.ontology_store import OntologyStore
from mindos.stores.conversation_store import ConversationStore
from mindos.stores.growth_store import GrowthStore
from zhijun_mcp.models import AccessError, GrantSpec, Principal, Subject
from zhijun_mcp.store import AccessStore
from zhijun_mcp.personal import PersonalContext
from zhijun_mcp.service import ExternalAgentService

RESOURCE = "https://box.example.com/mcp"


def claim(ontology, content="喜欢简洁的回答", **kwargs):
    return ontology.create_claim({"content": content, "section": "ways", "layer": "self_declared", **kwargs},
                                 [], trust_state="confirmed", trust_origin="utterance")


class Materials:
    def __init__(self):
        self.items = {"doc-a": {"id": "doc-a", "title": "资料 A", "version": 1, "updatedAt": "2026-09-17T00:00:00Z"}}

    def describe(self, ids):
        return {k: copy.deepcopy(v) for k, v in self.items.items() if k in ids}

    def catalog(self):
        return list(self.items.values())


class Rag:
    def __init__(self):
        self.calls, self.evidence, self.tokens = [], {}, {}
        self.sensitive = False
        self.after_search = None

    def item(self, mid):
        ref = "erv2_" + mid.replace("-", "a") + "x" * 32
        result = {"evidenceRef": ref, "materialId": mid, "materialVersion": 1, "title": "资料 A",
                  "text": "工作正文 BODY_SECRET", "locator": {"page": 1},
                  "verificationStatus": "verified", "containsSensitive": False, "score": .8,
                  "policyVersion": 2, "detectorRevision": "detector-2"}
        self.evidence[ref] = result
        return result

    def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        if self.after_search:
            self.after_search()
        items = [self.item(mid) for mid in kwargs["material_ids"][:kwargs["top_k"]]]
        if self.sensitive:
            token = "scf_" + str(len(self.tokens)).zfill(32)
            self.tokens[token] = items
            return {"status": "sensitive_confirmation_required", "confirmation": {
                "confirmToken": token, "expiresAt": "2035-01-01T00:00:00Z",
                "interactionId": kwargs["interaction_id"], "policyVersion": 2, "detectorRevision": "detector-2",
                "hits": [{"category": "phone", "locator": {"page": 1}, "materialId": items[0]["materialId"], "redactedPreview": "138****0000"}]}}
        return {"status": "ok", "items": items, "returnedCount": len(items), "policyVersion": 2, "detectorRevision": "detector-2"}

    def evidence_resolve(self, refs):
        self.calls.append(("resolve", refs))
        fields = ("evidenceRef", "materialId", "materialVersion", "title", "text", "locator", "containsSensitive", "verificationStatus")
        return {"items": [{**{k: self.evidence[r][k] for k in fields}, "evidenceExpiresAt": "2035-01-01T00:00:00Z"} for r in refs],
                "policyVersion": 2, "detectorRevision": "detector-2"}

    def confirm(self, token, **kwargs):
        self.calls.append(("confirm", token, kwargs))
        return {"status": "ok", "desensitized": kwargs["desensitize"], "items": self.tokens.pop(token),
                "containsSensitive": not kwargs["desensitize"], "policyVersion": 2, "detectorRevision": "detector-2"}


@pytest.fixture
def env(tmp_path):
    root = tmp_path.resolve()
    subject = Subject(accountId="account-a", boxId="box-a", workspaceId="a" * 64, ownershipEpoch=1)
    clock = [time.time()]
    store = AccessStore(root / "access.db", subject, RESOURCE, clock=lambda: clock[0])
    ontology = OntologyStore(root / "ontology.db")
    old = claim(ontology)
    conv, growth = ConversationStore(root / "conversations.db"), GrowthStore(root / "growth.db")
    personal = PersonalContext(ontology, conv, growth, store)
    rag, materials = Rag(), Materials()
    service = ExternalAgentService(store, personal, rag, materials, public_origin="https://box.example.com")
    store.set_enabled(True)
    spec = GrantSpec(agentId="workbuddy", agentName="WorkBuddy", sections=["ways"], materialIds=["doc-a"],
                     acknowledgedLegacyIds=[old["id"]], disclosureAccepted=True)
    grant = service.save_grant(spec)
    principal = Principal(**subject.model_dump(), agentId="workbuddy", grantId=grant["id"],
                          audience=RESOURCE, expiresAt=int(clock[0]) + 900)
    return SimpleNamespace(**locals())


def test_same_authorization_reads_both_domains_and_receipts_without_paths(env):
    personal = env.service.call(env.principal, "zhijun_get_personal_context", {})
    work = env.service.call(env.principal, "zhijun_search_work_data", {"query": "QUERY_SECRET"})
    assert personal["items"][0]["content"] == "喜欢简洁的回答"
    assert work["items"][0]["text"] == "工作正文 BODY_SECRET"
    assert personal["receipt"] != work["receipt"]
    raw = json.dumps(work)
    assert "erv2_" not in raw and "/private" not in raw
    assert work["items"][0]["source"]["location"] == {"page": 1}
    records = json.dumps(env.store.audits(), ensure_ascii=False)
    assert "QUERY_SECRET" not in records and "BODY_SECRET" not in records and "喜欢简洁" not in records
    assert env.rag.calls[0][2]["material_ids"] == ["doc-a"]


@pytest.mark.parametrize("change", [
    {"accountId": "other"}, {"boxId": "other"}, {"workspaceId": "b" * 64},
    {"ownershipEpoch": 2}, {"agentId": "other"}, {"audience": "https://other/mcp"}, {"expiresAt": 1},
])
def test_principal_cannot_cross_identity_boundaries(env, change):
    with pytest.raises(AccessError):
        env.service.call(env.principal.model_copy(update=change), "zhijun_get_personal_context", {})
    assert not env.rag.calls


def test_empty_scope_never_calls_rag_and_new_materials_not_added(env):
    spec = env.spec.model_copy(update={"materialIds": []})
    env.service.save_grant(spec, env.grant["id"], 1)
    assert env.service.call(env.principal, "zhijun_search_work_data", {"query": "q"})["items"] == []
    assert not env.rag.calls
    env.materials.items["new"] = {"id": "new", "version": 1}
    assert env.service.call(env.principal, "zhijun_search_work_data", {"query": "q"})["items"] == []


def test_101_materials_batched_before_retrieval(env):
    ids = ["doc-" + str(i) for i in range(101)]
    env.materials.items = {i: {"id": i, "version": 1} for i in ids}
    env.service.save_grant(env.spec.model_copy(update={"materialIds": ids}), env.grant["id"], 1)
    result = env.service.call(env.principal, "zhijun_search_work_data", {"query": "q", "limit": 5})
    assert [len(c[2]["material_ids"]) for c in env.rag.calls if c[0] == "search"] == [100, 1]
    assert len(result["items"]) == 5


@pytest.mark.parametrize("change", ["revoke", "pause", "scope", "acl", "version", "expire", "disable"])
def test_old_evidence_cannot_bypass_current_authorization(env, change):
    result = env.service.call(env.principal, "zhijun_search_work_data", {"query": "q"})
    reference = result["items"][0]["source"]["reference"]
    if change in ("revoke", "pause"):
        env.store.change_state(env.grant["id"], 1, "revoked" if change == "revoke" else "paused")
    elif change == "scope":
        env.service.save_grant(env.spec.model_copy(update={"materialIds": []}), env.grant["id"], 1)
    elif change == "acl":
        env.materials.items.clear()
    elif change == "version":
        env.materials.items["doc-a"]["version"] = 2
    elif change == "expire":
        env.clock[0] += 31 * 86400
    else:
        env.store.set_enabled(False)
    with pytest.raises(AccessError):
        env.service.call(env.principal, "zhijun_read_work_evidence", {"reference": reference})


def test_revoked_during_search_delivers_no_body(env):
    env.rag.after_search = lambda: env.store.change_state(env.grant["id"], 1, "revoked")
    with pytest.raises(AccessError):
        env.service.call(env.principal, "zhijun_search_work_data", {"query": "q"})
    assert [a["result"] for a in env.store.audits()] == ["denied"]


def test_audit_failure_stops_delivery(env, monkeypatch):
    def fail(*args, **kwargs):
        raise AccessError("ACCESS_STORAGE_UNAVAILABLE", 503)
    monkeypatch.setattr(env.store, "receipt", fail)
    with pytest.raises(AccessError, match="STORAGE"):
        env.service.call(env.principal, "zhijun_get_personal_context", {})


@pytest.mark.parametrize("change", ["policy", "detector", "expiry", "sensitivity", "path"])
def test_old_evidence_revalidates_de_policy_and_delivery_fence(env, change):
    search = env.service.call(env.principal, "zhijun_search_work_data", {"query": "q"})
    reference = search["items"][0]["source"]["reference"]
    resolve = env.rag.evidence_resolve
    def changed(refs):
        result = resolve(refs)
        if change == "policy": result["policyVersion"] = 3
        if change == "detector": result["detectorRevision"] = "changed"
        if change == "expiry": result["items"][0]["evidenceExpiresAt"] = "2020-01-01T00:00:00Z"
        if change == "sensitivity": result["items"][0]["containsSensitive"] = True
        if change == "path": result["items"][0]["locator"]["path"] = "/private/internal"
        return result
    env.rag.evidence_resolve = changed
    with pytest.raises(AccessError, match="EVIDENCE_CONTEXT_CHANGED"):
        env.service.call(env.principal, "zhijun_read_work_evidence", {"reference": reference})


def test_no_results_and_policy_blocked_are_empty_without_counts(env):
    for state in ("no_results", "sensitive_content_blocked"):
        env.rag.search = lambda *args, **kwargs: {"status": state, "items": [], "returnedCount": 0,
                                               "policyVersion": 2, "detectorRevision": "detector-2"}
        result = env.service.call(env.principal, "zhijun_search_work_data", {"query": "q"})
        assert result["items"] == [] and "returnedCount" not in result


def test_category_grants_future_claims_but_no_implicit_legacy_or_forbidden_claims(env):
    spec = env.spec.model_copy(update={"acknowledgedLegacyIds": []})
    env.service.save_grant(spec, env.grant["id"], 1)
    assert env.service.call(env.principal, "zhijun_get_personal_context", {})["items"] == []
    new = claim(env.ontology, "更喜欢先给结论")
    blocked = claim(env.ontology, "明确不外发")
    env.ontology.set_export_allowed(blocked["id"], False)
    claim(env.ontology, "敏感内容", privacy_level="sensitive")
    claim(env.ontology, "本地内容", device_scope="private-device")
    claim(env.ontology, "已经过期", valid_to="2020-01-01T00:00:00Z")
    claim(env.ontology, "只适用当时", scope="context_only")
    result = env.service.call(env.principal, "zhijun_get_personal_context", {})
    assert [c["id"] for c in result["items"]] == [new["id"]]
    # Explicit off remains off even if the legacy switch is turned on later.
    env.ontology.set_export_allowed(blocked["id"], True)
    assert blocked["id"] not in [c["id"] for c in env.service.call(env.principal, "zhijun_get_personal_context", {})["items"]]


def test_derivative_cannot_launder_legacy_or_explicit_deny(env):
    child = claim(env.ontology, "旧内容的新版本")
    with env.ontology._connect() as db:
        db.execute("UPDATE claims SET supersedes_id=? WHERE id=?", (env.old["id"], child["id"]))
        db.commit()
    env.service.save_grant(env.spec.model_copy(update={"acknowledgedLegacyIds": []}), env.grant["id"], 1)
    assert env.service.call(env.principal, "zhijun_get_personal_context", {})["items"] == []
    env.ontology.set_export_allowed(env.old["id"], False)
    assert child["id"] not in [c["id"] for c in env.personal.preview()]


def test_sensitive_parent_cannot_be_laundered_through_claim_evidence(env):
    parent = claim(env.ontology, "敏感来源", privacy_level="sensitive")
    child = claim(env.ontology, "派生后的总结")
    env.ontology.add_evidence(child["id"], [{"kind": "conversation_turn", "locator": {"claimId": parent["id"]}}])
    assert child["id"] not in [c["id"] for c in env.personal.preview()]


def test_migrated_denial_survives_legacy_review_cleanup(env):
    env.ontology.set_export_allowed(env.old["id"], False)
    assert not env.personal.preview()
    with env.ontology._connect() as db:
        db.execute("DELETE FROM review_events WHERE target_id=?", (env.old["id"],))
        db.commit()
    env.ontology.set_export_allowed(env.old["id"], True)
    assert not env.personal.preview()


def test_masked_approval_cannot_deliver_unmasked_confirmation(env):
    env.rag.sensitive = True
    request = env.service.call(env.principal, "zhijun_search_work_data", {"query": "q"})
    confirm = env.rag.confirm
    env.rag.confirm = lambda token, **kwargs: {**confirm(token, **kwargs), "desensitized": False}
    with pytest.raises(AccessError, match="DELIVERY_MODE_MISMATCH"):
        env.service.decide(request["requestId"], "masked")
    result = env.service.call(env.principal, "zhijun_get_request_status", {"requestId": request["requestId"]})
    assert result["status"] == "failed" and "items" not in result


@pytest.mark.parametrize("decision", ["masked", "original", "cancel"])
def test_sensitive_confirmation_is_human_only_and_bound(env, decision):
    env.rag.sensitive = True
    result = env.service.call(env.principal, "zhijun_search_work_data", {"query": "q"})
    assert result["status"] == "pending" and "scf_" not in json.dumps(result)
    with pytest.raises(AccessError):
        env.service.call(env.principal, "approve", {"requestId": result["requestId"]})
    env.service.decide(result["requestId"], decision)
    status = env.service.call(env.principal, "zhijun_get_request_status", {"requestId": result["requestId"]})
    assert status["status"] == ("cancelled" if decision == "cancel" else "ready")
    assert bool(status.get("items")) == (decision != "cancel")
    if decision != "cancel":
        assert status["items"][0]["delivery"] == decision
        approval = next(a for a in env.store.audits() if a["operation"] == "user_confirmation")
        assert approval["delivery"] == decision and approval["resources"][0]["id"] == result["requestId"]
    with pytest.raises(AccessError, match="DECIDED"):
        env.service.decide(result["requestId"], decision)


@pytest.mark.parametrize("change", ["version", "timeout", "revoke", "other-agent"])
def test_pending_confirmations_do_not_survive_changes(env, change):
    env.rag.sensitive = True
    result = env.service.call(env.principal, "zhijun_search_work_data", {"query": "q"})
    request_id = result["requestId"]
    if change == "version":
        env.materials.items["doc-a"]["version"] = 2
    elif change == "timeout":
        env.clock[0] += 601
    elif change == "revoke":
        env.store.change_state(env.grant["id"], 1, "revoked")
    else:
        other = env.service.save_grant(env.spec.model_copy(update={"agentId": "other"}))
        with pytest.raises(AccessError):
            env.service.call(env.principal.model_copy(update={"grantId": other["id"], "agentId": "other"}),
                             "zhijun_get_request_status", {"requestId": request_id})
        return
    with pytest.raises(AccessError):
        env.service.decide(request_id, "original")
    assert not any(c[0] == "confirm" for c in env.rag.calls)


def test_management_requires_owner_guard_and_optimistic_revision(env):
    from zhijun_mcp.management import build_router
    allowed = [False]
    def guard():
        if not allowed[0]:
            raise HTTPException(401)
    app = FastAPI()
    app.include_router(build_router(guard, lambda: env.service))
    client = TestClient(app)
    base = "/api/mindos/settings/external-agents"
    assert client.get(base).status_code == 401
    assert client.get(base + "/preview").status_code == 401
    allowed[0] = True
    assert client.get(base).json()["grants"][0]["id"] == env.grant["id"]
    assert client.patch(base + "/grants/" + env.grant["id"], json={"expectedRevision": 42, "state": "revoked"}).status_code == 409


def test_default_off_and_store_identity_is_immutable(tmp_path):
    subject = Subject(accountId="a", boxId="b", workspaceId="c", ownershipEpoch=1)
    path = tmp_path.resolve() / "store.db"
    assert not AccessStore(path, subject, RESOURCE).enabled()
    with pytest.raises(ValueError, match="MISMATCH"):
        AccessStore(path, subject.model_copy(update={"accountId": "other"}), RESOURCE)


def test_jwt_audience_lifetime_identity_and_token_type(env):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from zhijun_mcp.auth import TokenVerifier
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    claims = {"iss": "https://account.example.com", "aud": RESOURCE, "sub": "account-a", "client_id": "workbuddy",
        "jti": "token-a", "iat": now, "exp": now + 900, "scope": "zhijun:read", "box_id": "box-a",
        "workspace_id": "a" * 64, "ownership_epoch": 1, "grant_id": env.grant["id"]}
    verifier = TokenVerifier(claims["iss"], RESOURCE, lambda _: key.public_key())
    def verify(changes=None, typ="at+jwt"):
        token = jwt.encode({**claims, **(changes or {})}, key, algorithm="RS256", headers={"typ": typ})
        return asyncio.run(verifier.verify_token(token))
    assert verify().principal.agentId == "workbuddy"
    for changes in ({"aud": "other"}, {"aud": [RESOURCE, "other"]}, {"exp": now+901}, {"exp": now-1},
                    {"iss": "other"}, {"scope": "admin"}, {"client_id": ""}, {"ownership_epoch": True}):
        assert verify(changes) is None
    assert verify(typ="JWT") is None


def test_http_session_reads_both_real_domains_with_sources_and_revocation(env):
    from zhijun_mcp.server import create_server
    from zhijun_mcp.auth import VerifiedToken
    from zhijun_mcp.http import protect
    class Verify:
        async def verify_token(self, token):
            if token != "fixture-only":
                return None
            return VerifiedToken(token=token, client_id=env.principal.agentId, scopes=["zhijun:read"],
                expires_at=env.principal.expiresAt, resource=RESOURCE, principal=env.principal)
    class BoundGateway:
        async def call(self, principal, tool, arguments):
            return env.service.call(principal, tool, arguments)
    server = create_server(resource=RESOURCE, issuer="https://account.example.com", verifier=Verify(), gateway=BoundGateway())
    app = protect(server.streamable_http_app(), RESOURCE)
    with TestClient(app, base_url="https://box.example.com") as client:
        headers = {"Authorization": "Bearer fixture-only", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-11-25"}
        def call(name, arguments):
            result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})
            assert result.headers["cache-control"] == "no-store"
            return result.json()["result"]
        personal = call("zhijun_get_personal_context", {})["structuredContent"]
        work = call("zhijun_search_work_data", {"query": "q"})["structuredContent"]
        assert personal["items"][0]["source"]["type"] == "ontology_claim"
        assert work["items"][0]["source"]["type"] == "work_evidence"
        assert personal["receipt"] and work["receipt"]
        env.store.change_state(env.grant["id"], 1, "revoked")
        denied = call("zhijun_read_work_evidence", {"reference": work["items"][0]["source"]["reference"]})
        assert denied["isError"] is True and "BODY_SECRET" not in json.dumps(denied)
        assert client.post("/mcp", headers=headers, content=b"x" * 65537).status_code == 413
        assert client.get("/external-agents/authorize", headers={"Host": "evil.example"}).status_code == 400
