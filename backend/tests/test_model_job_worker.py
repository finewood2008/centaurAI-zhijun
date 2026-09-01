"""模型任务 worker 的取消、恢复与快照固定回归测试。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos import model_job_worker as worker_module
from mindos.runtime_config_provider import LocalOllamaSnapshot
from mindos.stores import model_job_store as mjs


class ModelJobWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._previous_path = mjs._DB_PATH
        mjs.ModelJobStore.reset()
        mjs._DB_PATH = Path(self._tmp.name) / "job_store.db"
        self.store = mjs.ModelJobStore.instance()
        self.active_snapshot = LocalOllamaSnapshot(
            base_url="http://127.0.0.1:11434",
            model="new-model",
            timeout_seconds=60,
            keep_alive=300,
            context_window=4096,
        )
        self.provider = SimpleNamespace(
            get_local_snapshot=lambda: self.active_snapshot,
            store=None,
        )
        self.worker = worker_module.ModelJobWorker()
        self._provider_patch = patch.object(
            worker_module, "get_provider", return_value=self.provider
        )
        self._provider_patch.start()

    def tearDown(self) -> None:
        self._provider_patch.stop()
        mjs.ModelJobStore.reset()
        mjs._DB_PATH = self._previous_path
        self._tmp.cleanup()

    def _create_and_claim(self, *, type_="pull") -> tuple[dict, dict]:
        job = self.store.create_job(
            type_=type_,
            target_model="qwen3:1.7b",
            config_revision=1,
            local_base_url="http://127.0.0.1:11434",
            local_timeout_seconds=60,
            local_keep_alive=300,
            local_context_window=4096,
        )
        claimed = self.store.claim_next("worker-a")
        self.assertIsNotNone(claimed)
        return job, claimed

    def test_pull_cancelled_during_stream_reaches_terminal_state(self) -> None:
        job, claimed = self._create_and_claim()

        def fake_pull(*_args, should_abort=None, **_kwargs):
            self.store.request_cancel(job["id"])
            self.assertIsNotNone(should_abort)
            self.assertTrue(should_abort())
            return {"status": "cancelled"}, []

        with patch.object(worker_module.ollama_client, "pull", side_effect=fake_pull):
            self.worker._execute(claimed, self.store, "worker-a")

        self.assertEqual(self.store.get(job["id"])["state"], mjs.STATE_CANCELLED)

    def test_recovered_pull_with_installed_model_is_completed(self) -> None:
        job, _ = self._create_and_claim()
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conn = sqlite3.connect(mjs._DB_PATH)
        try:
            conn.execute("UPDATE model_jobs SET lease_until=? WHERE id=?", (expired, job["id"]))
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(self.store.recover_expired(), 1)
        with patch.object(worker_module.ollama_client, "model_installed", return_value=True):
            self.worker._resolve_recovered_pulls(self.store)

        self.assertEqual(self.store.get(job["id"])["state"], mjs.STATE_SUCCEEDED)

    def test_job_snapshot_does_not_follow_later_runtime_change(self) -> None:
        job, _ = self._create_and_claim()
        self.active_snapshot = LocalOllamaSnapshot(
            base_url="http://127.0.0.1:22434",
            model="later-model",
            timeout_seconds=30,
            keep_alive=60,
            context_window=2048,
        )

        snapshot = self.worker._snapshot_from_job(self.store.get(job["id"]))
        self.assertEqual(snapshot.base_url, "http://127.0.0.1:11434")
        self.assertEqual(snapshot.model, "qwen3:1.7b")
        self.assertEqual(snapshot.timeout_seconds, 60)


if __name__ == "__main__":
    unittest.main()
