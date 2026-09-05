"""图片 VLM 自动描述（方案 A）回归测试。

覆盖降级契约：开关关闭、文件不存在、Ollama 不可用均返回空串，绝不向上抛异常；
成功时返回模型生成的描述文本。仅依赖标准库 + embedder，可独立运行：
    .venv\\Scripts\\python.exe -m unittest test_vlm_caption -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from embedder import caption_image_with_vlm, caption_image_with_vlm_result


def _make_image() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(b"fake image payload")
    tmp.close()
    return tmp.name


def _runtime_provider():
    return SimpleNamespace(
        store=object(),
        get_local_snapshot=lambda: SimpleNamespace(
            base_url="http://10.0.0.8:11434", model="qwen3-vl:2b", timeout_seconds=75,
        ),
    )


class VlmCaptionTests(unittest.TestCase):
    def test_disabled_returns_empty(self):
        with patch("embedder.VLM_CAPTION_ENABLED", False):
            self.assertEqual(caption_image_with_vlm("whatever.png"), "")

    def test_missing_file_returns_empty(self):
        with patch("embedder.VLM_CAPTION_ENABLED", True):
            self.assertEqual(caption_image_with_vlm("no_such_file.png"), "")

    def test_ollama_unavailable_returns_empty(self):
        path = _make_image()
        try:
            with patch("embedder.VLM_CAPTION_ENABLED", True), patch(
                "mindos.runtime_config_provider.get_provider", return_value=_runtime_provider()
            ), patch(
                "mindos.llm_transport.allowed_urlopen", side_effect=OSError("connection refused")
            ):
                self.assertEqual(caption_image_with_vlm(path), "")
        finally:
            os.unlink(path)

    def test_success_returns_chat_content_with_thinking_disabled(self):
        path = _make_image()
        try:
            class _Resp:
                def read(self):
                    return json.dumps(
                        {"message": {"content": "一条红色的锦鲤在水中游动"}}, ensure_ascii=False
                    ).encode("utf-8")

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            with patch("embedder.VLM_CAPTION_ENABLED", True), patch(
                "mindos.runtime_config_provider.get_provider", return_value=_runtime_provider()
            ), patch("mindos.llm_transport.allowed_urlopen", return_value=_Resp()) as request:
                self.assertEqual(caption_image_with_vlm(path), "一条红色的锦鲤在水中游动")
            self.assertEqual(request.call_args.kwargs["timeout"], 75)
            self.assertIn("http://10.0.0.8:11434/api/chat", request.call_args.args)
            body = json.loads(request.call_args.kwargs["data"].decode("utf-8"))
            self.assertEqual(body["model"], "qwen3-vl:2b")
            self.assertFalse(body["think"])
            self.assertEqual(body["messages"][0]["images"], ["ZmFrZSBpbWFnZSBwYXlsb2Fk"])
        finally:
            os.unlink(path)

    def test_thinking_without_content_is_not_used_as_caption(self):
        path = _make_image()
        try:
            class _Resp:
                def read(self):
                    return json.dumps(
                        {"message": {"content": "", "thinking": "图片中似乎有一条鱼"}}, ensure_ascii=False
                    ).encode("utf-8")

            with patch("embedder.VLM_CAPTION_ENABLED", True), patch(
                "mindos.runtime_config_provider.get_provider", return_value=_runtime_provider()
            ), patch("mindos.llm_transport.allowed_urlopen", return_value=_Resp()):
                self.assertEqual(caption_image_with_vlm(path), "")
        finally:
            os.unlink(path)

    def test_empty_content_returns_actionable_reason(self):
        path = _make_image()
        try:
            class _Resp:
                def read(self):
                    return json.dumps({"message": {"content": ""}}).encode("utf-8")

            with patch("embedder.VLM_CAPTION_ENABLED", True), patch(
                "mindos.runtime_config_provider.get_provider", return_value=_runtime_provider()
            ), patch("mindos.llm_transport.allowed_urlopen", return_value=_Resp()):
                self.assertEqual(caption_image_with_vlm_result(path), ("", "empty_response"))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
