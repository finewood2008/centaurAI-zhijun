import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from mindos import material_drafts, uploads
from mindos.stores import derived_store


def draft(title, body, revision):
    return {
        "title": title,
        "content": body,
        "revision": revision,
        "snapshotId": "snap-1",
        "snapshotVersion": 1,
        "origin": "user",
        "userEdited": True,
    }


class MaterialDraftCasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = derived_store.reset_for_tests(self.tmp / "derived.db")

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_requires_current_revision_and_returns_conflict_record(self):
        first = self.store.save_material_draft_cas("m1", "", draft("T", "first", "r1"), "h1", "user")
        self.assertEqual(first["content"]["revision"], "r1")
        second = self.store.save_material_draft_cas("m1", "r1", draft("T", "second", "r2"), "h2", "user")
        self.assertEqual(second["content"]["content"], "second")
        with self.assertRaises(derived_store.DraftRevisionConflict) as caught:
            self.store.save_material_draft_cas("m1", "r1", draft("T", "stale", "r3"), "h3", "user")
        self.assertEqual(caught.exception.current["content"]["revision"], "r2")
        self.assertEqual(self.store.get_derived_record("material", "m1", "GENERATED_DRAFT")["content"]["content"], "second")

    def test_mark_confirmed_persists_knowledge_link(self):
        self.store.save_material_draft_cas(
            "m-confirm", "", draft("标题", "可确认正文", "r-confirm"), "hash", "user", status="ok"
        )
        result = material_drafts.mark_confirmed("m-confirm", "r-confirm", "knowledge_confirmed")
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["knowledgeId"], "knowledge_confirmed")
        saved = self.store.get_derived_record("material", "m-confirm", "GENERATED_DRAFT")
        self.assertEqual(saved["status"], "confirmed")

    def test_purged_confirmed_card_reopens_material_draft(self):
        self.store.save_material_draft_cas(
            "m-purge", "", draft("标题", "保留的草稿正文", "r-purge"), "hash", "user", status="ok"
        )
        material_drafts.mark_confirmed("m-purge", "r-purge", "knowledge-purged")
        result = material_drafts.reopen_after_card_purged("m-purge", "knowledge-purged")
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["cardState"], "draft")
        self.assertEqual(result["content"], "保留的草稿正文")
        saved = self.store.get_derived_record("material", "m-purge", "GENERATED_DRAFT")
        self.assertEqual(saved["status"], "ok")

    def test_confirmation_lock_blocks_save_until_released(self):
        self.store.save_material_draft_cas(
            "m-lock", "", draft("标题", "初始正文", "r-lock"), "hash", "user", status="ok"
        )
        material_drafts.lock_for_confirmation("m-lock", "r-lock", "session-1")
        with self.assertRaises(material_drafts.DraftConfirmationLocked):
            material_drafts.save_draft("m-lock", "r-lock", "标题", "并发修改")
        material_drafts.unlock_confirmation("m-lock", "r-lock", "session-1")
        saved = material_drafts.save_draft("m-lock", "r-lock", "标题", "恢复后的修改")
        self.assertEqual(saved["content"], "恢复后的修改")

    def test_placeholder_title_is_repaired_to_file_name(self):
        placeholder = draft("待确认知识卡片", "原材料正文", "r-title")
        placeholder["userEdited"] = False
        self.store.save_material_draft_cas(
            "m-title", "", placeholder, "hash", "minimal", status="ok"
        )
        saved = material_drafts.ensure_minimal_draft("m-title", title="报告.pdf")
        self.assertEqual(saved["title"], "报告.pdf")
        self.assertNotEqual(saved["revision"], "r-title")


class MaterialDraftWorkflowTests(unittest.TestCase):
    def test_get_draft_card_initializes_minimal_draft(self):
        expected = {"status": "pending", "revision": "r1", "content": "minimum"}
        with patch.object(uploads, "_available_material_record"), patch(
            "mindos.material_drafts.ensure_minimal_draft", return_value=expected
        ) as ensure:
            response = uploads.mindos_material_draft_card("m1")
        ensure.assert_called_once_with("m1")
        self.assertEqual(response["revision"], "r1")

    def test_draft_card_rejects_material_not_yet_available(self):
        with patch.object(uploads, "_material_record", return_value={}), patch.object(
            uploads.ingestion, "status_of", return_value={"status": "processing"}
        ):
            with self.assertRaises(HTTPException) as caught:
                uploads.mindos_material_draft_card("m1")
        self.assertEqual(caught.exception.status_code, 409)

    def test_draft_card_uses_public_available_status_not_legacy_record_field(self):
        with patch.object(uploads, "_material_record", return_value={"material_id": "m1"}), patch.object(
            uploads.ingestion, "status_of", return_value={"status": "available"}
        ), patch.object(uploads.ingestion, "is_recycled", return_value=False), patch(
            "mindos.material_drafts.ensure_minimal_draft", return_value={"revision": "r1"}
        ):
            response = uploads.mindos_material_draft_card("m1")
        self.assertEqual(response["revision"], "r1")

    def test_regenerate_rejects_user_edited_draft(self):
        request = uploads.RegenerateRequest(item="draft")
        with patch.object(uploads, "_material_record"), patch.object(
            uploads.ingestion, "source_path_of", return_value="/tmp/source.txt"
        ), patch("mindos.material_drafts.ensure_minimal_draft", return_value={
            "userEdited": True, "revision": "r1"
        }), patch("mindos.material_drafts.submit_generation") as submit:
            with self.assertRaises(HTTPException) as caught:
                uploads.mindos_material_regenerate("m1", request)
        self.assertEqual(caught.exception.status_code, 409)
        submit.assert_not_called()

    def test_stale_snapshot_task_does_not_call_model_or_write_draft(self):
        store = MagicMock()
        store.get_derived_record.return_value = {"content": {"revision": "r1", "userEdited": False}}
        pipeline = MagicMock()
        pipeline.current_snapshot.return_value = {"snapshot_id": "new", "source_hash": "new-hash"}
        with patch.object(material_drafts.derived_store.DerivedStore, "instance", return_value=store), patch.object(
            material_drafts.MaterialPipelineStore, "instance", return_value=pipeline
        ), patch.object(material_drafts, "_call_llm") as call_model:
            material_drafts._generate("m1", "old", "old-hash", "old text", False)
        call_model.assert_not_called()
        store.save_material_draft_cas.assert_not_called()

if __name__ == "__main__":
    unittest.main()
