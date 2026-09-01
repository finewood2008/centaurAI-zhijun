"""P12 首页聚合与删除/恢复闭环测试。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wiki_store as real_wiki_store
from mindos import home, uploads, knowledge, governance
from mindos.services import ingestion
from mindos.stores import governance_store

_real_parse_fm = real_wiki_store._parse_frontmatter


def _new_store(self) -> Path:
    tmp = tempfile.mkdtemp(prefix="p12test_")
    path = Path(tmp) / "governance.db"
    governance_store.reset_for_tests(path)
    return path


class TestHome(unittest.TestCase):

    def setUp(self):
        _new_store(self)

    def test_home_overview(self):
        materials = [
            {"materialId": "mindos_a", "fileName": "A.pdf", "fileType": "document", "status": "available",
             "createdAt": "2026-08-13T00:00:00+00:00"},
            {"materialId": "mindos_b", "fileName": "B.png", "fileType": "image", "status": "available",
             "createdAt": "2026-08-13T01:00:00+00:00"},
        ]
        cards = [
            {"knowledgeId": "knowledge_1", "title": "卡片1", "content": "x", "updatedAt": "2026-08-13T02:00:00+00:00"},
            {"knowledgeId": "knowledge_2", "title": "卡片2", "content": "y", "updatedAt": "2026-08-13T03:00:00+00:00"},
        ]
        with patch.object(ingestion, "list_materials", side_effect=[materials, []]) as mock_list, \
             patch.object(knowledge, "knowledge_list", return_value={"items": cards}), \
             patch.object(governance_store.instance(), "list", return_value=[{"id": "g1"}, {"id": "g2"}]):
            result = home.home_overview()
        self.assertEqual(len(result["recentMaterials"]), 2)
        self.assertEqual(len(result["recentKnowledge"]), 2)
        # 最近编辑按 updatedAt 倒序
        self.assertEqual(result["recentKnowledge"][0]["knowledgeId"], "knowledge_2")
        self.assertEqual(result["failedCount"], 0)
        self.assertEqual(result["pendingGovernance"], 2)
        # list_materials 被调用两次：一次全部、一次 status=failed
        self.assertEqual(mock_list.call_args_list[1], ((), {"status": "failed"}))


class TestKnowledgeLegacyArchiveVisibility(unittest.TestCase):

    def setUp(self):
        _new_store(self)

    def _start(self, patchers):
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _page(self, archived=False, merged_into=None):
        extra = ""
        if archived:
            extra += "mindos_archived: true\n"
        if merged_into:
            extra += f'mindos_merged_into: "{merged_into}"\n'
        return {"path": "/wiki/card.md",
                "content": f'---\ntitle: "Card"\nmindos_card: true\n{extra}---\n# Card\nbody'}

    def test_knowledge_list_hides_legacy_archived_cards(self):
        """已移除归档入口后，历史归档/合并卡片仍不应在普通列表中出现。"""
        active = {"path": "/wiki/active.md",
                  "content": '---\ntitle: "Active"\nmindos_card: true\n---\n# Active\nbody'}
        archived = {"path": "/wiki/archived.md",
                    "content": '---\ntitle: "Archived"\nmindos_card: true\nmindos_archived: true\n---\n# Archived\nbody'}
        merged = {"path": "/wiki/merged.md",
                  "content": '---\ntitle: "Merged"\nmindos_card: true\nmindos_merged_into: "x"\n---\n# Merged\nbody'}

        def read_page(path):
            return {"/wiki/active.md": active, "/wiki/archived.md": archived, "/wiki/merged.md": merged}[path]

        self._start([
            patch.object(knowledge.wiki_store, "list_pages",
                         return_value={"items": [{"path": "/wiki/active.md"}, {"path": "/wiki/archived.md"}, {"path": "/wiki/merged.md"}]}),
            patch.object(knowledge.wiki_store, "read_page", side_effect=read_page),
            patch.object(knowledge, "_is_rag_eligible_page", return_value=True),
        ])
        active_view = knowledge.knowledge_list()["items"]
        self.assertEqual(len(active_view), 1)  # 仅活跃卡片


if __name__ == "__main__":
    unittest.main()
