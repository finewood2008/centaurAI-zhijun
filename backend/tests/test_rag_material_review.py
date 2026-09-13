"""Material use approval is independent from sensitive-content delivery."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from mindos import data_agent_rag as rag
from tests.test_data_agent_rag_v2_flow import FakeClient, item, search_result


INTERACTION = "conversation-a:tool:one"


@pytest.fixture(autouse=True)
def reset():
    rag.reset_for_tests()
    yield
    rag.reset_for_tests()


def pending(**kwargs):
    with pytest.raises(HTTPException) as caught:
        rag.search("验收要求", [], INTERACTION, require_review=True, **kwargs)
    assert caught.value.status_code == 409
    return caught.value.detail


def sensitive():
    return {"status": "sensitive_confirmation_required", "confirmation": {
        "confirmToken": "scf_" + "s" * 32, "expiresAt": "2030-01-01T00:00:00Z",
        "interactionId": INTERACTION, "policyVersion": 2, "detectorRevision": "detector-2",
        "hits": [{"category": "email", "materialId": "material-a", "locator": {"page": 1},
                  "redactedPreview": "a***@example.com", "field": "body"}]}}


def public_id():
    return pending()["ragV2"]["interactionId"]


def test_normal_evidence_requires_review_and_only_chosen_evidence_replays():
    first, second = item(), item("erv2_" + "b" * 32)
    second["materialId"] = "material-b"
    first["text"] = "正文" * 1000
    client = FakeClient(search_result([first, second]))
    client.delivered = [first, second]
    rag.reset_for_tests(client)
    prompt = pending(review_context={"scopeLabel": "当前项目"})
    assert prompt["code"] == "RAG_MATERIAL_REVIEW_REQUIRED"
    public = prompt["ragV2"]
    assert public["scopeLabel"] == "当前项目" and public["query"] == "验收要求"
    assert public["hits"] == [] and public["canReadOriginal"] is False
    assert len(public["items"][0]["preview"]) == 500
    assert "evidenceRef" not in str(public) and "erv2_" not in str(public)
    assert not any(call[0] == "resolve" for call in client.calls)
    assert rag.reviewed_material("conversation-a", "material-a", 1) is None
    selected = public["items"][1]["previewId"]
    assert rag.decide(public_id(), "use-selected", [selected])["evidenceCount"] == 1
    assert rag.search("验收要求", [], INTERACTION, require_review=True) == [second]
    assert rag.reviewed_material("conversation-a", "material-a", 1) is None
    assert rag.reviewed_material("conversation-b", "material-b", 1) is None
    assert rag.reviewed_material("conversation-a", "material-b", 1,
                                 interaction_id="conversation-a:other") is None
    reviewed = rag.reviewed_material("conversation-a", "material-b", 1, interaction_id=INTERACTION)
    assert reviewed["items"] == [second]
    assert len([call for call in client.calls if call[0] == "resolve"]) == 3


@pytest.mark.parametrize("state", ["no_results", "sensitive_content_blocked"])
def test_empty_or_blocked_outcomes_are_user_visible(state):
    client = FakeClient(search_result(status=state))
    rag.reset_for_tests(client)
    prompt = pending()["ragV2"]
    assert prompt["outcome"] == state and prompt["items"] == []
    with pytest.raises(HTTPException):
        rag.decide(public_id(), "use-selected", [])
    assert rag.decide(public_id(), "without-materials")["evidenceCount"] == 0
    assert rag.search("验收要求", [], INTERACTION, require_review=True) == []
    assert len(client.calls) == 1


@pytest.mark.parametrize("selection", [None, [], ["prv_" + "a" * 24], [1], ["bad"]])
def test_invalid_selection_does_not_resolve_or_discard_prompt(selection):
    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    pending()
    with pytest.raises(HTTPException) as caught:
        rag.decide(public_id(), "use-selected", selection)
    assert caught.value.status_code == 422
    assert not any(call[0] == "resolve" for call in client.calls)
    assert pending()["code"] == "RAG_MATERIAL_REVIEW_REQUIRED"


def test_duplicate_selection_and_old_continue_cannot_bypass_review():
    rag.reset_for_tests(FakeClient(search_result([item()])))
    preview = pending()["ragV2"]["items"][0]["previewId"]
    for action, selected in (("use-selected", [preview, preview]), ("continue-passed", None)):
        with pytest.raises(HTTPException):
            rag.decide(public_id(), action, selected)
    assert rag.reviewed_material("conversation-a", "material-a", 1) is None


def test_sensitive_confirmation_then_material_review_does_not_deliver_early():
    client = FakeClient(sensitive())
    rag.reset_for_tests(client)
    assert pending()["code"] == "RAG_SENSITIVE_CONFIRMATION_REQUIRED"
    assert rag.decide(public_id(), "masked")["status"] == "pending"
    prompt = pending()["ragV2"]
    assert prompt["status"] == "materials_confirmation_required"
    assert prompt["deliveryMode"] == "masked"
    assert rag.reviewed_material("conversation-a", "material-a", 1) is None
    assert rag.decide(public_id(), "use-selected", [prompt["items"][0]["previewId"]])["status"] == "ready"
    assert rag.search("验收要求", [], INTERACTION, require_review=True) == [item()]
    assert [call[0] for call in client.calls].count("confirm") == 1


def test_without_materials_never_calls_sensitive_confirm():
    client = FakeClient(sensitive())
    rag.reset_for_tests(client)
    pending()
    assert rag.decide(public_id(), "without-materials")["status"] == "ready"
    assert rag.search("验收要求", [], INTERACTION, require_review=True) == []
    assert not any(call[0] in {"confirm", "resolve", "risk"} for call in client.calls)


@pytest.mark.parametrize("field,value", [("text", "新正文"), ("title", "新标题"), ("locator", {"page": 2})])
def test_changed_evidence_requires_fresh_review(field, value):
    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    preview = pending()["ragV2"]["items"][0]["previewId"]
    client.delivered = {**item(), field: value}
    with pytest.raises(HTTPException) as caught:
        rag.decide(public_id(), "use-selected", [preview])
    assert caught.value.detail["code"] == "RAG_REVIEW_CONTEXT_CHANGED"
    assert rag.reviewed_material("conversation-a", "material-a", 1) is None
    assert INTERACTION not in rag._entries


def test_helper_rechecks_policy_and_ttl_and_never_accepts_legacy_auto_delivery():
    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    rag.search("验收要求", [], "conversation-a:legacy")
    assert rag.reviewed_material("conversation-a", "material-a", 1) is None
    preview = pending()["ragV2"]["items"][0]["previewId"]
    rag.decide(public_id(), "use-selected", [preview])
    original_resolve = client.evidence_resolve
    client.evidence_resolve = lambda refs: {**original_resolve(refs), "policyVersion": 3}
    assert rag.reviewed_material("conversation-a", "material-a", 1) is None
    assert INTERACTION not in rag._entries
    client.evidence_resolve = original_resolve
    preview = pending()["ragV2"]["items"][0]["previewId"]
    rag.decide(public_id(), "use-selected", [preview])
    rag._entries[INTERACTION].created_at -= 901
    assert rag.reviewed_material("conversation-a", "material-a", 1) is None


def test_concurrent_confirmation_executes_once_without_global_lock():
    entered, release = threading.Event(), threading.Event()

    class SlowClient(FakeClient):
        def confirm(self, token, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().confirm(token, **kwargs)

    client = SlowClient(sensitive())
    rag.reset_for_tests(client)
    pending()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(rag.decide, public_id(), "masked")
        assert entered.wait(3)
        try:
            with pytest.raises(HTTPException) as duplicate:
                rag.decide(public_id(), "original")
            assert duplicate.value.detail["code"] == "RAG_DECISION_IN_PROGRESS"
            assert rag._lock.acquire(blocking=False)
            rag._lock.release()
        finally:
            release.set()
        assert future.result()["status"] == "pending"
    assert [call[0] for call in client.calls].count("confirm") == 1


def test_route_selection_schema_is_strict():
    from mindos.chat_import_routes import RagV2Decision
    preview = "prv_" + "a" * 24
    assert RagV2Decision(interactionId=INTERACTION, action="use-selected",
                         selectedPreviewIds=[preview]).selectedPreviewIds == [preview]
    for payload in (
        {"action": "use-selected"},
        {"action": "use-selected", "selectedPreviewIds": []},
        {"action": "use-selected", "selectedPreviewIds": [preview, preview]},
        {"action": "use-selected", "selectedPreviewIds": [123]},
        {"action": "without-materials", "selectedPreviewIds": [preview]},
        {"action": "use-selected", "selectedPreviewIds": ["erv2_secret"]},
    ):
        with pytest.raises(ValidationError):
            RagV2Decision(interactionId=INTERACTION, **payload)


def test_replay_also_detects_post_approval_content_change():
    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    preview = pending()["ragV2"]["items"][0]["previewId"]
    rag.decide(public_id(), "use-selected", [preview])
    client.delivered = {**item(), "text": "审批后替换的正文"}
    with pytest.raises(HTTPException) as caught:
        rag.search("验收要求", [], INTERACTION, require_review=True)
    assert caught.value.detail["code"] == "RAG_REVIEW_CONTEXT_CHANGED"
    assert INTERACTION not in rag._entries


def test_expired_evidence_is_not_accepted_even_if_service_returns_it():
    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    preview = pending()["ragV2"]["items"][0]["previewId"]
    original = client.evidence_resolve

    def expired(refs):
        result = original(refs)
        result["items"][0]["evidenceExpiresAt"] = "2000-01-01T00:00:00Z"
        return result

    client.evidence_resolve = expired
    with pytest.raises(HTTPException) as caught:
        rag.decide(public_id(), "use-selected", [preview])
    assert caught.value.detail["code"] == "RAG_V2_CONTRACT_INVALID"
    assert INTERACTION not in rag._entries


def test_risk_release_requires_another_review_and_cannot_be_consumed_twice():
    notice = {"code": "SENSITIVE_CHECK_INCOMPLETE", "message": "暂不可用",
              "retrievedCount": 1, "checkedCount": 0, "withheldCount": 1, "retryable": True,
              "riskToken": "scf_" + "r" * 32, "riskEligibleCount": 1}
    client = FakeClient(search_result(status="sensitive_check_unavailable", notice=notice))
    rag.reset_for_tests(client)
    assert pending()["ragV2"]["riskAvailable"] is True
    assert rag.decide(public_id(), "risk-release")["status"] == "pending"
    prompt = pending()["ragV2"]
    assert prompt["items"][0]["verificationStatus"] == "unverified"
    assert prompt["items"][0]["containsSensitive"] is True
    with pytest.raises(HTTPException):
        rag.decide(public_id(), "risk-release")
    assert [call[0] for call in client.calls].count("risk") == 1
    assert rag.decide(public_id(), "without-materials")["evidenceCount"] == 0


def test_cancel_discards_pending_without_delivery():
    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    pending()
    assert rag.decide(public_id(), "cancel")["status"] == "cancelled"
    assert INTERACTION not in rag._entries
    assert rag.reviewed_material("conversation-a", "material-a", 1) is None


def test_reviewed_evidence_validates_all_selected_refs_in_one_call():
    first = item()
    second = {**item("erv2_" + "b" * 32), "materialId": "material-b"}
    client = FakeClient(search_result([first, second]))
    client.delivered = [first, second]
    rag.reset_for_tests(client)
    previews = pending()["ragV2"]["items"]
    rag.decide(public_id(), "use-selected", [value["previewId"] for value in previews])
    client.calls.clear()
    assert rag.reviewed_evidence("conversation-a", interaction_id=INTERACTION) == [first, second]
    assert client.calls == [("resolve", [first["evidenceRef"], second["evidenceRef"]], {})]
    client.calls.clear()
    assert rag.reviewed_material("conversation-a", "material-a", 1, interaction_id=INTERACTION)["items"] == [first]
    assert client.calls == [("resolve", [first["evidenceRef"], second["evidenceRef"]], {})]
    assert rag.reviewed_evidence("other-conversation", interaction_id=INTERACTION) is None


def test_one_expired_selected_ref_invalidates_entire_review_not_just_material():
    first = item()
    second = {**item("erv2_" + "b" * 32), "materialId": "material-b"}
    client = FakeClient(search_result([first, second]))
    client.delivered = [first, second]
    rag.reset_for_tests(client)
    previews = pending()["ragV2"]["items"]
    rag.decide(public_id(), "use-selected", [value["previewId"] for value in previews])
    original = client.evidence_resolve

    def partially_expired(refs):
        result = original(refs)
        for value in result["items"]:
            if value["materialId"] == "material-b":
                value["evidenceExpiresAt"] = "2000-01-01T00:00:00Z"
        return result

    client.evidence_resolve = partially_expired
    assert rag.reviewed_material("conversation-a", "material-a", 1, interaction_id=INTERACTION) is None
    assert INTERACTION not in rag._entries
