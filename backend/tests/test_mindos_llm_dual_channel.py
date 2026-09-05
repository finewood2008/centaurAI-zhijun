"""MindOS LLM 双通道专项测试（设计方案 §8）。

覆盖：
- 材料识别固定本机 Ollama（derived / tag_suggest），不随 QA 配置改变；
- 对话问答仅在显式外发授权且配置完整时调用外部 OpenAI 兼容端点；
- 外部 429/5xx/网络错误/空响应可回退本地；400/401/403/404 与配置错误不回落；
- meta.model / provider / fallbackUsed 在外部成功、本地直连、fallback、
  部分回答与无证据路径中的契约；
- 外部响应不把 API Key 透出到对外错误信息。
"""
import json
from contextlib import contextmanager
from dataclasses import replace
import importlib
import os
import sys
import urllib.error
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from mindos import derived, qa, tag_suggest
from mindos.runtime_config_provider import ChatProviderSnapshot, LocalOllamaSnapshot
from mindos.zhijun.routing import EGRESS_PERMIT


def _ollama_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(
        {"message": {"content": content}}, ensure_ascii=False
    ).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _openai_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(
        {"choices": [{"message": {"content": content}}]}, ensure_ascii=False
    ).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://ext.example.com/v1/chat/completions", code, "err", {}, None
    )


@contextmanager
def _network(**kwargs):
    """Keep the real destination/egress checks; replace only network I/O."""
    with patch("urllib.request.urlopen", **kwargs) as opened, patch(
        "urllib.request.build_opener", return_value=SimpleNamespace(open=opened),
    ):
        yield opened


class _SnapshotCase(unittest.TestCase):
    def setUp(self):
        self.snap = ChatProviderSnapshot(
            provider="ollama", external_enabled=False, base_url=None,
            model="local-qa", api_key_configured=False, secret_ref=None,
            timeout_seconds=60, total_budget_seconds=90, fallback_ollama=True,
            local=LocalOllamaSnapshot("http://127.0.0.1:11434", "local-qa", 60, 0, 4096),
        )
        self.key = "sk-test-key"
        runtime = SimpleNamespace(
            store=None, get_chat_snapshot=lambda: self.snap,
            resolve_api_key=lambda snap: self.key if snap.api_key_configured else None,
        )
        stub = patch.object(qa, "get_provider", return_value=runtime)
        stub.start()
        self.addCleanup(stub.stop)
        token = EGRESS_PERMIT.set(None)
        self.addCleanup(EGRESS_PERMIT.reset, token)

    def _call(self, question, evidence):
        return qa.call_local_qa_model(question, evidence, snap=self.snap)


def _enable_external(testcase):
    """A synthetic explicitly authorized request, never real settings/secrets."""
    testcase.snap = replace(
        testcase.snap, provider="openai", external_enabled=True,
        base_url="https://ext.example.com/v1", model="ext-qa-model",
        api_key_configured=True, secret_ref="synthetic-only",
    )
    token = EGRESS_PERMIT.set(lambda: None)
    testcase.addCleanup(EGRESS_PERMIT.reset, token)


