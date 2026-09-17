import pytest
from fastapi import HTTPException
from types import SimpleNamespace
import sys
import copy
import re

from mindos import data_agent_rag as rag


def item(ref="erv2_" + "a" * 32, *, verification="verified"):
    return {"evidenceRef": ref, "materialId": "material-a", "materialVersion": 1,
            "title": "资料", "text": "可交付片段", "score": .8, "locator": {"page": 1},
            "containsSensitive": verification == "unverified", "verificationStatus": verification,
            "policyVersion": 2, "detectorRevision": "detector-2"}


def search_result(items=None, *, status="ok", notice=None):
    values = list(items or [])
    result = {"status": status, "items": values, "returnedCount": len(values),
              "policyVersion": 2, "detectorRevision": "detector-2"}
    if notice is not None:
        result["detectionNotice"] = notice
    return result


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.delivered = item()

    def capabilities(self):
        return {"enabledCapabilities": ["mindos.sensitive.original.read"]}

    def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        result = copy.deepcopy(self.result)
        if result.get("status") == "sensitive_confirmation_required":
            # The service echoes the exact ID of this real REST invocation.
            result["confirmation"]["interactionId"] = kwargs["interaction_id"]
        return result

    def confirm(self, token, **kwargs):
        self.calls.append(("confirm", token, kwargs))
        self.delivered = item()
        return {"status": "ok", "desensitized": kwargs["desensitize"],
                "containsSensitive": not kwargs["desensitize"],
                "policyVersion": 2, "detectorRevision": "detector-2",
                "items": [self.delivered]}

    def confirm_unverified(self, token):
        self.calls.append(("risk", token, {}))
        self.delivered = {**item("erv2_" + "u" * 32, verification="unverified"),
                          "containsSensitive": True}
        return {"status": "unverified_original_released", "verificationStatus": "unverified",
                "riskAccepted": True, "desensitized": False, "containsSensitive": True,
                "message": "用户已确认风险，以下片段未经验证。",
                "policyVersion": 2, "detectorRevision": "detector-2",
                "items": [self.delivered]}

    def evidence_resolve(self, refs):
        self.calls.append(("resolve", refs, {}))
        delivered = self.delivered if isinstance(self.delivered, list) else [self.delivered]
        indexed = {value["evidenceRef"]: value for value in delivered}
        items = []
        for ref in refs:
            value = indexed[ref]
            resolved = {key: value[key] for key in (
                "materialId", "materialVersion", "title", "text", "locator",
                "containsSensitive", "verificationStatus",
            )}
            items.append({**resolved, "evidenceRef": ref,
                          "evidenceExpiresAt": "2030-01-01T00:00:00Z"})
        return {"items": items,
                "policyVersion": 2, "detectorRevision": "detector-2"}


def setup_function():
    rag.reset_for_tests()


def test_chat_import_contract_accepts_full_v2_material_id_length():
    from mindos.chat_import_routes import ImportFile, MaterialRef

    material_id = "m" * 128
    assert MaterialRef(materialId=material_id, version=1).materialId == material_id
    assert ImportFile(id="file-123456789012", name="a.txt", materialId=material_id).materialId == material_id


def test_search_uses_material_only_filter_and_resolves_cached_evidence():
    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    assert rag.search("问题", ["material-a"], "interaction-1") == [item()]
    actual_id = rag._entries["interaction-1"].actual_search_id
    assert re.fullmatch(r"interaction-1:search:[a-f0-9]{32}", actual_id)
    assert client.calls[0] == ("search", "问题", {"top_k": 5, "material_ids": ["material-a"], "interaction_id": actual_id})
    assert client.calls[1][0] == "resolve", "first use resolves the Search evidence fence"
    assert rag.search("问题", ["material-a"], "interaction-1") == [item()]
    assert client.calls[-1][0] == "resolve"


