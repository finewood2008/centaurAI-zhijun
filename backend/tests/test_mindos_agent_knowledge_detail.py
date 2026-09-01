"""MindOS Agent 知识卡片详情接口测试（AG-02-04）。

覆盖：
- GET /v1/agent/knowledge/{id} 正常返回（正文为清理后 body、标签、来源、证据可用标记）；
- 归档/合并/回收/不存在 → 统一 404（knowledge_view 返回 None）；
- 正文不含 frontmatter/重复标题（由 knowledge_view 保证，此处验证投影直取 body）；
- 来源关系由卡片 frontmatter 派生，不返回 Wiki path；
- 脱敏：不返回 source_path / Wiki path；
- 鉴权：需要 mindos.read；审计记录 knowledge 类型。

隔离环境：临时 agent DB + 独立 FastAPI app；mock knowledge_view。
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


def _view_payload(**overrides) -> dict:
    payload = {
        "knowledgeId": "knowledge_schedule",
        "title": "MindOS V1.0 开发排期摘要",
        "body": "MindOS 的开发排期按 P0/P1/P2/P3 阶段推进",
        "tags": ["MindOS", "开发排期"],
        "sources": [
            {
                "sourceType": "material", "id": "mindos_x",
                "title": "MindOSV1.0 开发排期管理表.docx",
                "archived": False,
            },
            {
                "sourceType": "knowledge", "id": "knowledge_other",
                "title": "另一张卡片", "archived": True,
            },
        ],
        "evidenceEligible": True,
        "revision": "rev_0123456789ab",
        "indexStatus": "ready",
        "updatedAt": "2026-08-18T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


class AgentKnowledgeDetailTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["MINDOS_AGENT_GATEWAY_ENABLED"] = "true"
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "agent.db"
        agent_store.reset_for_tests(self.db_path)
        self.app = _make_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        agent_store.reset_for_tests()
        self._tmp.cleanup()

    def _create(self, name="卡片详情客户端", scopes=None) -> str:
        res = self.client.post(
            "/api/agent/clients",
            json={"name": name, "scopes": scopes or ["mindos.read"]},
        )
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["token"]

    def _bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _get_knowledge(self, token: str, knowledge_id: str = "knowledge_schedule"):
        return self.client.get(
            f"/v1/agent/knowledge/{knowledge_id}", headers=self._bearer(token)
        )

    def test_knowledge_detail_shape(self):
        with patch("mindos.knowledge.knowledge_view", return_value=_view_payload()):
            token = self._create()
            res = self._get_knowledge(token)
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()["data"]
            self.assertEqual(data["knowledgeId"], "knowledge_schedule")
            self.assertEqual(data["title"], "MindOS V1.0 开发排期摘要")
            # 正文直接来自清理后的 body，不含 frontmatter
            self.assertEqual(data["content"], "MindOS 的开发排期按 P0/P1/P2/P3 阶段推进")
            self.assertEqual(data["tags"], ["MindOS", "开发排期"])
            self.assertTrue(data["evidenceEligible"])
            self.assertEqual(data["revision"], "rev_0123456789ab")
            self.assertEqual(data["indexStatus"], "ready")
            self.assertFalse(data["archived"])
            self.assertFalse(data["recycled"])
            self.assertTrue(data["readOnly"])
            self.assertEqual(data["updatedAt"], "2026-08-18T00:00:00+00:00")
            # 来源关系由卡片 frontmatter 派生
            refs = data["sourceRefs"]
            self.assertEqual(refs[0]["sourceType"], "material")
            self.assertEqual(refs[0]["id"], "mindos_x")
            self.assertEqual(refs[0]["title"], "MindOSV1.0 开发排期管理表.docx")
            self.assertFalse(refs[0]["archived"])
            self.assertTrue(refs[1]["archived"])

    def test_knowledge_detail_no_internal_paths(self):
        with patch("mindos.knowledge.knowledge_view", return_value=_view_payload()):
            token = self._create()
            res = self._get_knowledge(token)
            self.assertEqual(res.status_code, 200, res.text)
            raw = res.text
            for banned in ("source_path", "D:\\", "/data/", "watch_folder",
                           ".wikis/", "frontmatter", "chroma"):
                self.assertNotIn(banned, raw, f"响应泄露禁止字段: {banned}")

    def test_knowledge_detail_not_active_is_404(self):
        with patch("mindos.knowledge.knowledge_view", return_value=None):
            token = self._create()
            res = self._get_knowledge(token, "knowledge_archived")
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    def test_knowledge_detail_not_substantive_still_readable(self):
        # 无实质正文的卡片详情可读，但 evidenceEligible=false。
        with patch("mindos.knowledge.knowledge_view",
                   return_value=_view_payload(body="# 标题", evidenceEligible=False)):
            token = self._create()
            res = self._get_knowledge(token)
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()["data"]
            self.assertFalse(data["evidenceEligible"])
            self.assertEqual(data["content"], "# 标题")

    def test_knowledge_detail_requires_read_scope(self):
        token = self._create("无只读", scopes=["mindos.search"])
        res = self._get_knowledge(token)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "SCOPE_DENIED")

    def test_knowledge_detail_audit_recorded(self):
        with patch("mindos.knowledge.knowledge_view", return_value=_view_payload()):
            token = self._create()
            supplied = "atr_knowledge-detail-333"
            self.client.get(
                "/v1/agent/knowledge/knowledge_schedule",
                headers={**self._bearer(token), "X-Request-Id": supplied},
            )
            audit = self.client.get("/api/agent/audit", params={"traceId": supplied}).json()
            self.assertEqual(audit["total"], 1)
            self.assertEqual(audit["items"][0]["action"], "knowledge_detail")
            self.assertEqual(audit["items"][0]["resource_type"], "knowledge")
            self.assertEqual(audit["items"][0]["resource_id"], "knowledge_schedule")

    def test_capabilities_declares_get_knowledge(self):
        token = self._create("声明", scopes=["mindos.read"])
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("getKnowledge", res.json()["data"]["tools"])


if __name__ == "__main__":
    unittest.main()
