"""MindOS P14-10 内容生成草稿回归测试。

覆盖 mindos.generations：
- 校验：无来源拒绝、草稿类型非法拒绝、来源不存在 / 已归档拒绝、instruction 超长拒绝；
- 生成：模型失败 → 明确错误且不创建空草稿；成功 → draftId/content/citations/status，
  content 显式标注「待用户审阅」，草稿保存为 derived_records（不创建卡片、不进检索）；
- 另存为知识卡片：草稿不存在 / 状态非 ok 拒绝；成功 → 创建正式卡片并写入来源 ID
  frontmatter（mindos_source_material_ids），草稿本身保留；
- Review：草稿只作派生数据，不进入普通检索 / 问答证据，直到用户显式另存。

依赖项目 .venv，可独立于 server 运行：
    .venv\\Scripts\\python.exe -m unittest test_mindos_p14_10_generations -v
"""
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException
from pydantic import ValidationError

from mindos import generations
from mindos import knowledge
from mindos.derived import KIND_GENERATED_DRAFT
from mindos.stores import derived_store, governance_store, card_ledger_store

from mindos.generations import (
    CreateKnowledgeFromDraftRequest,
    GenerationRequest,
)


class GenerationValidationTests(unittest.TestCase):
    """入参校验：无来源 / 类型非法 / instruction 超长 / 来源不存在 / 来源已归档。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_no_sources_rejected_by_schema(self):
        with self.assertRaises(ValidationError):
            GenerationRequest(type="study_note", sourceIds=[])

    def test_no_sources_rejected_by_handler(self):
        # 函数内兜底：sourceIds 为空时返回 400（防御，正常由 schema 拦截）
        req = SimpleNamespace(type="study_note", sourceIds=[], instruction="")
        with self.assertRaises(HTTPException) as ctx:
            generations.create_generation(req)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_unsupported_type_rejected(self):
        req = SimpleNamespace(type="blog_post", sourceIds=["m1"], instruction="")
        with self.assertRaises(HTTPException) as ctx:
            generations.create_generation(req)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_instruction_too_long_rejected(self):
        with self.assertRaises(ValidationError):
            GenerationRequest(type="study_note", sourceIds=["m1"], instruction="x" * 501)

    def test_missing_source_rejected(self):
        # 来源不存在：material 解析失败且 knowledge 也找不到 → 404
        req = GenerationRequest(type="study_note", sourceIds=["ghost"], instruction="")
        with patch.object(generations.ingestion, "source_path_of", return_value=None), patch.object(
            generations.knowledge, "_find", side_effect=HTTPException(404, "不存在")
        ):
            with self.assertRaises(HTTPException) as ctx:
                generations.create_generation(req)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_archived_material_rejected(self):
        # 来源已归档：material 存在但在归档集合中 → 404
        req = GenerationRequest(type="study_note", sourceIds=["m_archived"], instruction="")
        with patch.object(generations.ingestion, "source_path_of", return_value="src://a.pdf"), patch.object(
            generations, "_excluded_material_ids", return_value={"m_archived"}
        ):
            with self.assertRaises(HTTPException) as ctx:
                generations.create_generation(req)
        self.assertEqual(ctx.exception.status_code, 404)


class GenerationCreateTests(unittest.TestCase):
    """生成逻辑：成功返回契约；失败不创建空草稿；草稿不进卡片。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _sources(self):
        return [
            {"sourceType": "material", "id": "m1", "title": "a.pdf", "text": "材料内容"},
            {"sourceType": "knowledge", "id": "k1", "title": "卡片A", "text": "卡片内容"},
        ]

    def test_model_failure_returns_503_and_no_draft(self):
        req = GenerationRequest(type="study_note", sourceIds=["m1", "k1"], instruction="")
        with patch.object(generations, "_resolve_source", side_effect=lambda sid: next(
            (s for s in self._sources() if s["id"] == sid), None
        )), patch.object(
            generations, "_call_llm", side_effect=urllib.error.URLError("offline")
        ):
            with self.assertRaises(HTTPException) as ctx:
                generations.create_generation(req)
        self.assertEqual(ctx.exception.status_code, 503)
        # 不创建空草稿：无任何 GENERATED_DRAFT 记录
        conn_rows = self.store._connect().execute(
            "SELECT COUNT(*) AS n FROM derived_records WHERE kind=?",
            (KIND_GENERATED_DRAFT,),
        ).fetchone()
        self.assertEqual(conn_rows["n"], 0)

    def test_generate_success_contract_and_review_marker(self):
        req = GenerationRequest(type="study_note", sourceIds=["m1", "k1"], instruction="重点突出结论")
        with patch.object(generations, "_resolve_source", side_effect=lambda sid: next(
            (s for s in self._sources() if s["id"] == sid), None
        )), patch.object(
            generations, "_call_llm", return_value="这是生成的草稿正文。"
        ), patch.object(
            generations.knowledge, "create_card_with_sources", return_value={"knowledgeId": "k"},
        ) as mock_create:
            resp = generations.create_generation(req)

        self.assertEqual(resp["status"], "ok")
        self.assertTrue(resp["draftId"].startswith("draft_"))
        # 服务端兜底确保草稿显式标注「待用户审阅」
        self.assertIn("待用户审阅", resp["content"])
        self.assertEqual(len(resp["citations"]), 2)
        self.assertEqual(resp["citations"][0]["sourceType"], "material")
        self.assertEqual(resp["citations"][1]["id"], "k1")

        # 草稿保存为派生数据（GENERATED_DRAFT）
        rec = self.store.get_derived_record("generation", resp["draftId"], KIND_GENERATED_DRAFT)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["content"]["type"], "study_note")
        self.assertEqual(rec["content"]["sourceIds"], ["m1", "k1"])

        # Review：生成草稿不得创建正式卡片（草稿不进入检索/问答，直到显式另存）
        mock_create.assert_not_called()

    def test_generate_knowledge_source(self):
        # 知识卡片来源解析（source_path_of 无 → 走 knowledge 分支）
        page = {"path": "/wiki/a.md", "title": "卡片A", "content": "---\ntags: []\n---\n卡片正文"}
        card_ledger_store.reset_for_tests(self._tmp / "cards.db")
        self.addCleanup(card_ledger_store.reset_for_tests)
        kid = knowledge._knowledge_id(page["path"])
        revision = knowledge._content_revision(page["content"])
        card_ledger_store.confirm_and_enqueue(kid, page["path"], revision, kid, {"body": "卡片正文"})
        self.assertFalse(knowledge._is_rag_eligible_page(page))
        card_ledger_store.activate_vector(kid, 1)
        with patch.object(generations.ingestion, "source_path_of", return_value=None), patch.object(
            generations.knowledge, "_find", return_value=page
        ), patch.object(
            generations.knowledge, "_is_archived", return_value=False
        ), patch.object(
            generations.knowledge.wiki_store, "_parse_frontmatter", return_value=({}, "卡片正文")
        ):
            src = generations._resolve_source("k1")
        self.assertEqual(src["sourceType"], "knowledge")
        self.assertEqual(src["id"], "k1")
        self.assertIn("卡片正文", src["text"])

    def test_material_source_resolution(self):
        # 原材料来源解析
        with patch.object(generations.ingestion, "source_path_of", return_value="src://a.pdf"), patch.object(
            generations, "_excluded_material_ids", return_value=set()
        ), patch.object(
            generations.ingestion.JobStore, "instance",
            return_value=MagicMock(get=lambda _m: {"material_id": "m1", "file_name": "a.pdf"}),
        ), patch.object(generations, "_input_text", return_value="已索引材料文本"):
            src = generations._resolve_source("m1")
        self.assertEqual(src["sourceType"], "material")
        self.assertEqual(src["title"], "a.pdf")
        self.assertEqual(src["text"], "已索引材料文本")

    def test_duplicate_source_ids_deduplicated(self):
        # P2-1：sourceIds 重复 → 按首次出现顺序去重，不重复拼接 prompt / 不重复 citation
        req = GenerationRequest(type="study_note", sourceIds=["m1", "k1", "m1"], instruction="")
        with patch.object(generations, "_resolve_source", side_effect=lambda sid: next(
            (s for s in self._sources() if s["id"] == sid), None
        )), patch.object(generations, "_call_llm", return_value="草稿正文。"):
            resp = generations.create_generation(req)
        self.assertEqual(len(resp["citations"]), 2)
        rec = self.store.get_derived_record("generation", resp["draftId"], KIND_GENERATED_DRAFT)
        self.assertEqual(rec["content"]["sourceIds"], ["m1", "k1"])
        self.assertEqual(
            rec["content"]["sourceRefs"],
            [{"sourceType": "material", "id": "m1"}, {"sourceType": "knowledge", "id": "k1"}],
        )