class DualChannelMaterialTests(unittest.TestCase):
    """材料识别强制本机 Ollama，不随 QA 配置改变。"""

    def test_wiki_and_material_use_shared_ollama_url(self):
        try:
            with patch.dict(
                os.environ,
                {
                    "CENTAUR_LOCAL_OLLAMA_URL": "http://192.168.10.20:11434/",
                    "CENTAUR_LOCAL_OLLAMA_MODEL": "qwen3:4b",
                    "CENTAUR_WIKI_AI_MODEL": "legacy-wiki-model",
                    "CENTAUR_RECOGNITION_AI_MODEL": "legacy-recognition-model",
                },
                clear=False,
            ):
                importlib.reload(config)
                self.assertEqual(config.WIKI_AI_OLLAMA_URL, "http://192.168.10.20:11434")
                self.assertEqual(
                    config.RECOGNITION_AI_OLLAMA_URL,
                    config.WIKI_AI_OLLAMA_URL,
                )
                self.assertEqual(config.WIKI_AI_MODEL, "qwen3:4b")
                self.assertEqual(
                    config.RECOGNITION_AI_MODEL,
                    config.WIKI_AI_MODEL,
                )
        finally:
            importlib.reload(config)

    def test_derived_call_llm_always_local_url(self):
        with patch("mindos.llm_transport.allowed_urlopen", return_value=_ollama_response("关系")) as urlopen:
            snap = derived.get_provider().get_local_snapshot()
            answer = derived._call_llm("system", "prompt", 0.1, 500, snap)
        self.assertEqual(answer, "关系")
        url = urlopen.call_args.args[0]
        self.assertTrue(
            url.startswith(snap.base_url.rstrip("/") + "/api/chat")
        )
        self.assertNotIn("ext.example.com", url)
        self.assertNotIn("Authorization", urlopen.call_args.kwargs["headers"])
        body = json.loads(urlopen.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(body["model"], snap.model)

    def test_generator_name_keeps_ollama_prefix(self):
        snap = SimpleNamespace(model="qwen3:1.7b")
        self.assertEqual(derived._generator_name(snap), "ollama:qwen3:1.7b")
        # 模型变更即改变指纹，驱动的派生重算判定随之更新
        snap2 = SimpleNamespace(model="qwen3:8b")
        self.assertNotEqual(derived._generator_name(snap), derived._generator_name(snap2))

    def test_tag_suggest_llm_always_local_url(self):
        with patch(
            "mindos.llm_transport.allowed_urlopen", return_value=_ollama_response("标签A、标签B")
        ) as urlopen:
            snap = tag_suggest.get_provider().get_local_snapshot()
            tags = tag_suggest._llm_suggest("正文", "标题", snap)
        self.assertEqual(tags, ["标签A", "标签B"])
        url = urlopen.call_args.args[0]
        self.assertTrue(
            url.startswith(snap.base_url.rstrip("/") + "/api/chat")
        )
        body = json.loads(urlopen.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(body["model"], snap.model)

    def test_tag_suggest_accepts_json_and_fenced_json(self):
        self.assertEqual(
            tag_suggest._parse_tags('["知识管理", "RAG"]'),
            ["知识管理", "RAG"],
        )
        self.assertEqual(
            tag_suggest._parse_tags('```json\n{"tags": [{"name": "文档处理"}, "索引"]}\n```'),
            ["文档处理", "索引"],
        )


class DualChannelExternalGateTests(_SnapshotCase):
    """QA 外部通道的配置门控与降级行为。"""

    def test_external_success_hits_openai_compatible_endpoint(self):
        _enable_external(self)
        with _network(return_value=_openai_response("外部回答")) as urlopen:
            result = self._call("问题", [])
        self.assertEqual(result["answer"], "外部回答")
        self.assertEqual(result["model"], self.snap.model)
        self.assertEqual(result["provider"], "openai")
        self.assertIs(result["fallbackUsed"], False)
        urlopen.assert_called_once()
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://ext.example.com/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-test-key")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], self.snap.model)

    def test_external_request_contains_all_retrieved_evidence(self):
        """已由请求路由授权的完整证据沿调用链传入；未授权请求单独拒绝。"""
        _enable_external(self)
        evidence = [
            qa.Evidence(
                citation_id="m1", source_type="material", material_id="mindos_private",
                knowledge_id=None, title="内部材料", snippet="材料片段", score=0.9,
                priority_bucket="material",
            ),
            qa.Evidence(
                citation_id="k1", source_type="knowledge", material_id=None,
                knowledge_id="knowledge_private", title="内部卡片", snippet="卡片片段",
                score=0.8, priority_bucket="knowledge",
            ),
        ]
        with _network(return_value=_openai_response("外部回答")) as urlopen:
            result = self._call("问题", evidence)

        self.assertEqual(result["provider"], "openai")
        request_body = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
        prompt = request_body["messages"][1]["content"]
        self.assertIn("材料片段", prompt)
        self.assertIn("卡片片段", prompt)

    def test_external_5xx_falls_back_to_local_ollama(self):
        _enable_external(self)
        with _network(
            side_effect=[_http_error(502), _ollama_response("本地兜底回答")],
        ) as urlopen:
            result = self._call("问题", [])
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(result["provider"], "ollama")
        self.assertIs(result["fallbackUsed"], True)
        self.assertEqual(result["model"], self.snap.local.model)
        local_req = urlopen.call_args_list[1][0][0]
        self.assertTrue(local_req.full_url.endswith("/api/chat"))
        body = json.loads(local_req.data.decode("utf-8"))
        self.assertEqual(body["model"], self.snap.local.model)

    def test_external_429_falls_back(self):
        _enable_external(self)
        with _network(
            side_effect=[_http_error(429), _ollama_response("兜底")],
        ) as urlopen:
            result = self._call("问题", [])
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(result["provider"], "ollama")
        self.assertIs(result["fallbackUsed"], True)

    def test_external_network_error_falls_back(self):
        _enable_external(self)
        with _network(
            side_effect=[urllib.error.URLError("refused"), _ollama_response("兜底")],
        ) as urlopen:
            result = self._call("问题", [])
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(result["provider"], "ollama")
        self.assertIs(result["fallbackUsed"], True)

    def test_external_timeout_falls_back(self):
        _enable_external(self)
        with _network(
            side_effect=[TimeoutError("timed out"), _ollama_response("兜底")],
        ) as urlopen:
            result = self._call("问题", [])
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(result["provider"], "ollama")
        self.assertIs(result["fallbackUsed"], True)

    def test_external_empty_answer_falls_back(self):
        _enable_external(self)
        with _network(
            side_effect=[_openai_response(""), _ollama_response("兜底")],
        ) as urlopen:
            result = self._call("问题", [])
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(result["provider"], "ollama")
        self.assertIs(result["fallbackUsed"], True)

    def test_external_401_never_falls_back(self):
        from fastapi import HTTPException

        _enable_external(self)
        with _network(side_effect=_http_error(401)) as urlopen:
            with self.assertRaises(HTTPException) as ctx:
                self._call("问题", [])
        self.assertEqual(ctx.exception.status_code, 401)
        urlopen.assert_called_once()
        self.assertNotIn("sk-test-key", str(ctx.exception.detail))

    def test_external_400_403_404_never_falls_back(self):
        from fastapi import HTTPException

        for code in (400, 403, 404):
            with self.subTest(code=code):
                _enable_external(self)
                with _network(side_effect=_http_error(code)) as urlopen:
                    with self.assertRaises(HTTPException) as ctx:
                        self._call("问题", [])
                self.assertEqual(ctx.exception.status_code, code)
                urlopen.assert_called_once()

    def test_external_enabled_but_config_missing_returns_503(self):
        from fastapi import HTTPException

        self.snap = replace(self.snap, external_enabled=True, provider="openai",
                            base_url="", model="", api_key_configured=False)
        with _network() as urlopen:
            with self.assertRaises(HTTPException) as ctx:
                self._call("问题", [])
        self.assertEqual(ctx.exception.status_code, 503)
        urlopen.assert_not_called()

    def test_no_external_enable_never_leaves_device(self):
        self.snap = replace(self.snap, provider="openai", external_enabled=False,
                            base_url="https://ext.example.com/v1", model="ext-model",
                            api_key_configured=True)
        with _network(return_value=_ollama_response("本地回答")) as urlopen:
            result = self._call("问题", [])
        self.assertEqual(result["provider"], "ollama")
        self.assertIs(result["fallbackUsed"], False)
        urlopen.assert_called_once()
        req = urlopen.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/api/chat"))
        self.assertIsNone(req.get_header("Authorization"))
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], self.snap.local.model)

    def test_invalid_provider_rejected_when_external_enabled(self):
        """非法提供商不能静默降级。"""
        from fastapi import HTTPException

        self.snap = replace(self.snap, external_enabled=True, provider="azure")
        with _network() as urlopen:
            with self.assertRaises(HTTPException) as ctx:
                self._call("问题", [])
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("provider", ctx.exception.detail.lower())
        urlopen.assert_not_called()

    def test_default_provider_ollama_external_enabled_stays_local(self):
        self.snap = replace(self.snap, external_enabled=True, provider="ollama")
        with _network(return_value=_ollama_response("本地")) as urlopen:
            result = self._call("问题", [])
        self.assertEqual(result["provider"], "ollama")
        self.assertIs(result["fallbackUsed"], False)
        urlopen.assert_called_once()

    def test_external_without_request_authorization_never_sends_or_falls_back(self):
        from fastapi import HTTPException

        _enable_external(self)
        EGRESS_PERMIT.set(None)
        with _network() as urlopen:
            with self.assertRaises(HTTPException) as ctx:
                self._call("问题", [])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["code"], "EGRESS_NOT_AUTHORIZED")
        urlopen.assert_not_called()

    def test_revoked_request_authorization_is_rechecked_before_network(self):
        from fastapi import HTTPException

        _enable_external(self)
        def revoked():
            raise HTTPException(409, {"code": "SOURCE_REVOKED"})
        EGRESS_PERMIT.set(revoked)
        with _network() as urlopen:
            with self.assertRaises(HTTPException) as ctx:
                self._call("问题", [])
        self.assertEqual(ctx.exception.detail["code"], "SOURCE_REVOKED")
        urlopen.assert_not_called()

    def test_external_path_still_holds_semaphore(self):
        from fastapi import HTTPException

        _enable_external(self)
        with _network() as urlopen:
            qa._qa_semaphore.acquire(blocking=False)
            try:
                with self.assertRaises(HTTPException) as ctx:
                    self._call("问题", [])
                self.assertEqual(ctx.exception.status_code, 429)
            finally:
                qa._qa_semaphore.release()
        urlopen.assert_not_called()


