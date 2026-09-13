"""Stable local review state must never reuse a real REST Search identity."""
import hashlib
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mindos import data_agent_rag as rag
from zhijun_worker.data_agent_rag_v2 import DataAgentRagV2Error
from tests.test_data_agent_rag_v2_flow import FakeClient, item, search_result


HANDLE = "conversation-a:r:one:context"


@pytest.fixture(autouse=True)
def clean():
    rag.reset_for_tests()
    yield
    rag.reset_for_tests()


def search_pending(query="验收要求"):
    with pytest.raises(HTTPException) as caught:
        rag.search(query, [], HANDLE, require_review=True)
    assert caught.value.status_code == 409
    return caught.value.detail["ragV2"]


def sensitive_result():
    return {"status": "sensitive_confirmation_required", "confirmation": {
        "confirmToken": "scf_" + "s" * 32, "expiresAt": "2030-01-01T00:00:00Z",
        "interactionId": "placeholder", "policyVersion": 2, "detectorRevision": "detector-2",
        "hits": [{"category": "email", "materialId": "material-a", "locator": {"page": 1},
                  "redactedPreview": "a***@example.com", "field": "body"}]}}


def test_search_attempt_and_renderer_id_are_unique_but_cached_replay_is_stable():
    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    first = search_pending()
    actual = first["interactionId"]
    assert re.fullmatch(r"conversation-a:search:[a-f0-9]{32}", actual)
    assert actual != HANDLE and len(actual) <= 128
    assert client.calls[0][2]["interaction_id"] == actual
    assert rag._entries[HANDLE].actual_search_id == actual
    assert search_pending() == first
    assert [call[0] for call in client.calls].count("search") == 1
    rag.decide(actual, "retry")
    second = search_pending()
    assert second["interactionId"] != actual
    assert second["items"][0]["previewId"] != first["items"][0]["previewId"]
    assert [call[0] for call in client.calls].count("search") == 2


@pytest.mark.parametrize("action", ["masked", "original", "without-materials", "cancel"])
def test_old_prompt_cannot_decide_new_attempt_or_use_stable_internal_alias(action):
    client = FakeClient(sensitive_result())
    rag.reset_for_tests(client)
    first = search_pending()
    rag.decide(first["interactionId"], "retry")
    second = search_pending()
    for stale in (first["interactionId"], HANDLE):
        with pytest.raises(HTTPException) as caught:
            rag.decide(stale, action)
        assert caught.value.detail["code"] == "RAG_CONFIRMATION_EXPIRED"
    assert rag._entries[HANDLE].actual_search_id == second["interactionId"]
    assert not any(call[0] == "confirm" for call in client.calls)


def test_wrong_search_confirmation_echo_is_rejected_without_token_consumption():
    class WrongEcho(FakeClient):
        def search(self, query, **kwargs):
            result = super().search(query, **kwargs)
            result["confirmation"]["interactionId"] = HANDLE
            return result

    client = WrongEcho(sensitive_result())
    rag.reset_for_tests(client)
    with pytest.raises(HTTPException) as caught:
        rag.search("验收要求", [], HANDLE, require_review=True)
    assert caught.value.detail["code"] == "RAG_V2_CONTRACT_INVALID"
    assert HANDLE not in rag._entries
    assert [call[0] for call in client.calls] == ["search"]


def test_confirm_network_retry_reuses_actual_search_and_same_idempotency_key():
    class LostResponse(FakeClient):
        def confirm(self, token, **kwargs):
            result = super().confirm(token, **kwargs)
            if len([call for call in self.calls if call[0] == "confirm"]) == 1:
                raise DataAgentRagV2Error("UPSTREAM_UNAVAILABLE", status=503, retryable=True)
            return result

    client = LostResponse(sensitive_result())
    rag.reset_for_tests(client)
    prompt = search_pending()
    actual = prompt["interactionId"]
    with pytest.raises(HTTPException):
        rag.decide(actual, "masked")
    assert rag.decide(actual, "masked")["status"] == "pending"
    calls = [call for call in client.calls if call[0] == "confirm"]
    token = "scf_" + "s" * 32
    expected = "zj-" + hashlib.sha256((actual + ":masked:" + token).encode()).hexdigest()[:48]
    assert [call[2]["idempotency_key"] for call in calls] == [expected, expected]
    assert [call[0] for call in client.calls].count("search") == 1
    assert search_pending()["interactionId"] == actual


