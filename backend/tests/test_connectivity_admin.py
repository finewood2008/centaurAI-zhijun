"""阶段 2：Consumer Connectivity 本机管理接口（撤销 / epoch / ACL）集成测试。

复用 server 相同的防护合同：写操作 loopback + CSRF（X-Requested-By），
读操作 loopback。TestClient 的 host 为 'testclient'，按 loopback 处理。
"""

from __future__ import annotations

import unittest

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.testclient import TestClient

from mindos import connectivity_admin
from mindos.stores import connectivity_store
from mindos.device_context import get_device_registry

CSRF = "centaur-vdb"


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_local(request: Request, x_requested_by: str | None = Header(default=None)):
    if x_requested_by != CSRF:
        raise HTTPException(403, "缺少 X-Requested-By 头（跨站请求防护）")
    if not _is_loopback(request):
        raise HTTPException(403, "本机管理接口仅允许 loopback 访问")


def require_loopback(request: Request):
    if not _is_loopback(request):
        raise HTTPException(403, "本机管理接口仅允许 loopback 访问")


def _build_app() -> FastAPI:
    app = FastAPI()
    connectivity_admin.configure_admin_guards(require_local, require_loopback)
    app.include_router(connectivity_admin.router)
    return app


class ConnectivityAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        connectivity_store.reset()
        get_device_registry().reset()
        self.app = _build_app()
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        connectivity_store.reset()
        get_device_registry().reset()

    def _headers(self) -> dict:
        return {"X-Requested-By": CSRF}

    def test_revoke_requires_csrf_and_loopback(self):
        payload = {
            "account_id": "account_1",
            "client_id": "client_1",
            "device_id": "device_local",
            "reason": "test",
        }
        self.assertEqual(self.client.post("/api/mindos/connectivity/revoke", json=payload).status_code, 403)
        res = self.client.post("/api/mindos/connectivity/revoke", json=payload, headers=self._headers())
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["newEpoch"], 1)

    def test_revoke_closes_sessions_and_invalidates_device_context(self):
        connectivity_store.register_session(
            session_id="s1",
            epoch_generation=1,
            expires_at=1_000_000_000_000,
            account_id="account_1",
            client_id="client_1",
            device_id="device_local",
        )
        from mindos.connectivity_ticket import ConnectivityPrincipal

        get_device_registry().get_or_create(
            ConnectivityPrincipal(
                account_id="account_1",
                client_id="client_1",
                device_id="device_local",
                scopes=frozenset({"mindos:access"}),
                expires_at=2_000_000_000,
                nonce="n",
                epoch_generation=1,
            )
        )
        ctx = get_device_registry().get("device_local")
        self.assertIsNotNone(ctx)
        generation_before = ctx.cache_generation

        res = self.client.post(
            "/api/mindos/connectivity/revoke",
            json={
                "account_id": "account_1",
                "client_id": "client_1",
                "device_id": "device_local",
                "reason": "test",
            },
            headers=self._headers(),
        )
        self.assertEqual(res.json()["closedSessions"], 1)
        self.assertGreater(ctx.cache_generation, generation_before)

    def test_epoch_rotate_and_acl_endpoints(self):
        headers = self._headers()
        rotate = self.client.post(
            "/api/mindos/connectivity/epoch/rotate",
            json={
                "account_id": "account_1",
                "client_id": "client_1",
                "device_id": "device_local",
                "reason": "test",
            },
            headers=headers,
        )
        self.assertEqual(rotate.json()["newEpoch"], 1)

        deny = self.client.post(
            "/api/mindos/connectivity/acl",
            json={"scope": "device:device_local", "denied": True, "reason": "test"},
            headers=headers,
        )
        self.assertTrue(deny.json()["success"])
        self.assertTrue(connectivity_store.is_device_denied(device_id="device_local"))

        bad = self.client.post(
            "/api/mindos/connectivity/acl",
            json={"scope": "bogus", "denied": True, "reason": "test"},
            headers=headers,
        )
        self.assertEqual(bad.status_code, 422)
        self.assertIn("scope", bad.json()["detail"])

    def test_state_is_readable_over_loopback_without_csrf(self):
        res = self.client.get("/api/mindos/connectivity/state")
        self.assertEqual(res.status_code, 200)
        self.assertIn("revocations", res.json())
        self.assertIn("deviceContexts", res.json())

    def test_exchange_requires_ticket(self):
        res = self.client.post("/api/mindos/connectivity/sessions/exchange")
        self.assertEqual(res.status_code, 401)
        self.assertIn("设备连接票据", res.json()["detail"])

    def test_exchange_returns_session_credential_and_registers_session(self):
        from mindos.connectivity_ticket import ConnectivityPrincipal, configure_ticket_verifier

        class _FakeVerifier:
            def verify(self, token: str, *, method: str, path: str) -> ConnectivityPrincipal:
                del token, method, path
                return ConnectivityPrincipal(
                    account_id="account_x",
                    client_id="client_x",
                    device_id="device_local",
                    scopes=frozenset({"mindos:access"}),
                    expires_at=2_000_000_000,
                    nonce="n1",
                    epoch_generation=0,
                )

        configure_ticket_verifier(_FakeVerifier())
        try:
            res = self.client.post(
                "/api/mindos/connectivity/sessions/exchange",
                headers={"Authorization": "Bearer ticket-1"},
            )
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertTrue(body["sessionToken"])
            self.assertEqual(body["deviceId"], "device_local")
            sessions = connectivity_store.active_sessions(device_id="device_local")
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["session_id"], body["sessionId"])
        finally:
            configure_ticket_verifier(None)


if __name__ == "__main__":
    unittest.main()
