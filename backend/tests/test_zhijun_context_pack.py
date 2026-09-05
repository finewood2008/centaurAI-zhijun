"""知君 Context Pack：只含已确认 ∧ 允许导出 ∧ 非敏感；导出开关；网关 scope 与回执。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import ontology
from mindos.agent import admin as agent_admin
from mindos.agent import router as agent_router
from mindos.stores import ontology_store as ontology_store_module
from mindos.stores.ontology_store import OntologyError
from mindos.zhijun import context_pack


def _make_app() -> FastAPI:
    app = FastAPI()

    def _allow():
        return None

    agent_router.install(app)
    app.include_router(agent_router.router)
    agent_admin.configure_admin_guards(_allow, _allow)
    app.include_router(agent_admin.admin_router)
    app.include_router(ontology.router)
    return app


class ContextPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.onto = ontology_store_module.reset_for_tests(Path(self._tmp.name) / "ontology.db")
        self._env = patch.dict(os.environ, {"MINDOS_AGENT_GATEWAY_ENABLED": "true", "ZHIJUN_PROVIDER": "fake"})
        self._env.start()
        self.client = TestClient(_make_app())
        self.exportable = self.onto.create_claim(
            {"content": "我在做远川项目", "section": "matters", "layer": "self_declared", "export_allowed": True}, [], trust_state="confirmed", trust_origin="utterance"
        )
        self.private_off = self.onto.create_claim(
            {"content": "我喜欢早起", "section": "ways", "layer": "self_declared"}, [], trust_state="confirmed", trust_origin="utterance"
        )
        self.sensitive = self.onto.create_claim(
            {"content": "我正在处理一段家庭矛盾", "section": "people", "layer": "self_declared", "privacy_level": "sensitive", "export_allowed": True}, [], trust_state="confirmed", trust_origin="utterance"
        )
        self.working = self.onto.create_claim({"content": "我可能偏内向", "section": "who", "layer": "hypothesis", "export_allowed": True}, [])

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _token(self, scopes: list[str]) -> str:
        res = self.client.post("/api/agent/clients", json={"name": "wanx", "scopes": scopes})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        return body.get("token") or body.get("data", {}).get("token")

    def test_build_pack_filters_and_receipt(self) -> None:
        pack = context_pack.build_pack(purpose="帮用户写周报", store=self.onto, consumer="wanx")
        self.assertEqual([c["id"] for c in pack["claims"]], [self.exportable["id"]])
        self.assertEqual(pack["counts"]["excludedNotExportable"], 2)
        self.assertNotIn("evidence", pack["claims"][0])
        self.assertTrue(pack["receiptId"].startswith("cpk_"))
        summary = context_pack.receipt_summary(self.onto)
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["last"]["consumer"], "wanx")
        with self.assertRaises(OntologyError):
            context_pack.build_pack(purpose="x", store=self.onto)
        with self.assertRaises(OntologyError):
            context_pack.build_pack(purpose="帮忙", sections=["nope"], store=self.onto)
        self.assertEqual(context_pack.build_pack(purpose="帮忙", sections=["who"], store=self.onto)["claims"], [])

    def test_export_toggle_endpoint_and_status(self) -> None:
        res = self.client.post(f"/api/mindos/ontology/claims/{self.private_off['id']}/export", json={"allowed": True})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["exportAllowed"])
        status = self.client.get("/api/mindos/ontology/context-pack").json()
        self.assertEqual(status["exportable"], 2)
        self.assertEqual(self.client.post("/api/mindos/ontology/claims/clm_missing/export", json={"allowed": True}).status_code, 404)
        events = self.onto.review_events(self.private_off["id"])
        self.assertEqual(events[0]["note"], "导出开关")

    def test_gateway_requires_scope_and_purpose(self) -> None:
        token = self._token(["mindos.read"])
        denied = self.client.post("/v1/agent/context-pack", json={"purpose": "写周报"}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "SCOPE_DENIED")
        token = self._token(["zhijun.profile"])
        bad = self.client.post("/v1/agent/context-pack", json={"purpose": "x"}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/v1/agent/context-pack", json={"purpose": "帮用户写周报", "maxClaims": 10}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(ok.status_code, 200, ok.text)
        data = ok.json()["data"]
        self.assertEqual([c["id"] for c in data["claims"]], [self.exportable["id"]])
        self.assertEqual(data["consumer"], ok.json()["data"]["consumer"])
        self.assertNotIn("家庭矛盾", ok.text)
        self.assertNotIn("偏内向", ok.text)
        audit = self.client.get("/api/agent/audit", params={"limit": 5}).json()["items"]
        self.assertTrue(any(item.get("action") == "context_pack" for item in audit), audit)
        caps = self.client.get("/v1/agent/capabilities", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(caps.status_code, 403)  # capabilities 需要 mindos.read
        both = self._token(["mindos.read", "zhijun.profile"])
        caps = self.client.get("/v1/agent/capabilities", headers={"Authorization": f"Bearer {both}"}).json()["data"]
        self.assertIn("context_pack", caps["tools"])

    def test_gateway_disabled_denies(self) -> None:
        with patch.dict(os.environ, {"MINDOS_AGENT_GATEWAY_ENABLED": "false"}):
            res = self.client.post("/v1/agent/context-pack", json={"purpose": "写周报"}, headers={"Authorization": "Bearer agk_x"})
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()
