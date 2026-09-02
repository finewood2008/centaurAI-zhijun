"""知君模型通道：JSON 解析、Ollama NDJSON / OpenAI SSE 流解析、错误映射、Anthropic 适配器（stub 客户端）、通道选择。"""
from __future__ import annotations

import io
import os
import unittest
import urllib.error
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from mindos.zhijun import provider as provider_module
from mindos.zhijun.provider import (
    AnthropicProvider,
    ChatRequest,
    Done,
    FakeProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderError,
    TextDelta,
    Usage,
    build_provider,
    parse_json_object,
)


def _req(**kw) -> ChatRequest:
    base = {"system": "sys", "messages": [{"role": "user", "content": "我在做远川项目"}]}
    base.update(kw)
    return ChatRequest(**base)


class _FakeResponse(io.BytesIO):
    """既能逐行迭代（流式）也能 read()（非流式）。"""


class ParseTests(unittest.TestCase):
    def test_parse_json_object_tolerates_fences_and_noise(self) -> None:
        self.assertEqual(parse_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json_object('前言 {"a": {"b": 2}} 后记'), {"a": {"b": 2}})
        with self.assertRaises(ValueError):
            parse_json_object("没有对象")
        with self.assertRaises(ValueError):
            parse_json_object("[1, 2]")


class OllamaTests(unittest.TestCase):
    def test_stream_parses_ndjson_and_usage(self) -> None:
        lines = [
            b'{"message":{"role":"assistant","content":"\xe4\xbd\xa0"},"done":false}\n',
            b'{"message":{"role":"assistant","content":"\xe5\xa5\xbd"},"done":false}\n',
            b'{"message":{"content":""},"done":true,"done_reason":"stop","prompt_eval_count":12,"eval_count":2}\n',
        ]
        with patch.object(provider_module.llm_transport, "allowed_urlopen", return_value=_FakeResponse(b"".join(lines))) as mocked:
            events = list(OllamaProvider("http://127.0.0.1:11434", "qwen3:4b", timeout=5).stream(_req()))
        self.assertEqual([e.text for e in events if isinstance(e, TextDelta)], ["你", "好"])
        self.assertEqual([e for e in events if isinstance(e, Usage)][0].input_tokens, 12)
        self.assertEqual(events[-1], Done("stop"))
        body = mocked.call_args.kwargs["data"]
        self.assertIn(b'"stream": true', body.replace(b"\n", b""))

    def test_stream_error_line_raises(self) -> None:
        with patch.object(provider_module.llm_transport, "allowed_urlopen", return_value=_FakeResponse(b'{"error":"model not found"}\n')):
            with self.assertRaises(ProviderError) as ctx:
                list(OllamaProvider("http://127.0.0.1:11434", "x", timeout=5).stream(_req()))
        self.assertEqual(ctx.exception.code, "PROVIDER_REJECTED")

    def test_complete_json_uses_json_format(self) -> None:
        payload = b'{"message":{"content":"{\\"claims\\":[],\\"entities\\":[]}"}}'
        with patch.object(provider_module.llm_transport, "allowed_urlopen", return_value=_FakeResponse(payload)) as mocked:
            result = OllamaProvider("http://127.0.0.1:11434", "x", timeout=5).complete_json(_req(json_schema={"type": "object"}))
        self.assertEqual(result, {"claims": [], "entities": []})
        self.assertIn(b'"format": "json"', mocked.call_args.kwargs["data"])

    def test_connection_errors_map_to_provider_error(self) -> None:
        with patch.object(provider_module.llm_transport, "allowed_urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(ProviderError) as ctx:
                list(OllamaProvider("http://127.0.0.1:11434", "x", timeout=5).stream(_req()))
        self.assertEqual(ctx.exception.status_code, 503)
        http_error = urllib.error.HTTPError("u", 429, "busy", {}, io.BytesIO(b""))
        with patch.object(provider_module.llm_transport, "allowed_urlopen", side_effect=http_error):
            with self.assertRaises(ProviderError) as ctx:
                list(OllamaProvider("http://127.0.0.1:11434", "x", timeout=5).stream(_req()))
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (429, "PROVIDER_BUSY"))


