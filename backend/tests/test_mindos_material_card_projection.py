"""原材料列表的知识卡片状态投影回归测试。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from mindos import uploads
from mindos import derived as derived_svc
from mindos.stores import card_ledger_store as ledger
from mindos.stores import derived_store


def setup_function():
    root = Path(tempfile.mkdtemp())
    derived_store.reset_for_tests(root / "derived.db")
    ledger.reset_for_tests(root / "card_ledger.db")


def _item(material_id: str, status: str = "available") -> dict:
    return {"materialId": material_id, "status": status}


def _draft(material_id: str, *, confirmed: bool, knowledge_id: str | None = None) -> None:
    content = {
        "title": "资料", "content": "正文", "revision": "draft-rev", "confirmed": confirmed,
    }
    if knowledge_id:
        content["knowledgeId"] = knowledge_id
    derived_store.DerivedStore.instance().set_derived_record(
        "material", material_id, derived_svc.KIND_GENERATED_DRAFT, "ok", content, "input", "test",
    )


def test_list_projection_marks_unconfirmed_draft_without_writes():
    _draft("mindos_draft", confirmed=False)
    items = [_item("mindos_draft")]

    uploads._attach_knowledge_card_states(items, device_scope="global")

    assert items[0]["knowledgeCard"] == {
        "state": "draft", "knowledgeId": None, "indexState": None, "errorCode": None,
    }
    assert ledger.get("knowledge_draft") is None


def test_list_projection_marks_confirmed_indexed_card_rag_available():
    _draft("mindos_confirmed", confirmed=True, knowledge_id="knowledge_confirmed")
    ledger.confirm_and_enqueue(
        "knowledge_confirmed", "Resources/confirmed.md", "rev-one", "confirm-one",
        {"title": "资料", "body": "正文", "content_revision": "rev-one"},
    )
    assert ledger.activate_vector("knowledge_confirmed", 1)
    items = [_item("mindos_confirmed")]

    uploads._attach_knowledge_card_states(items, device_scope="global")

    assert items[0]["knowledgeCard"]["state"] == "available"
    assert items[0]["knowledgeCard"]["knowledgeId"] == "knowledge_confirmed"
    assert items[0]["knowledgeCard"]["indexState"] == "indexed"


def test_list_projection_keeps_processing_material_out_of_card_draft_state():
    items = [_item("mindos_processing", status="processing")]

    uploads._attach_knowledge_card_states(items, device_scope="global")

    assert items[0]["knowledgeCard"]["state"] == "waiting"