def test_cached_evidence_resolution_is_split_at_contract_limit():
    values = [item("erv2_" + chr(97 + index) * 32) for index in range(11)]
    client = FakeClient(search_result(values))
    client.delivered = values
    rag.reset_for_tests(client)
    assert len(rag.search("问题", ["material-a"], "interaction-many", top_k=20)) == 11
    resolve_calls = [call for call in client.calls if call[0] == "resolve"]
    assert [len(call[1]) for call in resolve_calls] == [10, 1]
    client.calls.clear()
    assert len(rag.search("问题", ["material-a"], "interaction-many", top_k=20)) == 11
    resolve_calls = [call for call in client.calls if call[0] == "resolve"]
    assert [len(call[1]) for call in resolve_calls] == [10, 1]


def test_expired_evidence_ref_is_cleared_and_searched_again_immediately():
    from zhijun_worker.data_agent_rag_v2 import DataAgentRagV2Error

    class ExpiredEvidence(FakeClient):
        resolve_count = 0

        def evidence_resolve(self, refs):
            self.resolve_count += 1
            if self.resolve_count == 2:
                self.calls.append(("resolve", refs, {}))
                raise DataAgentRagV2Error("EVIDENCE_NOT_FOUND", status=404)
            return super().evidence_resolve(refs)

    client = ExpiredEvidence(search_result([item()]))
    rag.reset_for_tests(client)
    assert rag.search("问题", ["material-a"], "interaction-expired-evidence") == [item()]
    assert rag.search("问题", ["material-a"], "interaction-expired-evidence") == [item()]
    assert [call[0] for call in client.calls].count("search") == 2


def test_first_resolve_context_change_repeats_search_once():
    from zhijun_worker.data_agent_rag_v2 import DataAgentRagV2Error

    class ChangedBeforeFirstResolve(FakeClient):
        resolve_count = 0

        def evidence_resolve(self, refs):
            self.resolve_count += 1
            if self.resolve_count == 1:
                self.calls.append(("resolve", refs, {}))
                raise DataAgentRagV2Error("EVIDENCE_CONTEXT_CHANGED", status=409)
            return super().evidence_resolve(refs)

    client = ChangedBeforeFirstResolve(search_result([item()]))
    rag.reset_for_tests(client)
    assert rag.search("问题", ["material-a"], "interaction-first-resolve") == [item()]
    assert [call[0] for call in client.calls].count("search") == 2
    assert [call[0] for call in client.calls].count("resolve") == 2


def test_top_k_is_part_of_interaction_cache_fingerprint():
    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    rag.search("问题", ["material-a"], "interaction-top-k", top_k=5)
    rag.search("问题", ["material-a"], "interaction-top-k", top_k=3)
    searches = [call for call in client.calls if call[0] == "search"]
    assert [call[2]["top_k"] for call in searches] == [5, 3]

    with pytest.raises(HTTPException) as invalid:
        rag.search("问题", ["material-a"], "interaction-top-k", top_k=21)
    assert invalid.value.detail["code"] == "RAG_TOP_K_INVALID"


def test_more_than_one_hundred_materials_are_searched_in_bounded_batches():
    material_ids = ["material-%03d" % index for index in range(101)]

    class Batched(FakeClient):
        def search(self, query, **kwargs):
            self.calls.append(("search", query, kwargs))
            batch = kwargs["material_ids"]
            ref = "erv2_" + ("a" if len(batch) == 100 else "b") * 32
            value = {**item(ref), "materialId": batch[0], "score": .7 if len(batch) == 100 else .9}
            self.delivered = value
            return search_result([value])

    client = Batched(search_result())
    rag.reset_for_tests(client)
    values = rag.search_materials("问题", material_ids, "conversation-a:r:context", top_k=5)
    searches = [call for call in client.calls if call[0] == "search"]
    assert [len(call[2]["material_ids"]) for call in searches] == [100, 1]
    assert len(values) == 2 and values[0]["score"] == .9
    assert all(re.fullmatch(r"conversation-a:search:[a-f0-9]{32}", call[2]["interaction_id"]) for call in searches)
    assert len({call[2]["interaction_id"] for call in searches}) == len(searches)


