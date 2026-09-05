"""P9 标签与关联区块测试。"""
import json
import sys
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wiki_store
from mindos.services import ingestion
from mindos import knowledge, related

_real_parse_fm = wiki_store._parse_frontmatter


class _FakeJobStore:
    _instance = None
    def __init__(self):
        self._records = {}

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, material_id, file_name, file_type, source_path, folder=""):
        self._records[material_id] = {
            "material_id": material_id,
            "file_name": file_name,
            "file_type": file_type,
            "source_path": source_path,
            "job_id": "job_" + material_id,
            "folder": folder or "未分类",
            "created_at": 1700000000,
        }

    def get(self, material_id):
        return self._records.get(material_id)

    def list(self, device_scope="global"):
        return [record for record in self._records.values()
                if record.get("device_scope", "global") == device_scope]


def _setup_materials():
    store = _FakeJobStore()
    store._records.clear()
    store.register("mindos_tag1", "report.pdf", "document", "/data/report.pdf", "项目")
    store.register("mindos_tag2", "photo.jpg", "image", "/data/photo.jpg", "项目")
    store.register("mindos_tag3", "notes.txt", "document", "/data/notes.txt", "个人")
    return store


class TestMaterialTags(unittest.TestCase):

    def setUp(self):
        self.store = _setup_materials()
        _FakeJobStore._instance = self.store

    @patch("mindos.services.ingestion.JobStore")
    def test_material_tags_empty_by_default(self, mock_job_store_cls):
        mock_job_store_cls.instance.return_value = self.store
        with patch("mindos.services.ingestion._ann_get", return_value={"tags": []}):
            tags = ingestion.material_tags("mindos_tag1")
            self.assertEqual(tags, [])

    @patch("mindos.services.ingestion.JobStore")
    def test_add_material_tag(self, mock_job_store_cls):
        mock_job_store_cls.instance.return_value = self.store
        with patch("mindos.services.ingestion._ann_get", return_value={"tags": []}), \
             patch("mindos.services.ingestion._ann_set", return_value=({"tags": ["重要"]}, False)) as mock_set:
            tags = ingestion.set_material_tags("mindos_tag1", ["重要"], "add")
            self.assertEqual(tags, ["重要"])
            mock_set.assert_called_once_with("/data/report.pdf", {"tags": ["重要"]}, merge=True)

    @patch("mindos.services.ingestion.JobStore")
    def test_remove_material_tag(self, mock_job_store_cls):
        mock_job_store_cls.instance.return_value = self.store
        with patch("mindos.services.ingestion._ann_get", return_value={"tags": ["重要", "项目"]}), \
             patch("mindos.services.ingestion._ann_set", return_value=({"tags": ["项目"]}, False)) as mock_set:
            tags = ingestion.set_material_tags("mindos_tag1", ["重要"], "remove")
            self.assertEqual(tags, ["项目"])
            mock_set.assert_called_once_with("/data/report.pdf", {"tags": ["项目"]}, merge=True)

    @patch("mindos.services.ingestion.JobStore")
    def test_material_tags_nonexistent(self, mock_job_store_cls):
        mock_job_store_cls.instance.return_value = self.store
        tags = ingestion.material_tags("nonexistent")
        self.assertEqual(tags, [])

    @patch("mindos.services.ingestion.JobStore")
    def test_list_materials_filter_by_tag(self, mock_job_store_cls):
        mock_job_store_cls.instance.return_value = self.store
        ann_map = {
            "/data/report.pdf": {"tags": ["项目"]},
            "/data/photo.jpg": {"tags": ["个人"]},
            "/data/notes.txt": {"tags": []},
        }
        with patch("mindos.services.ingestion._ann_get", side_effect=lambda sp: ann_map.get(sp, {"tags": []})), \
             patch("mindos.services.ingestion.status_of") as mock_status:
            mock_status.side_effect = lambda mid, device_scope="global": {"materialId": mid, "status": "available", "createdAt": "2024-01-01T00:00:00+00:00", "fileName": mid, "fileType": "document", "jobId": "j", "errorMessage": None, "folder": "f"} if device_scope == "global" else None
            results = ingestion.list_materials(tag="项目")
            ids = [r["materialId"] for r in results]
            self.assertIn("mindos_tag1", ids)
            self.assertNotIn("mindos_tag3", ids)


