"""Synthetic workspace chat integration: review precedes every model boundary."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from mindos import data_agent_rag as rag
from mindos.zhijun import context_lookup
from mindos.zhijun.routing import Router, prepare_chat
from tests import test_task_routing as routing_fixtures
from tests.test_data_agent_rag_v2_flow import FakeClient, item, search_result


QUESTION = "请检索低空航空平台资料中的验收要求"
REQUEST_ID = "retrieval-integration-request"


@pytest.fixture
def chat(monkeypatch):
    case = routing_fixtures.RoutingTests(methodName="runTest")
    case.setUp()
    monkeypatch.setenv("ZHIJUN_WORKSPACE_ID", "workspace-retrieval-test")
    monkeypatch.setenv("ZHIJUN_MATERIAL_EVIDENCE", "1")
    case.local.configuration_revision = "synthetic-local"
    case.online.configuration_revision = "synthetic-online"
    calls = []

    def capability_call(operation, payload):
        calls.append((operation, payload))
        raise AssertionError("native RAG must not call legacy materials or DE model/consent capabilities: " + operation)

    monkeypatch.setattr("zhijun_worker.capabilities.require", lambda: SimpleNamespace(call=capability_call))
    case.capability_calls = calls
    case.no_legacy = Mock(side_effect=AssertionError("native evidence must not use legacy material registration"))
    monkeypatch.setattr("mindos.zhijun.routing.require_material", case.no_legacy)
    monkeypatch.setattr("mindos.chat_imports.require_material", case.no_legacy)
    try:
        yield case
    finally:
        rag.reset_for_tests()
        # Unwind the nested pytest overrides before the unittest ExitStack;
        # the opposite order leaks its MATERIAL_EVIDENCE=0 into later tests.
        monkeypatch.undo()
        case.tearDown()


def _client(*values):
    client = FakeClient(search_result(values))
    client.delivered = list(values)
    rag.reset_for_tests(client)
    return client


def _prepare(chat, *, online=False, refs=None):
    router = Router(chat.onto, chat.convs, chat.cid, provider=chat.online if online else chat.local)
    return prepare_chat(router, QUESTION, request_id=REQUEST_ID, material_refs=refs)


def _pending(chat, **kwargs):
    with pytest.raises(HTTPException) as caught:
        _prepare(chat, **kwargs)
    assert caught.value.detail["code"] == "RAG_MATERIAL_REVIEW_REQUIRED"
    return caught.value.detail["ragV2"]


def _select(prompt, *positions):
    return rag.decide(prompt["interactionId"], "use-selected",
                      [prompt["items"][position]["previewId"] for position in positions])


def _material_sources(plan):
    return [source for source in plan.preview["sources"] if source["kind"] == "material"]


def test_first_prepare_pauses_before_model_or_message_persistence(chat):
    client = _client(item())
    before = chat.convs.list_messages(chat.cid)
    prompt = _pending(chat)
    assert prompt["interactionId"].startswith(chat.cid + ":")
    assert prompt["items"][0]["preview"] == "可交付片段"
    assert not chat.local.requests and not chat.online.requests
    assert chat.convs.list_messages(chat.cid) == before
    assert not chat.capability_calls, "unapproved material must not register a model preview"
    assert [call[0] for call in client.calls].count("search") == 1
    assert client.calls[0][2]["material_ids"] == []


@pytest.mark.parametrize("content", ["检索 MindOS 核心功能", "请查找 MindOS 核心功能", "搜索 MindOS 核心功能"])
def test_explicit_lookup_verbs_trigger_review_without_material_nouns(chat, content):
    client = _client(item())
    router = Router(chat.onto, chat.convs, chat.cid, provider=chat.local)
    with pytest.raises(HTTPException) as caught:
        prepare_chat(router, content, request_id=REQUEST_ID)
    assert caught.value.detail["code"] == "RAG_MATERIAL_REVIEW_REQUIRED"
    assert [call[0] for call in client.calls].count("search") == 1
    assert not chat.local.requests


def test_unresolved_retrieval_reference_requests_clarification_before_search(chat):
    client = _client(item())
    router = Router(chat.onto, chat.convs, chat.cid, provider=chat.local)
    with pytest.raises(HTTPException) as caught:
        prepare_chat(router, "请检索这份文件的验收要求", request_id=REQUEST_ID)
    assert caught.value.detail["code"] == "RAG_QUERY_CLARIFICATION_REQUIRED"
    assert not client.calls
    assert not chat.local.requests


def test_only_selected_native_material_enters_context_without_local_registration(chat):
    first = {**item(), "text": "FIRST_UNSELECTED_PRIVATE_PAYLOAD"}
    second = {**item("erv2_" + "b" * 32), "materialId": "native-only-b",
              "title": "验收资料", "text": "SECOND_SELECTED_ACCEPTANCE_PAYLOAD"}
    client = _client(first, second)
    prompt = _pending(chat)
    assert _select(prompt, 1)["status"] == "ready"
    plan = _prepare(chat)
    request = str(plan.preview["request"])
    assert second["text"] in request
    assert first["text"] not in request
    assert [s["id"] for s in _material_sources(plan)] == ["native-only-b"]
    assert len([call for call in client.calls if call[0] == "search"]) == 1
    chat.no_legacy.assert_not_called()


def test_without_materials_resumes_same_request_with_no_evidence_text(chat):
    value = {**item(), "text": "OMITTED_MATERIAL_PAYLOAD"}
    client = _client(value)
    prompt = _pending(chat)
    assert rag.decide(prompt["interactionId"], "without-materials")["status"] == "ready"
    plan = _prepare(chat)
    assert value["text"] not in str(plan.preview["request"])
    assert not _material_sources(plan)
    assert plan.assembled.provenance["contextPlan"]["retrieval"]["selectedCount"] == 0
    assert len([call for call in client.calls if call[0] == "search"]) == 1
    assert not chat.local.requests


def test_explicit_scope_has_one_search_and_no_attachment_second_pass(chat, monkeypatch):
    value = item()
    client = _client(value)
    refs = [{"materialId": value["materialId"], "version": value["materialVersion"]}]
    attachment = Mock(side_effect=AssertionError("duplicate attachment retrieval"))
    monkeypatch.setattr("mindos.zhijun.routing.attachment_context", attachment)
    prompt = _pending(chat, refs=refs)
    _select(prompt, 0)
    plan = _prepare(chat, refs=refs)
    searches = [call for call in client.calls if call[0] == "search"]
    assert len(searches) == 1
    assert searches[0][2]["material_ids"] == [value["materialId"]]
    assert value["text"] in str(plan.preview["request"])
    attachment.assert_not_called()


def test_selected_low_score_material_remains_in_separate_cloud_authorization(chat):
    value = {**item(), "score": .001, "text": "LOW_SCORE_EXPLICITLY_SELECTED"}
    _client(value)
    prompt = _pending(chat, online=True)
    _select(prompt, 0)
    plan = _prepare(chat, online=True)
    sources = _material_sources(plan)
    assert len(sources) == 1
    assert sources[0]["key"] in plan.preview["missing"]
    assert not sources[0]["authorization"]
    assert value["text"] in str(plan.preview["request"])
    assert not chat.online.requests, "material selection is not cloud authorization"


def test_native_source_resolves_and_checks_lifecycle_without_legacy_fallback(chat):
    _client(item())
    prompt = _pending(chat)
    _select(prompt, 0)
    plan = _prepare(chat)
    source = _material_sources(plan)[0]
    internal_id = source["ref"]["ragInteractionId"]
    assert internal_id != prompt["interactionId"]
    assert rag._entries[internal_id].actual_search_id == prompt["interactionId"]
    assert source["ref"]["ragRevision"]
    fresh = plan.router.resolve(source["ref"])
    assert not fresh[0]["blocked"]
    assert fresh[0]["version"] == source["version"]
    plan.router.check_lifecycle(fresh)
    chat.no_legacy.assert_not_called()


@pytest.mark.parametrize("change", ["expired", "body_changed", "revision_changed"])
def test_expired_or_changed_native_source_fails_closed(chat, change):
    client = _client(item())
    prompt = _pending(chat)
    _select(prompt, 0)
    plan = _prepare(chat)
    source = _material_sources(plan)[0]
    if change == "expired":
        rag.reset_for_tests(client)
    elif change == "body_changed":
        client.delivered = [{**item(), "text": "CHANGED_AFTER_USER_REVIEW"}]
    else:
        source["ref"] = {**source["ref"], "ragRevision": "forged-revision"}
    fresh = plan.router.resolve(source["ref"])
    assert fresh[0]["blocked"]
    with pytest.raises(HTTPException) as caught:
        plan.router.check_lifecycle([source])
    assert caught.value.detail["code"] in {"SOURCE_UNAVAILABLE", "SOURCE_CHANGED"}
    chat.no_legacy.assert_not_called()


def test_reviewed_retrieval_disables_second_model_lookup(chat):
    _client(item())
    prompt = _pending(chat, online=True)
    _select(prompt, 0)
    plan = _prepare(chat, online=True)
    assert not context_lookup.eligible(plan, QUESTION, "deep", "deliberate", request_id=REQUEST_ID)
    assert not chat.online.requests


def test_evidence_checks_batch_within_boundary_but_refresh_before_model(chat):
    from mindos.zhijun.routing import GuardedProvider
    from mindos.zhijun.provider import ChatRequest

    values = [{**item("erv2_" + chr(97 + i) * 32), "materialId": f"native-{i}",
               "text": f"独立检索片段 {i}"} for i in range(5)]
    client = _client(*values)
    prompt = _pending(chat)
    _select(prompt, *range(5))
    client.calls.clear()
    plan = _prepare(chat)
    resolves = [call for call in client.calls if call[0] == "resolve"]
    # One cache replay + one batched validation, not two calls per passage
    # plus repeated per-material prepare/lifecycle calls.
    assert len(resolves) == 2
    assert all(len(call[1]) == 5 for call in resolves)
    guarded = GuardedProvider(plan.router, plan.provider, "chat", plan.refs,
                              revision=plan.preview["revision"])
    request = ChatRequest(**plan.preview["request"])
    client.calls.clear()
    guarded.check(request)
    assert len([call for call in client.calls if call[0] == "resolve"]) == 1
    client.delivered = [{**value, "text": "Changed after preview"} for value in values]
    with pytest.raises(HTTPException):
        guarded.check(request)
    assert not chat.local.requests


def test_risk_release_keeps_warning_and_cannot_use_standing_cloud_consent(chat):
    notice = {"code": "SENSITIVE_CHECK_INCOMPLETE", "message": "暂扣", "retrievedCount": 1,
              "checkedCount": 0, "withheldCount": 1, "retryable": True,
              "riskToken": "scf_" + "r" * 32, "riskEligibleCount": 1}
    client = FakeClient(search_result(status="sensitive_check_unavailable", notice=notice))
    rag.reset_for_tests(client)
    with pytest.raises(HTTPException) as caught:
        _prepare(chat, online=True)
    interaction = caught.value.detail["ragV2"]["interactionId"]
    rag.decide(interaction, "risk-release")
    prompt = _pending(chat, online=True)
    _select(prompt, 0)
    plan = _prepare(chat, online=True)
    source = _material_sources(plan)[0]
    assert source["unverifiedEvidence"] is True
    assert "未经验证原文" in source["title"]
    assert "用户放行不表示检测通过" in plan.preview["request"]["system"]
    service = plan.preview["service"]["id"]
    policy = {"enabled": True, "service": service, "purposes": ["chat"], "exclusions": [],
              "includeFiles": True, "revision": 1}
    assert plan.router.permission(source, service, "chat", policy) is None
    assert plan.router.permission({**source, "unverifiedEvidence": False}, service, "chat", policy)["kind"] == "default"
    plan.router.store.grant(plan.router.scope, [source], service, "chat")
    assert plan.router.permission(source, service, "chat", policy)["kind"] == "explicit"
    assert not chat.online.requests


def test_full_turn_resumes_original_request_then_replays_without_duplicate_messages(chat, monkeypatch):
    from mindos.zhijun.turn import run_turn

    _client(item())
    monkeypatch.setattr("mindos.zhijun.charter.enqueue", lambda *args, **kwargs: None)

    def run():
        return list(run_turn(chat.cid, QUESTION, provider=chat.local, conv_store=chat.convs,
                             ontology=chat.onto, request_id=REQUEST_ID))

    with pytest.raises(HTTPException) as caught:
        run()
    assert caught.value.detail["code"] == "RAG_MATERIAL_REVIEW_REQUIRED"
    assert chat.convs.list_messages(chat.cid) == []
    assert chat.local.requests == []
    _select(caught.value.detail["ragV2"], 0)
    events = run()
    assert any(name == "token" for name, _ in events)
    assert events[-1][0] == "message_done"
    messages = chat.convs.list_messages(chat.cid)
    assert len(messages) == 2
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert all(message["status"] == "complete" for message in messages)
    assert len(chat.local.requests) == 1
    replay = run()
    assert replay[0][1]["replayed"] is True
    assert chat.convs.list_messages(chat.cid) == messages
    assert len(chat.local.requests) == 1


@pytest.mark.parametrize("select", [True, False])
def test_actual_turn_history_does_not_inherit_unselected_attachment_sources(chat, monkeypatch, select):
    from mindos.zhijun.turn import run_turn

    first = item()
    second = {**item("erv2_" + "b" * 32), "materialId": "unselected-b", "text": "NEVER_SELECTED"}
    _client(first, second)
    monkeypatch.setattr("mindos.zhijun.charter.enqueue", lambda *args, **kwargs: None)
    refs = [{"materialId": value["materialId"], "version": 1} for value in (first, second)]

    def run():
        return list(run_turn(chat.cid, QUESTION, provider=chat.local, conv_store=chat.convs,
                             ontology=chat.onto, request_id=REQUEST_ID, material_refs=refs))

    with pytest.raises(HTTPException) as caught:
        run()
    prompt = caught.value.detail["ragV2"]
    if select:
        _select(prompt, 0)
    else:
        rag.decide(prompt["interactionId"], "without-materials")
    assert run()[-1][1]["status"] == "complete"
    user, assistant = chat.convs.list_messages(chat.cid)
    assert user["meta"]["requestedMaterialRefs"] == refs
    assert user["meta"]["materialRefs"] == (refs[:1] if select else [])
    source_refs = assistant["meta"]["routingSources"]
    material_sources = [ref for ref in source_refs if ref["kind"] == "material"]
    assert [ref["id"] for ref in material_sources] == ([first["materialId"]] if select else [])
    assert all(ref.get("ragInteractionId") for ref in material_sources)
    chat.no_legacy.assert_not_called()
    replay = run()
    assert replay[0][1]["replayed"] is True
    assert [message["id"] for message in chat.convs.list_messages(chat.cid)] == [user["id"], assistant["id"]]
    assert len(chat.local.requests) == 1