def test_confirmation_exposes_only_redacted_state_then_reuses_confirmed_items():
    pending = {"status": "sensitive_confirmation_required", "confirmation": {
        "confirmToken": "scf_" + "s" * 32, "expiresAt": "2030-01-01T00:00:00Z",
        "interactionId": "interaction-2", "policyVersion": 2, "detectorRevision": "detector-2",
        "hits": [{"category": "phone", "materialId": "material-a", "locator": {"page": 1},
                  "redactedPreview": "138****0000", "field": "body"}]}}
    client = FakeClient(pending)
    rag.reset_for_tests(client)
    with pytest.raises(HTTPException) as caught:
        rag.search("电话", ["material-a"], "interaction-2")
    detail = caught.value.detail
    assert detail["code"] == "RAG_SENSITIVE_CONFIRMATION_REQUIRED"
    assert detail["ragV2"]["hits"] == [{"category": "phone", "materialId": "material-a", "location": {"page": 1}, "redactedPreview": "138****0000", "field": "body"}]
    assert "scf_" not in str(detail)
    assert rag.decide("interaction-2", "masked")["status"] == "ready"
    assert rag.search("电话", ["material-a"], "interaction-2")[0]["text"] == "可交付片段"


def test_detection_incomplete_requires_explicit_passed_or_risk_choice():
    notice = {"code": "SENSITIVE_CHECK_INCOMPLETE", "message": "部分暂扣", "retrievedCount": 2,
              "checkedCount": 1, "withheldCount": 1, "retryable": True,
              "riskToken": "scf_" + "r" * 32, "riskEligibleCount": 1, "riskMessage": "风险"}
    client = FakeClient(search_result([item()], notice=notice))
    rag.reset_for_tests(client)
    with pytest.raises(HTTPException) as caught:
        rag.search("问题", ["material-a"], "interaction-3")
    public = caught.value.detail["ragV2"]
    assert public["passedCount"] == 1 and "riskToken" not in str(public)
    assert rag.decide("interaction-3", "continue-passed")["evidenceCount"] == 1

    unavailable = {**notice, "checkedCount": 0, "withheldCount": 2}
    rag.reset_for_tests(FakeClient(search_result(status="sensitive_check_unavailable", notice=unavailable)))
    with pytest.raises(HTTPException):
        rag.search("问题", ["material-a"], "interaction-4")
    with pytest.raises(HTTPException) as empty:
        rag.decide("interaction-4", "continue-passed")
    assert empty.value.detail["code"] == "RAG_NO_PASSED_EVIDENCE"


def test_confirmation_then_detector_failure_is_a_two_stage_choice():
    notice = {"code": "SENSITIVE_CHECK_INCOMPLETE", "message": "另有片段暂扣",
              "retrievedCount": 2, "checkedCount": 1, "withheldCount": 1,
              "retryable": True, "riskToken": "scf_" + "r" * 32,
              "riskEligibleCount": 1, "riskMessage": "风险"}
    pending = {"status": "sensitive_confirmation_required", "confirmation": {
        "confirmToken": "scf_" + "s" * 32, "expiresAt": "2030-01-01T00:00:00Z",
        "interactionId": "interaction-staged", "policyVersion": 2,
        "detectorRevision": "detector-2", "hits": [{
            "category": "person_name", "materialId": "material-a",
            "locator": {"page": 1}, "redactedPreview": "张*", "field": "body",
        }]}, "detectionNotice": notice}
    client = FakeClient(pending)
    rag.reset_for_tests(client)
    with pytest.raises(HTTPException) as first:
        rag.search("问题", ["material-a"], "interaction-staged")
    assert first.value.detail["ragV2"]["status"] == "sensitive_confirmation_required"
    assert rag.decide("interaction-staged", "masked")["status"] == "pending"
    with pytest.raises(HTTPException) as second:
        rag.search("问题", ["material-a"], "interaction-staged")
    prompt = second.value.detail["ragV2"]
    assert prompt["status"] == "sensitive_check_unavailable"
    assert prompt["passedCount"] == 1 and prompt["riskAvailable"] is True
    assert rag.decide("interaction-staged", "risk-release")["evidenceCount"] == 2


