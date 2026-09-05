"""Agent Gateway 与真实 server.app 的集成回归测试（AG-01 review 修复）。

验证 install(app) 注入的全局 RequestValidationError / Exception 处理器：
- 既有 /api/* 接口的非法请求仍返回 FastAPI 默认 422 契约（不被改写）；
- /v1/agent 路由在真实应用中正确注册并强制鉴权。

依赖项目 .venv，可独立于 server 运行：
    .venv\\Scripts\\python.exe -m unittest test_mindos_agent_server -v
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 网关默认拒绝；集成测试需显式启用，server.app 在请求期读取环境变量。
os.environ["MINDOS_AGENT_GATEWAY_ENABLED"] = "true"

from fastapi.testclient import TestClient

import server


class AgentServerIntegrationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def test_existing_api_keeps_default_422_contract(self):
        # /api/mindos/validate 是既有本机 API：非法请求体必须保持 FastAPI 默认 422 契约。
        res = self.client.post("/api/mindos/validate", json={})
        self.assertEqual(res.status_code, 422)
        body = res.json()
        self.assertIn("detail", body)

    def test_agent_route_registered_and_requires_credentials(self):
        # 网关已启用；无凭证访问 /v1/agent/capabilities → 401（非 404，说明路由已注册）。
        res = self.client.get("/v1/agent/capabilities")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "AUTHENTICATION_REQUIRED")
        self.assertIn("traceId", res.json())


if __name__ == "__main__":
    unittest.main()