class CreateKnowledgeFromDraftTests(unittest.TestCase):
    """草稿「另存为知识卡片」：状态校验、来源 ID 写入 frontmatter、草稿保留。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed_draft(self, draft_id="draft_x", status="ok", refs=None):
        refs = refs or [
            {"sourceType": "material", "id": "m1"},
            {"sourceType": "knowledge", "id": "k1"},
        ]
        self.store.set_derived_record(
            "generation", draft_id, KIND_GENERATED_DRAFT, status,
            {
                "type": "study_note",
                "content": "草稿正文\n\n> 待用户审阅",
                "citations": [],
                "sourceRefs": refs,
                "sourceIds": [r["id"] for r in refs],
            },
            "hash1", "g",
        )

    def test_missing_draft_rejected(self):
        req = CreateKnowledgeFromDraftRequest(title="", content="正文", tags=[])
        with self.assertRaises(HTTPException) as ctx:
            generations.create_knowledge_from_draft("draft_ghost", req)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_non_ok_draft_rejected(self):
        self._seed_draft(status="failed")
        req = CreateKnowledgeFromDraftRequest(title="", content="正文", tags=[])
        with self.assertRaises(HTTPException) as ctx:
            generations.create_knowledge_from_draft("draft_x", req)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_success_writes_source_ids_to_card(self):
        self._seed_draft()
        created = {
            "knowledgeId": "knowledge_abc",
            "title": "学习笔记",
            "content": "正文",
            "sources": [{"materialId": "m1"}, {"materialId": "k1"}],
        }
        with patch.object(
            generations, "_resolve_source",
            side_effect=lambda sid: {"sourceType": "material", "id": sid, "title": sid, "text": "x"},
        ), patch.object(
            generations.knowledge, "create_card_with_sources", return_value=created,
        ) as mock_create:
            resp = generations.create_knowledge_from_draft(
                "draft_x", CreateKnowledgeFromDraftRequest(title="学习笔记", content="用户编辑后的正文", tags=["笔记"])
            )

        self.assertEqual(resp["item"]["knowledgeId"], "knowledge_abc")
        # 以用户编辑后的正文与带类型来源引用创建正式卡片（混合 material / knowledge）
        mock_create.assert_called_once()
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["title"], "学习笔记")
        self.assertEqual(kwargs["content"], "用户编辑后的正文")
        self.assertEqual(kwargs["tags"], ["笔记"])
        self.assertEqual(
            kwargs["source_refs"],
            [{"sourceType": "material", "id": "m1"}, {"sourceType": "knowledge", "id": "k1"}],
        )

    def test_success_default_title(self):
        self._seed_draft(refs=[{"sourceType": "material", "id": "m1"}])
        with patch.object(generations, "_resolve_source", return_value={"sourceType": "material", "id": "m1", "title": "a", "text": "x"}), patch.object(
            generations.knowledge, "create_card_with_sources", return_value={"knowledgeId": "k"},
        ) as mock_create:
            generations.create_knowledge_from_draft(
                "draft_x", CreateKnowledgeFromDraftRequest(title="", content="正文", tags=[])
            )
        self.assertIn("基于 1 项来源", mock_create.call_args.kwargs["title"])

    def test_empty_content_rejected(self):
        # P2-2：草稿正文空白 → 400，不创建卡片
        self._seed_draft()
        with patch.object(generations.knowledge, "create_card_with_sources") as mock_create:
            with self.assertRaises(HTTPException) as ctx:
                generations.create_knowledge_from_draft(
                    "draft_x", CreateKnowledgeFromDraftRequest(title="", content="   ", tags=[])
                )
        self.assertEqual(ctx.exception.status_code, 400)
        mock_create.assert_not_called()

    def test_all_sources_invalid_returns_conflict(self):
        # P1-2：草稿创建后全部来源失效 → 409，不创建卡片
        self._seed_draft()
        with patch.object(generations, "_resolve_source", return_value=None), patch.object(
            generations.knowledge, "create_card_with_sources",
        ) as mock_create:
            with self.assertRaises(HTTPException) as ctx:
                generations.create_knowledge_from_draft(
                    "draft_x", CreateKnowledgeFromDraftRequest(title="", content="正文", tags=[])
                )
        self.assertEqual(ctx.exception.status_code, 409)
        mock_create.assert_not_called()

    def test_partial_sources_invalid_returns_conflict(self):
        # P1-2：部分来源失效同样 409，避免把原有引用链静默改写成不完整版本
        self._seed_draft()

        def _resolve(sid):
            if sid == "k1":
                return None  # 知识卡片来源已归档
            return {"sourceType": "material", "id": "m1", "title": "a", "text": "x"}

        with patch.object(generations, "_resolve_source", side_effect=_resolve), patch.object(
            generations.knowledge, "create_card_with_sources",
        ) as mock_create:
            with self.assertRaises(HTTPException) as ctx:
                generations.create_knowledge_from_draft(
                    "draft_x", CreateKnowledgeFromDraftRequest(title="", content="正文", tags=[])
                )
        self.assertEqual(ctx.exception.status_code, 409)
        mock_create.assert_not_called()

    def test_legacy_draft_partial_source_invalid_returns_conflict(self):
        # P1-1：历史草稿（无 sourceRefs）仅含 sourceIds，部分来源失效 → 409，
        # 不静默丢弃失效来源、不改写引用链
        self.store.set_derived_record(
            "generation", "draft_legacy", KIND_GENERATED_DRAFT, "ok",
            {
                "type": "study_note",
                "content": "草稿正文\n\n> 待用户审阅",
                "citations": [],
                "sourceIds": ["m1", "k1"],
            },
            "hash1", "g",
        )

        def _resolve(sid):
            if sid == "k1":
                return None  # k1 已归档
            return {"sourceType": "material", "id": "m1", "title": "a", "text": "x"}

        with patch.object(generations, "_resolve_source", side_effect=_resolve), patch.object(
            generations.knowledge, "create_card_with_sources",
        ) as mock_create:
            with self.assertRaises(HTTPException) as ctx:
                generations.create_knowledge_from_draft(
                    "draft_legacy", CreateKnowledgeFromDraftRequest(title="", content="正文", tags=[])
                )
        self.assertEqual(ctx.exception.status_code, 409)
        mock_create.assert_not_called()


class SourceRefsTests(unittest.TestCase):
    """P14-10 来源追溯：mindos_source_refs 混合解析、旧字段兼容、去重、_sources 带类型。"""

    def _page(self, refs_lines):
        content = '---\ntitle: "T"\nmindos_card: true\n' + "\n".join(refs_lines) + '\n---\n# T\n正文'
        return {"path": "/wiki/t.md", "title": "T", "content": content}

    def test_mixed_refs_parsed(self):
        page = self._page([
            'mindos_source_refs: [{"sourceType": "knowledge", "id": "knowledge_abc"}, {"sourceType": "material", "id": "mindos_m"}]',
        ])
        self.assertEqual(
            knowledge._source_refs(page),
            [{"sourceType": "knowledge", "id": "knowledge_abc"}, {"sourceType": "material", "id": "mindos_m"}],
        )

    def test_legacy_material_ids_compat(self):
        # 旧字段兼容：mindos_source_material_ids 解析为 material；knowledge_ 前缀被忽略
        page = self._page(['mindos_source_material_ids: ["mindos_m", "knowledge_ignored"]'])
        self.assertEqual(knowledge._source_refs(page), [{"sourceType": "material", "id": "mindos_m"}])

    def test_deduplicated(self):
        page = self._page([
            'mindos_source_refs: [{"sourceType": "material", "id": "mindos_m"}]',
            'mindos_source_material_ids: ["mindos_m"]',
        ])
        self.assertEqual(knowledge._source_refs(page), [{"sourceType": "material", "id": "mindos_m"}])

    def test_sources_returns_typed_entries(self):
        page = self._page([
            'mindos_source_refs: [{"sourceType": "knowledge", "id": "knowledge_abc"}, {"sourceType": "material", "id": "mindos_m"}]',
        ])
        with patch.object(knowledge, "_find", return_value={"path": "/wiki/k.md", "title": "卡片A", "content": "---\n---\n正文"}), patch.object(
            knowledge, "_is_archived", return_value=False
        ), patch.object(
            knowledge.governance_store, "instance",
            return_value=MagicMock(archived_material_ids=lambda: set()),
        ), patch.object(knowledge.ingestion, "status_of", return_value={"fileName": "a.pdf"}):
            sources = knowledge._sources(page)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["sourceType"], "knowledge")
        self.assertEqual(sources[0]["id"], "knowledge_abc")
        self.assertEqual(sources[0]["title"], "卡片A")
        self.assertEqual(sources[0]["archived"], False)
        self.assertEqual(sources[1]["sourceType"], "material")
        self.assertEqual(sources[1]["id"], "mindos_m")
        self.assertEqual(sources[1]["title"], "a.pdf")


if __name__ == "__main__":
    unittest.main(verbosity=2)