def test_expired_confirmation_is_cleared_before_new_search():
    from zhijun_worker.data_agent_rag_v2 import DataAgentRagV2Error

    pending = {"status": "sensitive_confirmation_required", "confirmation": {
        "confirmToken": "scf_" + "s" * 32, "expiresAt": "2030-01-01T00:00:00Z",
        "interactionId": "interaction-expired", "policyVersion": 2,
        "detectorRevision": "detector-2", "hits": [{
            "category": "email", "materialId": "material-a", "locator": {"page": 1},
            "redactedPreview": "a***@example.com", "field": "body",
        }]}}

    class Expiring(FakeClient):
        def confirm(self, *_args, **_kwargs):
            raise DataAgentRagV2Error("CONFIRM_TOKEN_EXPIRED", status=409)

    client = Expiring(pending)
    rag.reset_for_tests(client)
    with pytest.raises(HTTPException):
        rag.search("问题", ["material-a"], "interaction-expired")
    with pytest.raises(HTTPException) as expired:
        rag.decide("interaction-expired", "masked")
    assert expired.value.detail["code"] == "CONFIRM_TOKEN_EXPIRED"
    with pytest.raises(HTTPException):
        rag.search("问题", ["material-a"], "interaction-expired")
    assert [call[0] for call in client.calls].count("search") == 2


def test_risk_release_requires_server_token_and_preserves_unverified_marker():
    notice = {"code": "SENSITIVE_CHECK_INCOMPLETE", "message": "暂扣", "retrievedCount": 1,
              "checkedCount": 0, "withheldCount": 1, "retryable": True,
              "riskToken": "scf_" + "r" * 32, "riskEligibleCount": 1}
    client = FakeClient(search_result(status="sensitive_check_unavailable", notice=notice))
    rag.reset_for_tests(client)
    with pytest.raises(HTTPException):
        rag.search("问题", ["material-a"], "interaction-5")
    rag.decide("interaction-5", "risk-release")
    assert rag.search("问题", ["material-a"], "interaction-5")[0]["verificationStatus"] == "unverified"
    assert [call[0] for call in client.calls].count("risk") == 1


def test_search_rejects_unverified_or_mismatched_fence_items():
    rag.reset_for_tests(FakeClient(search_result([item(verification="unverified")])))
    with pytest.raises(HTTPException) as unverified:
        rag.search("问题", ["material-a"], "interaction-unverified")
    assert unverified.value.detail["code"] == "RAG_V2_CONTRACT_INVALID"

    wrong_fence = {**item(), "detectorRevision": "changed-detector"}
    rag.reset_for_tests(FakeClient(search_result([wrong_fence])))
    with pytest.raises(HTTPException) as changed:
        rag.search("问题", ["material-a"], "interaction-wrong-fence")
    assert changed.value.detail["code"] == "RAG_V2_CONTRACT_INVALID"


