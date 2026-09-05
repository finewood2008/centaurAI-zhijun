"""MindOS Agent Gateway 认证/授权回归测试（AG-01）。

覆盖：
- 无 token / 伪造 token / 停用 / 过期 → 401；
- scope 不足 → 403/SCOPE_DENIED；
- 有效凭证可读取 capabilities；
- token 明文不进入日志、审计与响应；
- 未配置 MINDOS_AGENT_GATEWAY_ENABLED（环境变量缺失）→ 403/POLICY_DENIED；
- 未处理异常 → 500/INTERNAL_ERROR 信封 + 审计；
- 失败审计记录已知目标 scope。

隔离环境：临时 agent DB + 独立 FastAPI app（注入本机管理守卫桩）。
依赖项目 .venv，可独立于 server 运行：
    .venv\\Scripts\\python.exe -m unittest test_mindos_agent_auth -v
"""
import os
import sys
import tempfile
import time
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


class AgentAuthTestCase(unittest.TestCase):
    def setUp(self):
        # 网关默认拒绝；测试显式开启（除验证默认拒绝的用例会临时移除）。
        os.environ["MINDOS_AGENT_GATEWAY_ENABLED"] = "true"
        self._tmp = tempfile.TemporaryDirectory()
        agent_store.reset_for_tests(Path(self._tmp.name) / "agent.db")
        self.app = _make_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        agent_store.reset_for_tests()
        self._tmp.cleanup()

    # ---- 辅助 ----------------------------------------------------

    def _create(self, name="测试客户端", scopes=None, expiresAt=None) -> str:
        body = {"name": name, "scopes": scopes or ["mindos.read"]}
        if expiresAt is not None:
            body["expiresAt"] = expiresAt.isoformat()
        res = self.client.post("/api/agent/clients", json=body)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["token"]

    def _bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _capabilities(self, token: str):
        return self.client.get("/v1/agent/capabilities", headers=self._bearer(token))

    # ---- 验收 1：无/伪造/停用/过期 token 均 401 ----------------------

    def test_no_token_rejected(self):
        res = self.client.get("/v1/agent/capabilities")
        self.assertEqual(res.status_code, 401)
        body = res.json()
        self.assertEqual(body["error"]["code"], "AUTHENTICATION_REQUIRED")
        self.assertIn("traceId", body)

    def test_fake_token_rejected(self):
        res = self._capabilities("totally-bogus-token")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "TOKEN_INVALID")

    def test_disabled_client_rejected(self):
        token = self._create("将被停用")
        res = self.client.get("/api/agent/clients")
        client_id = res.json()["clients"][0]["client_id"]
        disable = self.client.post(f"/api/agent/clients/{client_id}/disable")
        self.assertEqual(disable.status_code, 200, disable.text)
        res = self._capabilities(token)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "TOKEN_INVALID")

    def test_expired_client_rejected(self):
        # 管理员接口已拒绝过去时间，此处直连 store 构造已过期客户端。
        _, token = agent_store.instance().create_client(
            "已过期", ["mindos.read"], expires_at=time.time() - 60
        )
        res = self._capabilities(token)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "TOKEN_INVALID")

    def test_malformed_auth_header_rejected(self):
        res = self.client.get(
            "/v1/agent/capabilities", headers={"Authorization": "Basic abc"}
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    # ---- 验收 2：scope 不足 → 403/SCOPE_DENIED -----------------------

    def test_read_only_token_cannot_answer(self):
        token = self._create("只读", scopes=["mindos.read"])
        res = self.client.post(
            "/v1/agent/answers",
            json={"question": "MindOS 排期计划是什么？"},
            headers=self._bearer(token),
        )
        self.assertEqual(res.status_code, 403)
        body = res.json()
        self.assertEqual(body["error"]["code"], "SCOPE_DENIED")
        self.assertFalse(body["error"]["retryable"])

    def test_answer_scope_alone_denied_without_read(self):
        # 只有 mindos.answer 而无 mindos.read：问答不得绕过读取范围。
        token = self._create("仅问答", scopes=["mindos.answer"])
        res = self.client.post(
            "/v1/agent/answers",
            json={"question": "MindOS 排期计划是什么？"},
            headers=self._bearer(token),
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "SCOPE_DENIED")

    def test_missing_read_scope_cannot_capabilities(self):
        token = self._create("无只读", scopes=["mindos.search"])
        res = self._capabilities(token)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "SCOPE_DENIED")

    @patch("mindos.qa.answer_question")
    def test_answer_scope_passes_scope_check(self, mock_qa):
        # 具备 mindos.answer + mindos.read 时通过 scope 校验，进入 AG-03 问答服务。
        mock_qa.return_value = {
            "status": "ANSWERED", "question": "MindOS 排期计划是什么？",
            "answer": "按 P0-P3 阶段推进。", "citations": [],
            "correctionNotices": [],
            "meta": {"retrievedCount": 0, "usedEvidenceCount": 0},
        }
        token = self._create("问答", scopes=["mindos.answer", "mindos.read"])
        res = self.client.post(
            "/v1/agent/answers",
            json={"question": "MindOS 排期计划是什么？"},
            headers=self._bearer(token),
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["status"], "ANSWERED")

    def test_failed_scope_is_audited_with_target_scope(self):
        token = self._create("scope审计", scopes=["mindos.read"])
        res = self.client.post(
            "/v1/agent/answers",
            json={"question": "q"},
            headers=self._bearer(token),
        )
        self.assertEqual(res.status_code, 403)
        audit = self.client.get("/api/agent/audit").json()["items"]
        entry = next(a for a in audit if a["status_code"] == 403)
        self.assertEqual(entry["scope"], "mindos.answer")

    # ---- 验收 3：有效凭证可读取 capabilities --------------------------

    def test_valid_token_capabilities_ok(self):
        token = self._create("有效")
        res = self._capabilities(token)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["apiVersion"], "v1")

    # ---- 验收 3：token 明文不进入响应 --------------------------------

    def test_token_not_leaked_in_error_response(self):
        token = self._create("泄露检查", scopes=["mindos.search"])
        res = self._capabilities(token)  # 403：无 mindos.read
        self.assertNotIn(token, res.text)

    def test_token_not_in_audit(self):
        token = self._create("审计检查")
        self._capabilities(token)
        audit = self.client.get("/api/agent/audit").json()["items"]
        self.assertTrue(audit, "应有审计记录")
        for item in audit:
            self.assertNotIn(token, str(item))

    # ---- 默认拒绝：环境变量缺失 → 403/POLICY_DENIED -------------------

    def test_gateway_defaults_to_deny_when_env_missing(self):
        token = self._create("默认拒绝")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINDOS_AGENT_GATEWAY_ENABLED", None)
            res = self._capabilities(token)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "POLICY_DENIED")

    # ---- 未知 Agent 路径：统一错误信封 --------------------------------

    def test_unknown_agent_path_returns_404_envelope(self):
        token = self._create("未知路径")
        res = self.client.get("/v1/agent/nope", headers=self._bearer(token))
        self.assertEqual(res.status_code, 404)
        body = res.json()
        self.assertEqual(body["error"]["code"], "RESOURCE_NOT_FOUND")
        self.assertFalse(body["error"]["retryable"])
        self.assertIn("traceId", body)
        self.assertEqual(res.headers.get("X-Request-Id"), body["traceId"])

    def test_unknown_agent_path_gateway_disabled_returns_policy_denied(self):
        # 网关关闭时，未知 Agent 路径同样按默认拒绝处理，不泄漏路由存在性。
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINDOS_AGENT_GATEWAY_ENABLED", None)
            res = self.client.get("/v1/agent/nope")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "POLICY_DENIED")
        self.assertIn("traceId", res.json())

    # ---- 未处理异常 → 500/INTERNAL_ERROR 信封 + 审计 ------------------

    def test_unhandled_error_returns_500_envelope(self):
        token = self._create("500")
        # ServerErrorMiddleware 的 Exception handler 在 TestClient 默认
        # raise_server_exceptions=True 下会先重抛；生产 uvicorn 不重抛，故此处显式关闭。
        client = TestClient(self.app, raise_server_exceptions=False)
        with patch(
            "mindos.agent.router.service.capabilities",
            side_effect=RuntimeError("boom"),
        ):
            res = client.get("/v1/agent/capabilities", headers=self._bearer(token))
        self.assertEqual(res.status_code, 500)
        body = res.json()
        self.assertEqual(body["error"]["code"], "INTERNAL_ERROR")
        self.assertIn("traceId", body)
        self.assertEqual(res.headers.get("X-Request-Id"), body["traceId"])
        self.assertNotIn("boom", res.text)  # 不泄露原始异常
        audit = self.client.get("/api/agent/audit").json()["items"]
        entry = next(a for a in audit if a["status_code"] == 500)
        self.assertEqual(entry["outcome"], "error")
        # require_scope 成功时也写入目标 scope，500 审计应能定位调用目标。
        self.assertEqual(entry["scope"], "mindos.read")


if __name__ == "__main__":
    unittest.main()
