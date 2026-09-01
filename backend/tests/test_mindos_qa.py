"""MindOS P8 AI 问答回归测试。"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindos import qa
import lexical


# 多数问答测试只验证召回、模型调用或响应契约；它们的材料替身没有真实 SQLite
# 任务，因此默认显式声明为可用。状态准入本身由下方专用回归用例覆盖。
_status_patch = None


def setUpModule():
    global _status_patch
    _status_patch = patch("mindos.qa.ingestion.status_of", return_value={"status": "available"})
    _status_patch.start()


def tearDownModule():
    if _status_patch is not None:
        _status_patch.stop()


class QaValidationTests(unittest.TestCase):
    """问题校验。"""

    def test_web_qa_route_accepts_unwrapped_question_body(self):
        """Web 的 POST /api/mindos/qa 契约必须是 {"question": "..."}。

        source_ids 是 Agent 的可选 query 参数；若未显式标记 Query，FastAPI 会把
        set 类型合并为 body 字段，导致浏览器请求被错误要求包成 {"req": {...}}。
        """
        from fastapi import FastAPI

        app = FastAPI()
        qa.configure_write_guard(lambda: None)
        app.include_router(qa.router)
        schema = app.openapi()["paths"]["/api/mindos/qa"]["post"]["requestBody"]
        self.assertEqual(schema["content"]["application/json"]["schema"], {
            "$ref": "#/components/schemas/QaRequest"
        })

    def test_empty_question_returns_400(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            qa.answer_question(qa.QaRequest(question=""))
        self.assertEqual(ctx.exception.status_code, 400)


class QaHybridRetrievalTests(unittest.TestCase):
    """统一混合检索不依赖问题意图分类。"""

    def test_query_terms_remove_question_fillers(self):
        terms = qa._query_terms("MindOS开发会分几个阶段")
        self.assertIn("mindos", terms)
        self.assertIn("开发", terms)
        self.assertIn("阶段", terms)
        self.assertNotIn("几个", terms)

    def test_query_terms_split_continuous_chinese_concepts(self):
        """不依赖问句意图，也能将连续的「开发阶段」对齐到表格字段。"""
        terms = qa._query_terms("MindOS开发阶段分那几个")
        self.assertIn("mindos", terms)
        self.assertIn("开发", terms)
        self.assertIn("阶段", terms)
        self.assertNotIn("那几个", terms)

    def test_bm25_tokenizer_bridges_continuous_and_separated_chinese_words(self):
        tokens = lexical._tokenize("MindOS开发阶段")
        self.assertIn("开发", tokens)
        self.assertIn("阶段", tokens)

    def test_ascii_product_name_does_not_match_versioned_filename_substring(self):
        """产品定义问题不能把 MindOS-P14 当作 MindOS 的正文命中。"""
        self.assertEqual(qa._term_coverage("MindOS-P14 迭代计划", ["mindos"]), 0.0)
        self.assertEqual(qa._term_coverage("个人知识库（MindOS）", ["mindos"]), 1.0)

    def test_structured_matching_table_gets_bonus(self):
        terms = ["开发", "阶段"]
        table = "阶段\t日程\t目标\nP0\tD1-D2\t开发"
        self.assertGreater(qa._structure_bonus(table, terms), 0)
        self.assertEqual(qa._structure_bonus("无关段落", terms), 0)

    @patch("mindos.qa.get_source_chunks")
    @patch("mindos.qa.lexical.search", return_value=[])
    @patch("mindos.qa.embed_query", return_value=[0.1] * 10)
    @patch("mindos.qa.vector_search")
    def test_title_hit_enriches_same_material_structured_context(
        self, mock_vector, mock_embed, mock_lexical, mock_source_chunks
    ):
        """标题命中定位资料后，应补入同源阶段表，而非只把标题交给模型。"""
        mock_vector.return_value = [{
            "id": "schedule::title", "source_path": "schedule.docx",
            "text": "MindOSV1.0 开发排期", "vector_score": 0.92,
        }]
        mock_source_chunks.return_value = [
            {"id": "schedule::title", "text": "MindOSV1.0 开发排期"},
            {"id": "schedule::overview", "text": "阶段\t日程\t目标\nP0\tD1-D2\t方案确定\nP1\tD3-D8\t核心链路开发"},
        ]
        record = {"material_id": "mindos_schedule", "file_name": "MindOS 开发排期.docx", "file_type": "document"}
        with patch("mindos.qa.ingestion.material_for_source", return_value=record), patch(
            "mindos.qa.ingestion.recycled_material_ids", return_value=set()
        ):
            evidence = qa._build_material_evidence("MindOS排期计划是什么", limit=6)
        self.assertTrue(any("P1" in item.snippet for item in evidence))
        self.assertIn("P0", evidence[0].snippet)

    @patch("mindos.qa.lexical.search", return_value=[("schedule::overview", 7.5)])
    @patch("mindos.qa.get_chunks_by_ids")
    @patch("mindos.qa.embed_query", return_value=[0.1] * 10)
    @patch("mindos.qa.vector_search")
    def test_exact_structured_bm25_evidence_can_outrank_broad_semantic_match(
        self, mock_vector, mock_embed, mock_chunks, mock_lexical
    ):
        """精确命中的阶段表格不能因 BM25 固定低分而被泛化介绍文本压制。"""
        question = "MindOS开发会分几个阶段"
        mock_vector.return_value = [{
            "id": "guide::0",
            "source_path": "guide.md",
            "text": "MindOS 是本地知识库系统，支持资料导入和开发指引。",
            "vector_score": 0.95,
        }]
        mock_chunks.return_value = [{
            "id": "schedule::overview",
            "source_path": "schedule.docx",
            "text": "阶段\t日程\t核心目标\nP0\tD1-D2\t方案确定\nP1\tD3-D8\t核心链路开发\nP2\tD9-D13\tV1.0功能开发\nP3\tD14-D18\t稳定性提升",
        }]
        records = {
            "guide.md": {"material_id": "mindos_guide", "file_name": "开发指引.md", "file_type": "document"},
            "schedule.docx": {"material_id": "mindos_schedule", "file_name": "开发排期.docx", "file_type": "document"},
        }

        with patch("mindos.qa.ingestion.material_for_source", side_effect=lambda path: records.get(path)):
            with patch("mindos.qa.ingestion.recycled_material_ids", return_value=set()):
                evidence = qa._build_material_evidence(question, limit=6)

        self.assertEqual(evidence[0].material_id, "mindos_schedule")
        self.assertIn("P3", evidence[0].snippet)
        mock_lexical.assert_called_once_with(question, n_results=qa.VECTOR_CANDIDATES)

    def test_whitespace_only_returns_400(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            qa.answer_question(qa.QaRequest(question="   "))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_too_long_question_returns_400(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            qa.answer_question(qa.QaRequest(question="x" * 501))
        self.assertEqual(ctx.exception.status_code, 400)


class QaEvidenceTests(unittest.TestCase):
    """证据检索与组装。"""

    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_no_evidence_returns_insufficient(self, mock_vs, mock_eq, mock_kc):
        mock_eq.return_value = []
        mock_vs.return_value = []
        mock_kc.return_value = []
        with patch("mindos.qa.ingestion.material_for_source", return_value=None):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = []
                result = qa.answer_question(qa.QaRequest(question="无关问题测试"))
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["citations"], [])
        self.assertIsNone(result["meta"]["model"])
        self.assertEqual(result["meta"]["usedEvidenceCount"], 0)

    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_empty_knowledge_card_is_not_used_as_evidence(self, mock_vs, mock_eq, mock_kc, mock_model):
        """仅有标题/来源的空白卡片不能让模型据此编造答案。"""
        mock_eq.return_value = []
        mock_vs.return_value = []
        mock_kc.return_value = [{
            "knowledgeId": "knowledge_empty",
            "title": "MindOS 资料卡片",
            "snippet": "",
            "score": 1.0,
        }]
        with patch("mindos.qa.ingestion.material_for_source", return_value=None):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = []
                result = qa.answer_question(qa.QaRequest(question="MindOS是什么"))

        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        mock_model.assert_not_called()

    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_low_relevance_knowledge_card_does_not_consume_qa_evidence(self, mock_vs, mock_eq, mock_kc, mock_model):
        """卡片正文即使非空，低于相关度门槛也不能享受知识成品优先。"""
        mock_eq.return_value = []
        mock_vs.return_value = []
        mock_kc.return_value = [{
            "knowledgeId": "knowledge_noise", "title": "MindOS", "snippet": "无关的卡片正文",
            "score": qa.MIN_KNOWLEDGE_SCORE - 0.01,
        }]
        with patch("mindos.qa.ingestion.JobStore") as mock_store:
            mock_store.instance.return_value.list.return_value = []
            result = qa.answer_question(qa.QaRequest(question="MindOS是什么"))
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        mock_model.assert_not_called()

    @patch("mindos.qa.knowledge.search_cards")
    def test_product_question_keeps_definition_card_and_drops_versioned_name_noise(self, mock_cards):
        """卡片上限前应先过滤仅含产品名子串的版本/文件名噪声。"""
        mock_cards.return_value = [
            {
                "knowledgeId": "knowledge_p14",
                "title": "MindOS-P14 Review",
                "snippet": "MindOS-P14 智能解析迭代开发说明。",
                "score": 0.99,
            },
            {
                "knowledgeId": "knowledge_definition",
                "title": "功能需求原始.txt 的知识卡片",
                "snippet": "MindOS 是面向个人用户的 AI 增强型多模态知识库。",
                "score": 0.75,
            },
        ]
        evidence = qa._build_knowledge_evidence("MindOS是什么", limit=6)
        self.assertEqual([item.knowledge_id for item in evidence], ["knowledge_definition"])

    @patch("mindos.qa.lexical.search", return_value=[])
    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_material_evidence_in_response(self, mock_vs, mock_eq, mock_kc, mock_model, mock_lexical):
        """原材料命中：返回 materialId、文件名、片段，不含 source_path。"""
        mock_eq.return_value = [0.1] * 10
        mock_vs.return_value = [
            {
                "source_path": r"C:\private\plan.pdf",
                "text": "项目交付日期为 2026 年 9 月 30 日",
                "vector_score": 0.85,
            }
        ]
        mock_kc.return_value = []
        mock_record = {
            "material_id": "mindos_a1b2c3d4e5f6",
            "file_name": "项目计划.pdf",
            "file_type": "document",
            "source_path": r"C:\private\plan.pdf",
        }
        mock_model.return_value = "项目交付时间为 2026 年 9 月 30 日。"

        with patch("mindos.qa.ingestion.material_for_source", return_value=mock_record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [mock_record]
                result = qa.answer_question(qa.QaRequest(question="项目交付时间"))

        self.assertEqual(result["status"], "ANSWERED")
        self.assertGreaterEqual(len(result["citations"]), 1)
        citation = result["citations"][0]
        self.assertEqual(citation["sourceType"], "material")
        self.assertEqual(citation["materialId"], "mindos_a1b2c3d4e5f6")
        self.assertIsNone(citation["knowledgeId"])
        self.assertEqual(citation["title"], "项目计划.pdf")
        self.assertIn("交付日期", citation["snippet"])
        # 不含物理路径
        citation_str = str(citation)
        self.assertNotIn("source_path", citation_str)
        self.assertNotIn("saved_path", citation_str)
        self.assertNotIn("C:\\private", citation_str)

    @patch("mindos.qa.lexical.search", return_value=[])
    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards", return_value=[])
    @patch("mindos.qa.embed_query", return_value=[0.1] * 10)
    @patch("mindos.qa.vector_search")
    def test_unavailable_material_never_enters_qa_citations_or_model(
        self, mock_vector, _mock_embed, _mock_cards, mock_model, _mock_lexical
    ):
        """遗留向量命中不能绕过处理状态进入问答或 LLM prompt。"""
        mock_vector.return_value = [{
            "source_path": "paused.docx", "text": "尚未确认的处理内容", "vector_score": 0.99,
        }]
        record = {
            "material_id": "mindos_paused", "file_name": "暂停资料.docx",
            "file_type": "document", "source_path": "paused.docx",
        }
        with patch("mindos.qa.ingestion.material_for_source", return_value=record), \
             patch("mindos.qa.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.qa.ingestion.status_of", return_value={
                 "status": "queued", "errorCode": "service_interrupted",
             }):
            result = qa.answer_question(qa.QaRequest(question="处理内容是什么"))

        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["citations"], [])
        mock_model.assert_not_called()

    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_knowledge_card_evidence_in_response(self, mock_vs, mock_eq, mock_kc, mock_model):
        """知识卡片命中：返回 knowledgeId，sourceType 为 knowledge。"""
        mock_eq.return_value = []
        mock_vs.return_value = []
        mock_kc.return_value = [
            {
                "knowledgeId": "knowledge_abc123",
                "title": "项目概要",
                "snippet": "项目交付时间为 2026 年 9 月 30 日",
                "score": 0.9,
            }
        ]
        mock_model.return_value = "项目交付时间为 2026 年 9 月 30 日。"

        with patch("mindos.qa.ingestion.material_for_source", return_value=None):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = []
                result = qa.answer_question(qa.QaRequest(question="项目交付时间"))

        self.assertEqual(result["status"], "ANSWERED")
        self.assertEqual(len(result["citations"]), 1)
        citation = result["citations"][0]
        self.assertEqual(citation["sourceType"], "knowledge")
        self.assertEqual(citation["knowledgeId"], "knowledge_abc123")
        self.assertIsNone(citation["materialId"])

    @patch("mindos.qa.lexical.search", return_value=[])
    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_old_data_filtered(self, mock_vs, mock_eq, mock_kc, mock_model, mock_lexical):
        """旧 Wiki/Memory 数据即使检索命中也被过滤（无法反查到 JobStore 记录）。"""
        mock_eq.return_value = [0.1] * 10
        mock_vs.return_value = [
            {
                "source_path": r"C:\old_wiki\note.md",
                "text": "旧 Wiki 内容",
                "vector_score": 0.95,
            },
            {
                "source_path": r"C:\memory\agent.md",
                "text": "旧 Memory 内容",
                "vector_score": 0.90,
            },
        ]
        mock_kc.return_value = []
        mock_model.return_value = "test"

        # material_for_source 对旧数据返回 None
        with patch("mindos.qa.ingestion.material_for_source", return_value=None):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = []
                result = qa.answer_question(qa.QaRequest(question="旧数据测试"))

        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["citations"], [])
        mock_model.assert_not_called()

    @patch("mindos.qa.lexical.search", return_value=[])
    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_same_material_dedup(self, mock_vs, mock_eq, mock_kc, mock_model, mock_lexical):
        """同一资料多个 chunk 命中时，最多保留通用上限内的不同证据。"""
        mock_eq.return_value = [0.1] * 10
        mock_vs.return_value = [
            {
                "source_path": r"C:\doc.pdf",
                "text": "片段一：交付日期",
                "vector_score": 0.80,
            },
            {
                "source_path": r"C:\doc.pdf",
                "text": "片段二：交付时间",
                "vector_score": 0.90,
            },
        ]
        mock_kc.return_value = []
        mock_model.return_value = "交付时间。"

        mock_record = {
            "material_id": "mindos_dup1",
            "file_name": "doc.pdf",
            "file_type": "document",
            "source_path": r"C:\doc.pdf",
        }
        with patch("mindos.qa.ingestion.material_for_source", return_value=mock_record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [mock_record]
                result = qa.answer_question(qa.QaRequest(question="交付"))

        material_citations = [c for c in result["citations"] if c["sourceType"] == "material"]
        self.assertEqual(len(material_citations), 2)
        # 保留评分最高的
        self.assertIn("片段二", material_citations[0]["snippet"])

    @patch("mindos.qa.lexical.search", return_value=[])
    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_same_material_keeps_multiple_distinct_chunks_with_generic_cap(
        self, mock_vs, mock_eq, mock_kc, mock_model, mock_lexical
    ):
        """同一资料允许保留多个不同分块，但受通用上限控制。"""
        mock_eq.return_value = [0.1] * 10
        mock_kc.return_value = []
        mock_vs.return_value = [
            {"id": "flow::0", "source_path": "flow.txt", "text": "第一步：导入资料并上传文件。", "vector_score": 0.91},
            {"id": "flow::1", "source_path": "flow.txt", "text": "第二步：系统解析内容，生成摘要、标签和实体。", "vector_score": 0.89},
            {"id": "flow::2", "source_path": "flow.txt", "text": "第三步：整理为知识卡片，再用于检索与问答。", "vector_score": 0.87},
            {"id": "flow::3", "source_path": "flow.txt", "text": "第四步：通过版本、归档和治理持续维护。", "vector_score": 0.85},
        ]
        mock_model.return_value = "先导入并解析资料，再组织为知识卡片用于检索问答，最后进行版本和治理维护。"
        record = {"material_id": "mindos_flow", "file_name": "flow.txt", "file_type": "document", "source_path": "flow.txt"}

        with patch("mindos.qa.ingestion.material_for_source", return_value=record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [record]
                result = qa.answer_question(qa.QaRequest(question="MindOS整体流程是什么"))

        citations = [c for c in result["citations"] if c["sourceType"] == "material"]
        self.assertEqual(result["status"], "ANSWERED")
        self.assertEqual(len(citations), qa.MAX_CHUNKS_PER_MATERIAL)
        self.assertTrue(any("导入资料" in c["snippet"] for c in citations))
        self.assertTrue(any("知识卡片" in c["snippet"] for c in citations))
        mock_lexical.assert_called_once()


class QaModelTests(unittest.TestCase):
    """模型调用与错误映射。"""

    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_model_connection_failure_maps_503(self, mock_vs, mock_eq, mock_kc):
        from fastapi import HTTPException
        import urllib.error

        mock_eq.return_value = [0.1] * 10
        mock_vs.return_value = [
            {"source_path": "x", "text": "证据", "vector_score": 0.8},
        ]
        mock_kc.return_value = []
        mock_record = {
            "material_id": "mindos_t1",
            "file_name": "t.pdf",
            "file_type": "document",
            "source_path": "x",
        }

        with patch("mindos.qa.ingestion.material_for_source", return_value=mock_record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [mock_record]
                with patch("mindos.qa.llm_transport.allowed_urlopen", side_effect=urllib.error.URLError("refused")):
                    with self.assertRaises(HTTPException) as ctx:
                        qa.answer_question(qa.QaRequest(question="测试问题"))
        self.assertEqual(ctx.exception.status_code, 503)

    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_model_timeout_maps_504(self, mock_vs, mock_eq, mock_kc):
        from fastapi import HTTPException

        mock_eq.return_value = [0.1] * 10
        mock_vs.return_value = [
            {"source_path": "x", "text": "证据", "vector_score": 0.8},
        ]
        mock_kc.return_value = []
        mock_record = {
            "material_id": "mindos_t2",
            "file_name": "t.pdf",
            "file_type": "document",
            "source_path": "x",
        }

        with patch("mindos.qa.ingestion.material_for_source", return_value=mock_record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [mock_record]
                with patch("mindos.qa.llm_transport.allowed_urlopen", side_effect=TimeoutError("timed out")):
                    with self.assertRaises(HTTPException) as ctx:
                        qa.answer_question(qa.QaRequest(question="测试问题"))
        self.assertEqual(ctx.exception.status_code, 504)

    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_model_empty_output_maps_503(self, mock_vs, mock_eq, mock_kc):
        from fastapi import HTTPException

        mock_eq.return_value = [0.1] * 10
        mock_vs.return_value = [
            {"source_path": "x", "text": "证据", "vector_score": 0.8},
        ]
        mock_kc.return_value = []
        mock_record = {
            "material_id": "mindos_t3",
            "file_name": "t.pdf",
            "file_type": "document",
            "source_path": "x",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"message": {"content": ""}}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mindos.qa.ingestion.material_for_source", return_value=mock_record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [mock_record]
                with patch("mindos.qa.llm_transport.allowed_urlopen", return_value=mock_resp):
                    with self.assertRaises(HTTPException) as ctx:
                        qa.answer_question(qa.QaRequest(question="测试问题"))
        self.assertEqual(ctx.exception.status_code, 503)

    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_model_insufficient_output_preserves_retrieved_evidence_as_partial(self, mock_vs, mock_eq, mock_kc):
        """模型两次拒答不等于未检索到资料：必须保留证据并返回 PARTIAL_ANSWER。"""
        mock_eq.return_value = [0.1] * 10
        mock_vs.return_value = [
            {"source_path": "x", "text": "证据内容", "vector_score": 0.8},
        ]
        mock_kc.return_value = []
        mock_record = {
            "material_id": "mindos_t4",
            "file_name": "t.pdf",
            "file_type": "document",
            "source_path": "x",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"message": {"content": "资料不足，无法回答"}}, ensure_ascii=False).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mindos.qa.ingestion.material_for_source", return_value=mock_record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [mock_record]
                with patch("mindos.qa.llm_transport.allowed_urlopen", return_value=mock_resp):
                    result = qa.answer_question(qa.QaRequest(question="测试问题"))

        self.assertEqual(result["status"], "PARTIAL_ANSWER")
        self.assertGreaterEqual(len(result["citations"]), 1)
        self.assertEqual(result["answer"], qa.PARTIAL_ANSWER)
        self.assertGreaterEqual(result["meta"]["retrievedCount"], 1)
        self.assertEqual(result["meta"]["usedEvidenceCount"], result["meta"]["retrievedCount"])

    def test_evidence_first_retry_returns_grounded_second_answer(self):
        """首轮保守拒答时以同一证据重试；第二轮能归纳则正常 ANSWERED。"""
        evidence = [qa.Evidence(
            citation_id="m1", source_type="material", material_id="mindos_schedule",
            knowledge_id=None, title="开发排期.docx",
            snippet="阶段\t日程\t目标\nP0\tD1-D2\t方案确定\nP1\tD3-D8\t核心链路开发",
            score=0.9, priority_bucket="material",
        )]
        with patch.object(qa, "build_evidence", return_value=evidence), patch.object(
            qa.corrections, "match_corrections", return_value=[]
        ), patch.object(
            qa, "call_local_qa_model", side_effect=["资料不足，暂不生成结论。", "排期包含 P0 方案确定和 P1 核心链路开发。"]
        ) as model:
            result = qa.answer_question(qa.QaRequest(question="开发排期"))
        self.assertEqual(result["status"], "ANSWERED")
        self.assertIn("P0", result["answer"])
        self.assertEqual(len(result["citations"]), 1)
        self.assertEqual(model.call_count, 2)


class QaConcurrencyTests(unittest.TestCase):
    """并发限制。"""

    def test_concurrent_request_returns_429(self):
        from fastapi import HTTPException

        # 占用信号量
        qa._qa_semaphore.acquire(blocking=False)
        try:
            with self.assertRaises(HTTPException) as ctx:
                qa.call_local_qa_model("test", [], snap=qa.get_provider().get_chat_snapshot())
            self.assertEqual(ctx.exception.status_code, 429)
        finally:
            qa._qa_semaphore.release()


class QaNoWriteTests(unittest.TestCase):
    """问答后不产生写入。"""

    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_no_write_operations(self, mock_vs, mock_eq, mock_kc, mock_model):
        """问答后不调用任何真实写入入口。"""
        mock_eq.return_value = [0.1] * 10
        mock_vs.return_value = [
            {"source_path": "x", "text": "证据", "vector_score": 0.8},
        ]
        mock_kc.return_value = []
        mock_record = {
            "material_id": "mindos_nw1",
            "file_name": "t.pdf",
            "file_type": "document",
            "source_path": "x",
        }
        mock_model.return_value = "测试回答。"

        with patch("mindos.qa.ingestion.material_for_source", return_value=mock_record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [mock_record]
                with patch("wiki_store.write_page") as mock_write_page:
                    with patch("wiki_store.create_page") as mock_create_page:
                        with patch("mindos.qa.ingestion.start_ingestion") as mock_start:
                            with patch("mindos.qa.ingestion.retry_ingestion") as mock_retry:
                                result = qa.answer_question(qa.QaRequest(question="测试问题"))

        self.assertEqual(result["status"], "ANSWERED")
        mock_write_page.assert_not_called()
        mock_create_page.assert_not_called()
        mock_start.assert_not_called()
        mock_retry.assert_not_called()


class QaThresholdFilterTests(unittest.TestCase):
    """相关度阈值过滤。"""

    @patch("mindos.qa.lexical.search", return_value=[])
    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_low_score_filtered_to_insufficient(
        self, mock_vs, mock_eq, mock_kc, mock_model, mock_lexical
    ):
        """低相关向量命中被阈值过滤，返回 INSUFFICIENT_EVIDENCE，模型不调用。

        BM25 词面兜底读取真实索引（依赖本地数据状态），此处显式置空以隔离。
        """
        mock_eq.return_value = [0.1] * 10
        mock_vs.return_value = [
            {"source_path": "x", "text": "低相关内容", "vector_score": 0.10},
        ]
        mock_kc.return_value = []
        mock_record = {
            "material_id": "mindos_low1",
            "file_name": "t.pdf",
            "file_type": "document",
            "source_path": "x",
        }

        with patch("mindos.qa.ingestion.material_for_source", return_value=mock_record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [mock_record]
                result = qa.answer_question(qa.QaRequest(question="无关问题"))

        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["citations"], [])
        mock_model.assert_not_called()

    @patch("mindos.qa.lexical.search", return_value=[])
    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_filename_only_text_filtered(self, mock_vs, mock_eq, mock_kc, mock_model, mock_lexical):
        """仅文件名文本被丢弃，不进入 QA prompt（同时隔离 BM25 兜底）。

        BM25 词面兜底读取真实索引（依赖本地数据状态），此处显式置空以隔离。
        """
        mock_eq.return_value = [0.1] * 10
        mock_vs.return_value = [
            {"source_path": "x", "text": "report.pdf", "vector_score": 0.85},
        ]
        mock_kc.return_value = []
        mock_record = {
            "material_id": "mindos_fn1",
            "file_name": "report.pdf",
            "file_type": "document",
            "source_path": "x",
        }

        with patch("mindos.qa.ingestion.material_for_source", return_value=mock_record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [mock_record]
                result = qa.answer_question(qa.QaRequest(question="report"))

        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["citations"], [])
        mock_model.assert_not_called()


class QaTimeoutWrappingTests(unittest.TestCase):
    """URLError 包装的超时映射。"""

    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_urlerror_wrapped_timeout_maps_504(self, mock_vs, mock_eq, mock_kc):
        from fastapi import HTTPException
        import urllib.error
        import socket as _socket

        mock_eq.return_value = [0.1] * 10
        mock_vs.return_value = [
            {"source_path": "x", "text": "证据", "vector_score": 0.8},
        ]
        mock_kc.return_value = []
        mock_record = {
            "material_id": "mindos_to2",
            "file_name": "t.pdf",
            "file_type": "document",
            "source_path": "x",
        }

        # urllib 常将超时包装为 URLError(reason=socket.timeout)
        wrapped_timeout = urllib.error.URLError(_socket.timeout("timed out"))

        with patch("mindos.qa.ingestion.material_for_source", return_value=mock_record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [mock_record]
                with patch("mindos.qa.llm_transport.allowed_urlopen", side_effect=wrapped_timeout):
                    with self.assertRaises(HTTPException) as ctx:
                        qa.answer_question(qa.QaRequest(question="测试问题"))
        self.assertEqual(ctx.exception.status_code, 504)


class QaPriorityBucketTests(unittest.TestCase):
    """P14-05：知识成品优先于原材料（分桶拼接，不做全局分数排序、不给卡片加分）。"""

    @staticmethod
    def _ev(source_type, score, snippet="x"):
        return qa.Evidence(
            citation_id="k1" if source_type == "knowledge" else "m1",
            source_type=source_type,
            material_id=None if source_type == "knowledge" else "mindos_mat",
            knowledge_id="knowledge_k1" if source_type == "knowledge" else None,
            title="标题",
            snippet=snippet,
            score=score,
            priority_bucket=source_type,
        )

    @patch("mindos.qa._build_knowledge_evidence")
    @patch("mindos.qa._build_material_evidence")
    def test_knowledge_first_even_when_scores_lower(self, mock_mat, mock_know):
        """卡片分数低于材料：知识成品仍排在任何原材料之前。"""
        mock_know.return_value = [self._ev("knowledge", 0.30), self._ev("knowledge", 0.20)]
        mock_mat.return_value = [self._ev("material", 0.98), self._ev("material", 0.95)]

        result = qa.build_evidence("q", limit=6)

        self.assertEqual(
            [e.source_type for e in result],
            ["knowledge", "knowledge", "material", "material"],
        )
        # 内部桶标记正确，且桶内仍按相关度降序
        self.assertEqual([e.priority_bucket for e in result][:2], ["knowledge", "knowledge"])
        self.assertEqual(result[0].score, 0.30)

    @patch("mindos.qa._build_knowledge_evidence")
    @patch("mindos.qa._build_material_evidence")
    def test_materials_fill_when_no_cards(self, mock_mat, mock_know):
        """无卡片：原材料补齐全部证据条数。"""
        mock_know.return_value = []
        mock_mat.return_value = [
            self._ev("material", 0.70),
            self._ev("material", 0.90),
            self._ev("material", 0.80),
        ]

        result = qa.build_evidence("q", limit=6)

        self.assertEqual([e.source_type for e in result], ["material"] * 3)
        # 桶内按分数降序
        self.assertEqual([e.score for e in result], [0.90, 0.80, 0.70])

    @patch("mindos.qa._build_knowledge_evidence")
    @patch("mindos.qa._build_material_evidence")
    def test_cards_fill_budget_materials_excluded(self, mock_mat, mock_know):
        """即使卡片很多，也必须为一手原材料保留统一证据预算。"""
        mock_know.return_value = [
            self._ev("knowledge", 0.60),
            self._ev("knowledge", 0.50),
            self._ev("knowledge", 0.40),
            self._ev("knowledge", 0.30),
            self._ev("knowledge", 0.20),
            self._ev("knowledge", 0.10),
        ]
        mock_mat.return_value = [self._ev("material", 0.99) for _ in range(4)]

        result = qa.build_evidence("q", limit=6)

        self.assertEqual(len(result), 6)
        self.assertEqual([e.source_type for e in result], ["knowledge", "knowledge", "material", "material", "material", "material"])

    @patch("mindos.qa._build_knowledge_evidence")
    @patch("mindos.qa._build_material_evidence")
    def test_material_reserve_is_question_type_independent(self, mock_mat, mock_know):
        """无需识别流程意图，也会为原材料保留预算。"""
        mock_know.return_value = [self._ev("knowledge", 0.9), self._ev("knowledge", 0.8)]
        mock_mat.return_value = [self._ev("material", 0.7), self._ev("material", 0.6)]

        result = qa.build_evidence("任意问题", limit=6)

        self.assertEqual([e.source_type for e in result], ["knowledge", "knowledge", "material", "material"])

    def test_prompt_allows_llm_to_summarize_structured_evidence(self):
        prompt = qa._build_user_prompt(
            "MindOS整体流程是什么", [self._ev("material", 0.9, snippet="导入、解析、检索")]
        )
        self.assertIn("列表或表格时可统计、列举或归纳", prompt)
        self.assertIn("仅提及同一主题的背景内容不构成冲突", prompt)
        self.assertIn("不得补充证据外信息", prompt)

    @patch("mindos.qa._build_knowledge_evidence")
    @patch("mindos.qa._build_material_evidence")
    def test_cards_short_materials_fill_remaining(self, mock_mat, mock_know):
        """卡片不足：原材料补齐剩余条数，卡片仍在前。"""
        mock_know.return_value = [
            self._ev("knowledge", 0.90),
            self._ev("knowledge", 0.80),
        ]
        mock_mat.return_value = [self._ev("material", 0.70 + i * 0.01) for i in range(4)]

        result = qa.build_evidence("q", limit=6)

        self.assertEqual(
            [e.source_type for e in result],
            ["knowledge", "knowledge", "material", "material", "material", "material"],
        )

    @patch("mindos.qa._build_knowledge_evidence")
    @patch("mindos.qa._build_material_evidence")
    def test_cards_do_not_evict_materials_when_they_are_long(self, mock_mat, mock_know):
        """卡片自身很长时，也不能挤掉保留给原材料的位置。"""
        long_snippet = "语" * 700
        mock_know.return_value = [
            self._ev("knowledge", s, snippet=long_snippet)
            for s in (0.60, 0.50, 0.40, 0.30, 0.20, 0.10)
        ]
        mock_mat.return_value = [self._ev("material", 0.99, snippet=long_snippet)]

        result = qa.build_evidence("q", limit=6)

        self.assertEqual(len(result), 3)
        self.assertEqual([e.source_type for e in result], ["knowledge", "knowledge", "material"])
        total = sum(len(e.snippet) for e in result)
        self.assertLessEqual(total, qa.MAX_CONTEXT_CHARS)

    @patch("mindos.qa._build_knowledge_evidence")
    @patch("mindos.qa._build_material_evidence")
    def test_context_char_budget_truncates(self, mock_mat, mock_know):
        """字符预算截断：超预算时最后一条证据片段被截短，总量不超过 MAX_CONTEXT_CHARS。"""
        long_snippet = "语" * 700
        mock_know.return_value = [self._ev("knowledge", 0.90, snippet=long_snippet) for _ in range(2)]
        mock_mat.return_value = [self._ev("material", 0.80, snippet=long_snippet) for _ in range(4)]

        result = qa.build_evidence("q", limit=6)

        self.assertEqual(len(result), 6)
        total = sum(len(e.snippet) for e in result)
        self.assertLessEqual(total, qa.MAX_CONTEXT_CHARS)
        # 5 条完整(700) + 最后一条截短至剩余额度(3600-3500=100)
        self.assertEqual(len(result[-1].snippet), 100)
        self.assertNotEqual(result[-1].snippet, long_snippet)

    @patch("mindos.qa.lexical.search", return_value=[])
    @patch("mindos.qa.call_local_qa_model")
    @patch("mindos.qa.knowledge.search_cards")
    @patch("mindos.qa.embed_query")
    @patch("mindos.qa.vector_search")
    def test_response_citations_knowledge_first(self, mock_vs, mock_eq, mock_kc, mock_model, mock_lexical):
        """端到端：低分卡片 + 高分材料时，响应 citations 中知识卡片恒在原材料之前。"""
        mock_eq.return_value = [0.1] * 10
        mock_kc.return_value = [
            {
                "knowledgeId": "knowledge_low",
                "title": "项目概要",
                "snippet": "项目交付时间为 2026 年 9 月 30 日。",
                "score": 0.40,
            }
        ]
        mock_vs.return_value = [
            {
                "source_path": r"C:\private\plan.pdf",
                "text": "原始材料中的交付信息",
                "vector_score": 0.96,
            }
        ]
        mock_model.return_value = "交付时间。"
        mock_record = {
            "material_id": "mindos_mat01",
            "file_name": "plan.pdf",
            "file_type": "document",
            "source_path": r"C:\private\plan.pdf",
        }

        with patch("mindos.qa.ingestion.material_for_source", return_value=mock_record):
            with patch("mindos.qa.ingestion.JobStore") as mock_store:
                mock_store.instance.return_value.list.return_value = [mock_record]
                result = qa.answer_question(qa.QaRequest(question="项目交付时间"))

        self.assertEqual(result["status"], "ANSWERED")
        self.assertEqual(result["citations"][0]["sourceType"], "knowledge")
        self.assertEqual(result["citations"][0]["knowledgeId"], "knowledge_low")
        self.assertEqual(result["citations"][1]["sourceType"], "material")
        self.assertTrue(len(result["citations"]) >= 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