def test_confirm_and_risk_release_must_match_original_search_fence():
    pending = {"status": "sensitive_confirmation_required", "confirmation": {
        "confirmToken": "scf_" + "s" * 32, "expiresAt": "2030-01-01T00:00:00Z",
        "interactionId": "interaction-confirm-fence", "policyVersion": 2,
        "detectorRevision": "detector-2", "hits": [{
            "category": "email", "materialId": "material-a", "locator": {"page": 1},
            "redactedPreview": "a***@example.com", "field": "body",
        }]}}

    class ChangedConfirm(FakeClient):
        def confirm(self, token, **kwargs):
            value = super().confirm(token, **kwargs)
            value["detectorRevision"] = "changed-detector"
            value["items"] = [{**value["items"][0], "detectorRevision": "changed-detector"}]
            return value

    rag.reset_for_tests(ChangedConfirm(pending))
    with pytest.raises(HTTPException):
        rag.search("问题", ["material-a"], "interaction-confirm-fence")
    with pytest.raises(HTTPException) as confirm_changed:
        rag.decide("interaction-confirm-fence", "masked")
    assert confirm_changed.value.detail["code"] == "RAG_V2_CONTRACT_INVALID"

    notice = {"code": "SENSITIVE_CHECK_INCOMPLETE", "message": "暂扣", "retrievedCount": 1,
              "checkedCount": 0, "withheldCount": 1, "retryable": True,
              "riskToken": "scf_" + "r" * 32, "riskEligibleCount": 1}

    class ChangedRisk(FakeClient):
        def confirm_unverified(self, token):
            value = super().confirm_unverified(token)
            value["policyVersion"] = 3
            value["items"] = [{**value["items"][0], "policyVersion": 3}]
            return value

    rag.reset_for_tests(ChangedRisk(search_result(status="sensitive_check_unavailable", notice=notice)))
    with pytest.raises(HTTPException):
        rag.search("问题", ["material-a"], "interaction-risk-fence")
    with pytest.raises(HTTPException) as risk_changed:
        rag.decide("interaction-risk-fence", "risk-release")
    assert risk_changed.value.detail["code"] == "RAG_RISK_RESULT_UNKNOWN"
    assert risk_changed.value.detail["requiresFreshSearch"] is True
    assert risk_changed.value.detail["retryable"] is False


def test_confirm_and_risk_release_cannot_escape_search_material_filter():
    pending = {"status": "sensitive_confirmation_required", "confirmation": {
        "confirmToken": "scf_" + "s" * 32, "expiresAt": "2030-01-01T00:00:00Z",
        "interactionId": "interaction-confirm-scope", "policyVersion": 2,
        "detectorRevision": "detector-2", "hits": [{
            "category": "email", "materialId": "material-a", "locator": {"page": 1},
            "redactedPreview": "a***@example.com", "field": "body",
        }]}}

    class EscapingConfirm(FakeClient):
        def confirm(self, token, **kwargs):
            value = super().confirm(token, **kwargs)
            value["items"] = [{**value["items"][0], "materialId": "material-outside"}]
            return value

    rag.reset_for_tests(EscapingConfirm(pending))
    with pytest.raises(HTTPException):
        rag.search("问题", ["material-a"], "interaction-confirm-scope")
    with pytest.raises(HTTPException) as confirm_scope:
        rag.decide("interaction-confirm-scope", "masked")
    assert confirm_scope.value.detail["code"] == "RAG_V2_CONTRACT_INVALID"

    notice = {"code": "SENSITIVE_CHECK_INCOMPLETE", "message": "暂扣", "retrievedCount": 1,
              "checkedCount": 0, "withheldCount": 1, "retryable": True,
              "riskToken": "scf_" + "r" * 32, "riskEligibleCount": 1}

    class EscapingRisk(FakeClient):
        def confirm_unverified(self, token):
            value = super().confirm_unverified(token)
            value["items"] = [{**value["items"][0], "materialId": "material-outside"}]
            return value

    rag.reset_for_tests(EscapingRisk(search_result(status="sensitive_check_unavailable", notice=notice)))
    with pytest.raises(HTTPException):
        rag.search("问题", ["material-a"], "interaction-risk-scope")
    with pytest.raises(HTTPException) as risk_scope:
        rag.decide("interaction-risk-scope", "risk-release")
    assert risk_scope.value.detail["code"] == "RAG_RISK_RESULT_UNKNOWN"
    assert risk_scope.value.detail["requiresFreshSearch"] is True
    assert risk_scope.value.detail["retryable"] is False


