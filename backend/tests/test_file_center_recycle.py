"""Recoverable file deletion integration test without touching the real library."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import annotations
import server
from fastapi import HTTPException


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
            self.assertEqual(annotations.get(str(restored_path))["note"], "保留我")
            submit.assert_called_once()

    def test_alias_annotations_use_canonical_key_and_survive_recycle(self):
        source = self.root / "source.md"
        source.write_text("合成内容", encoding="utf-8")
        alias = Path(self.tempdir.name) / "watch-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        alias_source = str(alias / source.name)
        # Legacy alias metadata is preserved when the new endpoint updates it.
        annotations.set_annotation(alias_source, {"tags": ["原有标签"], "note": "原有备注"})
        with patch.object(server, "WATCH_FOLDER", str(self.root)), patch.object(
            server, "_TRASH_DIR", self.trash,
        ), patch.object(server, "delete_document", return_value=True), patch.object(
            server, "submit_index", return_value=True,
        ):
            server.set_annotation(server.AnnotationRequest(source_path=alias_source, note="新备注"))
            canonical = str(source.resolve())
            self.assertEqual(annotations.get(canonical)["tags"], ["原有标签"])
            self.assertEqual(server.get_annotations(alias_source)["annotation"]["note"], "新备注")
            recycled = server._recycle_document(alias_source)
            restored = server.restore_trash(recycled["trash_id"])
            self.assertEqual(annotations.get(restored["source_path"])["tags"], ["原有标签"])
            self.assertEqual(annotations.get(restored["source_path"])["note"], "新备注")

    def test_outside_symlink_rejected_before_metadata_read_or_migration(self):
        outside = Path(self.tempdir.name) / "outside.md"
        outside.write_text("范围外内容", encoding="utf-8")
        link = self.root / "escape.md"
        link.symlink_to(outside)
        with patch.object(server, "WATCH_FOLDER", str(self.root)), patch.object(
            annotations, "rename",
        ) as rename, patch.object(annotations, "get_map_for") as read:
            for action in (
                lambda: server.get_annotations(str(link)),
                lambda: server.set_annotation(server.AnnotationRequest(source_path=str(link), note="不能写")),
                lambda: server._recycle_document(str(link)),
            ):
                with self.subTest(action=action):
                    with self.assertRaises(HTTPException) as error:
                        action()
                    self.assertEqual(error.exception.status_code, 403)
            rename.assert_not_called()
            read.assert_not_called()
        self.assertEqual(outside.read_text(encoding="utf-8"), "范围外内容")

    def test_restore_name_collision_preserves_both_files_and_metadata(self):
        source = self.root / "collision.md"
        source.write_text("原内容", encoding="utf-8")
        annotations.set_annotation(str(source.resolve()), {"tags": ["原文件"]})
        with patch.object(server, "WATCH_FOLDER", str(self.root)), patch.object(
            server, "_TRASH_DIR", self.trash,
        ), patch.object(server, "delete_document", return_value=True), patch.object(
            server, "submit_index", return_value=True,
        ):
            recycled = server._recycle_document(str(source))
            source.write_text("新内容", encoding="utf-8")
            restored = server.restore_trash(recycled["trash_id"])
        self.assertNotEqual(restored["source_path"], str(source.resolve()))
        self.assertEqual(source.read_text(encoding="utf-8"), "新内容")
        self.assertEqual(Path(restored["source_path"]).read_text(encoding="utf-8"), "原内容")
        self.assertEqual(annotations.get(restored["source_path"])["tags"], ["原文件"])


if __name__ == "__main__":
    unittest.main()