def test_changed_desensitize_or_new_search_produces_new_confirm_key():
    class Retryable(FakeClient):
        def confirm(self, token, **kwargs):
            super().confirm(token, **kwargs)
            raise DataAgentRagV2Error("UPSTREAM_UNAVAILABLE", status=503, retryable=True)

    client = Retryable(sensitive_result())
    rag.reset_for_tests(client)
    first = search_pending()["interactionId"]
    for action in ("original", "masked"):
        with pytest.raises(HTTPException):
            rag.decide(first, action)
    rag.decide(first, "retry")
    second = search_pending()["interactionId"]
    with pytest.raises(HTTPException):
        rag.decide(second, "masked")
    keys = [call[2]["idempotency_key"] for call in client.calls if call[0] == "confirm"]
    assert len(set(keys)) == 3


def test_stale_initial_evidence_and_cached_evidence_searches_get_new_ids():
    class Stale(FakeClient):
        stale = True

        def evidence_resolve(self, refs):
            if self.stale:
                self.stale = False
                raise DataAgentRagV2Error("EVIDENCE_CONTEXT_CHANGED", status=409)
            return super().evidence_resolve(refs)

    client = Stale(search_result([item()]))
    rag.reset_for_tests(client)
    assert rag.search("验收要求", [], HANDLE) == [item()]
    client.stale = True
    assert rag.search("验收要求", [], HANDLE) == [item()]
    ids = [call[2]["interaction_id"] for call in client.calls if call[0] == "search"]
    assert len(ids) == 3 and len(set(ids)) == 3


def test_approval_replay_resolves_without_new_search_or_changed_actual_id():
    client = FakeClient(search_result([item()]))
    rag.reset_for_tests(client)
    prompt = search_pending()
    rag.decide(prompt["interactionId"], "use-selected", [prompt["items"][0]["previewId"]])
    assert rag.search("验收要求", [], HANDLE, require_review=True) == [item()]
    assert rag._entries[HANDLE].actual_search_id == prompt["interactionId"]
    assert len([call for call in client.calls if call[0] == "search"]) == 1
    assert rag.reviewed_evidence("conversation-a", interaction_id=HANDLE) == [item()]


