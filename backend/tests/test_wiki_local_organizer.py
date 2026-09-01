import json
import unittest
from unittest.mock import patch

import wiki_store


class _FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self._data


class WikiLocalOrganizerTest(unittest.TestCase):
    def test_status_checks_fixed_local_ollama_model(self):
        response = _FakeResponse({"models": [{"name": "qwen3:1.7b"}, {"name": "bge-m3:latest"}]})
        with patch.object(wiki_store.wiki_transport, "allowed_urlopen", return_value=response) as urlopen:
            status = wiki_store.local_organizer_status()

        self.assertEqual(urlopen.call_args.args[0], "http://127.0.0.1:11434/api/tags")
        self.assertTrue(status["available"])
        self.assertTrue(status["ready"])
        self.assertTrue(status["local_only"])
        self.assertEqual(status["model"], "qwen3:1.7b")
        self.assertEqual(status["memory_policy"]["keep_alive_seconds"], 0)
        self.assertEqual(status["memory_policy"]["max_loaded_models"], 1)

    def test_organizer_uses_local_json_api_without_credentials(self):
        organized = {
            "summary": "这是一段本地生成的摘要。",
            "tags": ["本地模型"],
            "para": "Resources",
            "concepts": [],
            "evidence": ["关键资料"],
        }
        response = _FakeResponse({"message": {"content": json.dumps(organized, ensure_ascii=False)}})
        snap = wiki_store.wiki_get_provider().get_local_snapshot()
        with (
            patch.object(wiki_store, "_available_memory_mb", return_value=8192),
            patch.object(wiki_store.wiki_transport, "allowed_urlopen", return_value=response) as urlopen,
        ):
            result = wiki_store._call_local_organizer("资料.md", "text", "关键资料", snap)

        call = urlopen.call_args
        self.assertEqual(call.args[0], "http://127.0.0.1:11434/api/chat")
        body = json.loads(call.kwargs["data"].decode("utf-8"))
        headers = {key.lower(): value for key, value in (call.kwargs["headers"] or {}).items()}
        self.assertNotIn("authorization", headers)
        self.assertEqual(body["model"], "qwen3:1.7b")
        self.assertEqual(body["format"]["type"], "object")
        self.assertIn("concepts", body["format"]["properties"])
        self.assertFalse(body["think"])
        self.assertFalse(body["stream"])
        self.assertEqual(body["keep_alive"], 0)
        self.assertEqual(body["options"]["num_ctx"], 4096)
        self.assertIn("关键资料", body["messages"][1]["content"])
        self.assertEqual(result, organized)

    def test_invalid_local_response_uses_rules_fallback(self):
        response = _FakeResponse({"message": {"content": "not json"}})
        snap = wiki_store.wiki_get_provider().get_local_snapshot()
        with (
            patch.object(wiki_store, "_available_memory_mb", return_value=8192),
            patch.object(wiki_store.wiki_transport, "allowed_urlopen", return_value=response),
        ):
            result = wiki_store._call_local_organizer("资料.md", "text", "内容", snap)
        self.assertIsNone(result)

    def test_low_memory_skips_model_and_uses_rules(self):
        snap = wiki_store.wiki_get_provider().get_local_snapshot()
        with (
            patch.object(wiki_store, "_available_memory_mb", return_value=1024),
            patch.object(wiki_store.wiki_transport, "allowed_urlopen") as urlopen,
        ):
            result = wiki_store._call_local_organizer("资料.md", "text", "内容", snap)
        self.assertIsNone(result)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
