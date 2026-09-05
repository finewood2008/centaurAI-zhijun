from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mindos import knowledge_index
from mindos.stores import card_ledger_store as ledger


def setup_function():
    root = Path(tempfile.mkdtemp())
    ledger.reset_for_tests(root / "card_ledger.db")


def _confirm_and_index(card_id: str = "knowledge_a", revision: str = "rev-current") -> None:
    ledger.confirm_and_enqueue(
        card_id, "Resources/a.md", revision, "idem-" + card_id,
        {"title": "A", "body": "body", "content_revision": revision},
    )
    state = ledger.get(card_id)
    assert state is not None
    assert ledger.activate_vector(card_id, int(state["desired_vector_version"]))


def _vector_result(card_id: str, revision: str, version: int = 1) -> dict:
    return {
        "ids": [["chunk-1"]],
        "metadatas": [[{
            "knowledge_id": card_id,
            "title": "A",
            "vector_version": version,
            "card_revision": revision,
        }]],
        "documents": [["A\n可信正文"]],
        "distances": [[0.1]],
    }


@pytest.mark.parametrize("setup", ["missing", "draft", "indexing", "failed", "stale", "old_version"])
def test_vector_search_fails_closed_for_ineligible_cards(setup: str):
    revision = "rev-current"
    if setup == "draft":
        ledger.ensure("knowledge_a", "Resources/a.md", revision)
    elif setup in {"indexing", "failed", "stale", "old_version"}:
        ledger.confirm_and_enqueue(
            "knowledge_a", "Resources/a.md", revision, "idem-a",
            {"title": "A", "body": "body", "content_revision": revision},
        )
        if setup == "failed":
            ledger.mark_vector_failed("knowledge_a")
        elif setup in {"stale", "old_version"}:
            state = ledger.get("knowledge_a")
            assert state is not None
            assert ledger.activate_vector("knowledge_a", int(state["desired_vector_version"]))

    chunk_revision = "rev-stale" if setup == "stale" else revision
    chunk_version = 0 if setup == "old_version" else 1
    with patch.object(knowledge_index, "ensure_index_readable"), \
         patch.object(knowledge_index, "embed_query", return_value=[0.1]), \
         patch.object(knowledge_index, "query_union_collection", return_value=_vector_result("knowledge_a", chunk_revision, chunk_version)):
        assert knowledge_index.search_cards("query") == []


def test_vector_search_returns_only_current_confirmed_indexed_revision():
    _confirm_and_index()
    with patch.object(knowledge_index, "ensure_index_readable"), \
         patch.object(knowledge_index, "embed_query", return_value=[0.1]), \
         patch.object(knowledge_index, "query_union_collection", return_value=_vector_result("knowledge_a", "rev-current")):
        assert knowledge_index.search_cards("query") == [{
            "knowledgeId": "knowledge_a", "title": "A", "snippet": "可信正文", "score": 0.9,
        }]


def test_recycle_restore_keeps_confirmation_and_requires_a_fresh_index():
    _confirm_and_index()
    recycled = ledger.transition_lifecycle_visibility("knowledge_a", "recycled", "rev-recycled")
    assert recycled is not None
    assert recycled["approval_state"] == "confirmed"
    assert recycled["visibility"] == "recycled"
    assert not ledger.is_rag_eligible(recycled, "rev-recycled")

    restored = ledger.transition_lifecycle_visibility("knowledge_a", "active", "rev-restored")
    assert restored is not None
    assert restored["approval_state"] == "confirmed"
    assert restored["visibility"] == "active"
    assert restored["index_state"] == "indexing"
    assert restored["desired_vector_version"] == 2
    assert not ledger.is_rag_eligible(restored, "rev-restored")
    assert ledger.activate_vector("knowledge_a", 2)
    assert ledger.is_rag_eligible(ledger.get("knowledge_a"), "rev-restored", 2)


def test_bulk_vector_verification_reads_union_once():
    records = {
        "ids": ["knowledge_a::v1::0", "knowledge_b::v2::0"],
        "metadatas": [
            {"knowledge_id": "knowledge_a", "vector_version": 1, "chunk_index": 0,
             "card_revision": "rev-a"},
            {"knowledge_id": "knowledge_b", "vector_version": 2, "chunk_index": 0,
             "card_revision": "rev-b"},
        ],
        "documents": ["A", "B"],
    }
    with patch.object(knowledge_index, "ensure_index_readable"), \
         patch.object(knowledge_index, "get_union_collection_records", return_value=records) as read, \
         patch.object(knowledge_index.card_ledger_store, "get_vector_manifest", return_value=None), \
         patch.object(knowledge_index.card_ledger_store, "record_vector_manifest"), \
         patch.object(knowledge_index.index_registry, "get_routing", return_value={"routing_epoch": 1}):
        result = knowledge_index.verify_card_vectors([
            ("knowledge_a", 1, "rev-a"), ("knowledge_b", 2, "rev-b"),
        ])

    assert result == {"knowledge_a": True, "knowledge_b": True}
    read.assert_called_once()
