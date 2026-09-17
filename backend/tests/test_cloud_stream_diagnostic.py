"""Offline contract tests for the privacy-safe cloud SSE diagnostic.

Run directly with Python's standard unittest runner; no application imports,
Data Engine checkout, credentials, provider SDK or network are required.
"""
from __future__ import annotations

import importlib.util
import builtins
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "diagnose_cloud_stream.py"
_SPEC = importlib.util.spec_from_file_location("_zhijun_cloud_stream_diagnostic_test", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
StreamAudit = _MODULE.StreamAudit


class StreamAuditTests(unittest.TestCase):
    def frame(self, audit, payload, *, event=None, flush=True):
        if event is not None:
            audit.feed("event: " + event)
        audit.feed("data: " + json.dumps(payload, ensure_ascii=False))
        if flush:
            audit.feed("")

    def result(self, audit):
        audit.finish()
        summary = audit.summary()
        self.assertEqual(set(summary), {
            "frames", "event_types", "delta_chars", "final_chars", "reasoning_chars",
            "refusal_seen", "tool_calls_seen", "finish_reason", "usage", "terminal_seen",
            "invalid_frames", "classification",
        })
        for key in ("frames", "delta_chars", "final_chars", "reasoning_chars", "invalid_frames"):
            self.assertIs(type(summary[key]), int)
            self.assertGreaterEqual(summary[key], 0)
        self.assertIsInstance(summary["event_types"], dict)
        self.assertIsInstance(summary["usage"], dict)
        for key in ("refusal_seen", "tool_calls_seen", "terminal_seen"):
            self.assertIs(type(summary[key]), bool)
        return summary

    def test_chat_completions_visible_deltas_and_done(self):
        audit = StreamAudit()
        for text in ("你好", "，世界"):
            self.frame(audit, {"object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": {"content": text}, "finish_reason": None}
            ]})
        self.frame(audit, {"object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": {}, "finish_reason": "stop"}
        ], "usage": {"prompt_tokens": 12, "completion_tokens": 5,
                       "completion_tokens_details": {"reasoning_tokens": 2}}})
        audit.feed("data: [DONE]")
        audit.feed("")
        summary = self.result(audit)
        self.assertEqual(summary["delta_chars"], len("你好，世界"))
        self.assertEqual(summary["final_chars"], 0)
        self.assertEqual(summary["classification"], "visible_delta")
        self.assertEqual(summary["finish_reason"], "stop")
        self.assertEqual(summary["usage"], {"input_tokens": 12, "output_tokens": 5, "reasoning_tokens": 2})
        self.assertEqual(summary["event_types"]["chat.completion.chunk"], 3)
        self.assertTrue(summary["terminal_seen"])
        self.assertEqual(summary["invalid_frames"], 0)

    def test_reasoning_only_length_is_not_a_visible_answer(self):
        audit = StreamAudit()
        self.frame(audit, {"object": "chat.completion.chunk", "choices": [
            {"delta": {"reasoning_content": "先分析条件，再分析方案"}, "finish_reason": None}
        ]})
        self.frame(audit, {"object": "chat.completion.chunk", "choices": [
            {"delta": {}, "finish_reason": "length"}
        ]})
        summary = self.result(audit)
        self.assertEqual(summary["classification"], "reasoning_only")
        self.assertEqual(summary["reasoning_chars"], len("先分析条件，再分析方案"))
        self.assertEqual(summary["delta_chars"], 0)
        self.assertEqual(summary["final_chars"], 0)
        self.assertEqual(summary["finish_reason"], "length")

    def test_responses_named_events_support_output_text_deltas(self):
        audit = StreamAudit()
        self.frame(audit, {"delta": "逐字输出"}, event="response.output_text.delta")
        self.frame(audit, {"type": "response.output_text.delta", "delta": "正常"},
                   event="response.output_text.delta")
        self.frame(audit, {"type": "response.completed", "response": {
            "status": "completed", "output_text": "逐字输出正常",
            "usage": {"input_tokens": 11, "output_tokens": 7,
                      "output_tokens_details": {"reasoning_tokens": 3}},
        }}, event="response.completed")
        summary = self.result(audit)
        self.assertEqual(summary["classification"], "visible_delta")
        self.assertEqual(summary["delta_chars"], len("逐字输出正常"))
        self.assertEqual(summary["final_chars"], len("逐字输出正常"))
        self.assertEqual(summary["event_types"]["response.output_text.delta"], 2)
        self.assertEqual(summary["usage"], {"input_tokens": 11, "output_tokens": 7, "reasoning_tokens": 3})
        self.assertTrue(summary["terminal_seen"])

    def test_completed_output_text_snapshot_only(self):
        audit = StreamAudit()
        self.frame(audit, {"type": "response.completed", "response": {
            "status": "completed", "output_text": "这是最终回答，但没有增量帧。",
        }})
        summary = self.result(audit)
        self.assertEqual(summary["classification"], "final_only")
        self.assertEqual(summary["delta_chars"], 0)
        self.assertEqual(summary["final_chars"], len("这是最终回答，但没有增量帧。"))
        self.assertTrue(summary["terminal_seen"])

    def test_responses_message_content_snapshot(self):
        audit = StreamAudit()
        self.frame(audit, {"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "第一段"},
                {"type": "output_text", "text": "第二段"},
            ]}],
        }})
        summary = self.result(audit)
        self.assertEqual(summary["classification"], "final_only")
        self.assertEqual(summary["final_chars"], len("第一段第二段"))
        self.assertEqual(summary["reasoning_chars"], 0)

    def test_chat_completions_message_content_snapshot(self):
        audit = StreamAudit()
        self.frame(audit, {"object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "非流式正文快照"},
             "finish_reason": "stop"}
        ]})
        summary = self.result(audit)
        self.assertEqual(summary["classification"], "final_only")
        self.assertEqual(summary["final_chars"], len("非流式正文快照"))
        self.assertEqual(summary["delta_chars"], 0)

    def test_multiline_data_and_finish_flushes_last_unterminated_frame(self):
        audit = StreamAudit()
        audit.feed(": keep-alive")
        audit.feed("event: response.output_text.delta")
        audit.feed('data: {"type": "response.output_text.delta",')
        audit.feed('data: "delta": "分行 JSON"}')
        summary = self.result(audit)
        self.assertEqual(summary["frames"], 1)
        self.assertEqual(summary["delta_chars"], len("分行 JSON"))
        self.assertEqual(summary["invalid_frames"], 0)
        self.assertEqual(summary["classification"], "visible_delta")

    def test_done_without_content_is_terminal_but_empty(self):
        audit = StreamAudit()
        audit.feed("data: [DONE]")
        summary = self.result(audit)
        self.assertTrue(summary["terminal_seen"])
        self.assertEqual(summary["classification"], "empty")
        self.assertEqual(summary["delta_chars"], 0)
        self.assertEqual(summary["final_chars"], 0)
        self.assertEqual(summary["invalid_frames"], 0)

    def test_invalid_json_is_counted_and_raw_payload_never_reported(self):
        audit = StreamAudit()
        secret = "private-invalid-payload-DO-NOT-PRINT"
        audit.feed("data: {" + secret)
        audit.feed("")
        summary = self.result(audit)
        self.assertEqual(summary["invalid_frames"], 1)
        self.assertEqual(summary["classification"], "empty")
        self.assertNotIn(secret, json.dumps(summary))

    def test_unknown_event_finish_reason_and_usage_cannot_leak_strings(self):
        audit = StreamAudit()
        secret = "sensitive-UPSTREAM-DATA-DO-NOT-PRINT"
        self.frame(audit, {"type": secret, "text": secret, "detail": secret}, event=secret)
        self.frame(audit, {"object": "chat.completion.chunk", "choices": [
            {"delta": {}, "finish_reason": secret}
        ], "usage": {"prompt_tokens": secret, "completion_tokens": secret,
                      "completion_tokens_details": {"reasoning_tokens": secret}, "custom": secret}})
        summary = self.result(audit)
        self.assertEqual(summary["finish_reason"], "unknown")
        self.assertEqual(summary["usage"], {})
        self.assertGreaterEqual(summary["event_types"].get("unknown", 0), 1)
        self.assertNotIn(secret, json.dumps(summary))
        self.assertEqual(summary["classification"], "empty")

    def test_refusal_and_tool_arguments_are_not_visible_answer_text(self):
        audit = StreamAudit()
        secret = "private-refusal-or-tool-argument"
        self.frame(audit, {"object": "chat.completion.chunk", "choices": [
            {"delta": {"refusal": secret, "tool_calls": [{"index": 0,
                "function": {"name": "search_materials", "arguments": secret}}]}, "finish_reason": "tool_calls"}
        ]})
        summary = self.result(audit)
        self.assertTrue(summary["refusal_seen"])
        self.assertTrue(summary["tool_calls_seen"])
        self.assertEqual(summary["delta_chars"], 0)
        self.assertEqual(summary["final_chars"], 0)
        self.assertEqual(summary["classification"], "empty")
        self.assertNotIn(secret, json.dumps(summary))

    def test_malformed_structured_types_are_ignored_without_crashing_or_leaking(self):
        audit = StreamAudit()
        secret = "private-malformed-content-part"
        self.frame(audit, {"type": {"private": secret}, "choices": [
            {"delta": {"content": [{"type": [secret], "text": secret}]},
             "finish_reason": {"private": secret}}
        ], "usage": {"input_tokens": [secret], "output_tokens": {"private": secret}}})
        summary = self.result(audit)
        self.assertEqual(summary["classification"], "empty")
        self.assertEqual(summary["finish_reason"], "unknown")
        self.assertEqual(summary["usage"], {})
        self.assertNotIn(secret, json.dumps(summary))

    def test_responses_refusal_and_function_call_output_are_not_answer(self):
        audit = StreamAudit()
        self.frame(audit, {"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "function_call", "name": "search_materials", "arguments": '{"query":"private"}'},
            {"type": "message", "content": [{"type": "refusal", "refusal": "不能回答"}]},
        ]}})
        summary = self.result(audit)
        self.assertTrue(summary["refusal_seen"])
        self.assertTrue(summary["tool_calls_seen"])
        self.assertEqual(summary["classification"], "empty")
        self.assertEqual(summary["final_chars"], 0)

    def test_repeated_final_snapshots_do_not_accumulate_or_add_delta(self):
        audit = StreamAudit()
        text = "最终完整回答"
        self.frame(audit, {"type": "response.output_text.delta", "delta": text})
        self.frame(audit, {"type": "response.output_text.done", "text": text})
        snapshot = {"type": "response.completed", "response": {"status": "completed", "output_text": text}}
        self.frame(audit, snapshot)
        self.frame(audit, snapshot)
        summary = self.result(audit)
        self.assertEqual(summary["delta_chars"], len(text))
        self.assertEqual(summary["final_chars"], len(text))
        self.assertEqual(summary["classification"], "visible_delta")
        self.assertEqual(audit.summary(), summary)

    def test_oversize_frame_is_dropped_and_next_frame_recovers(self):
        audit = StreamAudit()
        audit.feed("data: " + "x" * (1024 * 1024 + 1))
        # Further lines in the oversized frame must not become a new event.
        audit.feed('data: {"type":"response.output_text.delta","delta":"不能采纳"}')
        audit.feed("")
        self.frame(audit, {"type": "response.output_text.delta", "delta": "恢复正常"})
        summary = self.result(audit)
        self.assertEqual(summary["invalid_frames"], 1)
        self.assertEqual(summary["frames"], 1)
        self.assertEqual(summary["delta_chars"], len("恢复正常"))
        self.assertEqual(summary["classification"], "visible_delta")

    def test_non_object_json_is_invalid_without_breaking_following_frames(self):
        audit = StreamAudit()
        for payload in (["private"], "private", 42, None, True):
            self.frame(audit, payload)
        self.frame(audit, {"type": "response.output_text.delta", "delta": "有效正文"})
        summary = self.result(audit)
        self.assertEqual(summary["invalid_frames"], 5)
        self.assertEqual(summary["frames"], 1)
        self.assertEqual(summary["delta_chars"], len("有效正文"))
        self.assertNotIn("private", json.dumps(summary))

    def test_non_integer_and_negative_usage_values_are_not_reported(self):
        for value in (True, False, 1.25, "12", [12], {"tokens": 12}, None, -1):
            with self.subTest(value=value):
                audit = StreamAudit()
                self.frame(audit, {"object": "chat.completion.chunk", "choices": [], "usage": {
                    "input_tokens": value, "output_tokens": value,
                    "output_tokens_details": {"reasoning_tokens": value},
                }})
                self.assertEqual(self.result(audit)["usage"], {})

    def test_zero_usage_is_a_valid_integer_count(self):
        audit = StreamAudit()
        self.frame(audit, {"object": "chat.completion.chunk", "choices": [], "usage": {
            "input_tokens": 0, "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0},
        }})
        self.assertEqual(self.result(audit)["usage"], {
            "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
        })

    def test_eof_with_text_but_no_terminal_does_not_claim_completion(self):
        audit = StreamAudit()
        self.frame(audit, {"type": "response.output_text.delta", "delta": "输出到一半"}, flush=False)
        summary = self.result(audit)
        self.assertEqual(summary["classification"], "visible_delta")
        self.assertEqual(summary["delta_chars"], len("输出到一半"))
        self.assertFalse(summary["terminal_seen"])
        self.assertIsNone(summary["finish_reason"])
        audit.finish()
        self.assertEqual(audit.summary(), summary, "Repeated EOF flush cannot count the frame twice")

    def test_whitespace_is_counted_as_delta_without_claiming_a_complete_answer(self):
        audit = StreamAudit()
        self.frame(audit, {"type": "response.output_text.delta", "delta": " \t\n"})
        summary = self.result(audit)
        self.assertEqual(summary["classification"], "visible_delta")
        self.assertEqual(summary["delta_chars"], 3)
        self.assertEqual(summary["final_chars"], 0)
        self.assertFalse(summary["terminal_seen"])
        self.assertIsNone(summary["finish_reason"])