class OpenAITests(unittest.TestCase):
    def test_stream_parses_sse_frames(self) -> None:
        frames = [
            b": keep-alive\n",
            b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"},"finish_reason":null}]}\n',
            b"\n",
            b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n',
            b"data: [DONE]\n",
        ]
        with patch.object(provider_module.llm_transport, "allowed_urlopen", return_value=_FakeResponse(b"".join(frames))) as mocked:
            events = list(OpenAICompatibleProvider("https://api.example.com/v1", "m", "sk-test", timeout=5).stream(_req()))
        self.assertEqual([e.text for e in events if isinstance(e, TextDelta)], ["你", "好"])
        self.assertEqual([e for e in events if isinstance(e, Usage)][0].output_tokens, 2)
        self.assertEqual(events[-1], Done("stop"))
        self.assertEqual(mocked.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test")

    def test_complete_json_requests_json_object(self) -> None:
        payload = b'{"choices":[{"message":{"content":"{\\"claims\\":[]}"}}]}'
        with patch.object(provider_module.llm_transport, "allowed_urlopen", return_value=_FakeResponse(payload)) as mocked:
            result = OpenAICompatibleProvider("https://api.example.com/v1", "m", "k", timeout=5).complete_json(_req(json_schema={}))
        self.assertEqual(result, {"claims": []})
        self.assertIn(b'"response_format"', mocked.call_args.kwargs["data"])


class AnthropicStubTests(unittest.TestCase):
    def _stub_client(self, captured: dict):
        @contextmanager
        def _stream(**kwargs):
            captured["kwargs"] = kwargs

            class _Stream:
                text_stream = iter(["你", "好"])

                @staticmethod
                def get_final_message():
                    return SimpleNamespace(usage=SimpleNamespace(input_tokens=7, output_tokens=2), stop_reason="end_turn")

            yield _Stream()

        def _create(**kwargs):
            captured["create"] = kwargs
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"claims":[],"entities":[]}')])

        messages = SimpleNamespace(stream=_stream, create=_create)
        return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))

    def test_stream_and_json_use_sdk_shapes(self) -> None:
        captured: dict = {}
        prov = AnthropicProvider("claude-opus-5", "key", timeout=30, fallbacks=True)
        with patch.object(prov, "_get_client", return_value=self._stub_client(captured)):
            events = list(prov.stream(_req(effort="medium")))
            result = prov.complete_json(_req(json_schema={"type": "object"}))
        self.assertEqual([e.text for e in events if isinstance(e, TextDelta)], ["你", "好"])
        self.assertEqual(events[-1], Done("end_turn"))
        kwargs = captured["kwargs"]
        self.assertEqual(kwargs["model"], "claude-opus-5")
        self.assertEqual(kwargs["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(kwargs["output_config"]["effort"], "medium")
        self.assertEqual(kwargs["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", kwargs["betas"])
        self.assertNotIn("thinking", kwargs)
        self.assertEqual(result, {"claims": [], "entities": []})
        self.assertEqual(captured["create"]["output_config"]["format"]["type"], "json_schema")


class SelectionTests(unittest.TestCase):
    def test_fake_only_outside_production(self) -> None:
        with patch.dict(os.environ, {"ZHIJUN_PROVIDER": "fake", "MINDOS_RUNTIME_ENV": "development"}):
            self.assertIsInstance(build_provider(), FakeProvider)
        with patch.dict(os.environ, {"ZHIJUN_PROVIDER": "fake", "MINDOS_RUNTIME_ENV": "production"}):
            with self.assertRaises(ProviderError):
                build_provider()

    def test_default_is_local_ollama_from_snapshot(self) -> None:
        snap = SimpleNamespace(
            provider="ollama",
            external_enabled=False,
            base_url=None,
            model=None,
            timeout_seconds=30,
            local=SimpleNamespace(base_url="http://127.0.0.1:11434", model="qwen3:4b", timeout_seconds=60, keep_alive=0, context_window=4096),
        )
        with patch.dict(os.environ, {"ZHIJUN_PROVIDER": "", "ZHIJUN_LOCAL_NUM_CTX": "4096"}):
            prov = build_provider(snap)
        self.assertIsInstance(prov, OllamaProvider)
        self.assertEqual(prov.model, "qwen3:4b")
        self.assertFalse(prov.external)

    def test_openai_requires_full_config(self) -> None:
        snap = SimpleNamespace(provider="openai", external_enabled=True, base_url="https://api.example.com/v1", model="m", timeout_seconds=30, local=None)
        with patch.dict(os.environ, {"ZHIJUN_PROVIDER": ""}):
            with patch.object(provider_module, "get_provider", return_value=SimpleNamespace(resolve_api_key=lambda s: None)):
                with self.assertRaises(ProviderError) as ctx:
                    build_provider(snap)
            self.assertEqual(ctx.exception.code, "PROVIDER_MISCONFIGURED")
            with patch.object(provider_module, "get_provider", return_value=SimpleNamespace(resolve_api_key=lambda s: "sk")):
                prov = build_provider(snap)
        self.assertIsInstance(prov, OpenAICompatibleProvider)
        self.assertTrue(prov.external)

    def test_fake_reply_mentions_confirmed_and_working(self) -> None:
        text = "".join(
            e.text
            for e in FakeProvider().stream(_req(debug={"confirmedClaims": ["我在做远川项目"], "workingClaims": ["我可能偏内向"], "depth": "deep"}))
            if isinstance(e, TextDelta)
        )
        self.assertIn("【你告诉我的】我在做远川项目", text)
        self.assertIn("【我推测的】我可能偏内向", text)
        self.assertIn("【知君的看法】", text)


if __name__ == "__main__":
    unittest.main()