class TestKnowledgeTags(unittest.TestCase):

    def test_tags_from_frontmatter(self):
        page = {
            "path": "/wiki/test.md",
            "content": "---\ntitle: \"Test\"\ntags: [\"alpha\", \"beta\"]\nmindos_card: true\n---\n# Test\nBody",
        }
        tags = knowledge._tags(page)
        self.assertEqual(tags, ["alpha", "beta"])

    def test_tags_empty_frontmatter(self):
        page = {
            "path": "/wiki/test.md",
            "content": "---\ntitle: \"Test\"\ntags: []\nmindos_card: true\n---\n# Test\nBody",
        }
        tags = knowledge._tags(page)
        self.assertEqual(tags, [])

    def test_tags_no_frontmatter(self):
        page = {"path": "/wiki/test.md", "content": "No frontmatter"}
        tags = knowledge._tags(page)
        self.assertEqual(tags, [])

    def test_public_includes_tags(self):
        page = {
            "path": "/wiki/test.md",
            "content": '---\ntitle: "Test"\ntags: ["x"]\nmindos_card: true\nmindos_source_material_ids: []\n---\n# Test\nBody',
            "updated_at": 1700000000,
        }
        public = knowledge._public(page)
        self.assertEqual(public["tags"], ["x"])

    @patch("mindos.knowledge.wiki_store")
    def test_knowledge_create_persists_tags(self, mock_wiki):
        mock_wiki.create_page.return_value = {"path": "/wiki/new.md"}
        mock_wiki.write_page.return_value = {
            "path": "/wiki/new.md",
            "content": '---\ntitle: "Card"\ntags: ["tag1"]\nmindos_card: true\n---\n# Card\nBody',
            "updated_at": 1700000000,
        }
        req = knowledge.KnowledgeCreate(title="Card", content="Body", tags=["tag1"])
        result = knowledge.knowledge_create(req)
        written_content = mock_wiki.write_page.call_args[0][1]
        self.assertIn('"tag1"', written_content)

    @patch("mindos.knowledge.wiki_store")
    def test_knowledge_update_preserves_tags(self, mock_wiki):
        existing = {
            "path": "/wiki/exist.md",
            "content": '---\ntitle: "Old"\ntags: ["keep"]\nmindos_card: true\nmindos_source_material_ids: []\ncreated_at: "2024-01-01T00:00:00+00:00"\n---\n# Old\nBody',
            "updated_at": 1700000000,
        }
        mock_wiki._parse_frontmatter.side_effect = _real_parse_fm
        mock_wiki.list_pages.return_value = {"items": [{"path": "/wiki/exist.md"}]}
        mock_wiki.read_page.return_value = existing
        mock_wiki.write_page.return_value = {
            "path": "/wiki/exist.md",
            "content": '---\ntitle: "New"\ntags: ["keep"]\nmindos_card: true\n---\n# New\nBody',
            "updated_at": 1700000001,
        }
        kid = knowledge._knowledge_id("/wiki/exist.md")
        req = knowledge.KnowledgeUpdate(title="New", content="Body", tags=["keep"])
        result = knowledge.knowledge_update(kid, req)
        written_content = mock_wiki.write_page.call_args[0][1]
        self.assertIn('"keep"', written_content)

    @patch("mindos.knowledge.wiki_store")
    def test_knowledge_update_omitted_tags_preserves_existing_tags(self, mock_wiki):
        existing = {
            "path": "/wiki/exist.md",
            "content": '---\ntitle: "Old"\ntags: ["keep"]\nmindos_card: true\nmindos_source_material_ids: []\ncreated_at: "2024-01-01T00:00:00+00:00"\n---\n# Old\nBody',
            "updated_at": 1700000000,
        }
        mock_wiki._parse_frontmatter.side_effect = _real_parse_fm
        mock_wiki.list_pages.return_value = {"items": [{"path": "/wiki/exist.md"}]}
        mock_wiki.read_page.return_value = existing
        mock_wiki.write_page.return_value = {
            "path": "/wiki/exist.md",
            "content": '---\ntitle: "New"\ntags: ["keep"]\nmindos_card: true\n---\n# New\nBody',
            "updated_at": 1700000001,
        }
        kid = knowledge._knowledge_id("/wiki/exist.md")
        req = knowledge.KnowledgeUpdate(title="New", content="Body")
        knowledge.knowledge_update(kid, req)
        written_content = mock_wiki.write_page.call_args[0][1]
        self.assertIn('"keep"', written_content)

    @patch("mindos.knowledge.wiki_store")
    def test_knowledge_tags_endpoint_add(self, mock_wiki):
        existing = {
            "path": "/wiki/exist.md",
            "content": '---\ntitle: "T"\ntags: ["a"]\nmindos_card: true\nmindos_source_material_ids: []\n---\n# T\nBody',
            "updated_at": 1700000000,
        }
        mock_wiki._parse_frontmatter.side_effect = _real_parse_fm
        mock_wiki.list_pages.return_value = {"items": [{"path": "/wiki/exist.md"}]}
        mock_wiki.read_page.return_value = existing
        mock_wiki.write_page.return_value = {
            "path": "/wiki/exist.md",
            "content": '---\ntitle: "T"\ntags: ["a", "b"]\nmindos_card: true\n---\n# T\nBody',
            "updated_at": 1700000001,
        }
        req = knowledge.KnowledgeTagRequest(tags=["b"], action="add")
        kid = knowledge._knowledge_id("/wiki/exist.md")
        result = knowledge.knowledge_tags(kid, req)
        self.assertEqual(result["tags"], ["a", "b"])

    @patch("mindos.knowledge.wiki_store")
    def test_knowledge_tags_endpoint_remove(self, mock_wiki):
        existing = {
            "path": "/wiki/exist.md",
            "content": '---\ntitle: "T"\ntags: ["a", "b"]\nmindos_card: true\nmindos_source_material_ids: []\n---\n# T\nBody',
            "updated_at": 1700000000,
        }
        mock_wiki._parse_frontmatter.side_effect = _real_parse_fm
        mock_wiki.list_pages.return_value = {"items": [{"path": "/wiki/exist.md"}]}
        mock_wiki.read_page.return_value = existing
        mock_wiki.write_page.return_value = {
            "path": "/wiki/exist.md",
            "content": '---\ntitle: "T"\ntags: ["a"]\nmindos_card: true\n---\n# T\nBody',
            "updated_at": 1700000001,
        }
        req = knowledge.KnowledgeTagRequest(tags=["b"], action="remove")
        kid = knowledge._knowledge_id("/wiki/exist.md")
        result = knowledge.knowledge_tags(kid, req)
        self.assertEqual(result["tags"], ["a"])

    @patch("mindos.knowledge.wiki_store")
    def test_find_rejects_non_mindos_card(self, mock_wiki):
        """_find must not return old Wiki pages that lack mindos_card: true."""
        mock_wiki.list_pages.return_value = {"items": [{"path": "/wiki/old.md"}]}
        mock_wiki.read_page.return_value = {
            "path": "/wiki/old.md",
            "content": '---\ntitle: "Old Wiki"\ntags: ["x"]\n---\n# Old Wiki\nBody',
            "updated_at": 1700000000,
        }
        kid = knowledge._knowledge_id("/wiki/old.md")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            knowledge._find(kid)
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("mindos.knowledge.wiki_store")
    def test_tags_endpoint_rejects_non_mindos_card(self, mock_wiki):
        """Tag endpoint must not write to old Wiki pages."""
        mock_wiki.list_pages.return_value = {"items": [{"path": "/wiki/old.md"}]}
        mock_wiki.read_page.return_value = {
            "path": "/wiki/old.md",
            "content": '---\ntitle: "Old Wiki"\ntags: ["x"]\n---\n# Old Wiki\nBody',
            "updated_at": 1700000000,
        }
        kid = knowledge._knowledge_id("/wiki/old.md")
        req = knowledge.KnowledgeTagRequest(tags=["y"], action="add")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            knowledge.knowledge_tags(kid, req)
        self.assertEqual(ctx.exception.status_code, 404)
        mock_wiki.write_page.assert_not_called()


