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
        "revision": revision, **_input(),
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

    def get_sensitive_rule(self, rule_id):
        self.calls.append(("get", rule_id))
        return self._return(_item())

    def create_sensitive_rule(self, rule, *, idempotency_key):
        self.calls.append(("create", rule, idempotency_key))
        return self._return(_item())

    def update_sensitive_rule(self, rule_id, rule, *, revision):
        self.calls.append(("update", rule_id, rule, revision))
        return self._return(_item(revision + 1))

    def delete_sensitive_rule(self, rule_id, *, revision):
        self.calls.append(("delete", rule_id, revision))
        return self._return({"deleted": True})


class SensitiveRuleRoutesTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClient()
        routes._client_override = self.fake
        self.addCleanup(setattr, routes, "_client_override", None)
        app = FastAPI()
        app.include_router(routes.build_router())
        self.client = TestClient(app)

    def test_list_get_and_crud_expose_revision_not_transport_headers(self):
        self.assertEqual(self.client.get(PREFIX).status_code, 200)
        self.assertEqual(self.client.get(PREFIX + "/csr_rule_1").json()["revision"], 1)

        create_body = {"requestId": "rule-create-0001", "rule": _input()}
        created = self.client.post(PREFIX + "/custom", json=create_body)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["revision"], 1)
        self.assertNotIn("ETag", created.headers)

        updated = self.client.put(PREFIX + "/custom/csr_rule_1", json={
            "requestId": "rule-update-0001", "expectedRevision": 1, "rule": _input(),
        })
        self.assertEqual(updated.json()["revision"], 2)
        deleted = self.client.request(
            "DELETE", PREFIX + "/custom/csr_rule_1", json={"expectedRevision": 2},
        )
        self.assertEqual(deleted.json(), {"deleted": True})

        create_call = next(call for call in self.fake.calls if call[0] == "create")
        self.assertEqual(create_call[2], "rule-create-0001")
        update_call = next(call for call in self.fake.calls if call[0] == "update")
        self.assertEqual(update_call[3], 1)

    def test_renderer_input_is_strict_and_never_accepts_raw_if_match(self):
        valid = {"requestId": "rule-update-0001", "expectedRevision": 1, "rule": _input()}
        self.assertEqual(self.client.put(PREFIX + "/custom/csr_rule_1", json={**valid, "extra": True}).status_code, 422)
        self.assertEqual(self.client.put(PREFIX + "/custom/csr_rule_1", json={**valid, "expectedRevision": True}).status_code, 422)
        self.assertEqual(self.client.request(
            "DELETE", PREFIX + "/custom/csr_rule_1",
            json={"expectedRevision": 1, "ifMatch": '"1"'}
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


if __name__ == "__main__":
    unittest.main()