class DualChannelMetaContractTests(_SnapshotCase):
    """meta.model / provider / fallbackUsed 契约（§8）。"""

    @staticmethod
    def _evidence():
        return [
            qa.Evidence(
                citation_id="m1",
                source_type="material",
                material_id="mindos_t",
                knowledge_id=None,
                title="t.docx",
                snippet="阶段\t目标\nP0\t方案确定",
                score=0.9,
                priority_bucket="material",
            )
        ]

    def test_meta_no_evidence_contract(self):
        with patch.object(qa, "build_evidence", return_value=[]), patch.object(
            qa.corrections, "match_corrections", return_value=[]
        ):
            result = qa.answer_question(qa.QaRequest(question="没有资料的问题"))
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(result["meta"]["model"])
        self.assertIsNone(result["meta"]["provider"])
        self.assertIs(result["meta"]["fallbackUsed"], False)

    def test_meta_echoes_external_success(self):
        with patch.object(qa, "build_evidence", return_value=self._evidence()), patch.object(
            qa.corrections, "match_corrections", return_value=[]
        ), patch.object(
            qa,
            "call_local_qa_model",
            return_value={
                "answer": "外部回答",
                "model": "ext-qa-model",
                "provider": "openai",
                "fallbackUsed": False,
            },
        ):
            result = qa.answer_question(qa.QaRequest(question="开发排期"))
        self.assertEqual(result["status"], "ANSWERED")
        self.assertEqual(result["meta"]["model"], "ext-qa-model")
        self.assertEqual(result["meta"]["provider"], "openai")
        self.assertIs(result["meta"]["fallbackUsed"], False)

    def test_meta_echoes_local_fallback(self):
        with patch.object(qa, "build_evidence", return_value=self._evidence()), patch.object(
            qa.corrections, "match_corrections", return_value=[]
        ), patch.object(
            qa,
            "call_local_qa_model",
            return_value={
                "answer": "本地兜底",
                "model": "local-model",
                "provider": "ollama",
                "fallbackUsed": True,
            },
        ):
            result = qa.answer_question(qa.QaRequest(question="开发排期"))
        self.assertEqual(result["status"], "ANSWERED")
        self.assertEqual(result["meta"]["model"], "local-model")
        self.assertEqual(result["meta"]["provider"], "ollama")
        self.assertIs(result["meta"]["fallbackUsed"], True)

    def test_answer_audit_records_only_route_metadata_and_source_ids(self):
        evidence = [
            qa.Evidence(
                citation_id="m1", source_type="material", material_id="mindos_a",
                knowledge_id=None, title="材料", snippet="不得写入审计的正文", score=0.9,
                priority_bucket="material",
            ),
            qa.Evidence(
                citation_id="k1", source_type="knowledge", material_id=None,
                knowledge_id="knowledge_b", title="卡片", snippet="不得写入审计的正文", score=0.8,
                priority_bucket="knowledge",
            ),
        ]
        with patch.object(qa, "build_evidence", return_value=evidence), patch.object(
            qa.corrections, "match_corrections", return_value=[]
        ), patch.object(
            qa, "call_local_qa_model", return_value={
                "answer": "回答", "model": "external-model", "provider": "openai",
                "fallbackUsed": False,
            }
        ), patch("annotations.add_audit") as add_audit:
            qa.answer_question(qa.QaRequest(question="开发排期"))

        add_audit.assert_called_once_with(
            "qa.answer",
            payload={
                "model": "external-model",
                "provider": "openai",
                "fallbackUsed": False,
                "sourceIds": ["mindos_a", "knowledge_b"],
            },
        )

    def test_meta_string_mock_compat(self):
        with patch.object(qa, "build_evidence", return_value=self._evidence()), patch.object(
            qa.corrections, "match_corrections", return_value=[]
        ), patch.object(qa, "call_local_qa_model", return_value="字符串回答"):
            result = qa.answer_question(qa.QaRequest(question="开发排期"))
        self.assertEqual(result["meta"]["model"], self.snap.model)
        self.assertEqual(result["meta"]["provider"], "ollama")
        self.assertIs(result["meta"]["fallbackUsed"], False)

    def test_meta_partial_answer_present(self):
        with patch.object(qa, "build_evidence", return_value=self._evidence()), patch.object(
            qa.corrections, "match_corrections", return_value=[]
        ), patch.object(
            qa, "call_local_qa_model", side_effect=["资料不足，暂不生成结论。", "资料不足，暂不生成结论。"]
        ):
            result = qa.answer_question(qa.QaRequest(question="开发排期"))
        self.assertEqual(result["status"], "PARTIAL_ANSWER")
        self.assertEqual(result["meta"]["provider"], "ollama")
        self.assertFalse(result["meta"]["fallbackUsed"])
        self.assertEqual(result["meta"]["model"], self.snap.model)

    def test_partial_answer_uses_final_retry_metadata_and_audit(self):
        with patch.object(qa, "build_evidence", return_value=self._evidence()), patch.object(
            qa.corrections, "match_corrections", return_value=[]
        ), patch.object(
            qa,
            "call_local_qa_model",
            side_effect=[
                {
                    "answer": "资料不足，暂不生成结论。",
                    "model": "ext-qa-model",
                    "provider": "openai",
                    "fallbackUsed": False,
                },
                {
                    "answer": "资料不足，暂不生成结论。",
                    "model": "local-model",
                    "provider": "ollama",
                    "fallbackUsed": True,
                },
            ],
        ), patch("annotations.add_audit") as add_audit:
            result = qa.answer_question(qa.QaRequest(question="开发排期"))

        self.assertEqual(result["status"], "PARTIAL_ANSWER")
        self.assertEqual(result["meta"]["model"], "local-model")
        self.assertEqual(result["meta"]["provider"], "ollama")
        self.assertIs(result["meta"]["fallbackUsed"], True)
        audit_payload = add_audit.call_args.kwargs["payload"]
        self.assertEqual(audit_payload["model"], "local-model")
        self.assertEqual(audit_payload["provider"], "ollama")
        self.assertIs(audit_payload["fallbackUsed"], True)

    def test_meta_retry_success_prefers_retry_metadata(self):
        with patch.object(qa, "build_evidence", return_value=self._evidence()), patch.object(
            qa.corrections, "match_corrections", return_value=[]
        ), patch.object(
            qa,
            "call_local_qa_model",
            side_effect=[
                {
                    "answer": "资料不足，暂不生成结论。",
                    "model": "ext-qa-model",
                    "provider": "openai",
                    "fallbackUsed": False,
                },
                {
                    "answer": "排期包含 P0 方案确定。",
                    "model": "local-model",
                    "provider": "ollama",
                    "fallbackUsed": True,
                },
            ],
        ):
            result = qa.answer_question(qa.QaRequest(question="开发排期"))
        self.assertEqual(result["status"], "ANSWERED")
        self.assertEqual(result["meta"]["model"], "local-model")
        self.assertEqual(result["meta"]["provider"], "ollama")
        self.assertIs(result["meta"]["fallbackUsed"], True)


if __name__ == "__main__":
    unittest.main()
