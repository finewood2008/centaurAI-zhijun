import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import memory_store


class MemoryStoreListingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.memory_dir = Path(self.temp.name) / "memory"
        self.previous_memory_dir = memory_store.MEMORY_DIR
        memory_store.MEMORY_DIR = str(self.memory_dir)

    def tearDown(self):
        memory_store.MEMORY_DIR = self.previous_memory_dir
        self.temp.cleanup()

    def test_agent_memory_files_expose_agent_time_and_optional_user(self):
        conversation = self.memory_dir / "conversations" / "codex" / "one.md"
        conversation.parent.mkdir(parents=True)
        conversation.write_text(
            """---
source: tokenmanager
provider: "codex"
created_at: 1784475405594
user_id: "user-7"
user_name: "Alice"
user_email: "alice@example.com"
---

# 修复同步
""",
            encoding="utf-8",
        )
        imported = self.memory_dir / "imports" / "hermes.md"
        imported.parent.mkdir(parents=True)
        imported.write_text(
            """# Imported hermes memory

- imported_at: 2026-07-12T03:32:43
- source_agent: hermes
""",
            encoding="utf-8",
        )

        files = {item["path"]: item for item in memory_store.list_memory_files()}

        self.assertEqual(files["conversations/codex/one.md"]["agent"], "codex")
        self.assertEqual(files["conversations/codex/one.md"]["occurred_at"], 1784475405594)
        self.assertEqual(files["conversations/codex/one.md"]["user_name"], "Alice")
        self.assertEqual(files["imports/hermes.md"]["memory_type"], "agent_import")
        self.assertEqual(files["imports/hermes.md"]["agent"], "hermes")
        self.assertIsInstance(files["imports/hermes.md"]["occurred_at"], int)

    def test_identity_defaults_are_four_openclaw_files_and_legacy_memory_is_preserved(self):
        legacy = self.memory_dir / "MEMORY.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("# Existing long-term memory\n", encoding="utf-8")

        files = {item["path"] for item in memory_store.list_memory_files()}

        self.assertTrue({"SOUL.md", "AGENTS.md", "IDENTITY.md", "USER.md"}.issubset(files))
        self.assertIn("MEMORY.md", files)
        self.assertEqual(legacy.read_text(encoding="utf-8"), "# Existing long-term memory\n")

    def test_tokenmanager_memory_metadata_and_agent_context_are_aggregated(self):
        imported = self.memory_dir / "imports" / "tokenmanager" / "codex" / "one.md"
        imported.parent.mkdir(parents=True)
        imported.write_text(
            """---
source: tokenmanager-memory
memory_id: "memory-1"
provider: "codex"
scope: "agent"
kind: "learned_memory"
source_modified_at: 1784475405594
---

# Codex learned memory

Always preserve the incremental cursor.
""",
            encoding="utf-8",
        )

        files = {item["path"]: item for item in memory_store.list_memory_files()}
        item = files["imports/tokenmanager/codex/one.md"]
        self.assertEqual(item["agent"], "codex")
        self.assertEqual(item["memory_id"], "memory-1")
        self.assertEqual(item["occurred_at"], 1784475405594)

        context = memory_store.get_context(agent="codex", limit_chars=20_000)
        self.assertIn("Always preserve the incremental cursor", context["context"])

    def test_symlink_root_reads_writes_and_deletes_its_own_files(self):
        self.memory_dir.mkdir()
        alias = Path(self.temp.name) / "memory-alias"
        alias.symlink_to(self.memory_dir, target_is_directory=True)
        memory_store.MEMORY_DIR = str(alias)
        memory_store.write_memory_file("imports/test.md", "Own memory", skip_index=True)
        self.assertEqual(memory_store.read_memory_file("imports/test.md")["content"], "Own memory")
        collection = MagicMock()
        with patch.object(memory_store, "_get_memory_collection", return_value=collection):
            self.assertTrue(memory_store.delete_memory_file("imports/test.md"))
        self.assertFalse((self.memory_dir / "imports/test.md").exists())
        collection.delete.assert_called_once_with(where={
            "source_path": str((self.memory_dir / "imports/test.md").resolve()),
        })

    def test_symlink_root_still_rejects_traversal_and_external_symlinks(self):
        self.memory_dir.mkdir()
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("Must remain untouched", encoding="utf-8")
        (self.memory_dir / "escape.md").symlink_to(outside)
        for path in ("../outside.md", "escape.md"):
            with self.subTest(path=path):
                self.assertIsNone(memory_store.read_memory_file(path))
                self.assertFalse(memory_store.delete_memory_file(path))
                with self.assertRaises(ValueError):
                    memory_store.write_memory_file(path, "overwrite", skip_index=True)
        self.assertEqual(outside.read_text(encoding="utf-8"), "Must remain untouched")


if __name__ == "__main__":
    unittest.main()