class ProbeSafetyTests(unittest.TestCase):
    """Exercise the probe against fabricated modules and a disposable SQLite DB."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zhijun-probe-unit-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.source = root / "zhijun_models" / ("a" * 64) / "runtime.db"
        self.source.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.source)) as connection:
            connection.execute("CREATE TABLE runtime_settings (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO runtime_settings VALUES ('model', 'unit-model')")
            connection.execute("CREATE TABLE secret_store_metadata (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO secret_store_metadata VALUES ('managed_fernet_key_v1', 'synthetic-test-key')")
            connection.commit()
        self.initial_digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.key = "unit-private-key-DO-NOT-PRINT"
        self.store_paths = []
        self.secret_paths = []
        self.requests = []
        self.snapshot = SimpleNamespace(external_enabled=True, provider="openai", secret_ref="local:unit-key",
                                        base_url="https://provider.invalid/v1", model="unit-model",
                                        timeout_seconds=120, total_budget_seconds=180)
        self.args = SimpleNamespace(allow_network=True, runtime_db=str(self.source), de_backend=str(root / "no-real-backend"),
                                    scenario="short", max_tokens=1024,
                                    prompt="private-extra-prompt-MUST-NOT-BE-SENT")
        self.model_stream = ModuleType("mindos.zhijun_capabilities.model_stream")
        self.model_stream.__file__ = __file__
        self.model_stream.validate_request = Mock(side_effect=lambda value: value)

        def original_lines(response, checked):
            checked()
            yield 'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"连接测试成功"}}]}'
            yield ""
            yield "data: [DONE]"
            yield ""

        self.original_lines = original_lines
        self.model_stream._lines = original_lines

        def stream(**kwargs):
            self.requests.append(kwargs)
            kwargs["check"]()
            response = SimpleNamespace(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8"})
            list(self.model_stream._lines(response, kwargs["check"]))
            yield {"type": "text", "text": "连接测试成功"}
            yield {"type": "usage", "input_tokens": 10, "output_tokens": 6}
            yield {"type": "done"}

        self.model_stream.stream = Mock(side_effect=stream)
        test = self

        class WorkspaceModelsStore:
            def __init__(self, path):
                path = Path(path)
                test.store_paths.append(path)
                test.assertNotEqual(path.resolve(), test.source.resolve())
                test.assertTrue(path.is_file())
                # Simulate a live constructor's DDL/write, confined to the copy.
                with contextlib.closing(sqlite3.connect(path)) as connection:
                    connection.execute("CREATE TABLE unit_constructor_write (value TEXT)")
                    connection.execute("INSERT INTO unit_constructor_write VALUES ('copy-only')")
                    connection.commit()

        class EncryptedSQLiteSecretStore:
            def __init__(self, *, db_path):
                path = Path(db_path)
                test.secret_paths.append(path)
                test.assertNotEqual(path.resolve(), test.source.resolve())

        self.resolve_api_key = Mock(return_value=self.key)

        class WorkspaceRuntimeProvider:
            def __init__(self, store, secret_store, local):
                self.resolve_api_key = test.resolve_api_key

            def get_chat_snapshot(self):
                return test.snapshot

        modules = {name: ModuleType(name) for name in (
            "mindos", "mindos.zhijun_capabilities", "mindos.zhijun_capabilities.workspace_models_store",
            "mindos.secret_store", "mindos.runtime_config_provider",
        )}
        modules["mindos"].__path__ = []
        modules["mindos.zhijun_capabilities"].__path__ = []
        modules["mindos.zhijun_capabilities"].model_stream = self.model_stream
        modules["mindos.zhijun_capabilities.model_stream"] = self.model_stream
        store_module = modules["mindos.zhijun_capabilities.workspace_models_store"]
        store_module.WorkspaceModelsStore = WorkspaceModelsStore
        store_module.WorkspaceRuntimeProvider = WorkspaceRuntimeProvider
        modules["mindos.secret_store"].EncryptedSQLiteSecretStore = EncryptedSQLiteSecretStore
        modules["mindos.runtime_config_provider"].LocalOllamaSnapshot = Mock(return_value=SimpleNamespace())
        self.enterContext(patch.dict(sys.modules, modules))
        self.enterContext(patch.dict(os.environ, {"CENTAUR_MANAGED_MODEL_CONFIG": "", "CENTAUR_SECRET_STORE_KEY": ""}))
        self.enterContext(patch.object(sys, "path", list(sys.path)))
        self.connect = self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("Real network forbidden")))
        self.create_connection = self.enterContext(patch("socket.create_connection", side_effect=AssertionError("Real network forbidden")))

    def assert_source_unchanged(self):
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.initial_digest)
        with contextlib.closing(sqlite3.connect(self.source)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='unit_constructor_write'"
            ).fetchone())
        self.connect.assert_not_called()
        self.create_connection.assert_not_called()

    def test_network_opt_in_is_checked_before_any_de_import_or_source_access(self):
        original_import = builtins.__import__
        imports = []

        def guarded_import(name, *args, **kwargs):
            imports.append(name)
            if name == "mindos" or name.startswith("mindos."):
                raise AssertionError("DE import must not occur without opt-in")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            with self.assertRaisesRegex(RuntimeError, "NETWORK_OPT_IN_REQUIRED"):
                _MODULE.probe(SimpleNamespace(allow_network=False))
        self.assertFalse(any(name == "mindos" or name.startswith("mindos.") for name in imports))
        self.model_stream.stream.assert_not_called()
        self.assertEqual(self.store_paths, [])
        self.assert_source_unchanged()

    def test_fixed_synthetic_request_uses_only_copy_and_never_reports_key_or_prompt(self):
        report = _MODULE.probe(self.args)
        self.model_stream.stream.assert_called_once()
        self.assertEqual(len(self.requests), 1)
        sent = self.requests[0]
        self.assertEqual(sent["protocol"], "openai")
        self.assertEqual(sent["key"], self.key)
        self.assertEqual(sent["request"], {
            "system": "This is a synthetic connectivity test. No personal data is included.",
            "messages": [{"role": "user", "content": "只回答：连接测试成功"}],
            "max_tokens": 1024, "temperature": 0, "effort": "low",
        })
        self.assertEqual(sent["timeout"], 30)
        self.assertEqual(sent["budget"], 60)
        self.assertEqual(self.store_paths, self.secret_paths)
        self.assertEqual(len(self.store_paths), 1)
        self.assertFalse(self.store_paths[0].exists(), "Temporary copy must be cleaned after probe")
        self.assertNotIn(self.args.prompt, json.dumps(sent["request"]))
        self.assertNotIn(self.key, json.dumps(report))
        self.assertNotIn(self.args.prompt, json.dumps(report))
        self.assertNotIn(str(self.source), json.dumps(report))
        self.assertEqual(report["adapter_result"], "done")
        self.assertEqual(report["adapter_chars"], len("连接测试成功"))
        self.assertEqual(report["wire"]["classification"], "visible_delta")
        self.assertTrue(report["wire"]["terminal_seen"])
        self.assertIs(self.model_stream._lines, self.original_lines)
        self.assert_source_unchanged()

    def test_disabled_external_profile_never_resolves_key_or_calls_stream(self):
        self.snapshot.external_enabled = False
        with self.assertRaisesRegex(RuntimeError, "ACTIVE_OPENAI_PROFILE_REQUIRED"):
            _MODULE.probe(self.args)
        self.model_stream.stream.assert_not_called()
        self.resolve_api_key.assert_not_called()
        self.assertIs(self.model_stream._lines, self.original_lines)
        self.assert_source_unchanged()

    def test_stream_exception_restores_lines_and_redacts_private_exception(self):
        def failed_stream(**kwargs):
            raise RuntimeError(self.key + " " + self.args.prompt)

        self.model_stream.stream.side_effect = failed_stream
        report = _MODULE.probe(self.args)
        self.model_stream.stream.assert_called_once()
        self.assertEqual(report["adapter_result"], "diagnostic_failed")
        self.assertNotIn(self.key, json.dumps(report))
        self.assertNotIn(self.args.prompt, json.dumps(report))
        self.assertIs(self.model_stream._lines, self.original_lines)
        self.assert_source_unchanged()


if __name__ == "__main__":
    unittest.main()
