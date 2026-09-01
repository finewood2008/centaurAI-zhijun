"""MindOS Agent 统一搜索接口测试（AG-02-02）。

覆盖：
- POST /v1/agent/search 正常返回（material/knowledge 两个来源、摘要、ID、evidenceRef）；
- 鉴权：需要 mindos.search + mindos.read，缺失任一 scope 返回 403/SCOPE_DENIED；
- 校验：query 过短/过长、limit 超上限、非法 type、sourceIds 超 20、非空 cursor 均 400；
- 脱敏：内部含真实路径的命中，投影后响应全文不出现 source_path / 本地路径；
- 空结果返回 200 + 空数组（不是模型式「资料不足」）；
- evidenceRef 为 opaque 句柄且 client 绑定，可经 evidence 模块校验；
- capabilities 已向具备 mindos.search 的客户端声明 search 工具。

隔离环境：临时 agent DB + 独立 FastAPI app；mock 统一检索服务，避免依赖真实向量库。
依赖项目 .venv，可独立于 server 运行：
    .venv\\Scripts\\python.exe -m unittest test_mindos_agent_search -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos.agent import admin as agent_admin
from mindos.agent import evidence as agent_evidence
from mindos.agent import router as agent_router
from mindos.agent import store as agent_store
from mindos.services import search_service as shared_search


def _make_app() -> FastAPI:
    app = FastAPI()

    def _allow():
        return None

    agent_router.install(app)
    app.include_router(agent_router.router)
    agent_admin.configure_admin_guards(_allow, _allow)
    app.include_router(agent_admin.admin_router)
    return app


def _material_hit(**overrides) -> dict:
    hit = {
        "source_type": "material",
        "source_id": "mindos_c92dad98be40",
        "title": "MindOSV1.0 开发排期管理表.docx",
        "file_type": "document",
        "snippet": "阶段\t日程\t核心目标\t里程碑\nP0 排期 18 人天",
        "score": 0.82,
        "chunk_id": "chunk_internal_abc123",
        "source_path": r"D:\\watch\\MindOSV1.0 开发排期管理表.docx",
        "metadata": {"modality": "text"},
        "locator": None,
        "evidence_eligible": True,
    }
    hit.update(overrides)
    return hit


def _knowledge_hit(**overrides) -> dict:
    hit = {
        "source_type": "knowledge",
        "source_id": "knowledge_dev_schedule",
        "title": "MindOS V1.0 开发排期摘要",
        "snippet": "MindOS 的开发排期按 P0/P1/P2/P3 阶段推进",
        "score": 0.9,
        "chunk_id": None,
        "source_path": None,
        "metadata": {},
        "locator": None,
        "evidence_eligible": True,
    }
    hit.update(overrides)
    return hit


class AgentSearchTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["MINDOS_AGENT_GATEWAY_ENABLED"] = "true"
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "agent.db"
        agent_store.reset_for_tests(self.db_path)
        agent_evidence.reset_for_tests()
        self.app = _make_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        agent_store.reset_for_tests()
        agent_evidence.reset_for_tests()
        self._tmp.cleanup()

    # ---- 辅助 ----------------------------------------------------

    def _create(self, name="搜索客户端", scopes=None) -> str:
        res = self.client.post(
            "/api/agent/clients",
            json={"name": name, "scopes": scopes or ["mindos.search", "mindos.read"]},
        )
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["token"]

    def _bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _search(self, token: str, payload: dict):
        return self.client.post(
            "/v1/agent/search", json=payload, headers=self._bearer(token)
        )

    # ---- 正常返回（含 material + knowledge 两来源） -----------------

    @patch("mindos.services.search_service.search_unified")
    def test_search_returns_both_sources_and_evidence_ref(self, mock_unified):
        mock_unified.return_value = {
            "items": [_material_hit(), _knowledge_hit()],
            "total": 2,
        }
        token = self._create()
        res = self._search(
            token,
            {"query": "MindOS 排期计划", "types": ["knowledge", "material"], "limit": 10},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(data["query"], "MindOS 排期计划")
        self.assertEqual(data["total"], 2)
        self.assertIsNone(data["nextCursor"])
        self.assertEqual(len(data["items"]), 2)

        material = data["items"][0]
        self.assertEqual(material["sourceType"], "material")
        self.assertEqual(material["id"], "mindos_c92dad98be40")
        self.assertEqual(material["fileType"], "document")
        self.assertIn("P0", material["snippet"])
        self.assertTrue(material["evidenceEligible"])
        self.assertTrue(material["evidenceRef"].startswith("ev_"))
        self.assertIsNone(material["locator"])  # AG-02-03 前不返回伪定位

        knowledge = data["items"][1]
        self.assertEqual(knowledge["sourceType"], "knowledge")
        self.assertEqual(knowledge["id"], "knowledge_dev_schedule")
        self.assertTrue(knowledge["evidenceRef"].startswith("ev_"))

    @patch("mindos.services.search_service.search_unified")
    def test_search_evidence_ref_is_opaque_and_client_bound(self, mock_unified):
        mock_unified.return_value = {
            "items": [_material_hit()],
            "total": 1,
        }
        token = self._create()
        res = self._search(token, {"query": "排期计划"})
        ref = res.json()["data"]["items"][0]["evidenceRef"]
        # 句柄是 opaque：不得包含内部 chunk ID 或物理路径。
        self.assertNotIn("chunk_internal_abc123", ref)
        self.assertNotIn("D:", ref)
        # 签发 client 的 verify 应成功。
        clients = self.client.get("/api/agent/clients").json()["clients"]
        client_id = clients[0]["client_id"]
        record = agent_evidence.verify_evidence_ref(client_id, ref)
        self.assertIsNotNone(record)
        self.assertEqual(record["source_id"], "mindos_c92dad98be40")
        # 跨 client 使用被拒绝。
        other_token = self._create("其他客户端")
        other_client_id = self.client.get("/api/agent/clients").json()["clients"][0]["client_id"]
        self.assertNotEqual(other_client_id, client_id)
        self.assertIsNone(agent_evidence.verify_evidence_ref(other_client_id, ref))

    @patch("mindos.services.search_service.search_unified")
    def test_search_empty_result_is_200_empty_array(self, mock_unified):
        mock_unified.return_value = {"items": [], "total": 0}
        token = self._create()
        res = self._search(token, {"query": "不存在的内容"})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(data["items"], [])
        self.assertEqual(data["total"], 0)

    # ---- 脱敏：响应全文不含内部路径/内部字段 ----------------------

    @patch("mindos.services.search_service.search_unified")
    def test_search_response_contains_no_internal_paths(self, mock_unified):
        mock_unified.return_value = {
            "items": [
                _material_hit(
                    source_path=r"D:\watch\.mindos_uploads\abcd\开发排期管理表.docx",
                    title="开发排期管理表.docx",
                ),
                _knowledge_hit(),
            ],
            "total": 2,
        }
        token = self._create()
        res = self._search(token, {"query": "排期计划"})
        raw = res.text
        for banned in ("D:\\", "/data/", "watch_folder", ".mindos_uploads",
                       "source_path", "chunk_internal_abc123", "chroma"):
            self.assertNotIn(banned, raw, f"响应泄露禁止字段: {banned}")

    # ---- 鉴权 ------------------------------------------------------

    def test_search_requires_both_scopes(self):
        token = self._create("仅搜索", scopes=["mindos.search"])
        res = self._search(token, {"query": "排期计划"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "SCOPE_DENIED")

        read_only = self._create("仅只读", scopes=["mindos.read"])
        res = self._search(read_only, {"query": "排期计划"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "SCOPE_DENIED")

    def test_search_without_token_is_401(self):
        res = self.client.post("/v1/agent/search", json={"query": "排期计划"})
        self.assertEqual(res.status_code, 401)

    # ---- 校验错误契约 ----------------------------------------------

    def test_search_rejects_short_query(self):
        token = self._create()
        res = self._search(token, {"query": "a"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

    @patch("mindos.services.search_service.search_unified")
    def test_search_rejects_overlong_query(self, mock_unified):
        token = self._create()
        res = self._search(token, {"query": "长" * 501})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        mock_unified.assert_not_called()

    @patch("mindos.services.search_service.search_unified")
    def test_search_rejects_oversized_limit(self, mock_unified):
        token = self._create()
        res = self._search(token, {"query": "排期计划", "limit": 999999})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        mock_unified.assert_not_called()

    @patch("mindos.services.search_service.search_unified")
    def test_search_rejects_illegal_type(self, mock_unified):
        token = self._create()
        res = self._search(token, {"query": "排期计划", "types": ["answer"]})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        mock_unified.assert_not_called()

    @patch("mindos.services.search_service.search_unified")
    def test_search_rejects_too_many_source_ids(self, mock_unified):
        token = self._create()
        res = self._search(token, {"query": "排期计划", "sourceIds": [f"id_{i}" for i in range(21)]})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        mock_unified.assert_not_called()

    @patch("mindos.services.search_service.search_unified")
    def test_search_rejects_foreign_cursor(self, mock_unified):
        token = self._create()
        res = self._search(token, {"query": "排期计划", "cursor": "user_controlled_offset"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        mock_unified.assert_not_called()

    # ---- 审计 ------------------------------------------------------

    @patch("mindos.services.search_service.search_unified")
    def test_search_audit_recorded(self, mock_unified):
        mock_unified.return_value = {"items": [_material_hit()], "total": 1}
        token = self._create()
        supplied = "atr_search-audit-789"
        self.client.post(
            "/v1/agent/search",
            json={"query": "排期计划"},
            headers={**self._bearer(token), "X-Request-Id": supplied},
        )
        audit = self.client.get("/api/agent/audit", params={"traceId": supplied}).json()
        self.assertEqual(audit["total"], 1)
        self.assertEqual(audit["items"][0]["action"], "search")
        self.assertEqual(audit["items"][0]["outcome"], "ok")
        self.assertEqual(audit["items"][0]["status_code"], 200)

    # ---- capabilities 已声明 search 工具 ----------------------------

    def test_capabilities_declares_search_for_search_scope(self):
        token = self._create("声明检查", scopes=["mindos.read", "mindos.search"])
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("search", res.json()["data"]["tools"])
        # 未授予 search scope 的客户端不应看到 search 工具。
        read_only = self._create("仅只读声明", scopes=["mindos.read"])
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(read_only))
        self.assertNotIn("search", res.json()["data"]["tools"])

    # ---- 搜索结果定位（P0-2）：命中直接携带真实 locator --------------

    @patch("mindos.services.search_service.search_unified")
    def test_search_returns_locator_from_hit(self, mock_unified):
        locator = {
            "kind": "table", "partId": "part_1", "tableIndex": 1,
            "rowStart": 1, "rowEnd": 3, "columnStart": 0, "columnEnd": 3,
        }
        mock_unified.return_value = {
            "items": [_material_hit(locator=locator), _knowledge_hit()],
            "total": 2,
        }
        token = self._create()
        res = self._search(token, {"query": "排期计划"})
        self.assertEqual(res.status_code, 200, res.text)
        material = res.json()["data"]["items"][0]
        self.assertEqual(material["locator"], locator)
        self.assertEqual(material["locator"]["tableIndex"], 1)

    @patch("mindos.services.search_service.search_unified")
    def test_search_include_locator_false_returns_null(self, mock_unified):
        locator = {"kind": "transcript", "start": 5.0, "end": 9.0}
        mock_unified.return_value = {
            "items": [_material_hit(locator=locator)],
            "total": 1,
        }
        token = self._create()
        res = self._search(token, {"query": "排期计划", "include": {"locator": False}})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIsNone(res.json()["data"]["items"][0]["locator"])

    # ---- 空正文卡片（P1-4）：标题命中可返回但不可作证据 --------------

    @patch("mindos.services.search_service.search_unified")
    def test_search_empty_body_card_not_evidence_eligible(self, mock_unified):
        mock_unified.return_value = {
            "items": [_knowledge_hit(snippet="", evidence_eligible=False)],
            "total": 1,
        }
        token = self._create()
        res = self._search(token, {"query": "MindOS 是什么"})
        self.assertEqual(res.status_code, 200, res.text)
        item = res.json()["data"]["items"][0]
        self.assertEqual(item["sourceType"], "knowledge")
        self.assertFalse(item["evidenceEligible"])
        self.assertIsNone(item["evidenceRef"])


class SharedSearchServiceTests(unittest.TestCase):
    """共享检索服务：材料可检索状态过滤与 source_ids 前置过滤（P0/P1）。

    直接注入 I/O 依赖调用 build_material_candidates，验证：
    - require_available=True 时 processing / failed 材料不进候选；
    - source_ids 作为检索范围在排序/截断前过滤（指定但排名低的材料仍能命中）。
    """

    def _records(self):
        return {
            "ok.docx": {"material_id": "mindos_ok", "file_name": "ok.docx", "file_type": "document"},
            "proc.docx": {"material_id": "mindos_proc", "file_name": "proc.docx", "file_type": "document"},
            "fail.docx": {"material_id": "mindos_fail", "file_name": "fail.docx", "file_type": "document"},
            "low.docx": {"material_id": "mindos_low", "file_name": "low.docx", "file_type": "document"},
        }

    def _status_of(self, material_id):
        return {
            "mindos_ok": {"status": "available"},
            "mindos_proc": {"status": "processing"},
            "mindos_fail": {"status": "failed"},
            "mindos_low": {"status": "available"},
        }.get(material_id, {"status": "available"})

    def _build(self, chunks, *, source_ids=None, limit=10):
        records = self._records()
        return shared_search.build_material_candidates(
            "内容", limit,
            terms=["内容"], archived=set(), recycled=set(),
            require_available=True,
            source_ids=source_ids,
            embed_query_callable=lambda q: [0.1],
            vector_search_callable=lambda emb, n_results: chunks,
            lexical_search_callable=lambda q, n_results: [],
            get_chunks_by_ids_callable=lambda ids: [],
            get_source_chunks_callable=lambda sp, limit: [],
            material_for_source_callable=lambda sp: records.get(sp),
            source_path_of_callable=lambda mid: None,
            threshold_for_file_type_callable=lambda ft, reranked: 0.0,
            status_of_callable=self._status_of,
        )

    def _chunks(self):
        return [
            {"id": "ck_ok", "source_path": "ok.docx", "text": "可用材料内容", "vector_score": 0.9},
            {"id": "ck_proc", "source_path": "proc.docx", "text": "处理中材料内容", "vector_score": 0.95},
            {"id": "ck_fail", "source_path": "fail.docx", "text": "失败材料内容", "vector_score": 0.98},
            {"id": "ck_low", "source_path": "low.docx", "text": "低排名材料内容", "vector_score": 0.3},
        ]

    def test_material_candidates_exclude_processing_and_failed(self):
        rows = self._build(self._chunks(), limit=10)
        ids = {r["material_id"] for r in rows}
        self.assertIn("mindos_ok", ids)
        self.assertNotIn("mindos_proc", ids)
        self.assertNotIn("mindos_fail", ids)

    def test_material_candidates_source_ids_prefilter(self):
        """指定低排名材料时，在排序/截断前过滤，保证其仍能命中。"""
        rows = self._build(self._chunks(), limit=1, source_ids={"mindos_low"})
        self.assertEqual([r["material_id"] for r in rows], ["mindos_low"])

    def test_material_candidates_source_ids_excludes_others(self):
        rows = self._build(self._chunks(), limit=10, source_ids={"mindos_ok"})
        self.assertEqual([r["material_id"] for r in rows], ["mindos_ok"])

    def test_material_candidates_fail_closed_when_status_unknown(self):
        """状态服务返回 None / 抛异常（无法确认状态）→ fail-closed，不进候选。"""
        records = self._records()

        def build(status_of_callable):
            return shared_search.build_material_candidates(
                "内容", 10,
                terms=["内容"], archived=set(), recycled=set(),
                require_available=True,
                embed_query_callable=lambda q: [0.1],
                vector_search_callable=lambda emb, n: self._chunks(),
                lexical_search_callable=lambda q, n: [],
                get_chunks_by_ids_callable=lambda ids: [],
                get_source_chunks_callable=lambda sp, limit: [],
                material_for_source_callable=lambda sp: records.get(sp),
                source_path_of_callable=lambda mid: None,
                threshold_for_file_type_callable=lambda ft, r: 0.0,
                status_of_callable=status_of_callable,
            )

        # 状态返回 None → 全部排除
        self.assertEqual(build(lambda mid: None), [])
        # 状态服务异常 → 全部排除
        def boom(_mid):
            raise RuntimeError("status service down")
        self.assertEqual(build(boom), [])

    def test_locator_survives_corrupt_part_data(self):
        """损坏/异常定位数据不抛 500：非法字段省略，其余真实定位保留。"""
        table = shared_search.locator_for_part({
            "id": "p1", "part_type": "table",
            "location": {"table": "not-a-number", "page": "abc"},
            "text": "a\tb\nc\td",
            "image_meta": {},
        })
        self.assertEqual(table["kind"], "table")
        self.assertNotIn("tableIndex", table)
        self.assertNotIn("page", table)
        self.assertEqual(table["rowEnd"], 2)
        image = shared_search.locator_for_part({
            "id": "p2", "part_type": "image",
            "location": {"page": None},
            "image_meta": {"width": "abc", "height": float("nan"), "ocr_status": "ok"},
        })
        self.assertEqual(image["kind"], "embedded_image")
        self.assertNotIn("width", image)
        self.assertNotIn("height", image)

    def test_search_knowledge_source_ids_uses_exact_by_ids(self):
        """sourceIds 非空时经 search_cards_by_ids 精确读取，不做 top-k 放大+过滤。"""
        with patch("mindos.knowledge.search_cards") as mock_sc, \
             patch("mindos.knowledge.search_cards_by_ids") as mock_by_ids:
            mock_by_ids.return_value = [{
                "knowledgeId": "knowledge_x", "title": "排期", "snippet": "P0 阶段", "score": 0.5,
            }]
            hits = shared_search.search_knowledge("排期", limit=5, source_ids={"knowledge_x"})
        mock_by_ids.assert_called_once()
        mock_sc.assert_not_called()
        self.assertEqual([h["source_id"] for h in hits], ["knowledge_x"])

    def test_search_cards_by_ids_semantic_hit(self):
        """词面不中但向量语义命中的指定卡片也能被召回（范围受限混合检索）。"""
        page = {
            "path": "/wiki/semantic.md",
            "title": "语义卡片",
            "content": "---\nmindos_card: true\n---\n# 语义卡片\n这里讨论的是某个特定主题的详细实施步骤与注意事项",
        }
        kid = "knowledge_semantic"
        with patch("mindos.knowledge._find", return_value=page), \
             patch("mindos.knowledge_index.search_cards", return_value=[
                 {"knowledgeId": kid, "title": "语义卡片",
                  "snippet": "特定主题详细实施步骤", "score": 0.8},
             ]):
            hits = shared_search.search_knowledge(
                "完全不同的语义描述", limit=5, source_ids={kid}
            )
        self.assertEqual([h["source_id"] for h in hits], [kid])
        self.assertTrue(hits[0]["evidence_eligible"])

    def test_search_cards_by_ids_vector_hit_still_checks_active(self):
        """向量命中的卡片仍须通过 active / 正文有效性复核。"""
        page = {
            "path": "/wiki/archived.md",
            "title": "已归档卡片",
            "content": "---\nmindos_card: true\nmindos_archived: true\n---\n# 已归档卡片\n正文内容",
        }
        kid = "knowledge_archived"
        with patch("mindos.knowledge._find", return_value=page), \
             patch("mindos.knowledge_index.search_cards", return_value=[
                 {"knowledgeId": kid, "title": "已归档卡片", "snippet": "正文内容", "score": 0.9},
             ]):
            hits = shared_search.search_knowledge("语义描述", limit=5, source_ids={kid})
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