def test_parallel_same_handle_cannot_replace_pending_attempt():
    entered, release = threading.Event(), threading.Event()

    class Slow(FakeClient):
        def search(self, query, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().search(query, **kwargs)

    client = Slow(search_result([item()]))
    rag.reset_for_tests(client)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(search_pending)
        assert entered.wait(3)
        try:
            with pytest.raises(HTTPException) as caught:
                rag.search("其他问题", [], HANDLE, require_review=True)
            assert caught.value.detail["code"] == "RAG_SEARCH_IN_PROGRESS"
        finally:
            release.set()
        prompt = future.result()
    assert [call[0] for call in client.calls].count("search") == 1
    assert rag._entries[HANDLE].actual_search_id == prompt["interactionId"]


@pytest.mark.parametrize("status,code,fragment", [
    (401, "AUTHENTICATION_REQUIRED", "长期凭据"),
    (403, "CAPABILITY_DENIED", "能力"),
    (403, "SENSITIVE_ORIGINAL_CAPABILITY_DENIED", "脱敏"),
    (403, "SENSITIVE_ORIGINAL_POLICY_DENIED", "策略禁止"),
    (422, "VALIDATION_ERROR", "字段"),
    (503, "SENSITIVE_CHECK_UNAVAILABLE", "检测服务"),
    (503, "AUTH_CONTEXT_UNAVAILABLE", "原凭据"),
])
def test_error_mapping_preserves_safe_code_trace_and_retry_after(status, code, fragment):
    error = DataAgentRagV2Error(code, status=status, retryable=status == 503,
                               retry_after=12, trace_id="atr_example", message="Secret=must-not-appear")
    converted = rag._client_error(error)
    assert converted.status_code == status
    assert converted.detail["code"] == code and converted.detail["traceId"] == "atr_example"
    assert fragment in converted.detail["detail"]
    assert converted.detail["retryAfter"] == 12
    assert converted.headers == {"Retry-After": "12"}
    assert "must-not-appear" not in str(converted.detail)


@pytest.mark.parametrize("retry_after", [True, -1, "1\r\nAuthorization: secret", None])
def test_error_metadata_rejects_unsafe_values(retry_after):
    converted = rag._client_error(SimpleNamespace(status="bad", code="SECRET\nTOKEN",
                                                  retry_after=retry_after, trace_id="atr_x\r\nsecret"))
    assert converted.status_code == 503 and converted.detail["code"] == "RAG_V2_UNAVAILABLE"
    assert "traceId" not in converted.detail and "retryAfter" not in converted.detail
    assert converted.headers is None


@pytest.mark.parametrize("retry_after", [0, 86401, 604800, 9007199254740991])
def test_error_mapping_preserves_long_nonnegative_retry_after(retry_after):
    converted = rag._client_error(DataAgentRagV2Error("SENSITIVE_CHECK_UNAVAILABLE", status=503,
                                                     retry_after=retry_after))
    assert converted.detail["retryAfter"] == retry_after
    assert converted.headers == {"Retry-After": str(retry_after)}


def test_auth_context_unavailable_does_not_recreate_credentials_or_auto_retry():
    class Unavailable(FakeClient):
        def search(self, query, **kwargs):
            super().search(query, **kwargs)
            raise DataAgentRagV2Error("AUTH_CONTEXT_UNAVAILABLE", status=503, retry_after=5, trace_id="atr_auth")

    client = Unavailable(search_result())
    rag.reset_for_tests(client)
    for _ in range(2):
        with pytest.raises(HTTPException) as caught:
            rag.search("验收要求", [], HANDLE, require_review=True)
        assert caught.value.headers == {"Retry-After": "5"}
    calls = [call for call in client.calls if call[0] == "search"]
    assert len(calls) == 2 and calls[0][2]["interaction_id"] != calls[1][2]["interaction_id"]


def test_risk_response_loss_never_reconsumes_token():
    notice = {"code": "SENSITIVE_CHECK_INCOMPLETE", "message": "暂不可用",
              "retrievedCount": 1, "checkedCount": 0, "withheldCount": 1, "retryable": True,
              "riskToken": "scf_" + "r" * 32, "riskEligibleCount": 1}

    class LostRisk(FakeClient):
        def confirm_unverified(self, token):
            super().confirm_unverified(token)
            raise DataAgentRagV2Error("UPSTREAM_UNAVAILABLE", status=503, retryable=True,
                                      retry_after=604800, trace_id="atr_risk_lost")

    client = LostRisk(search_result(status="sensitive_check_unavailable", notice=notice))
    rag.reset_for_tests(client)
    actual = search_pending()["interactionId"]
    with pytest.raises(HTTPException) as lost:
        rag.decide(actual, "risk-release")
    assert lost.value.status_code == 503
    assert lost.value.detail["code"] == "RAG_RISK_RESULT_UNKNOWN"
    assert lost.value.detail["requiresFreshSearch"] is True
    assert lost.value.detail["retryable"] is False
    assert lost.value.detail["traceId"] == "atr_risk_lost"
    assert lost.value.detail["retryAfter"] == 604800
    assert lost.value.headers == {"Retry-After": "604800"}
    with pytest.raises(HTTPException) as stale:
        rag.decide(actual, "risk-release")
    assert stale.value.detail["code"] == "RAG_CONFIRMATION_EXPIRED"
    assert [call[0] for call in client.calls].count("risk") == 1
    assert search_pending()["interactionId"] != actual
