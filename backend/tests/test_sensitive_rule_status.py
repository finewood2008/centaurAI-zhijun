from __future__ import annotations

import io
import json
import unittest
from unittest.mock import Mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mindos import sensitive_rule_routes as routes
from zhijun_worker.data_agent_rag_v2 import DataAgentRagV2Client, DataAgentRagV2Error


PREFIX = "/api/mindos/settings/sensitive-rules"


class Response(io.BytesIO):
    def __init__(self, data=None, *, error=None, status=200, headers=None):
        envelope = {"traceId": "atr_status", "error" if error else "data": error or data}
        super().__init__(json.dumps(envelope).encode())
        self.status = status
        self.headers = headers or {"Cache-Control": "no-store"}


def client_with(response):
    client = object.__new__(DataAgentRagV2Client)
    client.base_url = "http://127.0.0.1:8618"
    client._credentials = {"appId": "agc_0123456789abcdef", "appSecret": "A" * 43}
    client.timeout = 10
    client._opener = Mock()
    client._opener.open.return_value = response
    return client


class SensitiveRuleStatusClientTest(unittest.TestCase):
    def test_only_public_status_get_is_requested_with_existing_credentials(self):
        for state in ("active", "applying"):
            expected = {"state": state, "applying": state == "applying"}
            client = client_with(Response(expected))
            self.assertEqual(client.get_sensitive_rule_status(), expected)
            self.assertEqual(client._opener.open.call_count, 1)
            request = client._opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:8618/v1/agent/apps/sensitive-delivery/status")
            self.assertEqual(request.method, "GET")
            self.assertIsNone(request.data)
            self.assertEqual(request.get_header("X-app-id"), "agc_0123456789abcdef")
            self.assertIsNone(request.get_header("Idempotency-key"))

    def test_response_is_exact_and_coherent(self):
        malformed = [
            {}, {"state": "active"}, {"state": "active", "applying": 0},
            {"state": "active", "applying": True},
            {"state": "applying", "applying": False},
            {"state": "failed", "applying": False},
            {"state": ["active"], "applying": False},
            {"state": "active", "applying": False, "queueDepth": 5},
        ]
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_SENSITIVE_RULE_STATUS_RESPONSE"):
                    client_with(Response(value)).get_sensitive_rule_status()

    def test_status_errors_retain_retry_after_without_retry_or_capability_escalation(self):
        client = client_with(Response(error={
            "code": "SENSITIVE_RULE_STATUS_UNAVAILABLE", "message": "unavailable", "retryable": True,
        }, status=503, headers={"Retry-After": "90"}))
        with self.assertRaises(DataAgentRagV2Error) as caught:
            client.get_sensitive_rule_status()
        self.assertEqual(caught.exception.retry_after, 90)
        self.assertEqual(caught.exception.trace_id, "atr_status")
        self.assertEqual(client._opener.open.call_count, 1)


class SensitiveRuleStatusFacadeTest(unittest.TestCase):
    def setUp(self):
        self.fake = Mock()
        self.fake.get_sensitive_rule_status.return_value = {"state": "applying", "applying": True}
        self.fake.list_sensitive_rules.return_value = {"items": [], "maxCustomRules": 4}
        previous = routes._client_override
        routes._client_override = self.fake
        self.addCleanup(setattr, routes, "_client_override", previous)
        app = FastAPI()
        app.include_router(routes.build_router())
        self.client = TestClient(app)

    def test_status_route_precedes_rule_id_and_has_no_store(self):
        response = self.client.get(PREFIX + "/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"state": "applying", "applying": True})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.fake.get_sensitive_rule.assert_not_called()

    def test_facade_rejects_internal_fields_even_if_client_is_replaced(self):
        self.fake.get_sensitive_rule_status.return_value = {
            "state": "active", "applying": False, "internalRevision": 7,
        }
        response = self.client.get(PREFIX + "/status")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("internalRevision", response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_optional_failure_is_private_and_does_not_disable_crud(self):
        for status, code in ((403, "CAPABILITY_DENIED"), (503, "SENSITIVE_RULE_STATUS_UNAVAILABLE")):
            self.fake.get_sensitive_rule_status.side_effect = DataAgentRagV2Error(
                code, status=status, retry_after=90, trace_id="atr_status", message="internal model failure",
            )
            response = self.client.get(PREFIX + "/status")
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Retry-After"], "90")
            self.assertEqual(response.json()["detail"]["retryAfter"], 90)
            self.assertNotIn("internal model failure", response.text)
            self.assertEqual(self.client.get(PREFIX).status_code, 200)

    def test_long_retry_after_passes_from_upstream_client_through_facade(self):
        for header, expected in (
            ("90000", 90000), ("604800", 604800), ("2147484", 2147484),
            ("9" * 5000, 9_007_199_254_740_991),
        ):
            with self.subTest(expected=expected):
                routes._client_override = client_with(Response(error={
                    "code": "SENSITIVE_RULE_STATUS_UNAVAILABLE", "message": "unavailable", "retryable": True,
                }, status=503, headers={"Retry-After": header}))
                response = self.client.get(PREFIX + "/status")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["Retry-After"], str(expected))
                self.assertEqual(response.json()["detail"]["retryAfter"], expected)

    def test_facade_preserves_direct_long_delay_and_rejects_invalid_types(self):
        for value, expected in ((90000, 90000), (10**30, 9_007_199_254_740_991), (-1, None), (True, None)):
            with self.subTest(value=value):
                self.fake.get_sensitive_rule_status.side_effect = DataAgentRagV2Error(
                    "SENSITIVE_RULE_STATUS_UNAVAILABLE", status=503, retry_after=value,
                )
                response = self.client.get(PREFIX + "/status")
                self.assertEqual(response.json()["detail"].get("retryAfter"), expected)
                self.assertEqual(response.headers.get("Retry-After"), str(expected) if expected is not None else None)

    def test_status_is_read_only_and_never_uses_mutation_guard(self):
        def deny():
            raise HTTPException(403, "write denied")

        app = FastAPI()
        app.include_router(routes.build_router(deny))
        self.assertEqual(TestClient(app).get(PREFIX + "/status").status_code, 200)


if __name__ == "__main__":
    unittest.main()