def test_risk_release_count_must_match_frozen_eligible_count():
    notice = {"code": "SENSITIVE_CHECK_INCOMPLETE", "message": "暂扣", "retrievedCount": 2,
              "checkedCount": 0, "withheldCount": 2, "retryable": True,
              "riskToken": "scf_" + "r" * 32, "riskEligibleCount": 2}
    client = FakeClient(search_result(status="sensitive_check_unavailable", notice=notice))
    rag.reset_for_tests(client)
    with pytest.raises(HTTPException):
        rag.search("问题", ["material-a"], "interaction-risk-count")
    with pytest.raises(HTTPException) as mismatch:
        rag.decide("interaction-risk-count", "risk-release")
    assert mismatch.value.detail["code"] == "RAG_RISK_RESULT_UNKNOWN"
    assert mismatch.value.detail["requiresFreshSearch"] is True
    assert mismatch.value.detail["retryable"] is False


def test_workspace_legacy_attachment_context_is_disabled_for_retrieval_only(monkeypatch):
    from mindos import chat_imports

    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    monkeypatch.setenv("ZHIJUN_WORKSPACE_ID", "workspace-test")
    with pytest.raises(HTTPException) as disabled:
        chat_imports.attachment_context(
            [{"materialId": "material-a", "version": 1}], "global", "问题",
            external=True, interaction_id="interaction-attachment",
        )
    assert disabled.value.detail["code"] == "RAG_RETRIEVAL_ONLY"
    assert client.calls == []


def test_workspace_implicit_material_context_reviews_app_scoped_v2_search(monkeypatch):
    from mindos import chat_imports
    from mindos.stores import chat_import_store
    from mindos.zhijun import context_sources

    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    monkeypatch.setenv("ZHIJUN_WORKSPACE_ID", "workspace-test")

    class Store:
        def protected_ids(self, scope):
            raise AssertionError("Local imports are not the App Search ACL")

    monkeypatch.setattr(chat_import_store, "ChatImportStore", lambda _convs: Store())
    monkeypatch.setattr(chat_imports, "require_material", lambda ident, scope: {
        "materialId": ident, "versionNumber": 1, "fileName": "资料"
    })
    router = SimpleNamespace(
        convs=object(), scope="scope-a", cid="conversation-a",
        ref=lambda kind, ident, **extra: {"kind": kind, "id": ident, **extra},
    )
    with pytest.raises(HTTPException) as pending:
        context_sources.material_candidates(
            router, ["问题"], interaction_id="conversation-a:r:request:context"
        )
    assert pending.value.detail["code"] == "RAG_MATERIAL_REVIEW_REQUIRED"
    prompt = pending.value.detail["ragV2"]
    rag.decide(prompt["interactionId"], "use-selected", selected_preview_ids=[prompt["items"][0]["previewId"]])
    candidates = context_sources.material_candidates(
        router, ["问题"], interaction_id="conversation-a:r:request:context"
    )
    assert candidates[0]["text"] == "可交付片段"
    assert candidates[0]["material"]["evidenceRef"].startswith("erv2_")
    assert client.calls[0] == (
        "search", "问题", {
            "top_k": 5,
            "material_ids": [],  # Client omits filters for the full App ACL.
            "interaction_id": rag._entries["conversation-a:r:request:context"].actual_search_id,
        },
    )


