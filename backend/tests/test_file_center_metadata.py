"""SQLite file-center metadata regression tests."""
import tempfile
import unittest
from pathlib import Path

import annotations


class FileCenterMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db = annotations._DB_PATH
        self.old_legacy_annotations = annotations._LEGACY_ANNOTATIONS_PATH
        self.old_legacy_groups = annotations._LEGACY_GROUPS_PATH
        annotations._DB_PATH = Path(self.tempdir.name) / "file_center.db"
        annotations._LEGACY_ANNOTATIONS_PATH = Path(self.tempdir.name) / "annotations.json"
        annotations._LEGACY_GROUPS_PATH = Path(self.tempdir.name) / "groups.json"
        annotations._INITIALIZED_PATH = None

    def tearDown(self):
        annotations._DB_PATH = self.old_db
        annotations._LEGACY_ANNOTATIONS_PATH = self.old_legacy_annotations
        annotations._LEGACY_GROUPS_PATH = self.old_legacy_groups
        annotations._INITIALIZED_PATH = None
        self.tempdir.cleanup()

    def test_batch_annotation_is_audited_and_undoable(self):
        paths = ["/watch/a.pdf", "/watch/b.pdf"]
        annotations.set_annotation(paths[0], {"tags": ["旧"], "note": "原备注"})

        result = annotations.batch_set_annotations(
            paths,
            {"tags": ["新"], "note": "追加", "importance": 4},
            tags_mode="add",
            note_mode="append",
        )

        self.assertEqual(result["updated"], 2)
        self.assertIsInstance(result["audit_id"], int)
        self.assertEqual(annotations.get(paths[0])["tags"], ["旧", "新"])
        self.assertEqual(annotations.get(paths[0])["note"], "原备注\n追加")
        self.assertEqual(annotations.get(paths[1])["importance"], 4)

        undone = annotations.undo_audit(result["audit_id"])
        self.assertEqual(undone["restored"], 2)
        self.assertEqual(annotations.get(paths[0])["tags"], ["旧"])
        self.assertEqual(annotations.get(paths[1]), annotations._empty())

    def test_groups_counts_and_rename_are_transactional(self):
        self.assertTrue(annotations.create_group("项目甲"))
        annotations.set_annotation("/watch/a.md", {"group": "项目甲"})
        self.assertEqual(annotations.list_groups(), [{"name": "项目甲", "count": 1}])
        self.assertTrue(annotations.rename_group("项目甲", "项目乙"))
        self.assertEqual(annotations.get("/watch/a.md")["group"], "项目乙")
        self.assertEqual(annotations.delete_group("项目乙"), 1)
        self.assertEqual(annotations.list_groups(), [])

    def test_trash_record_preserves_annotation_snapshot(self):
        audit_id = annotations.record_trash(
            {
                "id": "trash-1",
                "original_path": "/watch/a.pdf",
                "trash_path": "/trash/a.pdf",
                "file_name": "a.pdf",
                "size": 123,
                "annotation": {"tags": ["合同"], "caption": "采购合同"},
                "metadata": {"rag_strategy": "precise"},
            }
        )
        self.assertIsInstance(audit_id, int)
        record = annotations.get_trash("trash-1")
        self.assertEqual(record["annotation"]["tags"], ["合同"])
        self.assertEqual(record["metadata"]["rag_strategy"], "precise")
        annotations.mark_trash_restored("trash-1", "/watch/a.pdf")
        self.assertEqual(annotations.list_trash(), [])


if __name__ == "__main__":
    unittest.main()
