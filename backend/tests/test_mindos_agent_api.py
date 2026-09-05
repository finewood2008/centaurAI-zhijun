"""MindOS Agent Gateway API 契约回归测试（AG-01）。

覆盖：
- capabilities 响应结构（apiVersion/workspaceId/tools/writeModes/limits/supportedFileTypes）；
- traceId：X-Request-Id 传递、生成与响应头一致性（验收 5）；
- 本机管理员接口：创建/列表/轮换/停用与非法 scope；
- 审计：可按 traceId/clientId 查询、已脱敏；
- 校验错误 → 400/VALIDATION_ERROR；
- 限流器单元行为。

隔离环境：临时 agent DB + 独立 FastAPI app。
依赖项目 .venv，可独立于 server 运行：
    .venv\\Scripts\\python.exe -m unittest test_mindos_agent_api -v
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos.agent import admin as agent_admin
from mindos.agent import config as agent_config
from mindos.agent import router as agent_router
from mindos.agent import store as agent_store
from mindos.agent.rate_limit import RateLimiter


def _make_app() -> FastAPI:
    app = FastAPI()

    def _allow():
        return None

    agent_router.install(app)
    app.include_router(agent_router.router)
    agent_admin.configure_admin_guards(_allow, _allow)
    app.include_router(agent_admin.admin_router)
    return app


class AgentApiTestCase(unittest.TestCase):
    def setUp(self):
        # 网关默认拒绝；测试显式开启。
        os.environ["MINDOS_AGENT_GATEWAY_ENABLED"] = "true"
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "agent.db"
        agent_store.reset_for_tests(self.db_path)
        self.app = _make_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        agent_store.reset_for_tests()
        self._tmp.cleanup()

    # ---- 辅助 ----------------------------------------------------

    def _create(self, name="测试客户端", scopes=None) -> str:
        res = self.client.post(
            "/api/agent/clients",
            json={"name": name, "scopes": scopes or ["mindos.read"]},
        )
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["token"]

    def _bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    # ---- capabilities 结构（AG-01 只读能力声明） -------------------

    def test_capabilities_shape(self):
        token = self._create("结构检查", scopes=["mindos.read", "mindos.search"])
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(data["apiVersion"], "v1")
        self.assertEqual(data["workspaceId"], "default")
        self.assertIsInstance(data["tools"], list)
        # 阶段未实现的工具不对外声明；已声明工具必须落在 scopes 范围内。
        for tool in data["tools"]:
            self.assertIn(tool, {"search", "getEvidence", "getMaterial", "getKnowledge", "answer"})
        self.assertEqual(
            set(data["writeModes"]),
            {"import", "knowledgeDraft", "knowledgeCommit"},
        )
        self.assertFalse(any(data["writeModes"].values()), "AG-01 阶段写入应全部关闭")
        self.assertIn("searchPageSizeMax", data["limits"])
        self.assertIn("evidenceCharsMax", data["limits"])
        self.assertIn("answerQuestionCharsMax", data["limits"])
        self.assertTrue(data["supportedFileTypes"])
        # 不泄露服务器路径/物理目录
        raw = res.text
        for banned in ("D:\\", "/data/", "watch_folder", "source_path"):
            self.assertNotIn(banned, raw)

    def test_capabilities_requires_read_scope(self):
        token = self._create("无只读", scopes=["mindos.search"])
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        self.assertEqual(res.status_code, 403)

    # ---- 验收 5：traceId 一致性 -------------------------------------

    def test_trace_id_propagated(self):
        token = self._create("traceId")
        supplied = "atr_custom-request-id-123"
        res = self.client.get(
            "/v1/agent/capabilities",
            headers={**self._bearer(token), "X-Request-Id": supplied},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["traceId"], supplied)
        self.assertEqual(res.headers.get("X-Request-Id"), supplied)

    def test_trace_id_generated_and_consistent(self):
        token = self._create("traceId2")
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        body_trace = res.json()["traceId"]
        self.assertTrue(body_trace.startswith("atr_"))
        self.assertEqual(res.headers.get("X-Request-Id"), body_trace)

    def test_trace_id_audit_matches(self):
        token = self._create("traceId3")
        supplied = "atr_match-audit-456"
        self.client.get(
            "/v1/agent/capabilities",
            headers={**self._bearer(token), "X-Request-Id": supplied},
        )
        audit = self.client.get("/api/agent/audit", params={"traceId": supplied}).json()
        self.assertEqual(audit["total"], 1)
        self.assertEqual(audit["items"][0]["trace_id"], supplied)
        self.assertEqual(audit["items"][0]["action"], "capabilities")

    # ---- 校验错误契约 -----------------------------------------------

    def test_validation_error_contract(self):
        # answers 需 mindos.answer + mindos.read；配齐后非法请求体 → 400/VALIDATION_ERROR。
        token = self._create("校验", scopes=["mindos.answer", "mindos.read"])
        res = self.client.post("/v1/agent/answers", json={}, headers=self._bearer(token))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("traceId", res.json())

    # ---- 本机管理员：创建 / 列表 / 轮换 / 停用 -----------------------

    def test_admin_create_and_list(self):
        token = self._create("管理员A", scopes=["mindos.read", "mindos.search"])
        clients = self.client.get("/api/agent/clients").json()["clients"]
        self.assertEqual(len(clients), 1)
        client = clients[0]
        self.assertEqual(client["name"], "管理员A")
        self.assertEqual(set(client["scopes"]), {"mindos.read", "mindos.search"})
        self.assertEqual(client["status"], "active")
        self.assertNotIn("token", client)  # 列表不返回明文 token

    def test_admin_create_rejects_unknown_scope(self):
        res = self.client.post(
            "/api/agent/clients",
            json={"name": "非法scope", "scopes": ["admin"]},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

    def test_admin_create_rejects_empty_name(self):
        res = self.client.post(
            "/api/agent/clients", json={"name": "", "scopes": ["mindos.read"]}
        )
        self.assertEqual(res.status_code, 400)

    def test_admin_create_rejects_workspace_input(self):
        # V1 单工作区：不接受客户端/管理员自报 workspace。
        res = self.client.post(
            "/api/agent/clients",
            json={"name": "工作区", "scopes": ["mindos.read"], "workspace": "other"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

    def test_capabilities_workspace_is_always_default(self):
        token = self._create("工作区默认")
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["workspaceId"], "default")

    def test_admin_create_rejects_past_expiry(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        res = self.client.post(
            "/api/agent/clients",
            json={
                "name": "过去",
                "scopes": ["mindos.read"],
                "expiresAt": past.isoformat(),
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

    def test_admin_create_rejects_naive_expiry(self):
        # 无时区的 ISO-8601 视为非法，避免时区歧义。
        res = self.client.post(
            "/api/agent/clients",
            json={
                "name": "无时区",
                "scopes": ["mindos.read"],
                "expiresAt": "2027-01-01T00:00:00",
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

    def test_admin_create_accepts_future_aware_expiry(self):
        future = datetime.now(timezone.utc) + timedelta(days=30)
        res = self.client.post(
            "/api/agent/clients",
            json={
                "name": "未来",
                "scopes": ["mindos.read"],
                "expiresAt": future.isoformat(),
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        client = res.json()["client"]
        self.assertIsNotNone(client["expires_at"])

    def test_admin_rotate_invalidates_old_token(self):
        token = self._create("轮换")
        clients = self.client.get("/api/agent/clients").json()["clients"]
        client_id = clients[0]["client_id"]
        res = self.client.post(f"/api/agent/clients/{client_id}/rotate")
        self.assertEqual(res.status_code, 200)
        new_token = res.json()["token"]
        self.assertNotEqual(new_token, token)
        # 旧 token 立即失效
        old = self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        self.assertEqual(old.status_code, 401)
        # 新 token 可用
        new = self.client.get("/v1/agent/capabilities", headers=self._bearer(new_token))
        self.assertEqual(new.status_code, 200)

    def test_admin_disable_invalidates_token(self):
        token = self._create("停用")
        clients = self.client.get("/api/agent/clients").json()["clients"]
        client_id = clients[0]["client_id"]
        res = self.client.post(f"/api/agent/clients/{client_id}/disable")
        self.assertEqual(res.status_code, 200)
        res = self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        self.assertEqual(res.status_code, 401)

    def test_admin_audit_filter_by_client(self):
        token = self._create("审计过滤")
        self.client.get("/v1/agent/capabilities", headers=self._bearer(token))
        clients = self.client.get("/api/agent/clients").json()["clients"]
        client_id = clients[0]["client_id"]
        audit = self.client.get(
            "/api/agent/audit", params={"clientId": client_id}
        ).json()
        self.assertEqual(audit["total"], 1)
        self.assertEqual(audit["items"][0]["client_id"], client_id)
        self.assertEqual(audit["items"][0]["outcome"], "ok")
        self.assertEqual(audit["items"][0]["status_code"], 200)

    def test_admin_audit_records_denied_requests(self):
        res = self.client.get("/v1/agent/capabilities")  # 无 token → 401
        self.assertEqual(res.status_code, 401)
        audit = self.client.get("/api/agent/audit").json()["items"]
        self.assertTrue(any(a["status_code"] == 401 for a in audit))

    def test_legacy_workspace_migrated_on_startup(self):
        """旧版本遗留的非默认 workspace 记录，重启迁移后不再生效。"""
        import sqlite3

        _, token = agent_store.instance().create_client("遗留", ["mindos.read"])
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE agent_clients SET allowed_workspace='other'")
        conn.commit()
        conn.close()
        # 重新初始化触发启动迁移
        agent_store.reset_for_tests(self.db_path)
        record = agent_store.instance().authenticate(token)
        self.assertIsNotNone(record)
        self.assertEqual(record["workspace"], agent_config.WORKSPACE_ID)


class RateLimitConfigTestCase(unittest.TestCase):
    """rate_limits() 环境变量配置解析与严格校验。"""

    def _limits(self, raw: str) -> dict:
        with patch.dict(os.environ, {"MINDOS_AGENT_RATE_LIMITS_JSON": raw}):
            return agent_config.rate_limits()

    def test_defaults_when_env_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINDOS_AGENT_RATE_LIMITS_JSON", None)
            self.assertEqual(agent_config.rate_limits(), agent_config.RATE_LIMITS_PER_MINUTE)

    def test_valid_override_applies(self):
        limits = self._limits('{"capabilities": 5}')
        self.assertEqual(limits["capabilities"], 5)
        self.assertEqual(limits["search"], agent_config.RATE_LIMITS_PER_MINUTE["search"])

    def test_negative_rejected(self):
        self.assertEqual(self._limits('{"capabilities": -1}'), agent_config.RATE_LIMITS_PER_MINUTE)

    def test_float_rejected(self):
        self.assertEqual(self._limits('{"capabilities": 1.5}'), agent_config.RATE_LIMITS_PER_MINUTE)

    def test_unknown_action_rejected(self):
        self.assertEqual(self._limits('{"unknown": 2}'), agent_config.RATE_LIMITS_PER_MINUTE)

    def test_non_object_rejected(self):
        self.assertEqual(self._limits('"nope"'), agent_config.RATE_LIMITS_PER_MINUTE)

    def test_zero_means_completely_blocked(self):
        # 0 明确约定为「完全禁止」该动作。
        with patch.dict(os.environ, {"MINDOS_AGENT_RATE_LIMITS_JSON": '{"capabilities": 0}'}):
            self.assertEqual(agent_config.rate_limits()["capabilities"], 0)
        limiter = RateLimiter(limits={"capabilities": 0})
        self.assertFalse(limiter.check("c1", "capabilities")[0])


class RateLimiterTestCase(unittest.TestCase):
    """限流器单元行为（AG-01 基础实现）。"""

    def test_within_limit_allowed(self):
        limiter = RateLimiter(limits={"capabilities": 2})
        self.assertTrue(limiter.check("c1", "capabilities")[0])
        self.assertTrue(limiter.check("c1", "capabilities")[0])
        self.assertFalse(limiter.check("c1", "capabilities")[0])

    def test_per_client_isolation(self):
        limiter = RateLimiter(limits={"capabilities": 1})
        self.assertTrue(limiter.check("c1", "capabilities")[0])
        self.assertTrue(limiter.check("c2", "capabilities")[0])  # 不同 client 不互相影响
        self.assertFalse(limiter.check("c1", "capabilities")[0])

    def test_unconfigured_action_not_limited(self):
        limiter = RateLimiter(limits={})
        for _ in range(1000):
            self.assertTrue(limiter.check("c1", "anything")[0])

    def test_reset_clears_state(self):
        limiter = RateLimiter(limits={"capabilities": 1})
        self.assertTrue(limiter.check("c1", "capabilities")[0])
        self.assertFalse(limiter.check("c1", "capabilities")[0])
        limiter.reset()
        self.assertTrue(limiter.check("c1", "capabilities")[0])


if __name__ == "__main__":
    unittest.main()
