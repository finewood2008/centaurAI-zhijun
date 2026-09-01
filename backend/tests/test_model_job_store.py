"""模型任务持久化恢复的轻量回归测试（不依赖 FastAPI）。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos.stores import model_job_store as mjs


_LEGACY_SCHEMA = """
CREATE TABLE model_jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    target_model TEXT NOT NULL,
    state TEXT NOT NULL,
    progress_current INTEGER,
    progress_total INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message_safe TEXT,
    config_revision INTEGER,
    owner TEXT,
    cancel_requested_at TEXT
);
"""


class ModelJobStoreMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._previous_path = mjs._DB_PATH
        mjs.ModelJobStore.reset()
        mjs._DB_PATH = Path(self._tmp.name) / "job_store.db"

    def tearDown(self) -> None:
        mjs.ModelJobStore.reset()
        mjs._DB_PATH = self._previous_path
        self._tmp.cleanup()

    def test_legacy_table_gains_snapshot_columns(self) -> None:
        conn = sqlite3.connect(mjs._DB_PATH)
        try:
            conn.executescript(_LEGACY_SCHEMA)
            conn.commit()
        finally:
            conn.close()

        store = mjs.ModelJobStore.instance()
        conn = sqlite3.connect(mjs._DB_PATH)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(model_jobs)")}
        finally:
            conn.close()
        self.assertTrue(set(mjs._MODEL_JOB_COLUMN_MIGRATIONS).issubset(columns))

        job = store.create_job(
            type_="pull",
            target_model="qwen3:1.7b",
            config_revision=1,
            local_base_url="http://127.0.0.1:11434",
            local_timeout_seconds=60,
            local_keep_alive=300,
            local_context_window=4096,
        )
        self.assertEqual(job["state"], mjs.STATE_QUEUED)

    def test_expired_cancel_request_becomes_cancelled(self) -> None:
        store = mjs.ModelJobStore.instance()
        job = store.create_job(type_="pull", target_model="qwen3:1.7b", config_revision=1)
        claimed = store.claim_next("worker-a")
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(store.request_cancel(job["id"])["state"], mjs.STATE_CANCEL_REQUESTED)

        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conn = sqlite3.connect(mjs._DB_PATH)
        try:
            conn.execute("UPDATE model_jobs SET lease_until=? WHERE id=?", (expired, job["id"]))
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(store.recover_expired(), 1)
        self.assertEqual(store.get(job["id"])["state"], mjs.STATE_CANCELLED)

    def test_purge_removes_only_old_terminal_jobs(self) -> None:
        store = mjs.ModelJobStore.instance()
        old = store.create_job(type_="pull", target_model="old", config_revision=1)
        current = store.create_job(type_="pull", target_model="current", config_revision=1)
        queued = store.create_job(type_="pull", target_model="queued", config_revision=1)
        conn = sqlite3.connect(mjs._DB_PATH)
        try:
            old_finished = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
            current_finished = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()
            conn.execute(
                "UPDATE model_jobs SET state=?, finished_at=? WHERE id=?",
                (mjs.STATE_SUCCEEDED, old_finished, old["id"]),
            )
            conn.execute(
                "UPDATE model_jobs SET state=?, finished_at=? WHERE id=?",
                (mjs.STATE_FAILED, current_finished, current["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(store.purge_expired_terminal_jobs(30), 1)
        self.assertIsNone(store.get(old["id"]))
        self.assertIsNotNone(store.get(current["id"]))
        self.assertIsNotNone(store.get(queued["id"]))


if __name__ == "__main__":
    unittest.main()
