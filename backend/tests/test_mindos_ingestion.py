"""MindOS P2 导入服务回归测试。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos.services import ingestion
from mindos.stores import derived_store, job_store, material_pipeline_store
from mindos import material_worker


class MindosIngestionTests(unittest.TestCase):
    def setUp(self):
        # 切换到临时 JobStore DB，避免持久化开发数据（job_records.material_id
        # UNIQUE 约束）污染真实库，也保证测试可重复执行。
        self._tmp = tempfile.TemporaryDirectory()
        job_store.reset_for_tests(Path(self._tmp.name) / "jobs.db")
        material_pipeline_store.reset_for_tests(Path(self._tmp.name) / "pipeline.db")
        derived_store.reset_for_tests(Path(self._tmp.name) / "derived.db")

    def tearDown(self):
        job_store.reset_for_tests()
        material_pipeline_store.reset_for_tests()
        derived_store.reset_for_tests()
        self._tmp.cleanup()

    def test_watcher_state_mapping(self):
        self.assertEqual(ingestion._map_watcher_state("queued"), ingestion.ST_PROCESSING)
        self.assertEqual(ingestion._map_watcher_state("processing"), ingestion.ST_PROCESSING)
        self.assertEqual(ingestion._map_watcher_state("done"), ingestion.ST_AVAILABLE)
        self.assertEqual(ingestion._map_watcher_state("failed"), ingestion.ST_FAILED)
        self.assertEqual(ingestion._map_watcher_state("unknown"), ingestion.ST_PROCESSING)

    def test_public_record_never_exposes_source_path(self):
        record = {
            "material_id": "mindos_a1",
            "file_name": "plan.pdf",
            "file_type": "document",
            "source_path": r"C:\private\watch_folder\.mindos_uploads\plan.pdf",
            "job_id": "job_a1",
            "created_at": time.time(),
        }
        public = ingestion.public_record(record, ingestion.ST_PROCESSING, None)
        self.assertEqual(public["materialId"], "mindos_a1")
        self.assertNotIn("source_path", public)
        self.assertNotIn("saved_path", public)

    @patch("mindos.services.ingestion._submit_material_job")
    def test_mindos_submission_uses_material_pipeline(self, submit_material_job):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "uploaded"}):
            ingestion.start_ingestion("mindos_a2", "plan.pdf", "document", "/tmp/.mindos_uploads/plan.pdf")
        submit_material_job.assert_called_once()

    @patch("mindos.services.ingestion.get_job", return_value={"state": "done"})
    def test_retry_rejects_non_failed_job(self, get_job):
        with patch("mindos.services.ingestion.JobStore.instance") as store_instance:
            store_instance.return_value.get.return_value = {"source_path": "/tmp/plan.pdf"}
            with self.assertRaises(ingestion.RetryNotAllowed):
                ingestion.retry_ingestion("mindos_a3")

    def test_remove_from_queue_deletes_only_unprocessed_material(self):
        source = Path(self._tmp.name) / "queued.txt"
        source.write_text("queued", encoding="utf-8")
        ingestion.start_ingestion("mindos_queue", "queued.txt", "document", str(source))
        self.assertTrue(ingestion.remove_from_queue("mindos_queue"))
        self.assertFalse(source.exists())
        self.assertIsNone(job_store.JobStore.instance().get("mindos_queue"))

    def test_remove_from_queue_rejects_processing_material(self):
        source = Path(self._tmp.name) / "processing.txt"
        source.write_text("processing", encoding="utf-8")
        ingestion.start_ingestion("mindos_processing", "processing.txt", "document", str(source))
        from mindos.material_worker import run_epoch
        job = material_pipeline_store.MaterialPipelineStore.instance().claim_next_material_job(run_epoch=run_epoch())
        self.assertIsNotNone(job)
        with self.assertRaises(ingestion.RetryNotAllowed):
            ingestion.remove_from_queue("mindos_processing")

    def test_lifecycle_cancel_prevents_worker_from_finishing_processing_job(self):
        source = Path(self._tmp.name) / "lifecycle.txt"
        source.write_text("processing", encoding="utf-8")
        ingestion.start_ingestion("mindos_lifecycle", "lifecycle.txt", "document", str(source))
        from mindos.material_worker import run_epoch
        store = material_pipeline_store.MaterialPipelineStore.instance()
        job = store.claim_next_material_job(run_epoch=run_epoch())
        self.assertIsNotNone(job)
        self.assertTrue(store.cancel_for_lifecycle("mindos_lifecycle", 1))
        # 迟到的 worker 收尾不得将取消任务重新标记为 draft_ready。
        store.finish_material_job(job["job_id"], "draft_ready")
        current = store.material_job("mindos_lifecycle", 1)
        self.assertEqual(current["state"], material_pipeline_store.ST_CANCELED)
        self.assertTrue(source.exists())

    def test_derived_submission_failure_does_not_block_other_outputs(self):
        worker = material_worker.MaterialWorker()
        with patch("mindos.material_drafts.ensure_minimal_draft", side_effect=RuntimeError("draft failed")), \
             patch("mindos.material_drafts.submit_generation") as submit_generation, \
             patch("mindos.derived.submit_summary") as submit_summary, \
             patch("mindos.derived.submit_analysis") as submit_analysis:
            worker._trigger_derived("mindos_a4", "/tmp/ready.txt")
        submit_summary.assert_called_once_with("mindos_a4", "/tmp/ready.txt")
        submit_analysis.assert_called_once_with("mindos_a4", "/tmp/ready.txt")
        submit_generation.assert_called_once_with("mindos_a4", "/tmp/ready.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
