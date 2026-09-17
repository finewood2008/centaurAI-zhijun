"""Legacy non-workspace saved profiles retain their HTTP transport contracts.

Workspace factory authority is covered by test_workspace_capability_provider_entrypoints."""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mindos.zhijun import provider as providers
from mindos.zhijun.routing import EGRESS_PERMIT


class SavedProfileTransportTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "MINDOS_RUNTIME_ENV": "production",
            "ZHIJUN_PROVIDER": "",
            "ZHIJUN_OPENAI_BASE_URL": "https://wrong.invalid/v1",
            "ZHIJUN_OPENAI_MODEL": "wrong-model",
            "ZHIJUN_OPENAI_API_KEY": "wrong-secret",
            "ZHIJUN_OPENAI_TASK_MODEL": "wrong-task-model",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.local = SimpleNamespace(base_url="http://127.0.0.1:18134", model="npu-model",
                                     timeout_seconds=40, keep_alive=0, context_window=4096)
        self.snap = SimpleNamespace(provider="openai", external_enabled=True,
            base_url="https://workspace.invalid/v1", model="workspace-model", timeout_seconds=23,
            secret_ref="workspace-secret-ref", external_provider_id="saved-profile", local=self.local)
        self.runtime = Mock()
        self.runtime.get_chat_snapshot.return_value = self.snap
        self.runtime.resolve_api_key.return_value = "workspace-secret"
        factory = patch.object(providers, "get_provider", return_value=self.runtime)
        factory.start()
        self.addCleanup(factory.stop)
        # Non-workspace Web transport must not acquire a workspace capability.
        capabilities = patch("zhijun_worker.capabilities.require", side_effect=AssertionError("DE model must not be called"))
        capabilities.start()
        self.addCleanup(capabilities.stop)
        token = EGRESS_PERMIT.set(None)
        self.addCleanup(EGRESS_PERMIT.reset, token)
        self.req = providers.ChatRequest(system="system", messages=[{"role": "user", "content": "test"}])

    def test_saved_profile_does_not_borrow_ambient_endpoint_model_or_key(self):
        actual = providers.build_provider()
        self.assertIsInstance(actual, providers.OpenAICompatibleProvider)
        self.assertEqual(actual._base_url, self.snap.base_url)
        self.assertEqual(actual.model, "workspace-model")
        self.assertEqual(actual.task_model, "workspace-model")
        self.assertEqual(actual._api_key, "workspace-secret")
        self.assertEqual(actual._timeout, 23)
        self.assertRegex(actual.configuration_revision, r"^[a-f0-9]{64}$")

    def test_configuration_revision_is_stable_opaque_and_changes_on_rotation(self):
        original = providers.build_provider().configuration_revision
        self.assertEqual(providers.build_provider().configuration_revision, original)
        for field, value in (("secret_ref", "rotated-secret-ref"), ("model", "other-model"),
                             ("base_url", "https://other.invalid/v1"), ("external_provider_id", "other-account")):
            with self.subTest(field=field), patch.object(self.snap, field, value):
                self.assertNotEqual(providers.build_provider().configuration_revision, original)
        self.assertNotIn("workspace-secret", original)

    def test_saved_profile_incomplete_online_config_never_borrows_global_settings(self):
        for field in ("base_url", "model", "key"):
            with self.subTest(field=field):
                if field == "key":
                    self.runtime.resolve_api_key.return_value = None
                    context = patch.object(self.snap, "secret_ref", None)
                else:
                    context = patch.object(self.snap, field, None)
                with context, self.assertRaises(providers.ProviderError) as raised:
                    providers.build_provider()
                self.assertEqual(raised.exception.code, "PROVIDER_MISCONFIGURED")

    def test_saved_profile_local_choice_uses_configured_service(self):
        self.snap.external_enabled = False
        with patch.dict(os.environ, {"ZHIJUN_PROVIDER": "openai"}):
            actual = providers.build_provider()
        self.assertIsInstance(actual, providers.OllamaProvider)
        self.assertFalse(actual.external)
        self.assertEqual(actual._base_url, self.local.base_url)
        self.assertEqual(actual.model, "npu-model")
        self.assertEqual(actual._num_ctx, providers.DEFAULT_LOCAL_NUM_CTX)
        self.assertEqual(actual._timeout, 40)
        self.runtime.resolve_api_key.assert_not_called()

    def test_online_stream_reuses_sse_and_actual_transport_authorization(self):
        permit = Mock()
        EGRESS_PERMIT.set(permit)
        body = (b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":2,"completion_tokens":1}}\n'
                b'data: [DONE]\n')
        opener = Mock()
        opener.open.return_value = io.BytesIO(body)
        with patch("urllib.request.build_opener", return_value=opener) as build_opener:
            events = list(providers.build_provider().stream(self.req))
        self.assertEqual(events, [providers.TextDelta("hello"), providers.Usage(2, 1), providers.Done("stop")])
        self.assertEqual(permit.call_count, 2)
        self.assertIsInstance(build_opener.call_args.args[0], providers.llm_transport._NoModelRedirect)
        http_request = opener.open.call_args.args[0]
        self.assertEqual(http_request.full_url, "https://workspace.invalid/v1/chat/completions")
        self.assertEqual(http_request.get_header("Authorization"), "Bearer workspace-secret")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 23)

    def test_online_json_reuses_web_request_and_model(self):
        EGRESS_PERMIT.set(Mock())
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"claims":[]}'}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 3}}).encode())
        with patch.object(providers.llm_transport, "allowed_urlopen", return_value=response) as transport:
            provider = providers.build_provider()
            self.assertEqual(provider.complete_json(self.req), {"claims": []})
        payload = json.loads(transport.call_args.kwargs["data"])
        self.assertEqual(payload["model"], "workspace-model")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(provider.last_usage, {"input_tokens": 5, "output_tokens": 3})

    def test_online_without_permit_cannot_send_stream_or_json(self):
        for method in ("stream", "complete_json"):
            with self.subTest(method=method), patch.object(providers.llm_transport, "allowed_urlopen") as transport:
                with self.assertRaises(providers.ProviderError) as raised:
                    result = getattr(providers.build_provider(), method)(self.req)
                    if method == "stream":
                        list(result)
                self.assertEqual(raised.exception.code, "EGRESS_NOT_AUTHORIZED")
                transport.assert_not_called()

    def test_online_revalidates_revoked_permit_at_http_boundary(self):
        revoked = providers.ProviderError("revoked", code="EGRESS_NOT_AUTHORIZED", retryable=False)
        EGRESS_PERMIT.set(Mock(side_effect=[None, revoked]))
        with patch("urllib.request.build_opener") as opener:
            with self.assertRaises(providers.ProviderError) as raised:
                providers.build_provider().complete_json(self.req)
        self.assertEqual(raised.exception.code, "EGRESS_NOT_AUTHORIZED")
        opener.assert_not_called()

    def test_failure_and_timeout_never_fall_back_to_local_or_de(self):
        EGRESS_PERMIT.set(Mock())
        for error, code in ((TimeoutError(), "PROVIDER_TIMEOUT"),
                            (urllib.error.URLError("offline"), "PROVIDER_UNAVAILABLE"),
                            (urllib.error.HTTPError("url", 401, "unauthorized", {}, None), "PROVIDER_MISCONFIGURED")):
            with self.subTest(code=code), patch.object(providers.llm_transport, "allowed_urlopen", side_effect=error) as transport:
                with self.assertRaises(providers.ProviderError) as raised:
                    list(providers.build_provider().stream(self.req))
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(transport.call_count, 1)

    def test_forbidden_service_never_reaches_network(self):
        self.snap.base_url = "https://api.anthropic.com/v1"
        EGRESS_PERMIT.set(Mock())
        with patch.object(providers.llm_transport, "allowed_urlopen") as transport:
            with self.assertRaises(providers.ProviderError) as raised:
                providers.build_provider().complete_json(self.req)
        self.assertEqual(raised.exception.code, "SERVICE_FORBIDDEN")
        transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
