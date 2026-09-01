from __future__ import annotations

import os
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from mindos import stage_d_admin
from vector_store import IndexMaintenanceError, ensure_index_readable, index_maintenance


class StageDAdminTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = Path(self._tmp.name) / "maintenance.db"
        self._patch = patch.object(stage_d_admin, "STAGE_D_MAINTENANCE_DB_PATH", self._db)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_legacy_read_defaults_to_disabled(self):
        previous = os.environ.pop("MATERIAL_RAG_LEGACY_READ_ENABLED", None)
        try:
            self.assertFalse(stage_d_admin.legacy_read_enabled())
        finally:
            if previous is not None:
                os.environ["MATERIAL_RAG_LEGACY_READ_ENABLED"] = previous

    def test_plan_is_persistent_and_preflight_only(self):
        preflight = {
            "legacyReadEnabled": False,
            "safeToCleanup": True,
            "blockers": [],
            "collections": [],
            "fingerprint": "verified",
            "rollbackWindow": "backup retained until an administrator removes it",
        }
        with patch.object(stage_d_admin, "preflight_legacy_cleanup", return_value=preflight):
            plan = stage_d_admin.create_legacy_cleanup_plan()
        self.assertEqual(plan["state"], "prepared")
        self.assertEqual(plan["preflight"]["fingerprint"], "verified")
        from contextlib import closing
        with closing(stage_d_admin._connect()) as conn:
            row = conn.execute("SELECT state FROM legacy_rag_cleanup_plans WHERE token=?", (plan["cleanupToken"],)).fetchone()
        self.assertEqual(row["state"], "prepared")

    def test_plan_rejects_enabled_compatibility_read(self):
        with patch.object(stage_d_admin, "preflight_legacy_cleanup", return_value={"legacyReadEnabled": True, "safeToCleanup": True}):
            with self.assertRaisesRegex(ValueError, "legacy_read_enabled"):
                stage_d_admin.create_legacy_cleanup_plan()

    def test_maintenance_window_rejects_new_vector_operations(self):
        with index_maintenance():
            with self.assertRaises(IndexMaintenanceError):
                ensure_index_readable()

    def test_database_backup_includes_uncheckpointed_wal_and_manifest_hashes(self):
        root = Path(self._tmp.name)
        db_root = root / "db"
        db_root.mkdir()
        source = db_root / "card_ledger.db"
        conn = sqlite3.connect(source)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample VALUES('durable')")
        conn.commit()
        destination = root / "backup"

        with patch.object(stage_d_admin, "DB_ROOT", db_root):
            stage_d_admin._backup_databases(destination / "databases")
        with closing(sqlite3.connect(destination / "databases" / source.name)) as backup:
            self.assertEqual(backup.execute("SELECT value FROM sample").fetchone()[0], "durable")

        with patch("index_registry.get_routing", return_value={
            "routing_epoch": 7, "base_generation_id": "base-a", "delta_generation_id": "delta-a",
        }):
            stage_d_admin._write_backup_manifest(destination, backup_id="backup-a")
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["routingEpoch"], 7)
        self.assertEqual(manifest["files"][0]["path"], "databases/card_ledger.db")
        self.assertEqual(len(manifest["files"][0]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
