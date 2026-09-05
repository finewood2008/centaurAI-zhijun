"""P10 知识图谱测试。"""
import sys
import unittest
import tempfile
from unittest.mock import patch, MagicMock
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos import graph
from mindos.stores import card_ledger_store, job_store
import vector_store


def _material_nodes() -> dict:
    return {
        "mindos_a": {"id": "mindos_a", "type": "material", "label": "A.pdf", "fileType": "document", "tags": ["x"]},
        "mindos_b": {"id": "mindos_b", "type": "material", "label": "B.png", "fileType": "image", "tags": ["x"]},
        "mindos_c": {"id": "mindos_c", "type": "material", "label": "C.txt", "fileType": "document", "tags": []},
    }


class TestGraphHelpers(unittest.TestCase):

    def test_get_source_embedding_accepts_numpy_vector(self):
        """Chroma 的单条向量为 ndarray 时，图谱构建不应触发布尔歧义。"""
        collection = MagicMock()
        collection.get.return_value = {"embeddings": [np.array([0.1, 0.2])]}
        with patch.object(vector_store, "get_collection", return_value=collection):
            embedding = vector_store.get_source_embedding("/w/A.pdf")
        self.assertEqual(embedding, [0.1, 0.2])

    def test_add_edge_dedup(self):
        edges = []
        pairs = set()
        graph._add_edge(edges, pairs, "a", "b", "source", "来源")
        graph._add_edge(edges, pairs, "b", "a", "source", "来源")  # 反向去重
        self.assertEqual(len(edges), 1)

    def test_shared_tag_edges_capped(self):
        nodes = _material_nodes()
        pairs: set = set()
        edges = graph._shared_tag_edges(nodes, pairs)
        edge_pairs = {(e["source"], e["target"]) for e in edges}
        self.assertEqual(len(edges), 1)  # a-b 共享 x；c 无标签
        self.assertIn(("mindos_a", "mindos_b"), edge_pairs)

    def test_source_edge_priority_over_candidate(self):
        """同一节点对已有 source 边时，候选边（shared-tag/similar）不应再出现。"""
        nodes = _material_nodes()
        pairs: set[tuple[str, str]] = set()
        edges = []
        graph._add_edge(edges, pairs, "mindos_a", "mindos_b", "source", "来源")
        candidate = graph._shared_tag_edges(nodes, pairs)
        # mindos_a 与 mindos_b 共享标签，但两者已有 source 边 → 不新增 shared-tag 边
        self.assertEqual(candidate, [])
        self.assertEqual(len(edges), 1)

    def test_stats_isolated(self):
        nodes = _material_nodes()
        edges = [{"source": "mindos_a", "target": "mindos_b", "relation": "source"}]
        stats = graph._stats(nodes, edges)
        self.assertEqual(stats["totalNodes"], 3)
        self.assertEqual(stats["materials"], 3)
        self.assertEqual(stats["knowledge"], 0)
        self.assertEqual(stats["totalEdges"], 1)
        self.assertEqual(stats["sourceEdges"], 1)
        self.assertEqual(stats["isolatedNodes"], 1)  # mindos_c 孤立


