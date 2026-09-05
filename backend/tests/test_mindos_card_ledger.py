from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from mindos.stores import card_ledger_store as ledger
from mindos import lifecycle
import pytest


def setup_function():
    root = Path(tempfile.mkdtemp())
    ledger.reset_for_tests(root / "card_ledger.db")


def _confirm(revision: str, key: str):
    return ledger.confirm_and_enqueue(
        "knowledge_a", "Resources/a.md", revision, key,
        {"title": "A", "body": "body", "content_revision": revision},
    )


def test_content_change_advances_desired_version_and_keeps_old_active():
    _confirm("rev_one", "confirm-one")
    state = ledger.get("knowledge_a")
    assert state["desired_vector_version"] == 1
    assert ledger.activate_vector("knowledge_a", 1)
    _confirm("rev_two", "confirm-two")
    state = ledger.get("knowledge_a")
    assert state["desired_vector_version"] == 2
    assert state["active_vector_version"] == 1
    assert state["vector_sync_state"] == "pending"


def test_old_version_cannot_be_activated_after_new_mutation():
    _confirm("rev_one", "confirm-one")
    _confirm("rev_two", "confirm-two")
    assert not ledger.activate_vector("knowledge_a", 1)
    assert ledger.activate_vector("knowledge_a", 2)


