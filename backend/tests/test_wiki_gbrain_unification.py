"""Regression tests for Wiki-as-source / GBrain-as-index architecture."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gbrain_store
import wiki_store


class WikiGBrainUnificationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_wiki_dir = wiki_store.WIKI_DIR
        self.old_wiki_db = wiki_store.WIKI_DB_PATH
        wiki_store.WIKI_DIR = str(Path(self.tempdir.name) / "wiki")
        wiki_store.WIKI_DB_PATH = str(Path(self.tempdir.name) / "wiki" / "wiki.sqlite3")
        wiki_store._SCHEMA_READY = False

    def tearDown(self):
        wiki_store.WIKI_DIR = self.old_wiki_dir
        wiki_store.WIKI_DB_PATH = self.old_wiki_db
        wiki_store._SCHEMA_READY = False
        self.tempdir.cleanup()

    def test_wiki_write_is_source_and_incrementally_updates_gbrain(self):
        content = "---\ntitle: \"统一知识\"\ntype: concept\ntags: [\"架构\"]\n---\n\n# 统一知识\n\nWiki 是唯一真源。\n"
        with patch.object(
            gbrain_store,
            "sync_wiki_page",
            return_value={"success": True, "slug": "concepts/统一知识"},
        ) as sync:
            page = wiki_store.write_page("Concepts/统一知识.md", content)

        source = Path(wiki_store.WIKI_DIR) / "Concepts" / "统一知识.md"
        self.assertEqual(source.read_text(encoding="utf-8"), content)
        sync.assert_called_once_with("Concepts/统一知识.md", content)
        self.assertTrue(page["gbrain_sync"]["success"])
        self.assertEqual(wiki_store.stats()["vector_engine"], "gbrain")
        self.assertEqual(wiki_store.stats()["gbrain_synced"], 1)

    def test_wiki_search_uses_gbrain_and_maps_back_to_source_path(self):
        content = "---\ntitle: \"产品路线\"\ntype: concept\n---\n\n# 产品路线\n\n本地优先。\n"
        with patch.object(gbrain_store, "sync_wiki_page", return_value={"success": True}):
            wiki_store.write_page("Concepts/产品路线.md", content)
        with patch.object(
            gbrain_store,
            "search_pages",
            return_value={
                "items": [
                    {
                        "slug": "concepts/产品路线",
                        "title": "产品路线",
                        "type": "concept",
                        "chunk_id": 7,
                        "chunk_text": "坚持本地优先",
                        "score": 0.91,
                    }
                ]
            },
        ) as search:
            results = wiki_store.search_wiki("路线", n_results=5)

        search.assert_called_once_with("路线", mode="hybrid", limit=5)
        self.assertEqual(results[0]["page_path"], "Concepts/产品路线.md")
        self.assertEqual(results[0]["metadata"]["vector_engine"], "gbrain")

    def test_wiki_delete_propagates_to_gbrain_and_removes_metadata(self):
        content = "---\ntitle: \"待删除\"\ntype: note\n---\n\n# 待删除\n"
        with patch.object(gbrain_store, "sync_wiki_page", return_value={"success": True}):
            wiki_store.write_page("Resources/待删除.md", content)
        (Path(wiki_store.WIKI_DIR) / "Resources" / "待删除.md").unlink()
        with patch.object(gbrain_store, "delete_wiki_page", return_value={"success": True}) as delete:
            result = wiki_store.remove_page_from_gbrain("Resources/待删除.md")

        delete.assert_called_once_with("Resources/待删除.md")
        self.assertTrue(result["success"])
        self.assertIsNone(wiki_store.read_page("Resources/待删除.md"))
        self.assertEqual(wiki_store.stats()["gbrain_synced"], 0)

    def test_wiki_connection_context_commits_and_closes(self):
        connections = []
        original_connect = sqlite3.connect

        class TrackingConnection(sqlite3.Connection):
            closed = False

            def close(self):
                self.closed = True
                super().close()

        def connect(*args, **kwargs):
            connection = original_connect(*args, factory=TrackingConnection, **kwargs)
            connections.append(connection)
            return connection

        with patch.object(wiki_store.sqlite3, "connect", side_effect=connect):
            with wiki_store._connect() as connection:
                connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('close-test', 'ok')")
            self.assertTrue(connections[-1].closed)

            with wiki_store._connect() as connection:
                value = connection.execute("SELECT value FROM meta WHERE key='close-test'").fetchone()[0]

        self.assertEqual(value, "ok")
        self.assertTrue(all(connection.closed for connection in connections))


if __name__ == "__main__":
    unittest.main()