def test_rag_decision_is_bound_to_current_conversation(monkeypatch):
    from mindos import chat_import_routes

    interaction = "conversation-a:r:request:context:0"
    class Store:
        def __init__(self): self.updates = []
        def batches(self, _conversation_id):
            return [{"id": "batch-a", "rag_prompt_json": __import__("json").dumps({
                "interactionId": interaction
            })}]
        def update(self, *args, **kwargs): self.updates.append((args, kwargs))

    store = Store()
    monkeypatch.setattr(chat_import_routes, "_device_scope_of", lambda _request: "scope-a")
    monkeypatch.setattr(chat_import_routes.svc, "require_conversation", lambda *_args: store)
    monkeypatch.setattr(rag, "decide", lambda interaction_id, action: {
        "interactionId": interaction_id, "status": "cancelled" if action == "cancel" else "ready"
    })
    request = SimpleNamespace()
    mismatched = chat_import_routes.RagV2Decision(
        interactionId="conversation-b:r:request:context:0", action="masked"
    )
    with pytest.raises(HTTPException) as caught:
        chat_import_routes.rag_v2_decision("conversation-a", mismatched, request)
    assert caught.value.detail["code"] == "RAG_CONFIRMATION_CONTEXT_CHANGED"

    matched = chat_import_routes.RagV2Decision(
        interactionId="conversation-a:r:request:context:0", action="masked"
    )
    assert chat_import_routes.rag_v2_decision(
        "conversation-a", matched, request
    )["interactionId"].startswith("conversation-a:")
    assert store.updates[-1][0][1] == "queued"

    cancelled = chat_import_routes.RagV2Decision(
        interactionId=interaction, action="cancel"
    )
    chat_import_routes.rag_v2_decision("conversation-a", cancelled, request)
    assert store.updates[-1][0][1] == "paused"


def test_workspace_upload_transports_bytes_then_calls_real_v2_job(monkeypatch):
    from zhijun_worker import attachments
    from zhijun_worker import data_agent_rag_v2

    content = b"hello-rag-v2"
    operations = []

    class Capability:
        def call(self, name, payload):
            operations.append((name, payload))
            if name == "uploads.describe":
                return {"fileName": "sample.txt", "size": len(content)}
            if name == "uploads.read":
                import base64
                offset, limit = payload["offset"], payload["limit"]
                chunk = content[offset:offset + limit]
                return {"id": payload["id"], "data": base64.b64encode(chunk).decode(),
                        "size": len(content), "offset": offset,
                        "hasMore": offset + len(chunk) < len(content)}
            raise AssertionError("legacy material capability used: " + name)

    class Client:
        def upload_bytes(self, filename, body, **kwargs):
            assert filename == "sample.txt" and body == content
            assert kwargs["idempotency_key"].startswith("zj-upload-")
            return {"jobId": "aij_" + "a" * 32, "materialId": "material-a",
                    "materialVersion": 1,
                    "statusUrl": "/v1/agent/apps/material-jobs/aij_" + "a" * 32}

    class Store:
        def __init__(self): self.updates = []
        def file_update(self, *args, **kwargs): self.updates.append((args, kwargs))
        def protect(self, *args): self.protected = args
        def update(self, *args, **kwargs): pass
        def get(self, _batch): return {"files": [{"id": "file-123456789012", "name": "sample.txt", "size": len(content), "material_id": "material-a", "version": 1, "state": "reading", "error": None, "job_id": "aij_" + "a" * 32}]}

    store = Store()
    batch = {"state": "uploading", "files": [{"id": "file-123456789012", "name": "sample.txt", "size": len(content), "material_id": None}]}
    monkeypatch.setattr(attachments, "require", lambda: Capability())
    monkeypatch.setitem(sys.modules, "mindos.chat_import_routes", SimpleNamespace(batch_for=lambda *_: (store, batch)))
    monkeypatch.setattr(data_agent_rag_v2, "configured_client", lambda: Client())
    monkeypatch.setattr("mindos.chat_imports.file_view", lambda item, _scope: item)
    monkeypatch.setattr("mindos.domain_scope._device_scope_of", lambda _request: "global")
    request = SimpleNamespace(state=SimpleNamespace(zhijun_uploads={"files": [{"uploadId": "a" * 32}]}))
    result = attachments.upload_import("conv", "batch", "file-123456789012", request)
    assert result["job_id"].startswith("aij_")
    assert [name for name, _ in operations] == ["uploads.describe", "uploads.read"]
    assert store.updates[-1][0][1] == "reading"
