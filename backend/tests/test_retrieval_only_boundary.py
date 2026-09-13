"""Real Zhijun entry points must not call Data Agent material-management APIs."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mindos import chat_import_routes as routes, chat_imports as imports
from mindos.stores.chat_import_store import ChatImportStore
from tests import test_task_routing as routing_fixtures
from zhijun_worker import attachments, events, material_understanding


@pytest.fixture
def workspace(monkeypatch):
    case = routing_fixtures.RoutingTests(methodName="runTest")
    case.setUp()
    monkeypatch.setenv("ZHIJUN_WORKSPACE_ID", "retrieval-only-test")
    subject = SimpleNamespace(workspace_id="retrieval-only-test")
    monkeypatch.setattr("zhijun_worker.capabilities.current", lambda: (subject, None))
    denied = Mock(side_effect=AssertionError("Data Engine material API must not be called"))
    monkeypatch.setattr("zhijun_worker.data_agent_rag_v2.configured_client", denied)
    monkeypatch.setattr("zhijun_worker.capabilities.require", denied)
    monkeypatch.setattr(attachments, "require", denied)
    monkeypatch.setattr(events, "require", denied)
    monkeypatch.setattr(material_understanding, "require", denied)
    app = FastAPI()

    @app.middleware("http")
    async def bind_subject(request, call_next):
        request.state.zhijun_workspace = subject
        return await call_next(request)

    app.include_router(routes.build_router(lambda: None))
    case.http = TestClient(app)
    case.store = ChatImportStore(case.convs)
    case.denied = denied
    try:
        yield case
        denied.assert_not_called()
    finally:
        case.tearDown()


def _old_batch(workspace, state="waiting", *, complete_reply=False):
    batch = workspace.store.create(workspace.cid, "old-import-request-1", "原来的资料问题",
        [{"id": "old-file-00000001", "name": "old.txt", "size": 12,
          "materialId": "material-old", "version": 1}])
    workspace.store.file_update("old-file-00000001", "reading", job_id="mjob_" + "a" * 32,
                               status_url="/v1/agent/apps/material-jobs/old")
    # The historical record is seeded directly, without registering a new job.
    with workspace.convs._connect() as db:
        db.execute("UPDATE chat_import_batches SET state=? WHERE id=?", (state, batch["id"]))
    if complete_reply:
        workspace.convs.append_message(workspace.cid, "assistant", "已完成的历史回答",
                                      message_id="msg_reply_" + batch["id"], status="complete")
    return workspace.store.get(batch["id"])


def _assert_retrieval_only(operation):
    with pytest.raises(HTTPException) as caught:
        operation()
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "RAG_RETRIEVAL_ONLY"
    assert "知君" in caught.value.detail["detail"] or "检索" in caught.value.detail["detail"]


@pytest.mark.parametrize("existing", [False, True])
def test_create_import_http_fails_before_material_or_upload_capabilities(workspace, existing):
    file = {"id": "incoming-file-00001", "name": "a.txt", "size": 20}
    if existing:
        file.update(materialId="material-old", version=1)
    response = workspace.http.post(workspace.url + "/imports",
        json={"requestId": "new-import-request-1", "content": "问题", "files": [file]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RAG_RETRIEVAL_ONLY"
    assert not workspace.store.batches()
    assert not workspace.convs.list_messages(workspace.cid)


@pytest.mark.parametrize("suffix", ["seal", "retry", "files/old-file-00000001/retry", "files/old-file-00000001"])
def test_old_import_mutation_routes_fail_without_requeue_or_network(workspace, suffix):
    batch = _old_batch(workspace)
    before = workspace.store.get(batch["id"])
    response = workspace.http.post(workspace.url + "/imports/" + batch["id"] + "/" + suffix)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RAG_RETRIEVAL_ONLY"
    assert workspace.store.get(batch["id"]) == before


def test_worker_attachment_entry_points_reject_before_reading_uploaded_bytes(workspace):
    _assert_retrieval_only(lambda: attachments.upload_import("ignored", "ignored", "ignored", None))
    for action in ("retry", "resume"):
        _assert_retrieval_only(lambda: attachments.resume_or_retry("material-old", action, None))


def test_legacy_material_read_preview_and_duplicate_detection_are_not_fallbacks(workspace):
    _assert_retrieval_only(lambda: imports.read_ref({"materialId": "material-old", "version": 1}, "global"))
    _assert_retrieval_only(lambda: imports.attachment_context(
        [{"materialId": "material-old", "version": 1}], "global", "概览", external=False))
    _assert_retrieval_only(lambda: imports.find_duplicate(workspace.store, "global", "a" * 64, 20))
    _assert_retrieval_only(lambda: imports.require_material("not-locally-registered", "global"))
    response = workspace.http.get(workspace.url + "/files/material-old/preview", params={"version": 1})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RAG_RETRIEVAL_ONLY"


def test_automatic_material_text_read_and_model_extraction_are_disabled(workspace):
    _assert_retrieval_only(lambda: material_understanding.extract("material-old", 1))
    _assert_retrieval_only(lambda: material_understanding.assert_current({"materialId": "material-old", "version": 1}))


def test_old_material_understanding_job_fails_before_metadata_lookup(workspace):
    from mindos.zhijun.materials import run
    _assert_retrieval_only(lambda: run("material-old", store=workspace.onto, expected_version=1))


def test_file_views_do_not_poll_or_misrepresent_old_material_readiness(workspace):
    batch = _old_batch(workspace)
    file = batch["files"][0]
    for row in (file, {**file, "job_id": None}, {**file, "material_id": None}):
        view = imports.file_view(row, "global")
        assert view["state"] == "unavailable"
        assert view["code"] == "RAG_RETRIEVAL_ONLY"
        assert view["name"] == file["name"]
        assert view["materialId"] == row["material_id"]


@pytest.mark.parametrize("state", ["waiting", "queued", "uploading", "replying", "rag_consent", "consent", "paused"])
def test_recovery_terminates_old_pending_imports_but_preserves_records(workspace, state):
    before = _old_batch(workspace, state)
    messages = workspace.convs.list_messages(workspace.cid)
    imports.recover(workspace.store)
    after = workspace.store.get(before["id"])
    assert after["state"] == "failed"
    assert after["files"] == before["files"]
    assert after["content"] == before["content"]
    assert workspace.convs.list_messages(workspace.cid) == messages
    assert "Data Agent" in after["error"]
    imports.process_batch(after, workspace.store)
    assert workspace.store.get(before["id"])["state"] == "failed"


def test_recovery_preserves_a_completed_reply(workspace):
    before = _old_batch(workspace, "replying", complete_reply=True)
    imports.recover(workspace.store)
    after = workspace.store.get(before["id"])
    assert after["state"] == "complete"
    assert after["files"] == before["files"]
    assert workspace.convs.get_message("msg_reply_" + before["id"])["content"] == "已完成的历史回答"


def test_pending_batch_processing_is_terminal_without_model_or_status_call(workspace):
    before = _old_batch(workspace, "queued")
    imports.process_batch(before, workspace.store)
    assert workspace.store.get(before["id"])["state"] == "failed"
    assert not workspace.local.requests and not workspace.online.requests


def test_imports_response_reports_retrieval_only_and_never_returns_active_old_refs(workspace):
    before = _old_batch(workspace)
    refs = [{"materialId": "material-old", "version": 1}]
    workspace.store.select(workspace.cid, refs, True)
    response = workspace.http.get(workspace.url + "/imports")
    assert response.status_code == 200
    data = response.json()
    assert data["retrievalOnly"] is True and data["uploadEnabled"] is False
    assert data["code"] == "RAG_RETRIEVAL_ONLY"
    assert data["selection"]["refs"] == []
    assert data["items"][0]["state"] == "failed"
    assert workspace.store.selection(workspace.cid)["refs"] == refs
    assert workspace.store.get(before["id"]) == before, "read-only display must retain original records"


def test_material_ready_event_is_deduplicated_without_extract_job_or_material_read(workspace):
    event = {"eventId": "a" * 32, "type": "material.ready",
             "payload": {"materialId": "material-old", "version": 1},
             "executionRequestId": "test-event-request", "operationId": "system_material_event"}
    result = events.handle(event)
    assert result["accepted"] is True
    assert result["state"] == "skipped" and result["code"] == "RAG_RETRIEVAL_ONLY"
    assert result["jobIds"] == []
    assert events.handle(event)["duplicate"] is True


def test_nonworkspace_import_guard_and_unattached_file_view_remain_compatible(workspace, monkeypatch):
    monkeypatch.delenv("ZHIJUN_WORKSPACE_ID")
    assert imports.require_import_enabled() is None
    row = {"id": "legacy-file", "name": "legacy.txt", "size": 1,
           "material_id": None, "version": None, "state": "pending", "error": None}
    assert imports.file_view(row, "global")["state"] == "pending"
