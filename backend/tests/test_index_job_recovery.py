"""delta 损坏时的索引任务精确重放回归测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos.stores import job_store


class IndexJobRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._previous_path = job_store._DB_PATH
        self.store = job_store.reset_for_tests(Path(self._tmp.name) / "job_store.db")

    def tearDown(self) -> None:
        job_store.reset_for_tests(self._previous_path)
        self._tmp.cleanup()

    def _done_job(self, path: str, generation_id: str) -> None:
        self.assertTrue(self.store.enqueue_index_job(path, target_generation_id=generation_id))
        self.assertIsNotNone(self.store.claim_index_job(path))
        self.store.finish_index_job(path, "done")

    def test_requeues_only_jobs_written_to_corrupted_delta(self) -> None:
        self._done_job("source-in-bad-delta", "delta-bad")
        self._done_job("source-in-healthy-delta", "delta-healthy")
        self._done_job("source-in-legacy-base", "legacy-base")

        replayed = self.store.requeue_done_index_jobs_for_generation("delta-bad")

        self.assertEqual([row["source_path"] for row in replayed], ["source-in-bad-delta"])
        self.assertEqual(self.store.get_index_job("source-in-bad-delta")["state"], "queued")
        self.assertEqual(self.store.get_index_job("source-in-healthy-delta")["state"], "done")
        self.assertEqual(self.store.get_index_job("source-in-legacy-base")["state"], "done")

    def _failed_job(self, path: str, error_code: str) -> dict:
        self.assertTrue(self.store.enqueue_index_job(path))
        self.assertIsNotNone(self.store.claim_index_job(path))
        self.store.finish_index_job(path, "failed", error="test failure", error_code=error_code)
        return self.store.get_index_job(path)

    def test_requeues_infrastructure_failure_after_health_recovery(self) -> None:
        failed = self._failed_job("hnsw-write", "write_failed")
        self.assertEqual(failed["failure_class"], "infrastructure")

        replayed = self.store.requeue_recoverable_failures(infrastructure_only=True)

        self.assertEqual([row["source_path"] for row in replayed], ["hnsw-write"])
        self.assertEqual(self.store.get_index_job("hnsw-write")["state"], "queued")

    def test_never_automatically_requeues_business_failure(self) -> None:
        failed = self._failed_job("empty-document", "empty")
        self.assertEqual(failed["failure_class"], "business")

        replayed = self.store.requeue_recoverable_failures()

        self.assertEqual(replayed, [])
        self.assertEqual(self.store.get_index_job("empty-document")["state"], "failed")

    def test_transient_failure_waits_for_backoff_and_stops_after_limit(self) -> None:
        failed = self._failed_job("embedder-network", "embed_failed")
        self.assertEqual(failed["failure_class"], "transient")
        self.assertEqual(failed["auto_retry_count"], 1)
        self.assertGreater(failed["next_retry_at"], 0)
        self.assertEqual(self.store.requeue_recoverable_failures(failure_classes=("transient",)), [])

        conn = self.store._connect()
        try:
            conn.execute("UPDATE index_jobs SET next_retry_at=0 WHERE source_path=?", ("embedder-network",))
            conn.commit()
        finally:
            conn.close()
        replayed = self.store.requeue_recoverable_failures(failure_classes=("transient",))
        self.assertEqual([row["source_path"] for row in replayed], ["embedder-network"])

        # Exhausted transient jobs remain visible as failed and are not retried forever.
        self.assertIsNotNone(self.store.claim_index_job("embedder-network"))
        for _ in range(2):
            self.store.finish_index_job("embedder-network", "failed", error_code="embed_failed")
            conn = self.store._connect()
            try:
                conn.execute("UPDATE index_jobs SET next_retry_at=0 WHERE source_path=?", ("embedder-network",))
                conn.commit()
            finally:
                conn.close()
            self.store.requeue_recoverable_failures(failure_classes=("transient",))
            self.assertIsNotNone(self.store.claim_index_job("embedder-network"))
        self.store.finish_index_job("embedder-network", "failed", error_code="embed_failed")
        exhausted = self.store.get_index_job("embedder-network")
        self.assertEqual(exhausted["auto_retry_count"], 4)
        self.assertEqual(self.store.requeue_recoverable_failures(failure_classes=("transient",)), [])

    def test_success_clears_auto_retry_history(self) -> None:
        self._failed_job("recovered-embedder", "embed_failed")
        conn = self.store._connect()
        try:
            conn.execute("UPDATE index_jobs SET next_retry_at=0 WHERE source_path=?", ("recovered-embedder",))
            conn.commit()
        finally:
            conn.close()
        self.store.requeue_recoverable_failures(failure_classes=("transient",))
        self.assertIsNotNone(self.store.claim_index_job("recovered-embedder"))
        self.store.finish_index_job("recovered-embedder", "done")
        completed = self.store.get_index_job("recovered-embedder")
        self.assertEqual(completed["auto_retry_count"], 0)
        self.assertIsNone(completed["failure_class"])
        self.assertIsNone(completed["next_retry_at"])


if __name__ == "__main__":
    unittest.main()
