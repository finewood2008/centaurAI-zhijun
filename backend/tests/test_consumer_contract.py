"""Consumer API 合同一致性测试（阶段 0）。

守护 docs/development/consumer-api-contract/：
- openapi.json 必须与 consumer_api.create_app().openapi() 一致（防实现漂移）；
- VERSION 携带合同/协议版本；
- 关键端点与错误码在合同中完整。

任何 OpenAPI/schema/错误码的破坏性变更必须先提升合同版本并重新导出。
"""

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from consumer_api.app import create_app  # noqa: E402
from consumer_api.store import CURRENT_PROTOCOL_VERSION  # noqa: E402

CONTRACT_DIR = PROJECT_ROOT / "docs" / "development" / "consumer-api-contract"


class ConsumerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.openapi = json.loads((CONTRACT_DIR / "openapi.json").read_text(encoding="utf-8"))
        cls.contract_md = (CONTRACT_DIR / "CONTRACT.md").read_text(encoding="utf-8")
        cls.version_file = (CONTRACT_DIR / "VERSION").read_text(encoding="utf-8")

    def test_openapi_matches_implementation(self):
        live = create_app().openapi()
        self.assertEqual(self.openapi, live, "openapi.json 与实现漂移：请重新导出合同")

    def test_version_file(self):
        self.assertIn(f"protocol={CURRENT_PROTOCOL_VERSION}", self.version_file)
        self.assertIn("contract=1.0.0", self.version_file)

    def test_required_paths_present(self):
        paths = self.openapi["paths"]
        for path in (
            "/api/consumer/v1/auth/phone-code",
            "/api/consumer/v1/auth/protocol",
            "/api/consumer/v1/auth/login",
            "/api/consumer/v1/auth/refresh",
            "/api/consumer/v1/auth/logout",
            "/api/consumer/v1/auth/clients",
            "/api/consumer/v1/auth/step-up/sms/send",
            "/api/consumer/v1/devices",
            "/api/consumer/v1/devices/{device_id}/claim",
            "/api/consumer/v1/devices/{device_id}/ota",
            "/api/consumer/v1/sync/bootstrap",
            "/api/consumer/v1/sync/changes",
            "/api/consumer/v1/connectivity/sessions",
            "/.well-known/jwks.json",
        ):
            self.assertIn(path, paths, f"合同缺少端点：{path}")

    def test_error_codes_documented(self):
        for code in (
            "AUTH_REQUIRED",
            "TOKEN_EXPIRED",
            "CLIENT_REVOKED",
            "DEVICE_NOT_FOUND",
            "DEVICE_ALREADY_OWNED",
            "TICKET_ACTIVE",
            "SMS_CODE_INVALID",
            "STEP_UP_REQUIRED",
            "PROTOCOL_UPGRADE_REQUIRED",
            "PROTOCOL_UNSUPPORTED",
            "VALIDATION_ERROR",
        ):
            self.assertIn(f"`{code}`", self.contract_md, f"合同错误码表缺少：{code}")

    def test_protocol_section_documented(self):
        self.assertIn("协议确认", self.contract_md)
        self.assertIn(f"`{CURRENT_PROTOCOL_VERSION}`", self.contract_md)

    def test_mock_surface_flagged_dev_only(self):
        self.assertIn("/__mock/", self.contract_md)
        self.assertIn("仅联调", self.contract_md)


if __name__ == "__main__":
    unittest.main()
