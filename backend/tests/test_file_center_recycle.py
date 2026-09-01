"""Recoverable file deletion integration test without touching the real library."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import annotations
import server


class RecycleBinTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "watch"
        self.root.mkdir()
        self.trash = Path(self.tempdir.name) / ".trash"
        self.old_db = annotations._DB_PATH
        self.old_legacy_annotations = annotations._LEGACY_ANNOTATIONS_PATH
        self.old_legacy_groups = annotations._LEGACY_GROUPS_PATH
        annotations._DB_PATH = Path(self.tempdir.name) / "metadata.db"
        annotations._LEGACY_ANNOTATIONS_PATH = Path(self.tempdir.name) / "legacy-annotations.json"
        annotations._LEGACY_GROUPS_PATH = Path(self.tempdir.name) / "legacy-groups.json"
        annotations._INITIALIZED_PATH = None

    def tearDown(self):
        annotations._DB_PATH = self.old_db
        annotations._LEGACY_ANNOTATIONS_PATH = self.old_legacy_annotations
        annotations._LEGACY_GROUPS_PATH = self.old_legacy_groups
        annotations._INITIALIZED_PATH = None
        self.tempdir.cleanup()

    def test_recycle_then_restore_preserves_file_and_annotation(self):
        source = self.root / "smoke.md"
        source.write_text("可恢复删除测试", encoding="utf-8")
        annotations.set_annotation(str(source), {"tags": ["测试"], "note": "保留我"})

        with (
            patch.object(server, "WATCH_FOLDER", str(self.root)),
            patch.object(server, "_TRASH_DIR", self.trash),
            patch.object(server, "delete_document", return_value=True),
            patch.object(server, "submit_index", return_value=True) as submit,
        ):
            recycled = server._recycle_document(str(source))
            self.assertFalse(source.exists())
            self.assertTrue(Path(annotations.get_trash(recycled["trash_id"])["trash_path"]).exists())

            restored = server.restore_trash(recycled["trash_id"])
            restored_path = Path(restored["source_path"])
            self.assertTrue(restored_path.exists())
            self.assertEqual(restored_path.read_text(encoding="utf-8"), "可恢复删除测试")
            self.assertEqual(annotations.get(str(restored_path))["tags"], ["测试"])
            submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
