import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