class TestRelatedContent(unittest.TestCase):

    @patch("mindos.related.get_source_embedding")
    @patch("mindos.related.vector_search")
    @patch("mindos.related.ingestion.source_path_of")
    @patch("mindos.related.ingestion.material_tags")
    @patch("mindos.related.ingestion.detail_of")
    @patch("mindos.related.ingestion.material_for_source")
    @patch("mindos.related.knowledge.search_cards")
    @patch("mindos.related.knowledge.knowledge_list")
    def test_material_related_returns_similar(self, mock_klist, mock_search_cards, mock_mat_for_src,
                                               mock_detail, mock_mat_tags, mock_src_path,
                                               mock_vec_search, mock_get_embedding):
        mock_src_path.return_value = "/data/report.pdf"
        mock_mat_tags.return_value = ["项目"]
        mock_get_embedding.return_value = [0.1] * 128
        mock_vec_search.return_value = [
            {"source_path": "/data/other.pdf", "text": "similar content", "vector_score": 0.8},
        ]
        mock_mat_for_src.return_value = {
            "material_id": "mindos_other",
            "file_name": "other.pdf",
            "file_type": "document",
        }
        mock_detail.return_value = {"summary": "report summary"}
        mock_search_cards.return_value = []
        mock_klist.return_value = {"items": []}

        result = related.material_related("mindos_tag1")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], "mindos_other")
        self.assertEqual(result["items"][0]["sourceType"], "material")
        self.assertEqual(result["items"][0]["reason"], "内容相似")

    @patch("mindos.related.ingestion.source_path_of")
    def test_material_related_not_found(self, mock_src_path):
        mock_src_path.return_value = None
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            related.material_related("nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("mindos.related.embed_query")
    @patch("mindos.related.knowledge._find")
    @patch("mindos.related.knowledge._public")
    @patch("mindos.related.knowledge.search_cards")
    @patch("mindos.related.knowledge.knowledge_list")
    @patch("mindos.related.vector_search")
    @patch("mindos.related.ingestion.material_for_source")
    def test_knowledge_related_returns_similar(self, mock_mat_for_src, mock_vec_search,
                                               mock_klist, mock_search_cards, mock_public,
                                               mock_find, mock_embed):
        mock_page = {
            "path": "/wiki/card.md",
            "content": '---\ntitle: "Card"\ntags: ["x"]\nmindos_card: true\n---\n# Card\nBody text',
            "updated_at": 1700000000,
        }
        mock_find.return_value = mock_page
        mock_public.return_value = {"tags": ["x"], "title": "Card", "content": "body"}
        mock_embed.return_value = [0.1] * 128
        mock_vec_search.return_value = [
            {"source_path": "/data/rel.pdf", "text": "related", "vector_score": 0.7},
        ]
        mock_mat_for_src.return_value = {
            "material_id": "mindos_rel",
            "file_name": "rel.pdf",
            "file_type": "document",
        }
        mock_search_cards.return_value = []
        mock_klist.return_value = {"items": []}

        result = related.knowledge_related("knowledge_abc")
        self.assertGreater(len(result["items"]), 0)
        self.assertEqual(result["items"][0]["id"], "mindos_rel")
        self.assertEqual(result["items"][0]["sourceType"], "material")


class TestDualStateBoundary(unittest.TestCase):

    @patch("mindos.services.ingestion.JobStore")
    def test_material_tags_do_not_modify_file(self, mock_job_store_cls):
        """Material tags are stored in annotations metadata, not in file content."""
        store = _setup_materials()
        _FakeJobStore._instance = store
        mock_job_store_cls.instance.return_value = store

        with patch("mindos.services.ingestion._ann_get", return_value={"tags": []}), \
             patch("mindos.services.ingestion._ann_set", return_value=({"tags": ["test"]}, False)) as mock_set, \
             patch("builtins.open", side_effect=AssertionError("should not open file for writing")):
            tags = ingestion.set_material_tags("mindos_tag1", ["test"], "add")
            self.assertEqual(tags, ["test"])
            # Verify annotations.set_annotation was called (metadata storage)
            mock_set.assert_called_once()

    @patch("mindos.knowledge.wiki_store")
    def test_knowledge_tags_only_in_frontmatter(self, mock_wiki):
        """Knowledge card tags are stored in frontmatter, not in raw material files."""
        existing = {
            "path": "/wiki/card.md",
            "content": '---\ntitle: "T"\ntags: ["a"]\nmindos_card: true\nmindos_source_material_ids: []\n---\n# T\nBody',
            "updated_at": 1700000000,
        }
        mock_wiki._parse_frontmatter.side_effect = _real_parse_fm
        mock_wiki.list_pages.return_value = {"items": [{"path": "/wiki/card.md"}]}
        mock_wiki.read_page.return_value = existing
        mock_wiki.write_page.return_value = {
            "path": "/wiki/card.md",
            "content": '---\ntitle: "T"\ntags: ["a", "b"]\nmindos_card: true\n---\n# T\nBody',
            "updated_at": 1700000001,
        }
        req = knowledge.KnowledgeTagRequest(tags=["b"], action="add")
        kid = knowledge._knowledge_id("/wiki/card.md")
        result = knowledge.knowledge_tags(kid, req)
        self.assertEqual(result["tags"], ["a", "b"])
        # Verify write_page was called (knowledge card storage, not raw material)
        mock_wiki.write_page.assert_called_once()
        # Verify the path is a wiki path, not a material source_path
        written_path = mock_wiki.write_page.call_args[0][0]
        self.assertTrue("/wiki/" in written_path or str(written_path).endswith(".md"))


if __name__ == "__main__":
    unittest.main()
