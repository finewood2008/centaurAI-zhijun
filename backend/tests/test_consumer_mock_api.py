"""阶段 2：Consumer API（Mock）接口单测：登录注册/Refresh/撤销/设备/同步/票据。"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from consumer_api import store as store_module
from consumer_api.app import API_PREFIX, create_app
from consumer_api.store import CURRENT_PROTOCOL_VERSION, ConsumerState, stepup_request_digest

PHONE = "13800000001"
CODE = "123456"


class ConsumerMockApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = ConsumerState()
        self.client = TestClient(create_app(self.state))
        self.client.post(
            f"{API_PREFIX}/__mock/devices",
            json={"deviceId": "device_alpha", "name": "盒子A"},
        )
        self.login = self.client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": PHONE, "code": CODE, "clientName": "PC"},
        ).json()
        self.headers = {"Authorization": f"Bearer {self.login['accessToken']}"}

    def test_file_backed_state_persists_across_restart(self):
        """F4：文件持久化的 Mock 状态在重启（新实例）后不丢失。"""
        import tempfile
        from pathlib import Path

        from consumer_api.store import ConsumerState

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "consumer_mock.db"
            first = ConsumerState(db_path=db)
            login = first.register_or_login("13900000009", "PC")
            first.create_device("device_persist", "盒子P", "up_to_date")
            first.claim_device(login["accountId"], "device_persist", "claim-p")

            restarted = ConsumerState(db_path=db)
            self.assertEqual(
                restarted.authenticate_access(login["accessToken"])["account_id"],
                login["accountId"],
            )
            devices = restarted.list_devices(login["accountId"])
            self.assertEqual([d["deviceId"] for d in devices], ["device_persist"])
            restarted.reset()
            first.close()
            restarted.close()

    def test_login_registers_and_same_phone_merges_account(self):
        self.assertEqual(self.login["accountExists"], True)
        self.assertTrue(self.login["clientId"].startswith("client_"))
        second = self.client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": PHONE, "code": CODE, "clientName": "App"},
        ).json()
        self.assertEqual(second["accountId"], self.login["accountId"])
        self.assertNotEqual(second["clientId"], self.login["clientId"])

    def test_login_rejects_wrong_sms_code(self):
        res = self.client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": PHONE, "code": "000000", "clientName": "PC"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "SMS_CODE_INVALID")

    def test_protected_route_requires_bearer(self):
        res = self.client.get(f"{API_PREFIX}/devices")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "AUTH_REQUIRED")

    def test_refresh_returns_new_access_token(self):
        res = self.client.post(
            f"{API_PREFIX}/auth/refresh",
            json={"refreshToken": self.login["refreshToken"]},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertNotEqual(body["accessToken"], self.login["accessToken"])
        self.assertEqual(body["refreshToken"], self.login["refreshToken"])

    def test_logout_invalidates_access_token(self):
        res = self.client.post(f"{API_PREFIX}/auth/logout", headers=self.headers)
        self.assertEqual(res.status_code, 204)
        res = self.client.get(f"{API_PREFIX}/devices", headers=self.headers)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "AUTH_INVALID")

    def test_revoke_client_invalidates_tokens_and_other_client_unaffected(self):
        res = self.client.post(
            f"{API_PREFIX}/auth/clients/{self.login['clientId']}/revoke",
            headers=self.headers,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["newEpoch"], 1)

        res = self.client.get(f"{API_PREFIX}/devices", headers=self.headers)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "CLIENT_REVOKED")

        second = self.client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": PHONE, "code": CODE, "clientName": "App"},
        ).json()
        res = self.client.get(f"{API_PREFIX}/devices", headers={"Authorization": f"Bearer {second['accessToken']}"})
        self.assertEqual(res.status_code, 200)

    def test_claim_list_rename_and_detail(self):
        claim = self.client.post(
            f"{API_PREFIX}/devices/device_alpha/claim",
            json={"idempotencyKey": "claim-1"},
            headers=self.headers,
        ).json()
        self.assertEqual(claim["ownerAccountId"], self.login["accountId"])

        devices = self.client.get(f"{API_PREFIX}/devices", headers=self.headers).json()["devices"]
        self.assertEqual([d["deviceId"] for d in devices], ["device_alpha"])

        renamed = self.client.patch(
            f"{API_PREFIX}/devices/device_alpha",
            json={"name": "客厅盒子"},
            headers=self.headers,
        ).json()
        self.assertEqual(renamed["name"], "客厅盒子")

        detail = self.client.get(f"{API_PREFIX}/devices/device_alpha", headers=self.headers).json()
        self.assertEqual(detail["otaStatus"], "up_to_date")

    def test_claim_by_other_account_rejected(self):
        self.client.post(
            f"{API_PREFIX}/devices/device_alpha/claim",
            json={"idempotencyKey": "claim-1"},
            headers=self.headers,
        )
        other = self.client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": "13800000002", "code": CODE, "clientName": "App"},
        ).json()
        res = self.client.post(
            f"{API_PREFIX}/devices/device_alpha/claim",
            json={"idempotencyKey": "claim-2"},
            headers={"Authorization": f"Bearer {other['accessToken']}"},
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["error"]["code"], "DEVICE_ALREADY_OWNED")

    def test_sync_bootstrap_and_incremental_events(self):
        bootstrap = self.client.get(f"{API_PREFIX}/sync/bootstrap", headers=self.headers).json()
        self.assertEqual(bootstrap["events"], [])

        self.client.post(
            f"{API_PREFIX}/devices/device_alpha/claim",
            json={"idempotencyKey": "claim-1"},
            headers=self.headers,
        )
        changes = self.client.get(f"{API_PREFIX}/sync/changes?cursor=0", headers=self.headers).json()
        self.assertEqual([e["type"] for e in changes["events"]], ["device_added"])
        self.assertEqual(changes["events"][0]["device_id"], "device_alpha")

        self.client.patch(
            f"{API_PREFIX}/devices/device_alpha",
            json={"name": "客厅盒子"},
            headers=self.headers,
        )
        changes2 = self.client.get(
            f"{API_PREFIX}/sync/changes?cursor={changes['cursor']}",
            headers=self.headers,
        ).json()
        self.assertEqual([e["type"] for e in changes2["events"]], ["device_renamed"])
        self.assertEqual(changes2["events"][0]["payload"]["name"], "客厅盒子")

    def test_connectivity_session_is_idempotent_and_single_active(self):
        self.client.post(
            f"{API_PREFIX}/devices/device_alpha/claim",
            json={"idempotencyKey": "claim-1"},
            headers=self.headers,
        )
        body = {"deviceId": "device_alpha", "idempotencyKey": "sess-1"}
        first = self.client.post(f"{API_PREFIX}/connectivity/sessions", json=body, headers=self.headers).json()
        self.assertEqual(first["epochGeneration"], 0)
        self.assertTrue(first["nonce"])
        self.assertGreater(first["expiresAt"], first["connectBefore"])

        second = self.client.post(f"{API_PREFIX}/connectivity/sessions", json=body, headers=self.headers).json()
        self.assertEqual(second["ticket"], first["ticket"])

        other_key = self.client.post(
            f"{API_PREFIX}/connectivity/sessions",
            json={"deviceId": "device_alpha", "idempotencyKey": "sess-2"},
            headers=self.headers,
        ).json()
        self.assertEqual(other_key["ticket"], first["ticket"])

    def test_jwks_and_revocations_endpoints(self):
        jwks = self.client.get("/.well-known/jwks.json").json()
        self.assertEqual(jwks["keys"][0]["alg"], "RS256")
        self.assertTrue(jwks["keys"][0]["n"])

        self.client.post(
            f"{API_PREFIX}/devices/device_alpha/claim",
            json={"idempotencyKey": "claim-1"},
            headers=self.headers,
        )
        self.client.post(
            f"{API_PREFIX}/connectivity/sessions",
            json={"deviceId": "device_alpha", "idempotencyKey": "sess-1"},
            headers=self.headers,
        )
        self.client.post(
            f"{API_PREFIX}/auth/clients/{self.login['clientId']}/revoke",
            headers=self.headers,
        )
        payload = self.client.get(f"{API_PREFIX}/__mock/revocations?since=0").json()
        self.assertEqual(len(payload["revocations"]), 1)
        self.assertEqual(payload["revocations"][0]["clientId"], self.login["clientId"])
        self.assertEqual(payload["revocations"][0]["deviceId"], "device_alpha")

    def _stepup_remove_other(self, other_client_id: str, digest: str) -> str:
        """走完 send→verify，返回可用的 stepUpToken（用于移除 other_client_id）。"""
        sent = self.client.post(
            f"{API_PREFIX}/auth/step-up/sms/send",
            json={"action": "client.revoke", "target": {"clientId": other_client_id}, "requestDigest": digest},
            headers=self.headers,
        ).json()
        self.assertEqual(sent["devOnlyCode"], CODE)
        verified = self.client.post(
            f"{API_PREFIX}/auth/step-up/sms/verify",
            json={"challengeId": sent["challengeId"], "code": CODE},
            headers=self.headers,
        ).json()
        self.assertEqual(verified["action"], "client.revoke")
        return verified["stepUpToken"]

    def test_stepup_removes_other_client_and_is_single_use(self):
        other = self.client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": PHONE, "code": CODE, "clientName": "App"},
        ).json()
        other_client_id = other["clientId"]
        path = f"{API_PREFIX}/auth/clients/{other_client_id}"
        digest = stepup_request_digest("client.revoke", other_client_id, "DELETE", path)

        # 无 Step-up 凭证 → STEP_UP_REQUIRED
        res = self.client.delete(path, headers=self.headers)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "STEP_UP_REQUIRED")

        stepup = self._stepup_remove_other(other_client_id, digest)

        res = self.client.delete(path, headers={**self.headers, "X-Nexus-Step-Up": stepup})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["revokedClientId"], other_client_id)

        # 其他终端已撤销，当前终端不受影响
        self.assertEqual(
            self.client.get(f"{API_PREFIX}/devices", headers={"Authorization": f"Bearer {other['accessToken']}"}).status_code,
            401,
        )
        self.assertEqual(self.client.get(f"{API_PREFIX}/devices", headers=self.headers).status_code, 200)

        # Token 单次使用：重放拒绝
        res = self.client.delete(path, headers={**self.headers, "X-Nexus-Step-Up": stepup})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "STEP_UP_INVALID")

    def test_stepup_wrong_code_rejected(self):
        other = self.client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": PHONE, "code": CODE, "clientName": "App"},
        ).json()
        other_client_id = other["clientId"]
        path = f"{API_PREFIX}/auth/clients/{other_client_id}"
        digest = stepup_request_digest("client.revoke", other_client_id, "DELETE", path)
        sent = self.client.post(
            f"{API_PREFIX}/auth/step-up/sms/send",
            json={"action": "client.revoke", "target": {"clientId": other_client_id}, "requestDigest": digest},
            headers=self.headers,
        ).json()
        res = self.client.post(
            f"{API_PREFIX}/auth/step-up/sms/verify",
            json={"challengeId": sent["challengeId"], "code": "000000"},
            headers=self.headers,
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "SMS_CODE_INVALID")

    def test_stepup_cannot_remove_current_client(self):
        current = self.login["clientId"]
        path = f"{API_PREFIX}/auth/clients/{current}"
        digest = stepup_request_digest("client.revoke", current, "DELETE", path)
        stepup = self._stepup_remove_other(current, digest)
        res = self.client.delete(path, headers={**self.headers, "X-Nexus-Step-Up": stepup})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "STEP_UP_INVALID")
        # 当前终端仍可用
        self.assertEqual(self.client.get(f"{API_PREFIX}/devices", headers=self.headers).status_code, 200)


class ProtocolConfirmationTests(unittest.TestCase):
    """Auth WP C「协议确认」：版本发现、首次确认、升级重确认、审计字段。"""

    def test_login_confirms_current_protocol_and_returns_protocol_field(self):
        state = ConsumerState()
        client = TestClient(create_app(state))
        res = client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": "13800000011", "code": CODE, "clientName": "PC",
                  "protocolVersion": CURRENT_PROTOCOL_VERSION},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(
            body["protocol"],
            {"name": "mindos-consumer", "version": CURRENT_PROTOCOL_VERSION,
             "latestVersion": CURRENT_PROTOCOL_VERSION, "confirmed": True},
        )

    def test_protocol_discovery_endpoint_matches_current(self):
        state = ConsumerState()
        client = TestClient(create_app(state))
        info = client.get(f"{API_PREFIX}/auth/protocol").json()
        self.assertEqual(info["name"], "mindos-consumer")
        self.assertEqual(info["version"], CURRENT_PROTOCOL_VERSION)

    def test_legacy_client_without_version_implicitly_agrees(self):
        state = ConsumerState()
        client = TestClient(create_app(state))
        res = client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": "13800000012", "code": CODE, "clientName": "Legacy"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["protocol"]["confirmed"])
        self.assertEqual(res.json()["protocol"]["version"], CURRENT_PROTOCOL_VERSION)

    def test_upgrade_reconfirm_requires_latest_version(self):
        state = ConsumerState()
        client = TestClient(create_app(state))
        # 客户端声明旧版本 → 升级重确认（426），不得静默绕过
        res = client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": "13800000013", "code": CODE, "clientName": "Old",
                  "protocolVersion": 0},  # >0 过旧 → 触发 426
        )
        # 重新以旧版本号表示（CURRENT_PROTOCOL_VERSION > 0 时）：
        if CURRENT_PROTOCOL_VERSION > 1:
            old = CURRENT_PROTOCOL_VERSION - 1
            res = client.post(
                f"{API_PREFIX}/auth/login",
                json={"phone": "13800000013", "code": CODE, "clientName": "Old",
                      "protocolVersion": old},
            )
            self.assertEqual(res.status_code, 426)
            self.assertEqual(res.json()["error"]["code"], "PROTOCOL_UPGRADE_REQUIRED")
            self.assertEqual(
                res.json()["error"]["details"]["latestVersion"], CURRENT_PROTOCOL_VERSION,
            )
        else:
            self.assertEqual(res.status_code, 200)  # 当前版本即 1，0 视为隐式同意
        # 更新到最新版本 → 确认成功
        ok = client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": "13800000013", "code": CODE, "clientName": "Upgraded",
                  "protocolVersion": CURRENT_PROTOCOL_VERSION},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["protocol"]["confirmed"])

    def test_unsupported_future_protocol_rejected(self):
        state = ConsumerState()
        client = TestClient(create_app(state))
        res = client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": "13800000014", "code": CODE, "clientName": "Future",
                  "protocolVersion": CURRENT_PROTOCOL_VERSION + 1},
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "PROTOCOL_UNSUPPORTED")

    def test_confirmation_is_audited_with_account_client_version_and_time(self):
        state = ConsumerState()
        client = TestClient(create_app(state))
        res = client.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": "13800000015", "code": CODE, "clientName": "PC",
                  "protocolVersion": CURRENT_PROTOCOL_VERSION},
        ).json()
        with state._lock:
            rows = state._conn.execute(
                "SELECT account_id, client_id, protocol_version, client_name, created_at "
                "FROM consumer_protocol_confirmations WHERE client_id=?",
                (res["clientId"],),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["account_id"], res["accountId"])
        self.assertEqual(row["protocol_version"], CURRENT_PROTOCOL_VERSION)
        self.assertEqual(row["client_name"], "PC")
        self.assertGreater(row["created_at"], 0)

    def test_old_version_tuple_loads_correctly(self):
        # 确保 current 版本在模块加载时可作为路径判定依据
        self.assertGreaterEqual(CURRENT_PROTOCOL_VERSION, 1)


if __name__ == "__main__":
    unittest.main()
