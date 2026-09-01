"""Unified application retrieval contract tests."""
import unittest
from unittest.mock import patch

import server


class RetrievalApiTests(unittest.TestCase):
    def test_knowledge_scope_filters_memory_deduplicates_and_honors_limit(self):
        duplicate = "公司章程规定董事会每季度召开一次会议。"
        raw = {
            "results": [
                {"id": "d1", "text": duplicate, "source_path": "/docs/rules.docx", "score": 0.9,
                 "metadata": {"file_name": "rules.docx", "file_type": "docx"}},
                {"id": "d2", "text": duplicate, "source_path": "/docs/copy.docx", "score": 0.8,
                 "metadata": {"file_name": "copy.docx", "file_type": "docx"}},
                {"text": "仅供内部使用的个人记忆", "rel_path": "memory/MEMORY.md", "score": 0.7,
                 "metadata": {"file_name": "MEMORY.md", "file_type": "memory"}},
                {"text": "董事会的 Wiki 摘要", "page_path": "董事会.md", "score": 0.6,
                 "metadata": {"file_name": "董事会", "file_type": "wiki"}},
            ],
            "reranked": True,
        }

        with patch.object(server, "search_documents", return_value=raw):
            result = server.retrieve_context(server.RetrievalRequest(
                query="董事会", scope="knowledge", limit=2,
            ))

        self.assertEqual(result["total"], 2)
        self.assertEqual([hit["source_type"] for hit in result["hits"]], ["document", "wiki"])
        self.assertTrue(result["reranked"])

    def test_memory_scope_uses_memory_store_and_normalizes_hits(self):
        raw = [
            {"id": "m1", "text": "用户偏好简洁的会议纪要。", "rel_path": "agents/USER.md",
             "memory_type": "profile", "score": 0.88},
            {"text": "---", "rel_path": "agents/AGENTS.md", "score": 0.7},
        ]

        with patch.object(server.memory_store, "search_memory", return_value=raw) as search:
            result = server.retrieve_context(server.RetrievalRequest(
                query="用户偏好", scope="memory", limit=3, mode="hybrid",
            ))

        search.assert_called_once_with("用户偏好", n_results=9)
        self.assertEqual(result["mode"], "text")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["hits"][0]["source_type"], "memory")
        self.assertEqual(result["hits"][0]["title"], "USER.md")

    def test_all_scope_never_returns_more_than_limit(self):
        raw = {
            "results": [
                {"text": f"不同的检索结果 {index}", "source_path": f"/docs/{index}.md", "score": 1 - index / 10}
                for index in range(5)
            ],
            "reranked": False,
        }

        with patch.object(server, "search_documents", return_value=raw):
            result = server.retrieve_context(server.RetrievalRequest(
                query="测试", scope="all", limit=2,
            ))

        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["hits"]), 2)


if __name__ == "__main__":
    unittest.main()
