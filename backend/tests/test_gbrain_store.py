"""GBrain adapter contract tests; never touch the real local brain."""
import unittest
from unittest.mock import patch

import gbrain_store


class GBrainStoreTests(unittest.TestCase):
    def test_status_reports_local_independent_runtime(self):
        config = {
            "engine": "pglite",
            "embedding_model": "ollama:bge-m3",
            "embedding_dimensions": 1024,
            "database_path": "/tmp/centaur-brain",
        }
        with (
            patch.object(gbrain_store, "initialize", return_value=config),
            patch.object(gbrain_store, "_ollama_status", return_value={"reachable": True, "model_available": True}),
            patch.object(gbrain_store, "_call", side_effect=[{"page_count": 4}, {"brain_score": 95}]),
        ):
            result = gbrain_store.status()

        self.assertTrue(result["ready"])
        self.assertTrue(result["independent"])
        self.assertTrue(result["local_embeddings"])
        self.assertEqual(result["embedding_model"], "ollama:bge-m3")
        self.assertEqual(result["stats"]["page_count"], 4)

    def test_cloud_embedding_configuration_is_rejected(self):
        with patch.object(
            gbrain_store,
            "initialize",
            return_value={"embedding_model": "openai:text-embedding-3-large"},
        ):
            with self.assertRaisesRegex(gbrain_store.GBrainError, "不是本地 Ollama"):
                gbrain_store._assert_local_embeddings()

    def test_hybrid_search_disables_cloud_expansion(self):
        rows = [{"slug": "concepts/a", "title": "A", "score": 0.8}]
        with (
            patch.object(gbrain_store, "_assert_local_embeddings", return_value={}),
            patch.object(gbrain_store, "_call", return_value=rows) as call,
        ):
            result = gbrain_store.search_pages("本地知识", mode="hybrid", limit=8)

        tool, params = call.call_args.args[:2]
        self.assertEqual(tool, "query")
        self.assertFalse(params["expand"])
        self.assertEqual(params["mode"], "conservative")
        self.assertEqual(result["items"], rows)

    def test_capture_adds_provenance_and_local_frontmatter(self):
        with (
            patch.object(gbrain_store, "_assert_local_embeddings", return_value={}),
            patch.object(gbrain_store, "_call", return_value={"status": "imported"}) as call,
        ):
            result = gbrain_store.put_page(
                "半人马路线",
                "# 路线\n\n坚持本地优先。",
                page_type="concept",
                tags=["产品", "本地AI"],
            )

        tool, params = call.call_args.args[:2]
        self.assertEqual(tool, "put_page")
        self.assertEqual(params["ingested_via"], "centaur-vector-db-ui")
        self.assertIn("type: concept", params["content"])
        self.assertIn("本地AI", params["content"])
        self.assertTrue(result["slug"].startswith("concepts/"))

    def test_sync_forces_filesystem_walk_for_ignored_wiki(self):
        # P1-4：测试环境强制隔离后 WIKI_DIR 落在临时数据根下，需要先创建目录
        gbrain_store.WIKI_ROOT.mkdir(parents=True, exist_ok=True)
        with (
            patch.object(gbrain_store, "_assert_local_embeddings", return_value={}),
            patch.object(gbrain_store, "_run", return_value={"imported": 3}) as run,
        ):
            result = gbrain_store.sync_wiki()

        self.assertTrue(result["success"])
        self.assertEqual(run.call_args.kwargs["extra_env"]["GIT_CEILING_DIRECTORIES"], str(gbrain_store.PROJECT_ROOT.resolve()))


if __name__ == "__main__":
    unittest.main()
