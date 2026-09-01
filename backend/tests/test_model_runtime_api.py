"""模型运行时管理 API 路由契约测试（P1 §6.1 / §6.2 / §6.4）。

- 独立 FastAPI app：GET/PUT/test 往返、409 冲突、400 校验、脱敏投影；
- 真实 server.require_local：缺 CSRF 头 / 非 loopback 被拒（验收 8）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from mindos import llm_transport, model_runtime as mr, runtime_config_provider as rcp
from mindos.secret_store import MemorySecretStore
from mindos.stores import model_job_store as mjs
from mindos.stores.runtime_settings_store import RuntimeSettingsStore, reset_for_tests

_QA_URL = "http://127.0.0.1:8000/v1"


def _permissive_guard():
    return None


def _make_app() -> FastAPI:
    app = FastAPI()
    mr.configure_guards(_permissive_guard)
    app.include_router(mr.router)
    # 与 server.py 一致，注册统一错误响应 {code, message, details?}（§6.2.1）。
    mr.install_error_handlers(app)
    return app


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmp.name) / "rt.db")
        # 直接构造独立 store，避免污染全局单例（不影响真实 server.app 路由测试）。
        self.store = RuntimeSettingsStore(db_path=self._db)
        self.provider = rcp.RuntimeConfigProvider(
            store=self.store, secret_store=MemorySecretStore()
        )
        mr.set_runtime_provider(self.provider)
        self.app = _make_app()
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        mr.set_runtime_provider(None)
        self._tmp.cleanup()


class MaterialRuntimeApiTest(_Base):
    def test_get_defaults(self) -> None:
        resp = self.client.get("/api/system/models/material-runtime")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["revision"], 0)
        self.assertEqual(body["source"], "defaults")
        self.assertEqual(body["appliesTo"], ["summary", "entities", "relations", "tags", "contentDrafts", "wiki"])
        self.assertIn("health", body)

    def test_put_and_get_roundtrip(self) -> None:
        resp = self.client.put(
            "/api/system/models/material-runtime",
            json={"baseUrl": "http://127.0.0.1:11434", "model": "qwen3:8b", "timeoutSeconds": 120},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["revision"], 1)
        get = self.client.get("/api/system/models/material-runtime").json()
        self.assertEqual(get["source"], "runtime_settings")
        self.assertEqual(get["model"], "qwen3:8b")

    def test_put_invalid_url_rejected(self) -> None:
        resp = self.client.put(
            "/api/system/models/material-runtime",
            json={"baseUrl": "http://127.0.0.1:11434/api/chat", "model": "m", "timeoutSeconds": 120},
        )
        self.assertEqual(resp.status_code, 400)

    def test_put_unknown_field_rejected_422(self) -> None:
        resp = self.client.put(
            "/api/system/models/material-runtime",
            json={"baseUrl": "http://127.0.0.1:11434", "model": "m", "timeoutSeconds": 120, "extraField": 1},
        )
        self.assertEqual(resp.status_code, 422)

    def test_put_remote_address_is_accepted(self) -> None:
        resp = self.client.put(
            "/api/system/models/material-runtime",
            json={"baseUrl": "http://10.1.2.3:11434", "model": "m", "timeoutSeconds": 120},
        )
        self.assertEqual(resp.status_code, 200)

    def test_ssrf_protection_endpoint_is_removed(self) -> None:
        resp = self.client.put(
            "/api/system/models/security/ssrf-protection", json={"enabled": False}
        )
        self.assertEqual(resp.status_code, 404)

    def test_put_stale_revision_conflict(self) -> None:
        self.client.put(
            "/api/system/models/material-runtime",
            json={"baseUrl": "http://127.0.0.1:11434", "model": "m", "timeoutSeconds": 120},
        )
        resp = self.client.put(
            "/api/system/models/material-runtime",
            json={"baseUrl": "http://127.0.0.1:11434", "model": "m2", "timeoutSeconds": 120, "revision": 0},
        )
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body["code"], "conflict")
        self.assertIn("message", body)
        self.assertIn("details", body)
        self.assertIn("revision=1", body["details"])

    def test_put_missing_revision_after_existing_conflicts(self) -> None:
        self.client.put(
            "/api/system/models/material-runtime",
            json={"baseUrl": "http://127.0.0.1:11434", "model": "m", "timeoutSeconds": 120},
        )
        resp = self.client.put(
            "/api/system/models/material-runtime",
            json={"baseUrl": "http://127.0.0.1:11434", "model": "m2", "timeoutSeconds": 120},
        )
        self.assertEqual(resp.status_code, 409)

    def test_test_endpoint_candidate_not_persisted(self) -> None:
        fake = SimpleNamespace(
            read=lambda: b'{"models":[{"name":"qwen3:8b"}]}', status=200
        )
        with patch.object(llm_transport, "allowed_urlopen", return_value=fake) as m:
            resp = self.client.post(
                "/api/system/models/material-runtime/test",
                json={"baseUrl": "http://127.0.0.1:11434", "model": "qwen3:8b", "timeoutSeconds": 120},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["models"], ["qwen3:8b"])
        # 候选不持久化
        get = self.client.get("/api/system/models/material-runtime").json()
        self.assertEqual(get["source"], "defaults")
        self.assertEqual(get["model"], "qwen3:1.7b")
        self.assertNotEqual(get["model"], "qwen3:8b")

    def test_connectivity_test_requires_exact_ollama_model_name(self) -> None:
        fake = SimpleNamespace(
            read=lambda: b'{"models":[{"name":"qwen3-vl:2b:latest"}]}', status=200
        )
        with patch.object(llm_transport, "allowed_urlopen", return_value=fake) as request:
            resp = self.client.post(
                "/api/system/models/material-runtime/test",
                json={"baseUrl": "http://127.0.0.1:11434", "model": "qwen3-vl:2b", "timeoutSeconds": 120},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(resp.json()["errorCode"], "model_not_installed")
        self.assertEqual(resp.json()["models"], ["qwen3-vl:2b:latest"])
        self.assertIn("/api/tags", request.call_args.args[0])


class ChatProviderApiTest(_Base):
    def _put_openai(self, **kw) -> dict:
        body = {
            "provider": "openai",
            "externalEnabled": True,
            "baseUrl": _QA_URL,
            "model": "deepseek-chat",
            "timeoutSeconds": 60,
            "totalBudgetSeconds": 90,
            "fallbackOllama": True,
        }
        body.update(kw)
        return body

    def test_put_and_get_roundtrip(self) -> None:
        resp = self.client.put(
            "/api/system/models/chat-provider",
            json=self._put_openai(apiKey="sk-test"),
        )
        self.assertEqual(resp.status_code, 200)
        get = self.client.get("/api/system/models/chat-provider").json()
        self.assertTrue(get["apiKeyConfigured"])
        self.assertTrue(get["externalEnabled"])
        self.assertEqual(get["effectiveProvider"], "openai")
        self.assertNotIn("sk-test", str(get))  # 不泄露明文
        self.assertTrue(get["apiKeyHint"].endswith("test"))

    def test_put_enabled_without_key_rejected(self) -> None:
        resp = self.client.put(
            "/api/system/models/chat-provider",
            json=self._put_openai(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_put_unknown_field_rejected_422(self) -> None:
        resp = self.client.put(
            "/api/system/models/chat-provider",
            json=self._put_openai(apiKey="sk-test", extraField=1),
        )
        self.assertEqual(resp.status_code, 422)

    def test_ollama_provider_forces_external_off(self) -> None:
        resp = self.client.put(
            "/api/system/models/chat-provider",
            json={
                "provider": "ollama",
                "externalEnabled": True,
                "timeoutSeconds": 60,
                "totalBudgetSeconds": 90,
                "fallbackOllama": True,
            },
        )
        self.assertEqual(resp.status_code, 200)
        get = self.client.get("/api/system/models/chat-provider").json()
        self.assertFalse(get["externalEnabled"])
        self.assertEqual(get["effectiveProvider"], "ollama")

    def test_effective_provider_ollama_when_disabled(self) -> None:
        self.client.put(
            "/api/system/models/chat-provider",
            json=self._put_openai(apiKey="sk-test", externalEnabled=False),
        )
        get = self.client.get("/api/system/models/chat-provider").json()
        self.assertFalse(get["externalEnabled"])
        self.assertEqual(get["effectiveProvider"], "ollama")

    def test_put_stale_revision_conflict(self) -> None:
        self.client.put(
            "/api/system/models/chat-provider",
            json=self._put_openai(apiKey="sk-a"),
        )
        resp = self.client.put(
            "/api/system/models/chat-provider",
            json=self._put_openai(apiKey="sk-b", revision=0),
        )
        self.assertEqual(resp.status_code, 409)

    def test_test_endpoint_openai(self) -> None:
        fake = SimpleNamespace(read=lambda: b"{}", status=200)
        with patch.object(llm_transport, "allowed_urlopen", return_value=fake) as m:
            resp = self.client.post(
                "/api/system/models/chat-provider/test",
                json={
                    "provider": "openai",
                    "externalEnabled": True,
                    "baseUrl": _QA_URL,
                    "model": "deepseek-chat",
                    "apiKey": "sk-candidate",
                },
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        req = m.call_args
        self.assertIn("Bearer sk-candidate", req.kwargs["headers"]["Authorization"])

    def test_get_never_leaks_secret_ref(self) -> None:
        self.client.put(
            "/api/system/models/chat-provider",
            json=self._put_openai(apiKey="sk-test"),
        )
        get = self.client.get("/api/system/models/chat-provider").json()
        self.assertNotIn("secret_ref", str(get))
        self.assertNotIn("mindos_rt_", str(get))


class ModelJobsApiTest(_Base):
    """P2 模型任务路由：不访问真实 Ollama，只校验持久化任务契约。"""

    def setUp(self) -> None:
        super().setUp()
        self._job_db = Path(self._tmp.name) / "model_jobs.db"
        self._previous_job_db = mjs._DB_PATH
        mjs.ModelJobStore.reset()
        mjs._DB_PATH = self._job_db

    def tearDown(self) -> None:
        mjs.ModelJobStore.reset()
        mjs._DB_PATH = self._previous_job_db
        super().tearDown()

    def test_pull_returns_202_and_duplicate_returns_existing_job(self) -> None:
        first = self.client.post(
            "/api/system/models/material-runtime/pull", json={"model": "qwen3:1.7b"}
        )
        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.json()["deduplicated"])

        duplicate = self.client.post(
            "/api/system/models/material-runtime/pull", json={"model": "qwen3:1.7b"}
        )
        self.assertEqual(duplicate.status_code, 202)
        self.assertTrue(duplicate.json()["deduplicated"])
        self.assertEqual(duplicate.json()["jobId"], first.json()["jobId"])

    def test_list_lookup_and_cancel_contract(self) -> None:
        created = self.client.post(
            "/api/system/models/material-runtime/pull", json={"model": "qwen3:1.7b"}
        ).json()
        job_id = created["jobId"]

        listed = self.client.get("/api/system/models/jobs?limit=1")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"][0]["jobId"], job_id)
        self.assertIn("nextCursor", listed.json())

        cancelled = self.client.post(f"/api/system/models/jobs/{job_id}/cancel", json={})
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["state"], "cancelled")

        missing = self.client.get("/api/system/models/jobs/not-found")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "not_found")

    def test_models_projection_marks_running_model(self) -> None:
        tags = {"models": [{"name": "qwen3:1.7b", "size": 100}]}
        with patch.object(mr.ollama_client, "tags", return_value=tags), patch.object(
            mr.ollama_client, "running_models", return_value={"qwen3:1.7b"}
        ):
            response = self.client.get("/api/system/models/material-runtime/models")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["models"][0]["running"])


class ErrorContractTest(_Base):
    """§6.2.1 统一错误体契约：所有 system-models 错误响应形如 {code, message, details?}。

    前端按契约解析（code/message 必填，details 可选）。校验 handler 同时覆盖
    HTTPException（已带统一详情的规范异常）与 RequestValidationError（422）。
    """

    def _assert_contract(self, body: dict) -> None:
        self.assertIn("code", body)
        self.assertIn("message", body)
        self.assertTrue(isinstance(body["code"], str))
        self.assertTrue(isinstance(body["message"], str))
        self.assertNotIn("detail", body)  # 不再暴露 FastAPI 的 detail 顶层字段

    def test_invalid_config_400(self) -> None:
        resp = self.client.put(
            "/api/system/models/chat-provider",
            json={
                "provider": "openai",
                "externalEnabled": True,
                "baseUrl": _QA_URL,
                "model": "deepseek-chat",
                "timeoutSeconds": 60,
                "totalBudgetSeconds": 90,
                "fallbackOllama": True,
            },
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self._assert_contract(body)
        self.assertEqual(body["code"], "invalid_config")

    def test_conflict_409(self) -> None:
        self.client.put(
            "/api/system/models/material-runtime",
            json={"baseUrl": "http://127.0.0.1:11434", "model": "m", "timeoutSeconds": 120},
        )
        resp = self.client.put(
            "/api/system/models/material-runtime",
            json={"baseUrl": "http://127.0.0.1:11434", "model": "m2", "timeoutSeconds": 120, "revision": 0},
        )
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self._assert_contract(body)
        self.assertEqual(body["code"], "conflict")
        self.assertIsInstance(body.get("details"), list)

    def test_validation_error_422(self) -> None:
        resp = self.client.put(
            "/api/system/models/material-runtime",
            json={"baseUrl": "http://127.0.0.1:11434", "model": "m", "timeoutSeconds": 120, "bad": 1},
        )
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self._assert_contract(body)
        self.assertEqual(body["code"], "validation_error")
        self.assertTrue(any("bad" in d for d in body["details"]))

    def test_test_endpoint_failure_uses_error_code_contract(self) -> None:
        # 契约：失败响应统一带 errorCode（非 category），且用候选配置不持久化。
        with patch.object(
            llm_transport, "allowed_urlopen", side_effect=OSError("connect refused")
        ):
            resp = self.client.post(
                "/api/system/models/material-runtime/test",
                json={"baseUrl": "http://127.0.0.1:11434", "model": "qwen3:8b", "timeoutSeconds": 120},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["errorCode"], "connection")
        self.assertIn("model", body)
        self.assertIn("latencyMs", body)
        self.assertNotIn("category", body)
        # 候选未持久化
        get = self.client.get("/api/system/models/material-runtime").json()
        self.assertEqual(get["model"], "qwen3:1.7b")


class GuardTest(unittest.TestCase):
    """真实 require_local：缺 CSRF 头 / 非 loopback 拒绝（验收 8）。"""

    @classmethod
    def setUpClass(cls) -> None:
        import server

        # 恢复全局单例到隔离数据根默认库，避免被其他测试的临时 store 污染。
        reset_for_tests(None)
        rcp.reset_provider_for_tests()
        cls.client = TestClient(server.app)

    def test_get_without_csrf_rejected(self) -> None:
        resp = self.client.get("/api/system/models/material-runtime")
        self.assertEqual(resp.status_code, 403)

    def test_get_with_csrf_ok(self) -> None:
        resp = self.client.get(
            "/api/system/models/material-runtime",
            headers={"X-Requested-By": "centaur-vdb"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_require_local_rejects_non_loopback(self) -> None:
        from fastapi import HTTPException, Request

        import server

        req = Request({"type": "http", "client": ("203.0.113.5", 12345)})
        with self.assertRaises(HTTPException) as ctx:
            server.require_local(req, x_requested_by="centaur-vdb")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
