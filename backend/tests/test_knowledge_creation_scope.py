"""Real Wiki creation registers only scoped drafts, never RAG admission."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import wiki_store
from mindos import knowledge
from mindos.stores import card_ledger_store as ledger, job_store, governance_store


@pytest.fixture
def isolated(tmp_path):
    old_ledger, old_job, old_governance = ledger._PATH, job_store._DB_PATH, governance_store._DB_PATH
    old_wiki_dir, old_wiki_db = wiki_store.WIKI_DIR, wiki_store.WIKI_DB_PATH
    ledger.reset_for_tests(tmp_path / "cards.db")
    job_store.reset_for_tests(tmp_path / "jobs.db")
    governance_store.reset_for_tests(tmp_path / "governance.db")
    wiki_store.WIKI_DIR = str(tmp_path / "wiki")
    wiki_store.WIKI_DB_PATH = str(tmp_path / "wiki" / "wiki.sqlite3")
    wiki_store._SCHEMA_READY = False
    with patch("gbrain_store.sync_wiki_page", return_value={"success": True}), patch.object(
        knowledge.knowledge_index, "count_card_chunks", return_value=0,
    ), patch.object(knowledge, "_schedule_vector_repairs") as schedule:
        try:
            yield job_store.JobStore.instance(), schedule
        finally:
            wiki_store.WIKI_DIR, wiki_store.WIKI_DB_PATH = old_wiki_dir, old_wiki_db
            wiki_store._SCHEMA_READY = False
            ledger.reset_for_tests(old_ledger)
            job_store.reset_for_tests(old_job)
            governance_store.reset_for_tests(old_governance)


def assert_draft(kid, scope, schedule):
    state = ledger.get(kid, device_scope=scope)
    assert state is not None
    assert state["device_scope"] == scope
    assert state["approval_state"] == "draft"
    assert state["index_state"] == "none"
    assert state["current_revision"] == state["content_revision"]
    assert not ledger.is_rag_eligible(state)
    assert ledger.list_vector_jobs() == []
    schedule.assert_not_called()


def test_http_create_uses_server_device_context_not_body_scope(isolated):
    _, schedule = isolated
    app = FastAPI()

    @app.middleware("http")
    async def synthetic_identity(request: Request, call_next):
        # Identity middleware substitute for this in-process synthetic app only.
        if device := request.headers.get("x-synthetic-device"):
            request.state.mindos_device_context = SimpleNamespace(device_id=device)
        return await call_next(request)

    app.add_api_route("/knowledge", knowledge.knowledge_create, methods=["POST"])
    with TestClient(app) as client:
        for device, scope in (("a", "device:a"), ("b", "device:b"), (None, "global")):
            response = client.post("/knowledge", headers={"x-synthetic-device": device} if device else {}, json={
                "title": "新建草稿", "content": "合成正文，只是草稿。", "device_scope": "device:forged",
            })
            assert response.status_code == 200, response.text
            kid = response.json()["item"]["knowledgeId"]
            assert_draft(kid, scope, schedule)
            assert ledger.get(kid, device_scope="device:forged") is None
            if device:
                assert ledger.get(kid) is None


def test_from_material_reads_and_registers_only_requested_device(isolated, tmp_path):
    store, schedule = isolated
    source = tmp_path / "material.md"
    source.write_text("合成原文", encoding="utf-8")
    store.register("mindos_owned", "material.md", "document", str(source), device_scope="device:a")
    with pytest.raises(HTTPException) as denied:
        knowledge.knowledge_create_from_material("mindos_owned", device_scope="device:b")
    assert denied.value.status_code == 404
    assert list((tmp_path / "wiki").rglob("*.md")) == []
    result = knowledge.knowledge_create_from_material("mindos_owned", device_scope="device:a")
    kid = result["item"]["knowledgeId"]
    assert_draft(kid, "device:a", schedule)
    assert [row["knowledgeId"] for row in knowledge.cards_referencing_material(
        "mindos_owned", device_scope="device:a",
    )] == [kid]
    assert knowledge.cards_referencing_material("mindos_owned", device_scope="device:b") == []
    assert knowledge.cards_referencing_material("mindos_owned", device_scope="global") == []


def test_internal_source_creation_keeps_explicit_scope_without_confirmation(isolated):
    _, schedule = isolated
    item = knowledge.create_card_with_sources(
        "来源草稿", "合成、未经确认的整理内容。",
        source_refs=[{"sourceType": "material", "id": "mindos_synthetic"}], device_scope="device:a",
    )
    assert_draft(item["knowledgeId"], "device:a", schedule)
    assert len(knowledge.cards_referencing_material("mindos_synthetic", device_scope="device:a")) == 1
    assert knowledge.cards_referencing_material("mindos_synthetic", device_scope="global") == []


def test_reading_legacy_card_does_not_assign_device_or_remove_internal_dependency(isolated):
    _, schedule = isolated
    page = wiki_store.create_page("旧稿", folder="Resources", page_type="note")
    page = wiki_store.write_page(page["path"],
        '---\nmindos_card: true\nmindos_source_material_ids: ["mindos_legacy"]\n---\n# 旧稿\n原内容。')
    kid = knowledge._knowledge_id(page["path"])
    assert knowledge.cards_referencing_material("mindos_legacy", device_scope="device:a") == []
    assert knowledge.cards_referencing_material("mindos_legacy", device_scope="global") == []
    assert [item["knowledgeId"] for item in knowledge.cards_referencing_material("mindos_legacy")] == [kid]
    assert ledger.get(kid) is None
    schedule.assert_not_called()
