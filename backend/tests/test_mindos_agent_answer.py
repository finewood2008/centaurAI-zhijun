"""MindOS Agent 带引用的问答接口测试（AG-03）。

覆盖：
- POST /v1/agent/answers 正常返回（status / answer / citations 关联精确 evidenceRef / locator）；
- options：sourceIds 范围前置传入、maxEvidence 上限、includeEvidence=false 隐藏正文；
- 强类型 options：拒绝未知字段（模型名/temperature/systemPrompt 等）；
- INSUFFICIENT_EVIDENCE 透传；
- 引用的 evidenceRef 可经 evidence:resolve 展开（真实 chunk 精确句柄）；
- 鉴权：需要 mindos.answer + mindos.read，缺任一 403；
- question / maxEvidence / sourceIds 数量校验 → 400；
- qa 层错误映射：429→RATE_LIMITED，503→SERVICE_UNAVAILABLE，504→GATEWAY_TIMEOUT；
- 审计记录；capabilities 声明 answer 工具；响应不含内部路径/模型名。

隔离环境：临时 agent DB + 独立 FastAPI app；mock qa.answer_question。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mindos.agent import admin as agent_admin
from mindos.agent import evidence as agent_evidence
from mindos.agent import router as agent_router
from mindos.agent import store as agent_store


def _make_app() -> FastAPI:
    app = FastAPI()

    def _allow():
        return None

    agent_router.install(app)
    app.include_router(agent_router.router)
    agent_admin.configure_admin_guards(_allow, _allow)
    app.include_router(agent_admin.admin_router)
    return app


def _qa_result(**overrides) -> dict:
    # 模拟 qa.answer_question(include_internal_meta=True) 的输出：citations 带
    # _chunkKey / _sourcePath / locator 内部字段（Agent 层投影时剔除路径）。
    result = {
        "status": "ANSWERED",
        "question": "MindOS 排期计划是什么",
        "answer": "MindOS 的开发排期按 P0/P1/P2/P3 四个阶段推进。",
        "citations": [
            {
                "citationId": "m1", "sourceType": "material",
                "materialId": "mindos_x", "knowledgeId": None,
                "title": "排期表.docx", "snippet": "P0 排期 18 人天",
                "_chunkKey": "schedule::ck1", "_sourcePath": "schedule.docx",
                "locator": {
                    "kind": "table", "partId": "part_1", "tableIndex": 1,
                    "rowStart": 1, "rowEnd": 3, "columnStart": 0, "columnEnd": 3,
                },
            },
            {
                "citationId": "k1", "sourceType": "knowledge",
                "materialId": None, "knowledgeId": "knowledge_sched",
                "title": "排期摘要", "snippet": "P0 阶段推进",
                "_chunkKey": None, "_sourcePath": None, "locator": None,
            },
        ],
        "correctionNotices": [],
        "meta": {"model": "internal-model", "retrievedCount": 2, "usedEvidenceCount": 2},
    }
    result.update(overrides)
    return result


class AgentAnswerTestCase(unittest.TestCase):
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

    def _create(self, name="问答客户端", scopes=None) -> str:
        res = self.client.post(
            "/api/agent/clients",
            json={"name": name, "scopes": scopes or ["mindos.answer", "mindos.read"]},
        )
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["token"]

    def _bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _answer(self, token: str, payload: dict):
        return self.client.post(
            "/v1/agent/answers", json=payload, headers=self._bearer(token)
        )

    # ---- 正常返回 --------------------------------------------------

    @patch("mindos.qa.answer_question")
    def test_answer_returns_answer_and_citations(self, mock_qa):
        mock_qa.return_value = _qa_result()
        token = self._create()
        res = self._answer(token, {"question": "MindOS 排期计划是什么"})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(data["status"], "ANSWERED")
        self.assertIn("P0", data["answer"])
        self.assertEqual(len(data["citations"]), 2)
        material = data["citations"][0]
        self.assertEqual(material["sourceType"], "material")
        self.assertEqual(material["id"], "mindos_x")
        self.assertTrue(material["evidenceRef"].startswith("ev_"))
        # 真实命中表格定位
        self.assertEqual(material["locator"]["kind"], "table")
        self.assertEqual(material["locator"]["tableIndex"], 1)
        knowledge = data["citations"][1]
        self.assertTrue(knowledge["evidenceRef"].startswith("ev_"))
        self.assertIsNone(knowledge["locator"])
        # meta 只含检索统计，不返回内部模型名
        self.assertEqual(data["meta"], {"retrievedCount": 2, "usedEvidenceCount": 2})
        raw = res.text
        for banned in ("source_path", "_sourcePath", "_chunkKey", "D:\\",
                       "internal-model", "chunk_id"):
            self.assertNotIn(banned, raw, f"响应泄露禁止字段: {banned}")

    @patch("mindos.qa.answer_question")
    def test_answer_insufficient_evidence(self, mock_qa):
        mock_qa.return_value = _qa_result(
            status="INSUFFICIENT_EVIDENCE", answer="资料不足，无法回答",
            citations=[], meta={"retrievedCount": 0, "usedEvidenceCount": 0},
        )
        token = self._create()
        res = self._answer(token, {"question": "无关问题"})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(data["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(data["citations"], [])

    @patch("mindos.qa.answer_question")
    def test_answer_material_citation_without_path_has_no_ref(self, mock_qa):
        mock_qa.return_value = _qa_result(citations=[
            dict(_qa_result()["citations"][0], _sourcePath=None),
        ])
        token = self._create()
        res = self._answer(token, {"question": "排期计划"})
        self.assertEqual(res.status_code, 200, res.text)
        material = res.json()["data"]["citations"][0]
        self.assertIsNone(material["evidenceRef"])
        # 无 source_path 时 locator 仍随 QA 保留（真实定位可返回）
        self.assertEqual(material["locator"]["kind"], "table")

    @patch("mindos.qa.answer_question")
    def test_answer_material_citation_without_chunk_has_no_ref(self, mock_qa):
        """缺精确 chunk_key 时不签发材料句柄（避免回退到首个分块引用错误内容）。"""
        mock_qa.return_value = _qa_result(citations=[
            dict(_qa_result()["citations"][0], _chunkKey=None),
        ])
        token = self._create()
        res = self._answer(token, {"question": "排期计划"})
        self.assertEqual(res.status_code, 200, res.text)
        material = res.json()["data"]["citations"][0]
        self.assertIsNone(material["evidenceRef"])
        self.assertEqual(material["locator"]["kind"], "table")

    def test_agent_answer_truncated_citation_keeps_evidence_ref(self):
        """上下文预算截断后，Agent 引用仍保留真实 chunk evidenceRef / locator。

        真实走 qa.build_evidence 的截断路径：6 条大片段超 MAX_CONTEXT_CHARS，
        最后一条被截断重建；断言其 chunk_key / source_path / locator 未丢失。
        """
        from mindos import qa
        from mindos.agent import answer_service
        from mindos.agent.auth import AgentPrincipal

        client, _ = agent_store.instance().create_client("截断测试", ["mindos.answer", "mindos.read"])
        principal = AgentPrincipal(
            client_id=client["client_id"], name=client["name"],
            scopes=frozenset(client["scopes"]), workspace_id="default",
        )
        big = "长" * 1000
        materials = [
            qa.Evidence(
                citation_id=f"m{i}", source_type="material",
                material_id=f"mindos_{i}", knowledge_id=None,
                title=f"材料{i}", snippet=big, score=0.9 - i * 0.01,
                priority_bucket="material",
                chunk_key=f"ck{i}", source_path=f"path{i}",
                locator={"kind": "table", "partId": f"p{i}", "tableIndex": i},
            )
            for i in range(1, 7)
        ]
        from mindos.agent.schemas import AnswerRequest
        with patch.object(qa, "_build_knowledge_evidence", return_value=[]), \
             patch.object(qa, "_build_material_evidence", return_value=materials), \
             patch.object(qa, "call_local_qa_model", return_value="答案"), \
             patch.object(qa.corrections, "match_corrections", return_value=[]):
            data = answer_service.answer(AnswerRequest(question="排期计划"), principal)
        # 6×1000 > 3600 → 最后一条被截断，但元数据保留 → evidenceRef 非 null、locator 非 null
        self.assertEqual(data["status"], "ANSWERED")
        truncated = data["citations"][-1]
        self.assertTrue(truncated["evidenceRef"].startswith("ev_"))
        self.assertEqual(truncated["locator"]["kind"], "table")

    # ---- options：sourceIds / maxEvidence / includeEvidence ----------

    @patch("mindos.qa.answer_question")
    def test_answer_options_source_ids_passed_to_qa(self, mock_qa):
        mock_qa.return_value = _qa_result()
        token = self._create()
        res = self._answer(token, {
            "question": "排期计划", "options": {"sourceIds": ["mindos_x"]},
        })
        self.assertEqual(res.status_code, 200, res.text)
        mock_qa.assert_called_once()
        _, kwargs = mock_qa.call_args
        self.assertEqual(kwargs["source_ids"], {"mindos_x"})

    @patch("mindos.qa.answer_question")
    def test_answer_options_max_evidence_passed_to_qa(self, mock_qa):
        mock_qa.return_value = _qa_result()
        token = self._create()
        res = self._answer(token, {
            "question": "排期计划", "options": {"maxEvidence": 3},
        })
        self.assertEqual(res.status_code, 200, res.text)
        _, kwargs = mock_qa.call_args
        self.assertEqual(kwargs["limit"], 3)

    @patch("mindos.qa.answer_question")
    def test_answer_options_include_evidence_false_hides_snippet_only(self, mock_qa):
        mock_qa.return_value = _qa_result()
        token = self._create()
        res = self._answer(token, {
            "question": "排期计划", "options": {"includeEvidence": False},
        })
        self.assertEqual(res.status_code, 200, res.text)
        material = res.json()["data"]["citations"][0]
        # 正文隐藏，但 citation 元数据 / evidenceRef / locator 保留
        self.assertEqual(material["snippet"], "")
        self.assertTrue(material["evidenceRef"].startswith("ev_"))
        self.assertEqual(material["locator"]["kind"], "table")

    @patch("mindos.qa.answer_question")
    def test_answer_rejects_unknown_options_fields(self, mock_qa):
        # 模型名 / temperature / systemPrompt / 工具指令等未知字段一律拒绝。
        token = self._create()
        for unknown in ({"model": "llama3"}, {"temperature": 0.7},
                        {"systemPrompt": "你是..."}, {"tools": ["x"]}):
            res = self._answer(token, {"question": "排期计划", "options": unknown})
            self.assertEqual(res.status_code, 400, f"应拒绝 {unknown}")
            self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        mock_qa.assert_not_called()

    @patch("mindos.qa.answer_question")
    def test_answer_rejects_max_evidence_overflow(self, mock_qa):
        token = self._create()
        res = self._answer(token, {
            "question": "排期计划", "options": {"maxEvidence": 7},
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        mock_qa.assert_not_called()

    @patch("mindos.qa.answer_question")
    def test_answer_rejects_too_many_source_ids(self, mock_qa):
        token = self._create()
        res = self._answer(token, {
            "question": "排期计划",
            "options": {"sourceIds": [f"id_{i}" for i in range(21)]},
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        mock_qa.assert_not_called()

    # ---- 引用的 evidenceRef 可展开（精确 chunk 句柄） ----------------

    @patch("mindos.qa.answer_question")
    def test_answer_citation_evidence_ref_resolvable(self, mock_qa):
        mock_qa.return_value = _qa_result(citations=[_qa_result()["citations"][0]])
        token = self._create()
        res = self._answer(token, {"question": "排期计划"})
        ref = res.json()["data"]["citations"][0]["evidenceRef"]
        self.assertTrue(ref.startswith("ev_"))
        # 精确句柄：resolve 优先按真实命中 chunk_key 读取。
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.services.ingestion.material_for_source",
                   return_value={"material_id": "mindos_x"}), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids") as get_chunks:
            gov.return_value.archived_material_ids.return_value = set()
            get_chunks.return_value = [{
                "id": "schedule::ck1", "source_path": "schedule.docx",
                "text": "P0 排期 18 人天", "metadata": {},
            }]
            ev = self.client.post(
                "/v1/agent/evidence:resolve",
                json={"evidenceRefs": [ref]},
                headers=self._bearer(token),
            )
        self.assertEqual(ev.status_code, 200, ev.text)
        item = ev.json()["data"]["items"][0]
        self.assertEqual(item["sourceId"], "mindos_x")
        self.assertIn("P0", item["text"])

    # ---- 鉴权 ------------------------------------------------------

    def test_answer_requires_both_scopes(self):
        read_only = self._create("仅只读", scopes=["mindos.read"])
        res = self._answer(read_only, {"question": "排期计划"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "SCOPE_DENIED")
        answer_only = self._create("仅问答", scopes=["mindos.answer"])
        res = self._answer(answer_only, {"question": "排期计划"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "SCOPE_DENIED")

    def test_answer_without_token_is_401(self):
        res = self.client.post("/v1/agent/answers", json={"question": "排期计划"})
        self.assertEqual(res.status_code, 401)

    # ---- 校验与错误映射 --------------------------------------------

    def test_answer_rejects_short_question(self):
        token = self._create()
        res = self._answer(token, {"question": "a"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

    @patch("mindos.qa.answer_question")
    def test_answer_rejects_overlong_question(self, mock_qa):
        token = self._create()
        res = self._answer(token, {"question": "长" * 501})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        mock_qa.assert_not_called()

    @patch("mindos.qa.answer_question", side_effect=HTTPException(429, "busy"))
    def test_answer_qa_429_maps_to_rate_limited(self, mock_qa):
        token = self._create()
        res = self._answer(token, {"question": "排期计划"})
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.json()["error"]["code"], "RATE_LIMITED")
        self.assertTrue(res.json()["error"]["retryable"])

    @patch("mindos.qa.answer_question", side_effect=HTTPException(503, "model down"))
    def test_answer_qa_503_maps_to_service_unavailable(self, mock_qa):
        token = self._create()
        res = self._answer(token, {"question": "排期计划"})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["error"]["code"], "SERVICE_UNAVAILABLE")
        self.assertTrue(res.json()["error"]["retryable"])
        # 不泄露模型细节
        self.assertNotIn("model", res.text)
        self.assertNotIn("down", res.text)

    @patch("mindos.qa.answer_question", side_effect=HTTPException(504, "timeout"))
    def test_answer_qa_504_maps_to_gateway_timeout(self, mock_qa):
        token = self._create()
        res = self._answer(token, {"question": "排期计划"})
        self.assertEqual(res.status_code, 504)
        self.assertEqual(res.json()["error"]["code"], "GATEWAY_TIMEOUT")
        self.assertTrue(res.json()["error"]["retryable"])
        self.assertNotIn("timeout", res.text)

    # ---- 审计与能力声明 --------------------------------------------

    @patch("mindos.qa.answer_question")
    def test_answer_audit_recorded(self, mock_qa):
        mock_qa.return_value = _qa_result()
        token = self._create()
        supplied = "atr_answer-audit-555"
        self.client.post(
            "/v1/agent/answers",
            json={"question": "排期计划"},
            headers={**self._bearer(token), "X-Request-Id": supplied},
        )
        audit = self.client.get("/api/agent/audit", params={"traceId": supplied}).json()
        self.assertEqual(audit["total"], 1)
        self.assertEqual(audit["items"][0]["action"], "answer")
        self.assertEqual(audit["items"][0]["outcome"], "ok")
        self.assertEqual(audit["items"][0]["scope"], "mindos.answer,mindos.read")

    def test_capabilities_declares_answer_for_answer_scope(self):
        token = self._create("声明", scopes=["mindos.read", "mindos.answer"])
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("answer", res.json()["data"]["tools"])
        read_only = self._create("仅只读声明", scopes=["mindos.read"])
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(read_only))
        self.assertNotIn("answer", res.json()["data"]["tools"])


if __name__ == "__main__":
    unittest.main()