def test_purged_path_reuses_state_and_monotonically_advances_version():
    _confirm("rev_one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)
    ledger.mark_visibility("knowledge_a", "purged", "purge")
    state = ledger.ensure("knowledge_a", "Resources/a.md", "rev_recreated")
    assert state["visibility"] == "active"
    assert state["desired_vector_version"] > 1
    assert state["active_vector_version"] is None


def test_vector_job_lease_recovery_pauses_until_explicit_retry():
    ledger.ensure("knowledge_a", "Resources/a.md", "rev_one")
    job_id = ledger.enqueue_vector_repair("knowledge_a", 1, '{"body":"x"}')
    job = ledger.claim_vector_job(lease_seconds=0)
    assert job and job["job_id"] == job_id
    assert ledger.reclaim_vector_jobs() == 1
    assert ledger.claim_vector_job() is None
    assert ledger.list_vector_jobs()[0]["state"] == "paused"


def test_paused_confirmed_index_is_visible_and_manual_retry_requeues():
    _confirm("rev-one", "confirm-one")
    assert ledger.pause_vector_jobs() == 1
    state = ledger.get("knowledge_a")
    assert state["index_state"] == "index_failed"
    assert state["index_error_code"] == "service_interrupted"
    job = ledger.retry_index("knowledge_a", "rev-one", {"body": "body"})
    assert job["state"] == "queued"
    assert ledger.get("knowledge_a")["index_state"] == "indexing"


def test_non_transient_failure_is_terminal_and_external_change_revokes_eligibility():
    _confirm("rev-one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)
    state = ledger.get("knowledge_a")
    assert ledger.is_rag_eligible(state, "rev-one", 1)

    job = ledger.list_vector_jobs()[0]
    assert ledger.finish_vector_job(job["job_id"], False, "invalid input") == "failed"
    state = ledger.mark_needs_reconfirmation("knowledge_a")
    assert state and state["approval_state"] == "draft"
    assert state["index_state"] == "none"
    assert not ledger.is_rag_eligible(state, "rev-one", 1)


def test_confirm_and_enqueue_is_idempotent_and_fail_closed_until_indexed():
    result = ledger.confirm_and_enqueue(
        "knowledge_confirmed", "Resources/a.md", "rev-a", "idem-1",
        {"title": "A", "body": "body", "content_revision": "rev-a"},
    )
    state = ledger.get("knowledge_confirmed")
    assert state["approval_state"] == "confirmed"
    assert state["index_state"] == "indexing"
    assert not ledger.is_rag_eligible(state)
    assert result["job"]["state"] == "queued"

    repeated = ledger.confirm_and_enqueue(
        "knowledge_confirmed", "Resources/a.md", "rev-a", "idem-1",
        {"title": "A", "body": "body", "content_revision": "rev-a"},
    )
    assert repeated["idempotent"] is True
    assert repeated["job"]["job_id"] == result["job"]["job_id"]
    assert len(ledger.list_vector_jobs()) == 1

    with pytest.raises(ledger.ConfirmationConflict):
        ledger.confirm_and_enqueue(
            "knowledge_confirmed", "Resources/a.md", "rev-a", "different-key",
            {"title": "A", "body": "body", "content_revision": "rev-a"},
        )


def test_material_confirmation_uses_card_revision_for_outbox_not_draft_revision():
    session = ledger.begin_material_confirmation("material_a", "draft-rev", "idem-a", {"material_id": "material_a"})
    ledger.mark_confirmation_file_committed(
        session["session_id"], "knowledge_a",
        {"title": "A", "body": "body", "content_revision": "card-rev", "rel_path": "Resources/a.md"},
    )
    result = ledger.finalize_material_confirmation(
        session["session_id"], "knowledge_a", "Resources/a.md", "draft-rev",
        {"title": "A", "body": "body", "content_revision": "card-rev"},
    )
    state = ledger.get("knowledge_a")
    assert state["current_revision"] == "card-rev"
    assert "card-rev" in result["job"]["payload_json"]
    assert "draft-rev" not in result["job"]["payload_json"]


def test_existing_card_confirmation_exposes_confirming_before_outbox_commit():
    session = ledger.begin_card_confirmation(
        "knowledge_existing", "card-rev", "idem-existing",
        {"title": "A", "body": "body", "content_revision": "card-rev", "rel_path": "Resources/a.md"},
    )
    ledger.mark_confirmation_file_committed(
        session["session_id"], "knowledge_existing",
        {"title": "A", "body": "body", "content_revision": "card-rev", "rel_path": "Resources/a.md"},
    )
    assert ledger.get("knowledge_existing")["approval_state"] == "confirming"
    result = ledger.finalize_material_confirmation(
        session["session_id"], "knowledge_existing", "Resources/a.md", "card-rev",
        {"title": "A", "body": "body", "content_revision": "card-rev"},
    )
    assert result["job"]["state"] == "queued"
    assert ledger.get("knowledge_existing")["approval_state"] == "confirmed"


def test_edit_as_draft_revokes_eligibility_and_retry_requires_confirmed_revision():
    _confirm("rev-one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)
    state = ledger.get("knowledge_a")
    assert ledger.is_rag_eligible(state, "rev-one")

    draft = ledger.edit_as_draft("knowledge_a", "rev-one")
    assert draft["approval_state"] == "draft"
    assert draft["index_state"] == "none"
    assert not ledger.is_rag_eligible(draft, "rev-one")
    with pytest.raises(ledger.ConfirmationConflict):
        ledger.retry_index("knowledge_a", "rev-one", {"body": "body"})


def test_confirmed_card_working_draft_keeps_old_version_rag_eligible():
    _confirm("rev-one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)
    draft = ledger.begin_edit_draft("knowledge_a", "rev-one", {
        "title": "A changed", "content": "new body", "tags": [], "folderId": None, "sourceRefs": [],
    })
    state = ledger.get("knowledge_a")
    assert state["current_revision"] == "rev-one"
    assert ledger.is_rag_eligible(state, "rev-one", 1)

    saved = ledger.save_edit_draft("knowledge_a", draft["draft_revision"], {
        "title": "A changed", "content": "newer body", "tags": ["x"], "folderId": None, "sourceRefs": [],
    })
    result = ledger.begin_pending_update("knowledge_a", saved["draft_revision"], "rev-two", {
        "title": "A changed", "body": "newer body", "content_revision": "rev-two", "rel_path": "Resources/a.md",
    })
    assert result["job"]["state"] == "queued"
    assert ledger.is_rag_eligible(ledger.get("knowledge_a"), "rev-one", 1)
    assert ledger.pending_update_can_index("knowledge_a", "rev-two", 2)


def test_pending_update_activates_only_after_new_version_is_ready():
    _confirm("rev-one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)
    draft = ledger.begin_edit_draft("knowledge_a", "rev-one", {
        "title": "A", "content": "new", "tags": [], "folderId": None, "sourceRefs": [],
    })
    result = ledger.begin_pending_update("knowledge_a", draft["draft_revision"], "rev-two", {
        "title": "A", "body": "new", "content_revision": "rev-two", "rel_path": "Resources/a.md",
    })
    assert ledger.mark_pending_vector_written("knowledge_a", "rev-two", result["pending"]["vector_version"], 1)
    assert ledger.mark_pending_file_committed("knowledge_a", "rev-two", result["pending"]["vector_version"], "file-hash")
    assert ledger.activate_pending_update("knowledge_a", "rev-two", result["pending"]["vector_version"])
    state = ledger.get("knowledge_a")
    assert state["current_revision"] == "rev-two"
    assert state["indexed_vector_version"] == 2
    assert ledger.get_edit_draft("knowledge_a") is None
    assert not ledger.is_rag_eligible(state, "rev-one", 1)
    assert ledger.is_rag_eligible(state, "rev-two", 2)


def test_failed_pending_update_can_retry_without_revoking_current_version():
    _confirm("rev-one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)
    draft = ledger.begin_edit_draft("knowledge_a", "rev-one", {
        "title": "A", "content": "new", "tags": [], "folderId": None, "sourceRefs": [],
    })
    ledger.begin_pending_update("knowledge_a", draft["draft_revision"], "rev-two", {
        "title": "A", "body": "new", "content_revision": "rev-two", "rel_path": "Resources/a.md",
    })
    ledger.fail_pending_update("knowledge_a", "index_timeout")
    retry = ledger.retry_pending_update("knowledge_a")
    assert retry["state"] == "queued"
    assert ledger.pending_update_can_index("knowledge_a", "rev-two", 2)
    assert ledger.is_rag_eligible(ledger.get("knowledge_a"), "rev-one", 1)


def test_restart_recovers_pending_update_and_job_together():
    _confirm("rev-one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)
    initial_job = ledger.list_vector_jobs()[0]
    ledger.finish_vector_job(initial_job["job_id"], True)
    draft = ledger.begin_edit_draft("knowledge_a", "rev-one", {
        "title": "A", "content": "new", "tags": [], "folderId": None, "sourceRefs": [],
    })
    result = ledger.begin_pending_update("knowledge_a", draft["draft_revision"], "rev-two", {
        "title": "A", "body": "new", "content_revision": "rev-two", "rel_path": "Resources/a.md",
    })
    claimed = ledger.claim_vector_job()
    assert claimed and claimed["job_id"] == result["job"]["job_id"]

    recovered = ledger.recover_interrupted_vector_jobs()

    assert recovered["recovered"] == 1
    pending = ledger.get_pending_update("knowledge_a")
    assert pending and pending["state"] == "recovering"
    assert ledger.list_vector_jobs()[0]["state"] == "queued"
    assert ledger.is_rag_eligible(ledger.get("knowledge_a"), "rev-one", 1)


def test_restart_marks_already_activated_job_done():
    _confirm("rev-one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)

    recovered = ledger.recover_interrupted_vector_jobs()

    assert recovered["completed"] == 1
    assert ledger.list_vector_jobs()[0]["state"] == "done"


def test_manifest_and_metadata_folder_are_persistent_without_vector_bump():
    _confirm("rev-one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)
    ledger.record_vector_manifest("knowledge_a", 1, "rev-one", 2, "ids-hash", body_hash="body-hash")
    before = ledger.get("knowledge_a")

    moved = ledger.update_folder_metadata("knowledge_a", 42, "rev-one")

    assert moved["folder_id"] == 42
    assert moved["metadata_revision"] == 1
    assert moved["current_revision"] == before["current_revision"]
    assert moved["active_vector_version"] == before["active_vector_version"]
    assert ledger.get_vector_manifest("knowledge_a", 1)["expected_chunk_count"] == 2


def test_working_draft_can_be_explicitly_discarded_and_restored():
    _confirm("rev-one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)
    draft = ledger.begin_edit_draft("knowledge_a", "rev-one", {
        "title": "A", "content": "new", "tags": [], "folderId": None, "sourceRefs": [],
    })
    assert ledger.discard_edit_draft("knowledge_a")
    assert ledger.get_edit_draft("knowledge_a") is None
    ledger.restore_edit_draft(draft)
    assert ledger.get_edit_draft("knowledge_a")["draft_revision"] == draft["draft_revision"]


def test_lifecycle_requires_explicit_discard_for_working_draft():
    _confirm("rev-one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)
    ledger.begin_edit_draft("knowledge_a", "rev-one", {
        "title": "A", "content": "new", "tags": [], "folderId": None, "sourceRefs": [],
    })
    with patch.object(lifecycle.knowledge, "cards_referencing_knowledge", return_value=[]), \
         patch.object(lifecycle.derived_store.DerivedStore.instance(), "list_corrections", return_value=[]), \
         patch.object(lifecycle, "_knowledge_referencing_drafts", return_value=[]):
        blocking = lifecycle._knowledge_blocking_deps("knowledge_a")

    edit_dep = next(item for item in blocking if item["type"] == "editDraft")
    assert edit_dep["allowedActions"] == ["discard"]
    plan = lifecycle._plan_dependency_actions(
        "knowledge", "knowledge_a", blocking,
        [lifecycle.DependencyAction(type="editDraft", id="knowledge_a", action="discard")],
    )
    assert plan[0]["kind"] == "discard_edit_draft"


def test_lifecycle_blocks_delete_while_pending_update_is_running():
    _confirm("rev-one", "confirm-one")
    assert ledger.activate_vector("knowledge_a", 1)
    draft = ledger.begin_edit_draft("knowledge_a", "rev-one", {
        "title": "A", "content": "new", "tags": [], "folderId": None, "sourceRefs": [],
    })
    ledger.begin_pending_update("knowledge_a", draft["draft_revision"], "rev-two", {
        "title": "A", "body": "new", "content_revision": "rev-two", "rel_path": "Resources/a.md",
    })
    with patch.object(lifecycle.knowledge, "cards_referencing_knowledge", return_value=[]), \
         patch.object(lifecycle.derived_store.DerivedStore.instance(), "list_corrections", return_value=[]), \
         patch.object(lifecycle, "_knowledge_referencing_drafts", return_value=[]):
        blocking = lifecycle._knowledge_blocking_deps("knowledge_a")

    pending = next(item for item in blocking if item["type"] == "pendingUpdate")
    assert pending["allowedActions"] == []
    with pytest.raises(Exception) as exc:
        lifecycle._plan_dependency_actions("knowledge", "knowledge_a", blocking, [
            lifecycle.DependencyAction(type="editDraft", id="knowledge_a", action="discard"),
            lifecycle.DependencyAction(type="pendingUpdate", id="knowledge_a", action="cancel"),
        ])
    assert getattr(exc.value, "status_code", None) in {400, 409}


def test_ledger_health_check_reports_schema_version():
    health = ledger.health_check()
    assert health["ok"] is True
    assert health["schemaVersion"] >= 2


def test_retry_index_requeues_only_confirmed_current_revision():
    _confirm("rev-one", "confirm-one")
    ledger.mark_vector_failed("knowledge_a")
    job = ledger.retry_index("knowledge_a", "rev-one", {"title": "A", "body": "body", "content_revision": "rev-one"})
    state = ledger.get("knowledge_a")
    assert job["state"] == "queued"
    assert state["index_state"] == "indexing"
    with pytest.raises(ledger.ConfirmationConflict):
        ledger.retry_index("knowledge_a", "stale", {"body": "body"})


def test_purge_recovery_completes_after_file_was_already_isolated():
    ledger.ensure("knowledge_a", "Resources/a.md", "rev_one")
    ledger.create_purge_job("purge_a", "knowledge_a", "Resources/a.md")
    ledger.update_purge_job("purge_a", "governance_cleaned")
    with patch.object(lifecycle.governance_store.instance(), "purge_knowledge_items"), \
         patch.object(lifecycle.knowledge, "knowledge_purge"), \
         patch.object(lifecycle.knowledge.wiki_store, "_delete_page_index"):
        result = lifecycle.recover_pending_purges()
    assert result["recovered"] == 1
    assert ledger.list_purge_jobs()[0]["state"] == "completed_with_vector_cleanup_pending"