class TestBuildGraph(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        card_ledger_store.reset_for_tests(Path(self.tmp.name) / "cards.db")
        self.addCleanup(card_ledger_store.reset_for_tests)
        job_store.reset_for_tests(Path(self.tmp.name) / "jobs.db")
        self.addCleanup(job_store.reset_for_tests)
        self.material_records = [
            {"material_id": "mindos_a", "file_name": "A.pdf", "file_type": "document", "source_path": "/w/A.pdf"},
            {"material_id": "mindos_b", "file_name": "B.png", "file_type": "image", "source_path": "/w/B.png"},
        ]

    def _start(self, patchers):
        """启动所有 patch 并在测试结束后逐一还原，避免污染其他测试。"""
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _material_patch(self, **overrides):
        _read = overrides.get("read_page", {"path": "", "content": ""})
        # Current cards become retrievable only after explicit confirmation and indexing.
        for item in overrides.get("list_pages", {"items": []})["items"]:
            page = _read(item["path"]) if callable(_read) else _read
            if not graph.knowledge._is_mindos_card(page):
                continue
            kid = graph.knowledge._knowledge_id(page["path"])
            revision = graph.knowledge._content_revision(page["content"])
            card_ledger_store.confirm_and_enqueue(kid, page["path"], revision, kid, {"body": page["content"]})
            card_ledger_store.activate_vector(kid, 1)
        store = job_store.JobStore.instance()
        for rec in self.material_records:
            store.register(rec["material_id"], rec["file_name"], rec["file_type"], rec["source_path"])
        for mid in overrides.get("recycled", set()):
            store.set_recycled(mid, True)
        read_page_patch = (
            patch("mindos.graph.knowledge.wiki_store.read_page", side_effect=_read)
            if callable(_read)
            else patch("mindos.graph.knowledge.wiki_store.read_page", return_value=_read)
        )
        return [
            patch("mindos.graph.ingestion.material_tags", return_value=overrides.get("material_tags", [])),
            patch("mindos.graph.ingestion.source_path_of", return_value=overrides.get("source_path", "/w/A.pdf")),
            patch("mindos.graph.ingestion.material_for_source", return_value=overrides.get("material_for_source", None)),
            patch("mindos.graph.get_source_embedding", return_value=overrides.get("embedding", None)),
            patch("mindos.graph.vector_search", return_value=overrides.get("vector_search", [])),
            patch("mindos.graph.embed_query", return_value=overrides.get("embed_query", [])),
            patch("mindos.graph.knowledge.search_cards", return_value=overrides.get("search_cards", [])),
            patch("mindos.graph.knowledge.wiki_store.list_pages", return_value=overrides.get("list_pages", {"items": []})),
            read_page_patch,
        ]

    def test_empty_graph(self):
        self.material_records = []
        self._start([
            patch.object(graph.ingestion.JobStore, "instance", return_value=MagicMock(list=lambda: [])),
            patch("mindos.graph.ingestion.material_tags", return_value=[]),
            patch("mindos.graph.knowledge.wiki_store.list_pages", return_value={"items": []}),
        ])
        data = graph.build_graph()
        self.assertEqual(data["nodes"], [])
        self.assertEqual(data["edges"], [])
        self.assertEqual(data["stats"]["totalNodes"], 0)
        self.assertEqual(data["stats"]["isolatedNodes"], 0)

    def test_source_edge_from_card(self):
        card_page = {
            "path": "/wiki/card.md",
            "title": "Card",
            "content": '---\ntitle: "Card"\nmindos_card: true\nmindos_source_material_ids: ["mindos_a"]\n---\n# Card\nBody',
            "updated_at": 1700000000,
        }
        self._start(self._material_patch(
            list_pages={"items": [{"path": "/wiki/card.md"}]},
            read_page=card_page,
        ))
        data = graph.build_graph()
        source_edges = [e for e in data["edges"] if e["relation"] == "source"]
        self.assertEqual(len(source_edges), 1)
        edge = source_edges[0]
        self.assertTrue(edge["source"].startswith("knowledge_"))  # 卡片 → 来源材料
        self.assertEqual(edge["target"], "mindos_a")
        self.assertEqual(data["stats"]["sourceEdges"], 1)
        # 卡片节点存在
        card_node = next(n for n in data["nodes"] if n["type"] == "knowledge")
        self.assertEqual(card_node["label"], "Card")
        # 引用次数：卡片引用 1 个来源，材料被引用 1 次
        material_node = next(n for n in data["nodes"] if n["id"] == "mindos_a")
        self.assertEqual(card_node["referenceCount"], 1)
        self.assertEqual(material_node["referenceCount"], 1)
        self.assertEqual(next(n for n in data["nodes"] if n["id"] == "mindos_b")["referenceCount"], 0)

    def test_old_wiki_page_filtered(self):
        old_page = {
            "path": "/wiki/old.md",
            "content": '---\ntitle: "Old Wiki"\n---\n# Old Wiki\nBody',
            "updated_at": 1700000000,
        }
        self._start(self._material_patch(
            list_pages={"items": [{"path": "/wiki/old.md"}]},
            read_page=old_page,
        ))
        data = graph.build_graph()
        knowledge_nodes = [n for n in data["nodes"] if n["type"] == "knowledge"]
        self.assertEqual(len(knowledge_nodes), 0)  # 旧 Wiki 页面被过滤

    def test_similar_edge_from_vector(self):
        card_page = {
            "path": "/wiki/card.md",
            "content": '---\ntitle: "Card"\nmindos_card: true\n---\n# Card\nBody text',
            "updated_at": 1700000000,
        }
        self._start(self._material_patch(
            material_for_source={"material_id": "mindos_b", "file_name": "B.png", "file_type": "image"},
            embedding=[0.1] * 128,
            vector_search=[{"source_path": "/w/B.png", "text": "similar", "vector_score": 0.8}],
            embed_query=[0.1] * 128,
            list_pages={"items": [{"path": "/wiki/card.md"}]},
            read_page=card_page,
        ))
        data = graph.build_graph()
        similar_edges = [e for e in data["edges"] if e["relation"] == "similar"]
        self.assertGreaterEqual(len(similar_edges), 1)
        self.assertEqual(data["stats"]["similarEdges"], len(similar_edges))

    def test_graph_excludes_recycled_material(self):
        """已回收材料不出现在图谱节点与边中，记录仍保留可恢复。"""
        self._start(self._material_patch(recycled={"mindos_a"}))
        data = graph.build_graph()
        material_ids = {n["id"] for n in data["nodes"] if n["type"] == "material"}
        self.assertNotIn("mindos_a", material_ids)  # 已归档材料被排除
        self.assertIn("mindos_b", material_ids)
        # 所有边的两端都不应引用已归档材料
        for edge in data["edges"]:
            self.assertNotIn("mindos_a", (edge["source"], edge["target"]))

    def test_graph_excludes_recycled_and_merged_knowledge(self):
        """已回收/已合并知识卡片不进入图谱节点，也不参与相似召回。"""
        active_card = {"path": "/wiki/active.md",
                       "content": '---\ntitle: "Active"\nmindos_card: true\n---\n# Active\nbody text'}
        archived_card = {"path": "/wiki/archived.md",
                         "content": '---\ntitle: "Archived"\nmindos_card: true\nmindos_recycled: true\n---\n# Archived\nbody text'}
        merged_card = {"path": "/wiki/merged.md",
                       "content": '---\ntitle: "Merged"\nmindos_card: true\nmindos_merged_into: "knowledge_x"\n---\n# Merged\nbody'}

        def read_page(path):
            return {
                "/wiki/active.md": active_card,
                "/wiki/archived.md": archived_card,
                "/wiki/merged.md": merged_card,
            }[path]

        self._start(self._material_patch(
            list_pages={"items": [{"path": "/wiki/active.md"}, {"path": "/wiki/archived.md"}, {"path": "/wiki/merged.md"}]},
            read_page=read_page,
        ))
        data = graph.build_graph()
        knowledge_ids = {n["id"] for n in data["nodes"] if n["type"] == "knowledge"}
        self.assertEqual(len(knowledge_ids), 1)  # 仅 active 卡片
        active_kid = graph.knowledge._knowledge_id("/wiki/active.md")
        archived_kid = graph.knowledge._knowledge_id("/wiki/archived.md")
        merged_kid = graph.knowledge._knowledge_id("/wiki/merged.md")
        self.assertIn(active_kid, knowledge_ids)
        self.assertNotIn(archived_kid, knowledge_ids)  # 归档卡片被排除
        self.assertNotIn(merged_kid, knowledge_ids)  # 已合并卡片被排除


if __name__ == "__main__":
    unittest.main()
