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

    def test_save_with_existing_revision_cannot_recreate_deleted_draft(self):
        with self.assertRaises(derived_store.DraftRevisionConflict):
            self.store.save_material_draft_cas(
                "m-deleted", "r-deleted", draft("T", "stale", "r-next"),
                "hash", "user",
            )
        self.assertIsNone(
            self.store.get_derived_record("material", "m-deleted", "GENERATED_DRAFT")
        )

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

    def test_user_save_rebases_over_unedited_generated_revision(self):
        generated = draft("模型标题", "模型正文", "r-model")
        generated.update({"origin": "model", "userEdited": False})
        self.store.save_material_draft_cas(
            "m-generated", "", generated, "hash", "model", status="ok"
        )
        pipeline = MagicMock()
        pipeline.current_snapshot.return_value = None
        with patch.object(material_drafts.MaterialPipelineStore, "instance", return_value=pipeline):
            saved = material_drafts.save_draft(
                "m-generated", "r-before-model", "用户标题", "用户正文"
            )
        self.assertEqual(saved["title"], "用户标题")
        self.assertEqual(saved["content"], "用户正文")
        self.assertTrue(saved["userEdited"])
        self.assertEqual(saved["status"], "ok")

    def test_user_save_does_not_overwrite_another_user_revision(self):
        self.store.save_material_draft_cas(
            "m-user-conflict", "", draft("另一会话", "已保存正文", "r-other"),
            "hash", "user", status="ok",
        )
        pipeline = MagicMock()
        pipeline.current_snapshot.return_value = None
        with patch.object(material_drafts.MaterialPipelineStore, "instance", return_value=pipeline), \
             self.assertRaises(derived_store.DraftRevisionConflict):
            material_drafts.save_draft(
                "m-user-conflict", "r-stale", "当前会话", "不能覆盖"
            )

    def test_exact_user_save_retry_is_idempotent_after_lost_response(self):
        self.store.save_material_draft_cas(
            "m-idempotent", "", draft("旧标题", "旧正文", "r-old"),
            "hash", "user", status="ok",
        )
        pipeline = MagicMock()
        pipeline.current_snapshot.return_value = None
        with patch.object(material_drafts.MaterialPipelineStore, "instance", return_value=pipeline):
            first = material_drafts.save_draft(
                "m-idempotent", "r-old", "保存标题", "保存正文"
            )
            retried = material_drafts.save_draft(
                "m-idempotent", "r-old", "保存标题", "保存正文"
            )
        self.assertEqual(retried["revision"], first["revision"])
        self.assertEqual(retried["content"], "保存正文")

    def test_stale_pending_generation_is_requeued_once_and_lease_renewed(self):
        pending = draft("材料标题", "兜底正文", "r-pending")
        pending.update({"origin": "minimal", "userEdited": False})
        record = self.store.save_material_draft_cas(
            "m-stale", "", pending, "hash", "minimal", status="pending"
        )
        stale_now = record["updated_at"] + material_drafts.PENDING_GENERATION_LEASE_SECONDS + 1
        with patch.object(material_drafts, "submit_generation", return_value=True) as submit:
            recovered = material_drafts.recover_stale_generation(
                "m-stale", "/tmp/source.md", now=stale_now
            )
            fresh = material_drafts.recover_stale_generation(
                "m-stale", "/tmp/source.md", now=stale_now
            )
        submit.assert_called_once_with("m-stale", "/tmp/source.md")
        self.assertEqual(recovered["status"], "pending")
        self.assertEqual(fresh["status"], "pending")

    def test_stale_pending_generation_becomes_failed_when_queue_is_unavailable(self):
        pending = draft("材料标题", "兜底正文", "r-pending")
        pending.update({"origin": "minimal", "userEdited": False})
        record = self.store.save_material_draft_cas(
            "m-stale-failed", "", pending, "hash", "minimal", status="pending"
        )
        stale_now = record["updated_at"] + material_drafts.PENDING_GENERATION_LEASE_SECONDS + 1
        with patch.object(material_drafts, "submit_generation", return_value=False):
            recovered = material_drafts.recover_stale_generation(
                "m-stale-failed", "/tmp/source.md", now=stale_now
            )
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["errorCode"], "generation_queue_unavailable")

    def test_missing_draft_is_created_and_generation_is_submitted_immediately(self):
        snapshot = {
            "snapshot_id": "snap-new", "version": 1, "source_hash": "source-hash"
        }
        pipeline = MagicMock()
        pipeline.current_snapshot.return_value = snapshot
        saga = MagicMock()
        saga.read_snapshot_text.return_value = "可编辑的材料正文"
        with patch.object(material_drafts.MaterialPipelineStore, "instance", return_value=pipeline), \
             patch.object(material_drafts, "MaterialSnapshotSaga", return_value=saga), \
             patch.object(material_drafts, "submit_generation", return_value=True) as submit:
            result = material_drafts.recover_stale_generation("m-missing", "/tmp/source.md")
        submit.assert_called_once_with("m-missing", "/tmp/source.md")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["content"], "可编辑的材料正文")

    def test_running_generation_cannot_overwrite_clean_pending_user_save(self):
        title = "材料标题"
        body = "兜底正文"
        revision = material_drafts._revision(title, body)
        pending = draft(title, body, revision)
        pending.update({"origin": "minimal", "userEdited": False})
        self.store.save_material_draft_cas(
            "m-running", "", pending, "source-hash", "minimal", status="pending"
        )
        snapshot = {
            "snapshot_id": "snap-1", "version": 1, "source_hash": "source-hash"
        }
        pipeline = MagicMock()
        pipeline.current_snapshot.return_value = snapshot
        pipeline.get_snapshot.return_value = snapshot
        provider = MagicMock()
        provider.get_local_snapshot.return_value = MagicMock(model="local-model")

        def save_while_model_is_running(*_args, **_kwargs):
            saved = material_drafts.save_draft("m-running", revision, title, body)
            self.assertTrue(saved["userEdited"])
            return "后台模型生成的新正文"

        with patch.object(material_drafts.MaterialPipelineStore, "instance", return_value=pipeline), \
             patch.object(material_drafts, "get_provider", return_value=provider), \
             patch.object(material_drafts, "_call_llm", side_effect=save_while_model_is_running):
            material_drafts._generate(
                "m-running", "snap-1", "source-hash", body, False
            )
        final = material_drafts.draft_of("m-running")
        self.assertTrue(final["userEdited"])
        self.assertEqual(final["title"], title)
        self.assertEqual(final["content"], body)
        self.assertEqual(final["status"], "ok")

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

    def test_pending_recovery_does_not_race_placeholder_title_repair(self):
        pending = {
            "status": "pending", "title": "待确认知识卡片", "revision": "r-old",
            "content": "兜底正文", "userEdited": False,
        }
        with patch(
            "mindos.material_drafts.recover_stale_generation", return_value=pending
        ) as recover, patch(
            "mindos.material_drafts.ensure_minimal_draft"
        ) as ensure:
            result = uploads._repair_missing_confirmed_card(
                "m-placeholder", source_path="/tmp/source.md"
            )
        recover.assert_called_once_with("m-placeholder", "/tmp/source.md")
        ensure.assert_not_called()
        self.assertEqual(result, pending)

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
