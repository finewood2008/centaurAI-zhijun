"""MindOS Agent 证据展开接口测试（AG-02-03）。

覆盖：
- POST /v1/agent/evidence:resolve 展开材料/知识卡片证据，顺序与请求一致；
- 定位：音频/视频转写返回有限递增时间戳；表格返回真实 part/行定位；无效时间返回 null；
- 预算：>10 refs / maxCharsPerItem>3000 → 400；单次总量超限 → 413；
- 安全：伪造/过期/跨 client/归档/回收/失败 ref → 统一 404；处理中 → 409；
- 重复 ref 只返回一次并标记 deduplicated；
- 脱敏：响应全文不含 source_path / chunk_id / 本地路径；
- 鉴权：需要 mindos.read。

隔离环境：临时 agent DB + 独立 FastAPI app；mock 生命周期/向量块/派生 part，
不依赖真实向量库与开发机材料。
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


def _make_app() -> FastAPI:
    app = FastAPI()

    def _allow():
        return None

    agent_router.install(app)
    app.include_router(agent_router.router)
    agent_admin.configure_admin_guards(_allow, _allow)
    app.include_router(agent_admin.admin_router)
    return app


class AgentEvidenceTestCase(unittest.TestCase):
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

    def _create(self, name="证据客户端", scopes=None) -> str:
        res = self.client.post(
            "/api/agent/clients",
            json={"name": name, "scopes": scopes or ["mindos.read"]},
        )
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["token"]

    def _bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _client_id(self) -> str:
        clients = self.client.get("/api/agent/clients").json()["clients"]
        return clients[0]["client_id"]

    def _resolve(self, token: str, payload: dict):
        return self.client.post(
            "/v1/agent/evidence:resolve", json=payload, headers=self._bearer(token)
        )

    def test_resolve_material_text_with_transcript_locator(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.services.ingestion.material_for_source",
                   return_value={"material_id": "mindos_audio_1"}), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids") as get_chunks:
            gov.return_value.archived_material_ids.return_value = set()
            get_chunks.return_value = [{
                "id": "ck_transcript_1",
                "source_path": r"D:\data\录音.mp3",
                "text": "第一阶段从三月份开始推进",
                "metadata": {"modality": "transcript", "start_time": 12.4, "end_time": 19.8},
            }]
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_audio_1", chunk_key="ck_transcript_1",
                source_path=r"D:\data\录音.mp3", title="录音.mp3",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()["data"]
            self.assertEqual(len(data["items"]), 1)
            item = data["items"][0]
            self.assertEqual(item["sourceType"], "material")
            self.assertEqual(item["sourceId"], "mindos_audio_1")
            self.assertEqual(item["text"], "第一阶段从三月份开始推进")
            self.assertFalse(item["truncated"])
            self.assertEqual(
                item["locator"],
                {"kind": "transcript", "start": 12.4, "end": 19.8},
            )
            self.assertEqual(data["totalChars"], len(item["text"]))

    def test_resolve_invalid_transcript_time_returns_null_locator(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.services.ingestion.material_for_source",
                   return_value={"material_id": "mindos_audio_bad"}), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids") as get_chunks:
            gov.return_value.archived_material_ids.return_value = set()
            # end <= start 或 NaN：不得返回可点击定位
            get_chunks.return_value = [{
                "id": "ck_bad",
                "source_path": "audio_bad.mp3",
                "text": "bad timing",
                "metadata": {"modality": "transcript", "start_time": 30.0, "end_time": 20.0},
            }]
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_audio_bad", chunk_key="ck_bad",
                source_path="audio_bad.mp3",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 200, res.text)
            self.assertIsNone(res.json()["data"]["items"][0]["locator"])

    def test_resolve_material_table_locator(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.services.ingestion.material_for_source",
                   return_value={"material_id": "mindos_x"}), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids") as get_chunks, \
             patch("mindos.stores.derived_store.DerivedStore.instance") as ds_inst:
            gov.return_value.archived_material_ids.return_value = set()
            get_chunks.return_value = [{
                "id": "ck_table_1",
                "source_path": "schedule.docx",
                "text": "阶段\t日程\nP0\tQ1",
                "metadata": {"part_id": "mindos_x::table::3"},
            }]
            ds_inst.return_value.get_part.return_value = {
                "id": "mindos_x::table::3",
                "part_type": "table",
                "text": "阶段\t日程\nP0\tQ1",
                "location": {"table": 2},
                "image_meta": {},
            }
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_x", chunk_key="ck_table_1",
                source_path="schedule.docx",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 200, res.text)
            loc = res.json()["data"]["items"][0]["locator"]
            self.assertEqual(loc["kind"], "table")
            self.assertEqual(loc["partId"], "mindos_x::table::3")
            self.assertEqual(loc["tableIndex"], 2)
            self.assertEqual(loc["rowStart"], 1)
            self.assertEqual(loc["rowEnd"], 2)
            self.assertEqual(loc["columnEnd"], 2)

    def test_resolve_knowledge_evidence(self):
        with patch("mindos.knowledge.evidence_body", return_value="MindOS 的开发排期按 P0/P1/P2/P3 阶段推进。"):
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="knowledge",
                source_id="knowledge_schedule", title="排期摘要",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 200, res.text)
            item = res.json()["data"]["items"][0]
            self.assertEqual(item["sourceType"], "knowledge")
            self.assertEqual(item["sourceId"], "knowledge_schedule")
            self.assertIn("P0", item["text"])
            self.assertIsNone(item["locator"])
            self.assertFalse(item["truncated"])

    def test_resolve_respects_max_chars_per_item(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.services.ingestion.material_for_source",
                   return_value={"material_id": "mindos_long"}), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids") as get_chunks:
            gov.return_value.archived_material_ids.return_value = set()
            get_chunks.return_value = [{
                "id": "ck_long", "source_path": "long.txt",
                "text": "长" * 200, "metadata": {},
            }]
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_long", chunk_key="ck_long",
                source_path="long.txt",
            )
            res = self._resolve(token, {"evidenceRefs": [ref], "maxCharsPerItem": 100})
            self.assertEqual(res.status_code, 200, res.text)
            item = res.json()["data"]["items"][0]
            self.assertEqual(len(item["text"]), 100)
            self.assertTrue(item["truncated"])

    def test_resolve_duplicate_refs_returned_once_in_order(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.services.ingestion.material_for_source") as mfs, \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids") as get_chunks:
            gov.return_value.archived_material_ids.return_value = set()
            mfs.side_effect = lambda sp: {"a.txt": {"material_id": "mindos_a"},
                                          "b.txt": {"material_id": "mindos_b"}}.get(sp)
            get_chunks.side_effect = lambda ids: [{
                "id": ids[0], "source_path": ("a.txt" if ids[0] == "ck_a" else "b.txt"),
                "text": f"内容-{ids[0]}", "metadata": {},
            }]
            token = self._create()
            client_id = self._client_id()
            ref_a = agent_evidence.sign_evidence_ref(
                client_id=client_id, source_type="material",
                source_id="mindos_a", chunk_key="ck_a", source_path="a.txt",
            )
            ref_b = agent_evidence.sign_evidence_ref(
                client_id=client_id, source_type="material",
                source_id="mindos_b", chunk_key="ck_b", source_path="b.txt",
            )
            res = self._resolve(token, {"evidenceRefs": [ref_b, ref_a, ref_b]})
            self.assertEqual(res.status_code, 200, res.text)
            items = res.json()["data"]["items"]
            # 顺序与请求一致，重复 ref 只返回一次
            self.assertEqual([i["evidenceRef"] for i in items], [ref_b, ref_a])
            self.assertTrue(items[0]["deduplicated"])
            self.assertNotIn("deduplicated", items[1])
            expected_chars = len("内容-ck_a") + len("内容-ck_b")
            self.assertEqual(res.json()["data"]["totalChars"], expected_chars)

    # ---- 安全：无效/伪造/过期/跨 client/生命周期 ---------------------

    def test_resolve_forged_ref_is_404(self):
        token = self._create()
        res = self._resolve(token, {"evidenceRefs": ["ev_forged_123"]})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_resolve_expired_ref_is_404(self):
        token = self._create()
        client_id = self._client_id()
        # ttl 为负 → 签发即过期
        ref = agent_evidence.sign_evidence_ref(
            client_id=client_id, source_type="material",
            source_id="mindos_x", chunk_key="ck_x", ttl_seconds=-100,
        )
        res = self._resolve(token, {"evidenceRefs": [ref]})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_resolve_cross_client_ref_is_404(self):
        token_a = self._create("客户端A")
        client_a = self._client_id()
        token_b = self._create("客户端B")
        ref = agent_evidence.sign_evidence_ref(
            client_id=client_a, source_type="material",
            source_id="mindos_x", chunk_key="ck_x",
        )
        res = self._resolve(token_b, {"evidenceRefs": [ref]})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_resolve_archived_material_is_404(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov:
            gov.return_value.archived_material_ids.return_value = {"mindos_x"}
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_x", chunk_key="ck_x",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_resolve_recycled_material_is_404(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value={"mindos_x"}), \
             patch("mindos.stores.governance_store.instance") as gov:
            gov.return_value.archived_material_ids.return_value = set()
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_x", chunk_key="ck_x",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_resolve_processing_material_is_409(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "processing"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov:
            gov.return_value.archived_material_ids.return_value = set()
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_x", chunk_key="ck_x",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 409)
            self.assertEqual(res.json()["error"]["code"], "EVIDENCE_NOT_READY")

    def test_resolve_failed_material_is_404(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "failed"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov:
            gov.return_value.archived_material_ids.return_value = set()
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_x", chunk_key="ck_x",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_resolve_missing_chunk_is_404(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids", return_value=[]):
            gov.return_value.archived_material_ids.return_value = set()
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_x", chunk_key="ck_gone",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    # ---- 预算与校验 ------------------------------------------------

    def test_resolve_rejects_too_many_refs(self):
        token = self._create()
        res = self._resolve(token, {"evidenceRefs": [f"ev_fake{i}" for i in range(11)]})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

    def test_resolve_rejects_oversized_max_chars(self):
        token = self._create()
        res = self._resolve(token, {"evidenceRefs": ["ev_x"], "maxCharsPerItem": 3001})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

    def test_resolve_rejects_total_over_budget(self):
        token = self._create()
        # 5 个 ref × 3000 = 15000 > 12000 → 413（校验在句柄校验之前）
        res = self._resolve(
            token, {"evidenceRefs": [f"ev_fake{i}" for i in range(5)], "maxCharsPerItem": 3000}
        )
        self.assertEqual(res.status_code, 413)
        self.assertEqual(res.json()["error"]["code"], "CONTENT_LIMIT_EXCEEDED")

    # ---- 脱敏与鉴权 ------------------------------------------------

    def test_resolve_response_has_no_internal_paths(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.services.ingestion.material_for_source",
                   return_value={"material_id": "mindos_x"}), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids") as get_chunks:
            gov.return_value.archived_material_ids.return_value = set()
            # 内部 source_path / chunk_id 只存在于服务端记录中，正文是正常文档内容。
            get_chunks.return_value = [{
                "id": "chunk_internal_xyz",
                "source_path": r"D:\watch\.mindos_uploads\file.docx",
                "text": "阶段表 P0 开发排期 18 人天",
                "metadata": {},
            }]
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_x", chunk_key="chunk_internal_xyz",
                source_path=r"D:\watch\.mindos_uploads\file.docx",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 200, res.text)
            raw = res.text
            for banned in ("source_path", "D:\\", "watch_folder", ".mindos_uploads",
                           "chunk_internal_xyz", "chroma"):
                self.assertNotIn(banned, raw, f"响应泄露禁止字段: {banned}")

    def test_resolve_requires_read_scope(self):
        token = self._create("仅搜索", scopes=["mindos.search"])
        res = self._resolve(token, {"evidenceRefs": ["ev_x"]})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "SCOPE_DENIED")

    def test_resolve_without_token_is_401(self):
        res = self.client.post("/v1/agent/evidence:resolve", json={"evidenceRefs": ["ev_x"]})
        self.assertEqual(res.status_code, 401)

    def test_resolve_audit_recorded(self):
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.services.ingestion.material_for_source",
                   return_value={"material_id": "mindos_audit"}), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids") as get_chunks:
            gov.return_value.archived_material_ids.return_value = set()
            get_chunks.return_value = [{
                "id": "ck_audit", "source_path": "audit.txt",
                "text": "审计内容", "metadata": {},
            }]
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_audit", chunk_key="ck_audit",
                source_path="audit.txt",
            )
            supplied = "atr_evidence-audit-111"
            self.client.post(
                "/v1/agent/evidence:resolve",
                json={"evidenceRefs": [ref]},
                headers={**self._bearer(token), "X-Request-Id": supplied},
            )
            audit = self.client.get("/api/agent/audit", params={"traceId": supplied}).json()
            self.assertEqual(audit["total"], 1)
            self.assertEqual(audit["items"][0]["action"], "evidence:resolve")
            self.assertEqual(audit["items"][0]["outcome"], "ok")
            # 审计记录绝不出现明文 ref 或内部路径
            self.assertNotIn(ref, audit["items"][0]["request_digest"])
            self.assertNotIn("source_path", audit["items"][0]["request_digest"])

    # ---- chunk 归属校验（P1）：stale chunk 不得返回与 evidenceRef 不一致的正文 ----

    def test_resolve_stale_chunk_source_path_mismatch_is_404(self):
        """chunk 的 source_path 与签发记录不一致（stale/索引变更）→ 404。"""
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids") as get_chunks:
            gov.return_value.archived_material_ids.return_value = set()
            get_chunks.return_value = [{
                "id": "ck_x",
                "source_path": "other.docx",  # 与签发时 source_path 不同
                "text": "旧索引内容",
                "metadata": {},
            }]
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_x", chunk_key="ck_x",
                source_path="schedule.docx",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_resolve_chunk_mapped_to_different_material_is_404(self):
        """chunk 的 source_path 虽一致，但映射到不同材料（ID 冲突/错误关联）→ 404。"""
        with patch("mindos.services.ingestion.status_of", return_value={"status": "available"}), \
             patch("mindos.services.ingestion.recycled_material_ids", return_value=set()), \
             patch("mindos.services.ingestion.material_for_source",
                   return_value={"material_id": "mindos_other"}), \
             patch("mindos.stores.governance_store.instance") as gov, \
             patch("vector_store.get_chunks_by_ids") as get_chunks:
            gov.return_value.archived_material_ids.return_value = set()
            get_chunks.return_value = [{
                "id": "ck_x", "source_path": "schedule.docx",
                "text": "内容", "metadata": {},
            }]
            token = self._create()
            ref = agent_evidence.sign_evidence_ref(
                client_id=self._client_id(), source_type="material",
                source_id="mindos_x", chunk_key="ck_x",
                source_path="schedule.docx",
            )
            res = self._resolve(token, {"evidenceRefs": [ref]})
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_capabilities_declares_get_evidence(self):
        token = self._create("声明", scopes=["mindos.read"])
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("getEvidence", res.json()["data"]["tools"])


if __name__ == "__main__":
    unittest.main()
