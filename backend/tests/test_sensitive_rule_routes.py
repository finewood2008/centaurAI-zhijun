from __future__ import annotations

import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mindos import sensitive_rule_routes as routes
from zhijun_worker.data_agent_rag_v2 import DataAgentRagV2Error


PREFIX = "/api/mindos/settings/sensitive-rules"


def _input(**updates) -> dict:
    value = {
        "name": "项目代号", "description": "能够识别尚未公开的内部项目代号信息。",
        "examples": ["灯塔计划"], "counterExamples": ["普通项目"], "enabled": True,
        "deliveryMode": "confirm", "allowOriginalAfterConfirm": True,
    }
    value.update(updates)
    return value


def _item(revision=1) -> dict:
    return {
        "ruleId": "csr_rule_1", "source": "custom", "immutable": False,
        "revision": revision, "etag": f'"{revision}"', **_input(),
        "acknowledgeSimilarRuleId": None,
        "masking": {"strategy": "fixed", "prefixCharacters": 0,
                    "suffixCharacters": 0, "replacement": "[项目代号已脱敏]"},
    }


class FakeClient:
    def __init__(self):
        self.calls = []
        self.error = None

    def _return(self, value):
        if self.error is not None:
            raise self.error
        return value

    def list_sensitive_rules(self):
        self.calls.append(("list",))
        return self._return({"items": [_item()], "total": 1, "builtinCount": 0,
                             "customCount": 1, "maxCustomRules": 30})

    def get_sensitive_capabilities(self):
        self.calls.append(("capabilities",))
        return self._return({"policyWrite": True, "builtinWrite": True, "rolloutManage": True})

    def get_sensitive_rollout_status(self):
        self.calls.append(("rollout-status",))
        return self._return({"state": "pending", "scanEnabled": True, "applying": False,
                             "historicalScanRequired": True, "retryAvailable": False,
                             "targetDetectorRevision": "sensitive-detector-v2:" + "a" * 24})

    def start_sensitive_rollout(self, revision, *, idempotency_key):
        self.calls.append(("rollout-start", revision, idempotency_key))
        return self._return(self.get_sensitive_rollout_status())

    def retry_sensitive_rollout(self, revision, *, idempotency_key):
        self.calls.append(("rollout-retry", revision, idempotency_key))
        return self._return(self.get_sensitive_rollout_status())

    def get_sensitive_rule(self, rule_id):
        self.calls.append(("get", rule_id))
        return self._return(_item())

    def create_sensitive_rule(self, rule, *, idempotency_key):
        self.calls.append(("create", rule, idempotency_key))
        return self._return(_item())

    def update_sensitive_rule(self, rule_id, rule, *, etag):
        self.calls.append(("update", rule_id, rule, etag))
        return self._return(_item(2))

    def delete_sensitive_rule(self, rule_id, *, etag):
        self.calls.append(("delete", rule_id, etag))
        return self._return({"deleted": True})

    def update_builtin_sensitive_rule(self, rule_id, rule, *, etag):
        self.calls.append(("builtin-update", rule_id, rule, etag))
        return self._return({**_item(2), "ruleId": rule_id, "source": "built_in",
                             "immutable": True, "etag": '"builtin:1:1"'})

    def reset_builtin_sensitive_rule(self, rule_id, *, etag, acknowledge_similar_rule_id=None):
        self.calls.append(("builtin-reset", rule_id, etag, acknowledge_similar_rule_id))
        return self._return({**_item(3), "ruleId": rule_id, "source": "built_in",
                             "immutable": True, "etag": '"builtin:1:2"'})


class SensitiveRuleRoutesTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClient()
        routes._client_override = self.fake
        self.addCleanup(setattr, routes, "_client_override", None)
        app = FastAPI()
        app.include_router(routes.build_router())
        self.client = TestClient(app)

    def test_list_get_and_crud_preserve_opaque_etag_in_safe_body(self):
        self.assertEqual(self.client.get(PREFIX).status_code, 200)
        self.assertEqual(self.client.get(PREFIX + "/csr_rule_1").json()["etag"], '"1"')

        create_body = {"requestId": "rule-create-0001", "rule": _input()}
        created = self.client.post(PREFIX + "/custom", json=create_body)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["revision"], 1)
        self.assertNotIn("ETag", created.headers)

        updated = self.client.put(PREFIX + "/custom/csr_rule_1", json={
            "expectedEtag": '"1"', "rule": _input(),
        })
        self.assertEqual(updated.json()["revision"], 2)
        deleted = self.client.request(
            "DELETE", PREFIX + "/custom/csr_rule_1", json={"expectedEtag": '"2"'},
        )
        self.assertEqual(deleted.json(), {"deleted": True})

        create_call = next(call for call in self.fake.calls if call[0] == "create")
        self.assertEqual(create_call[2], "rule-create-0001")
        update_call = next(call for call in self.fake.calls if call[0] == "update")
        self.assertEqual(update_call[3], '"1"')

    def test_renderer_input_is_strict_and_never_accepts_raw_if_match(self):
        valid = {"expectedEtag": '"1"', "rule": _input()}
        self.assertEqual(self.client.put(PREFIX + "/custom/csr_rule_1", json={**valid, "extra": True}).status_code, 422)
        self.assertEqual(self.client.put(PREFIX + "/custom/csr_rule_1", json={**valid, "expectedEtag": '1'}).status_code, 422)
        self.assertEqual(self.client.request(
            "DELETE", PREFIX + "/custom/csr_rule_1",
            json={"expectedEtag": '"1"', "ifMatch": '"1"'}
        ).status_code, 422)
        self.assertEqual(self.client.put(
            PREFIX + "/custom/bad%20id", json=valid,
        ).status_code, 422)
        self.assertEqual(self.client.post(
            PREFIX + "/custom", json={
                "requestId": "rule-create-0001",
                "rule": _input(deliveryMode="block", allowOriginalAfterConfirm=True),
            }
        ).status_code, 422)
        self.assertEqual(self.client.post(PREFIX + "/custom", json={
            "requestId": "rule-create-0002", "rule": _input(examples=["x" * 201]),
        }).status_code, 422)

    def test_similar_conflict_exposes_only_safe_id_and_redacts_upstream_message(self):
        self.fake.error = DataAgentRagV2Error(
            "CUSTOM_RULE_SIMILAR", status=409, message="secret internal detail",
            trace_id="atr_safe", similar_rule_id="csr_existing",
        )
        response = self.client.post(PREFIX + "/custom", json={
            "requestId": "rule-create-0001", "rule": _input(),
        })
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertEqual(detail["similarRuleId"], "csr_existing")
        self.assertNotIn("secret internal detail", response.text)
        self.assertNotIn("If-Match", response.text)
        self.assertNotIn("ETag", response.text)

        self.fake.error = DataAgentRagV2Error(
            "CUSTOM_RULE_SIMILAR", status=409, similar_rule_id="bad\r\nid",
        )
        unsafe = self.client.post(PREFIX + "/custom", json={
            "requestId": "rule-create-0002", "rule": _input(),
        })
        self.assertNotIn("similarRuleId", unsafe.json()["detail"])

    def test_documented_errors_use_fixed_safe_guidance(self):
        cases = {
            "CUSTOM_RULE_CATALOG_TOO_LARGE": (422, "检测提示容量不足"),
            "BUILTIN_RULE_FIELD_IMMUTABLE": (422, "字段不可修改"),
            "BUILTIN_RULE_PROTECTED": (422, "系统安全底线"),
            "SENSITIVE_SCAN_DISABLED": (409, "历史扫描服务未启用"),
        }
        for code, (status, expected) in cases.items():
            with self.subTest(code=code):
                self.fake.error = DataAgentRagV2Error(
                    code, status=status, message="secret private upstream detail",
                )
                response = self.client.get(PREFIX)
                self.assertEqual(response.status_code, status)
                self.assertIn(expected, response.json()["detail"]["detail"])
                self.assertNotIn("secret private upstream detail", response.text)

    def test_write_guard_applies_only_to_mutations(self):
        def deny():
            raise HTTPException(403, "denied")

        app = FastAPI()
        app.include_router(routes.build_router(deny))
        guarded = TestClient(app)
        self.assertEqual(guarded.get(PREFIX).status_code, 200)
        self.assertEqual(guarded.post(
            PREFIX + "/custom", json={"requestId": "rule-create-0001", "rule": _input()}
        ).status_code, 403)
        self.assertEqual(guarded.post(PREFIX + "/rollout/start", json={
            "requestId": "rollout-0001", "expectedDetectorRevision": "sensitive-detector-v2:" + "a" * 24,
            "confirmHistoricalScan": True,
        }).status_code, 403)

    def test_builtin_patch_reset_and_rollout_are_explicit_and_constrained(self):
        self.assertEqual(self.client.get(PREFIX + "/capabilities").json()["builtinWrite"], True)
        self.assertEqual(self.client.get(PREFIX + "/rollout/status").json()["state"], "pending")
        updated = self.client.put(PREFIX + "/built-in/person_name", json={
            "expectedEtag": '"builtin:1:0"', "rule": {"enabled": False},
        })
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["etag"], '"builtin:1:1"')
        self.assertIn(("builtin-update", "person_name", {"enabled": False}, '"builtin:1:0"'), self.fake.calls)
        self.assertEqual(self.client.put(PREFIX + "/built-in/person_name", json={
            "expectedEtag": '"builtin:1:0"', "rule": {"name": "非法字段"},
        }).status_code, 422)
        reset = self.client.post(PREFIX + "/built-in/person_name/reset", json={
            "expectedEtag": '"builtin:1:1"', "acknowledgeSimilarRuleId": "csr_other",
        })
        self.assertEqual(reset.json()["etag"], '"builtin:1:2"')
        self.assertIn(("builtin-reset", "person_name", '"builtin:1:1"', "csr_other"), self.fake.calls)

        revision = "sensitive-detector-v2:" + "a" * 24
        self.assertEqual(self.client.post(PREFIX + "/rollout/start", json={
            "requestId": "rollout-0001", "expectedDetectorRevision": revision,
            "confirmHistoricalScan": False,
        }).status_code, 422)
        started = self.client.post(PREFIX + "/rollout/start", json={
            "requestId": "rollout-0001", "expectedDetectorRevision": revision,
            "confirmHistoricalScan": True,
        })
        self.assertEqual(started.status_code, 202)
        self.assertIn(("rollout-start", revision, "rollout-0001"), self.fake.calls)


if __name__ == "__main__":
    unittest.main()
