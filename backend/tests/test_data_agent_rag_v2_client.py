from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from zhijun_worker.data_agent_rag_v2 import (
    MAX_FILE_BYTES,
    DataAgentRagV2Client,
    DataAgentRagV2Error,
    configured_client,
)


APP_ID = "agc_0123456789abcdef"
APP_SECRET = "A" * 43


class _Headers(dict):
    def get_content_charset(self, default=None):
        return "utf-8"


class _Response(io.BytesIO):
    def __init__(self, status: int, body: bytes, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = _Headers(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _success(data: dict, trace_id: str = "atr_test") -> bytes:
    return json.dumps({"traceId": trace_id, "data": data}).encode()


def _accepted(job_id: str) -> dict:
    return {"jobId": job_id, "materialId": "mindos_material_1", "materialVersion": 1,
            "statusUrl": "/v1/agent/apps/material-jobs/" + job_id}


def _job(job_id: str, *, state="processing", stage="indexing", index_state="building") -> dict:
    ready = state == "ready"
    return {"jobId": job_id, "materialId": "mindos_material_1", "materialVersion": 1,
            "state": state, "stage": stage, "indexState": index_state,
            "progress": {"parsedCharacters": 100, "chunkCount": 2,
                         "indexedChunks": 2 if ready else 1},
            "allowedActions": [] if ready else ["poll"], "latestMaterialVersion": None,
            "updateInProgress": False, "errorCode": None, "failureStage": None,
            "retryable": None}


def _rule(rule_id="csr_rule_1", *, revision=1, source="custom") -> dict:
    result = {
        "ruleId": rule_id, "source": source, "immutable": source == "built_in",
        "revision": revision, "name": "项目代号", "description": "能够识别尚未公开的内部项目代号信息。",
        "examples": ["灯塔计划"], "counterExamples": ["普通项目"], "enabled": True,
        "deliveryMode": "confirm", "allowOriginalAfterConfirm": True,
        "masking": {"strategy": "fixed", "prefixCharacters": 0,
                    "suffixCharacters": 0, "replacement": "[项目代号已脱敏]"},
        "createdAt": 1.0, "updatedAt": 1.0,
    }
    if source == "built_in":
        result.update({
            "displayName": "项目代号", "editable": True, "deletable": False,
            "resettable": True, "overridden": False, "overrideApplied": False,
            "overrideNeedsReview": False, "definitionRevision": 1,
            "overrideBaseDefinitionRevision": 1, "overrideRevision": 0,
            "recognizerKind": "semantic", "recognizerRevision": 1,
            "editableFields": ["displayName", "enabled", "deliveryMode", "masking"],
            "systemConstraints": {"requiredEnabled": False,
                                  "minimumDeliveryMode": "confirm",
                                  "maskingFloor": result["masking"].copy()},
        })
    return result


def _rule_input(**updates) -> dict:
    value = {
        "name": "项目代号", "description": "能够识别尚未公开的内部项目代号信息。",
        "examples": ["灯塔计划"], "counterExamples": ["普通项目"], "enabled": True,
        "deliveryMode": "confirm", "allowOriginalAfterConfirm": True,
    }
    value.update(updates)
    return value


def _catalogue(items: list[dict]) -> dict:
    builtin = sum(item["source"] == "built_in" for item in items)
    custom = len(items) - builtin
    return {
        "items": items, "total": len(items), "builtinCount": builtin,
        "customCount": custom, "maxCustomRules": 30,
        "enabledRuleCount": sum(item["enabled"] for item in items),
        "detectorPromptTokens": 100, "detectorPromptTokenLimit": 1600,
        "detectorPromptWithinLimit": True, "detectorPromptTokensRemaining": 1500,
        "epoch": 2, "detectorRevision": "sensitive-detector-v2:test",
    }


class DataAgentRagV2ClientTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.credential_file = Path(self.temp.name) / "credentials.json"
        self.credential_file.write_text(
            json.dumps({"appId": APP_ID, "appSecret": APP_SECRET}), encoding="utf-8"
        )
        self.credential_file.chmod(0o600)

    def client(self, responses):
        opener = _Opener(responses)
        client = DataAgentRagV2Client(
            "http://127.0.0.1:8618", str(self.credential_file), opener=opener
        )
        return client, opener

    def test_credentials_require_absolute_regular_exact_unique_contract(self):
        with self.assertRaisesRegex(DataAgentRagV2Error, "ABSOLUTE_REGULAR"):
            DataAgentRagV2Client("http://127.0.0.1:8618", "relative.json")

        extra = Path(self.temp.name) / "extra.json"
        extra.write_text(
            json.dumps({"appId": APP_ID, "appSecret": APP_SECRET, "extra": True}),
            encoding="utf-8",
        )
        extra.chmod(0o600)
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_CREDENTIAL_FILE"):
            DataAgentRagV2Client("http://127.0.0.1:8618", str(extra))

        duplicate = Path(self.temp.name) / "duplicate.json"
        duplicate.write_text(
            '{"appId":"%s","appId":"%s","appSecret":"%s"}'
            % (APP_ID, APP_ID, APP_SECRET),
            encoding="utf-8",
        )
        duplicate.chmod(0o600)
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_CREDENTIAL_FILE"):
            DataAgentRagV2Client("http://127.0.0.1:8618", str(duplicate))

        link = Path(self.temp.name) / "link.json"
        link.symlink_to(self.credential_file)
        with self.assertRaisesRegex(DataAgentRagV2Error, "ABSOLUTE_REGULAR"):
            DataAgentRagV2Client("http://127.0.0.1:8618", str(link))

        exposed = Path(self.temp.name) / "exposed.json"
        exposed.write_bytes(self.credential_file.read_bytes())
        exposed.chmod(0o644)
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_CREDENTIAL_FILE"):
            DataAgentRagV2Client("http://127.0.0.1:8618", str(exposed))

    def test_base_url_requires_https_except_loopback(self):
        with self.assertRaisesRegex(DataAgentRagV2Error, "HTTPS_REQUIRED"):
            DataAgentRagV2Client("http://data-agent.internal:8618", str(self.credential_file))
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_BASE_URL"):
            DataAgentRagV2Client("https://user@example.com/path", str(self.credential_file))
        DataAgentRagV2Client("http://[::1]:8618", str(self.credential_file))
        DataAgentRagV2Client("https://data-agent.example", str(self.credential_file))

    def test_environment_resolution_prefers_explicit_values(self):
        secrets_dir = Path(self.temp.name) / "secrets"
        secrets_dir.mkdir()
        fallback = secrets_dir / "data-agent-rag-v2.json"
        fallback.write_bytes(self.credential_file.read_bytes())
        fallback.chmod(0o600)
        env = {
            "ZHIJUN_DATA_AGENT_BASE_URL": "https://data-agent.example",
            "ZHIJUN_DATA_AGENT_CREDENTIAL_FILE": str(self.credential_file),
            "CENTAUR_SECRET_STORE_DIR": str(secrets_dir),
            "ZHIJUN_CAPABILITY_URL": "http://127.0.0.1:9999/internal/zhijun/capabilities",
        }
        client = configured_client(env)
        self.assertEqual(client.base_url, "https://data-agent.example")
        self.assertEqual(client.credential_file, self.credential_file)

        fallback_client = configured_client(
            {
                "CENTAUR_SECRET_STORE_DIR": str(secrets_dir),
                "ZHIJUN_CAPABILITY_URL": "http://127.0.0.1:9999/internal/zhijun/capabilities",
            }
        )
        self.assertEqual(fallback_client.base_url, "http://127.0.0.1:9999")
        self.assertEqual(fallback_client.credential_file, fallback)

    def test_capabilities_returns_data_and_retains_trace_id(self):
        client, opener = self.client([_Response(200, _success({"capabilities": ["mindos.search"]}))])
        result = client.capabilities()
        self.assertEqual(result, {"capabilities": ["mindos.search"]})
        self.assertEqual(client.last_trace_id, "atr_test")
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8618/v1/agent/apps/capabilities")
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("X-app-id"), APP_ID)
        self.assertEqual(request.get_header("X-app-secret"), APP_SECRET)
        self.assertEqual(timeout, 180)

    def test_search_confirm_risk_and_evidence_requests_match_contract(self):
        client, opener = self.client([_Response(200, _success({})) for _ in range(4)])
        client.search("  最终检索词  ", "chat-1:turn-2", top_k=5, material_ids=["mindos_1"])
        client.confirm("scf_" + "x" * 32, desensitize=True, idempotency_key="confirm-key-0001")
        client.confirm_unverified("scf_" + "r" * 32)
        client.evidence_resolve(["erv2_first", "erv2_second"])

        search = opener.requests[0][0]
        self.assertEqual(search.full_url, "http://127.0.0.1:8618/v1/agent/apps/search")
        self.assertEqual(
            json.loads(search.data),
            {
                "query": "最终检索词",
                "topK": 5,
                "types": ["material"],
                "clientContext": {"interactionId": "chat-1:turn-2"},
                "filters": {"materialIds": ["mindos_1"]},
            },
        )
        confirm = opener.requests[1][0]
        self.assertEqual(confirm.get_header("Idempotency-key"), "confirm-key-0001")
        self.assertEqual(json.loads(confirm.data)["desensitize"], True)
        risk = opener.requests[2][0]
        self.assertIsNone(risk.get_header("Idempotency-key"))
        self.assertEqual(
            json.loads(risk.data),
            {
                "confirmToken": "scf_" + "r" * 32,
                "decision": "release_unverified_original",
                "acknowledgeRisk": True,
            },
        )
        resolve = opener.requests[3][0]
        self.assertEqual(json.loads(resolve.data), {"evidenceRefs": ["erv2_first", "erv2_second"]})

    def test_upload_bytes_builds_safe_multipart_and_validates_size(self):
        capabilities = {"enabledCapabilities": ["mindos.import"],
                        "supportedFileTypes": ["pdf", "txt"],
                        "maxFileBytes": MAX_FILE_BYTES}
        client, opener = self.client([
            _Response(200, _success(capabilities)),
            _Response(202, _success(_accepted("aij_" + "a" * 32))),
            _Response(200, _success(capabilities)),
            _Response(200, _success(capabilities)),
        ])
        result = client.upload_bytes(
            "报告 2026.pdf",
            b"synthetic-pdf",
            idempotency_key="upload-key-0001",
            title="报告",
            external_reference="chat-1",
        )
        self.assertEqual(result["jobId"], "aij_" + "a" * 32)
        request = opener.requests[1][0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8618/v1/agent/apps/material-jobs")
        self.assertIn("multipart/form-data; boundary=", request.get_header("Content-type"))
        self.assertIn(b'name="file"', request.data)
        self.assertIn(b"filename*=UTF-8''%E6%8A%A5%E5%91%8A%202026.pdf", request.data)
        self.assertIn(b"synthetic-pdf", request.data)
        with self.assertRaisesRegex(DataAgentRagV2Error, "UPLOAD_FILE_TYPE_OR_SIZE_INVALID"):
            client.upload_bytes("x.exe", b"x", idempotency_key="upload-key-0002")
        with self.assertRaisesRegex(DataAgentRagV2Error, "UPLOAD_FILE_TYPE_OR_SIZE_INVALID"):
            client.upload_bytes("x.txt", b"x" * (MAX_FILE_BYTES + 1), idempotency_key="upload-key-0003")

    def test_job_status_and_wait_indexed_use_status_url_contract(self):
        job_id = "aij_" + "b" * 32
        client, opener = self.client(
            [
                _Response(200, _success(_job(job_id))),
                _Response(200, _success(_job(job_id, state="ready", stage="completed", index_state="indexed"))),
            ]
        )
        with patch("zhijun_worker.data_agent_rag_v2.time.sleep") as sleeping:
            result = client.wait_indexed(job_id, timeout=10, poll_interval=0.1)
        self.assertEqual(result["indexState"], "indexed")
        sleeping.assert_called_once_with(0.1)
        self.assertTrue(all(job_id in item[0].full_url for item in opener.requests))

    def test_upload_and_job_status_reject_incoherent_contracts(self):
        capabilities = {"enabledCapabilities": ["mindos.import"],
                        "supportedFileTypes": ["txt"], "maxFileBytes": MAX_FILE_BYTES}
        client, _ = self.client([
            _Response(200, _success(capabilities)),
            _Response(202, _success({"jobId": "aij_" + "a" * 32})),
        ])
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_UPLOAD_ACCEPTED_RESPONSE"):
            client.upload_bytes("x.txt", b"x", idempotency_key="upload-key-invalid")

        job_id = "aij_" + "c" * 32
        invalid = _job(job_id, state="ready", stage="parsing", index_state="indexed")
        client, _ = self.client([_Response(200, _success(invalid))])
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_JOB_STATUS_RESPONSE"):
            client.job_status(job_id)

        client, _ = self.client([_Response(200, _success(_job("aij_" + "d" * 32)))])
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_JOB_STATUS_RESPONSE"):
            client.job_status(job_id)

    def test_http_error_has_stable_metadata_and_retry_after(self):
        body = json.dumps(
            {
                "traceId": "atr_failed",
                "error": {
                    "code": "SENSITIVE_CHECK_UNAVAILABLE",
                    "message": "temporary failure",
                    "retryable": True,
                },
            }
        ).encode()
        error = HTTPError(
            "http://127.0.0.1:8618/v1/agent/apps/search",
            503,
            "Unavailable",
            _Headers({"Retry-After": "17"}),
            io.BytesIO(body),
        )
        client, _ = self.client([error])
        with self.assertRaises(DataAgentRagV2Error) as caught:
            client.search("question", "interaction-1")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, "SENSITIVE_CHECK_UNAVAILABLE")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after, 17)
        self.assertEqual(caught.exception.trace_id, "atr_failed")

    def test_rejects_duplicate_json_oversize_and_secret_reflection(self):
        duplicate = b'{"traceId":"atr_x","data":{},"data":{}}'
        client, _ = self.client([_Response(200, duplicate)])
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_JSON_RESPONSE"):
            client.capabilities()

        client, _ = self.client([_Response(200, _success({"value": APP_SECRET}))])
        with self.assertRaisesRegex(DataAgentRagV2Error, "SECRET_REFLECTION_BLOCKED"):
            client.capabilities()

    def test_transport_uses_proxy_free_redirect_refusing_opener(self):
        with patch("zhijun_worker.data_agent_rag_v2.urllib.request.build_opener") as build:
            build.return_value = _Opener([])
            DataAgentRagV2Client("http://127.0.0.1:8618", str(self.credential_file))
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        with self.assertRaisesRegex(DataAgentRagV2Error, "REDIRECT_REFUSED"):
            handlers[1].redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid")

    def test_sensitive_rule_crud_owns_secret_etag_and_if_match_headers(self):
        created = _rule()
        updated = _rule(revision=2)
        client, opener = self.client([
            _Response(201, _success(created), {"ETag": '"1"'}),
            _Response(200, _success(updated), {"ETag": '"2"'}),
            _Response(200, _success({"deleted": True})),
        ])
        create_result = client.create_sensitive_rule(
            _rule_input(), idempotency_key="rule-create-0001"
        )
        update_result = client.update_sensitive_rule(
            "csr_rule_1", _rule_input(name=" 项目代号 "), etag='"1"',
        )
        self.assertEqual(client.delete_sensitive_rule("csr_rule_1", etag='"2"'), {"deleted": True})

        self.assertEqual(create_result["revision"], 1)
        self.assertEqual(create_result["etag"], '"1"')
        self.assertNotIn("createdAt", create_result)
        self.assertEqual(update_result["revision"], 2)
        self.assertEqual(update_result["etag"], '"2"')
        create, update, delete = [entry[0] for entry in opener.requests]
        self.assertEqual(create.get_header("Idempotency-key"), "rule-create-0001")
        self.assertEqual(create.get_header("X-app-secret"), APP_SECRET)
        self.assertIsNone(create.get_header("If-match"))
        self.assertEqual(update.get_header("If-match"), '"1"')
        self.assertIsNone(update.get_header("Idempotency-key"))
        self.assertEqual(delete.get_header("If-match"), '"2"')
        self.assertEqual(json.loads(update.data)["name"], "项目代号")

    def test_sensitive_rule_get_and_list_validate_and_sanitize_contract(self):
        custom = _rule()
        builtin = _rule("mobile_phone", source="built_in")
        builtin["aliases"] = ["手机号"]
        client, opener = self.client([
            _Response(200, _success(custom), {"ETag": '"1"'}),
            _Response(200, _success(_catalogue([builtin, custom]))),
        ])
        item = client.get_sensitive_rule("csr_rule_1")
        catalogue = client.list_sensitive_rules()
        self.assertEqual(item["ruleId"], "csr_rule_1")
        self.assertNotIn("createdAt", item)
        self.assertEqual(catalogue["items"][0]["aliases"], ["手机号"])
        self.assertEqual(catalogue["items"][0]["editableFields"],
                         ["displayName", "enabled", "deliveryMode", "masking"])
        self.assertEqual(catalogue["customCount"], 1)
        self.assertTrue(opener.requests[0][0].full_url.endswith("/sensitive-delivery/rules/csr_rule_1"))

    def test_sensitive_rule_input_and_upstream_contract_are_strict(self):
        client, opener = self.client([])
        for invalid in (
            _rule_input(extra=True),
            _rule_input(enabled=1),
            _rule_input(deliveryMode="block", allowOriginalAfterConfirm=True),
            _rule_input(examples=[]),
        ):
            with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_SENSITIVE_RULE_INPUT"):
                client.create_sensitive_rule(invalid, idempotency_key="rule-create-0001")
        self.assertEqual(opener.requests, [])

        client, _ = self.client([_Response(200, _success(_rule()), {"ETag": 'W/"1"'})])
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_SENSITIVE_RULE_ETAG"):
            client.get_sensitive_rule("csr_rule_1")

        bad_catalogue = _catalogue([_rule()])
        bad_catalogue["customCount"] = 2
        client, _ = self.client([_Response(200, _success(bad_catalogue))])
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_SENSITIVE_RULE_CATALOGUE_RESPONSE"):
            client.list_sensitive_rules()

    def test_sensitive_rule_similar_conflict_exposes_only_validated_rule_id(self):
        body = json.dumps({"traceId": "atr_similar", "error": {
            "code": "CUSTOM_RULE_SIMILAR", "message": "internal similarity detail",
            "retryable": False,
        }}).encode()
        valid = HTTPError("http://127.0.0.1", 409, "Conflict",
                          _Headers({"X-Similar-Rule-Id": "csr_existing"}), io.BytesIO(body))
        client, _ = self.client([valid])
        with self.assertRaises(DataAgentRagV2Error) as caught:
            client.create_sensitive_rule(_rule_input(), idempotency_key="rule-create-0001")
        self.assertEqual(caught.exception.similar_rule_id, "csr_existing")

        invalid = HTTPError("http://127.0.0.1", 409, "Conflict",
                            _Headers({"X-Similar-Rule-Id": "bad\r\nheader"}), io.BytesIO(body))
        client, _ = self.client([invalid])
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_SIMILAR_RULE_RESPONSE"):
            client.create_sensitive_rule(_rule_input(), idempotency_key="rule-create-0002")

    def test_sensitive_capabilities_only_exposes_effective_rule_permissions(self):
        client, opener = self.client([_Response(200, _success({
            "enabledCapabilities": ["mindos.read", "mindos.sensitive.policy.write",
                                    "mindos.sensitive.policy.builtin.write",
                                    "mindos.sensitive.rollout.manage"],
            "secretInternalSetting": "not-for-renderer",
        }))])
        self.assertEqual(client.get_sensitive_capabilities(), {
            "policyWrite": True, "builtinWrite": True, "rolloutManage": True,
        })
        self.assertEqual(opener.requests[0][0].method, "GET")
        client, _ = self.client([_Response(200, _success({
            "enabledCapabilities": ["mindos.sensitive.policy.builtin.write",
                                    "mindos.sensitive.rollout.manage"],
        }))])
        self.assertEqual(client.get_sensitive_capabilities(), {
            "policyWrite": False, "builtinWrite": False, "rolloutManage": False,
        })

    def test_builtin_rule_uses_opaque_etag_and_reset_acknowledgement_header(self):
        current = _rule("person_name", source="built_in")
        changed = _rule("person_name", source="built_in")
        changed["overrideRevision"] = 1
        changed["overridden"] = True
        changed["overrideApplied"] = True
        changed["changeImpact"] = "semantic_detection"
        changed["historicalScanRequired"] = True
        reset = _rule("person_name", source="built_in")
        reset["overrideRevision"] = 2
        client, opener = self.client([
            _Response(200, _success(current), {"ETag": '"builtin:1:0"'}),
            _Response(200, _success(changed), {"ETag": '"builtin:1:1"'}),
            _Response(200, _success(reset), {"ETag": '"builtin:1:2"'}),
        ])
        self.assertEqual(client.get_sensitive_rule("person_name")["etag"], '"builtin:1:0"')
        changed_result = client.update_builtin_sensitive_rule(
            "person_name", {"enabled": False}, etag='"builtin:1:0"',
        )
        self.assertEqual(changed_result["etag"], '"builtin:1:1"')
        self.assertEqual(changed_result["changeImpact"], "semantic_detection")
        self.assertTrue(changed_result["historicalScanRequired"])
        reset_result = client.reset_builtin_sensitive_rule(
            "person_name", etag='"builtin:1:1"',
            acknowledge_similar_rule_id="csr_similar",
        )
        self.assertEqual(reset_result["etag"], '"builtin:1:2"')
        put, post = [entry[0] for entry in opener.requests[1:]]
        self.assertTrue(put.full_url.endswith("/rules/built-in/person_name"))
        self.assertEqual(put.get_header("If-match"), '"builtin:1:0"')
        self.assertEqual(json.loads(put.data), {"enabled": False})
        self.assertTrue(post.full_url.endswith("/rules/built-in/person_name:reset"))
        self.assertEqual(post.get_header("If-match"), '"builtin:1:1"')
        self.assertEqual(post.get_header("X-acknowledge-similar-rule-id"), "csr_similar")
        self.assertIsNone(post.data)

    def test_builtin_rule_rejects_invalid_etags_fields_and_mismatched_response(self):
        client, opener = self.client([])
        for etag in ('"1"', '"builtin:1:0"\r\nX-Hijack: yes', 'W/"builtin:1:0"'):
            with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_SENSITIVE_RULE_ETAG"):
                client.update_builtin_sensitive_rule("person_name", {"enabled": False}, etag=etag)
        for payload in ({"ruleId": "person_name"}, {"masking": {"strategy": "full"}},
                        {"enabled": "false"}):
            with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_SENSITIVE_RULE_INPUT"):
                client.update_builtin_sensitive_rule(
                    "person_name", payload, etag='"builtin:1:0"')
        self.assertEqual(opener.requests, [])

        current = _rule("person_name", source="built_in")
        client, _ = self.client([_Response(200, _success(current),
                                          {"ETag": '"builtin:2:0"'})])
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_SENSITIVE_RULE_ETAG"):
            client.get_sensitive_rule("person_name")

    def test_rollout_status_start_retry_use_latest_revision_and_idempotency(self):
        revision = "sensitive-detector-v2:" + "a" * 24
        pending = {"state": "pending", "scanEnabled": True, "applying": False,
                   "historicalScanRequired": True, "retryAvailable": False,
                   "targetDetectorRevision": revision}
        applying = {**pending, "state": "applying", "applying": True,
                    "historicalScanRequired": False}
        failed = {**pending, "state": "failed", "historicalScanRequired": False,
                  "retryAvailable": True}
        client, opener = self.client([
            _Response(200, _success(pending)),
            _Response(202, _success(applying)),
            _Response(202, _success(failed)),
        ])
        self.assertEqual(client.get_sensitive_rollout_status(), pending)
        self.assertEqual(client.start_sensitive_rollout(
            revision, idempotency_key="rollout-start-0001"), applying)
        self.assertEqual(client.retry_sensitive_rollout(
            revision, idempotency_key="rollout-retry-0001"), failed)
        start, retry = [entry[0] for entry in opener.requests[1:]]
        self.assertTrue(start.full_url.endswith("/sensitive-delivery/rollout:start"))
        self.assertEqual(start.get_header("Idempotency-key"), "rollout-start-0001")
        self.assertEqual(json.loads(start.data), {
            "expectedDetectorRevision": revision, "confirmHistoricalScan": True,
        })
        self.assertTrue(retry.full_url.endswith("/sensitive-delivery/rollout:retry"))
        self.assertEqual(retry.get_header("Idempotency-key"), "rollout-retry-0001")
        self.assertEqual(json.loads(retry.data), {
            "expectedDetectorRevision": revision, "confirmRetry": True,
        })

    def test_rollout_rejects_untrusted_revision_or_response(self):
        client, opener = self.client([])
        with self.assertRaisesRegex(DataAgentRagV2Error, "INVALID_SENSITIVE_ROLLOUT_REVISION"):
            client.start_sensitive_rollout("sensitive-detector-v2:wrong",
                                           idempotency_key="rollout-start-0001")
        self.assertEqual(opener.requests, [])
        bad = {"state": "failed", "scanEnabled": True, "applying": False,
               "historicalScanRequired": False, "retryAvailable": False,
               "targetDetectorRevision": "sensitive-detector-v2:" + "a" * 24}
        client, _ = self.client([_Response(200, _success(bad))])
        with self.assertRaisesRegex(DataAgentRagV2Error,
                                    "INVALID_SENSITIVE_ROLLOUT_STATUS_RESPONSE"):
            client.get_sensitive_rollout_status()


if __name__ == "__main__":
    unittest.main()
