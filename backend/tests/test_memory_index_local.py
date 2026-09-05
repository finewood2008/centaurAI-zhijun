"""Offline-only encoder loading: fake model construction, no downloads or real weights."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

import embedder
from mindos.zhijun import memory_index


class LocalEncoderTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name)
        (self.path / "config.json").write_text("{}", encoding="utf-8")
        self.model = SimpleNamespace(encode=Mock(side_effect=lambda texts, **kw: [[1., 0.] for _ in texts]))
        self.constructor = Mock(return_value=self.model)
        self.patches = [
            patch.object(embedder, "_text_model", None),
            patch.object(embedder, "_local_text_failure", None),
            patch.object(embedder, "TEXT_MODEL_ACTIVE", str(self.path)),
            patch.object(embedder, "_get_device", return_value="cpu"),
            patch.object(memory_index, "_IMPORT_FAILED", False),
            patch.dict("sys.modules", {"sentence_transformers": SimpleNamespace(SentenceTransformer=self.constructor)}),
        ]
        for guard in self.patches:
            guard.start()
        memory_index.CACHE.clear()

    def tearDown(self):
        memory_index.CACHE.clear()
        for guard in reversed(self.patches):
            guard.stop()
        self.directory.cleanup()

    def test_installed_path_loads_once_shared_and_strictly_offline(self):
        self.assertIs(embedder.get_local_text_embedder(), self.model)
        self.assertIs(embedder.get_local_text_embedder(), self.model)
        self.assertIs(embedder.get_text_embedder(), self.model)
        self.constructor.assert_called_once_with(str(self.path.resolve()), device="cpu", local_files_only=True, trust_remote_code=False)
        self.assertIs(embedder._text_model, self.model)

    def test_loaded_encoder_is_reused_without_disk_or_constructor(self):
        embedder._text_model = self.model
        with patch.object(embedder, "TEXT_MODEL_ACTIVE", "not-a-local-path"):
            self.assertIs(embedder.get_local_text_embedder(), self.model)
        self.constructor.assert_not_called()

    def test_hub_id_missing_or_incomplete_path_never_downloads_or_retries(self):
        for path in ("BAAI/bge-small-zh-v1.5", "https://example.invalid/model", str(self.path / "missing")):
            with self.subTest(path=path), patch.object(embedder, "TEXT_MODEL_ACTIVE", path), self.assertLogs("embedder", level="WARNING") as log:
                self.assertIsNone(embedder.get_local_text_embedder())
                self.assertIsNone(embedder.get_local_text_embedder())
                self.assertEqual(len(log.output), 1)
                self.assertIn("使用关键词检索", log.output[0])
        self.constructor.assert_not_called()
        (self.path / "config.json").unlink()
        self.assertIsNone(embedder.get_local_text_embedder())
        self.constructor.assert_not_called()

    def test_failed_initialization_does_not_repeat_until_path_changes(self):
        self.constructor.side_effect = OSError("incomplete local files")
        with self.assertLogs("embedder", level="WARNING") as log:
            self.assertIsNone(embedder.get_local_text_embedder())
            self.assertIsNone(embedder.get_local_text_embedder())
        self.assertEqual(len(log.output), 1)
        self.constructor.assert_called_once()
        self.assertIsNone(embedder._text_model, "failed partial initialization is never published")
        other = self.path / "repaired-model"
        other.mkdir()
        (other / "modules.json").write_text("[]", encoding="utf-8")
        self.constructor.side_effect = None
        with patch.object(embedder, "TEXT_MODEL_ACTIVE", str(other)):
            self.assertIs(embedder.get_local_text_embedder(), self.model)
        self.assertEqual(self.constructor.call_count, 2)

    def test_concurrent_initialization_uses_embedder_lock(self):
        gate = threading.Barrier(4)
        def get():
            gate.wait()
            return embedder.get_local_text_embedder()
        with ThreadPoolExecutor(max_workers=4) as pool:
            models = list(pool.map(lambda _: get(), range(4)))
        self.assertTrue(all(model is self.model for model in models))
        self.constructor.assert_called_once()

    def test_index_loads_all_documents_locally_and_rebuilds_versions(self):
        docs = {f"c{i}": f"合成内容 {i}" for i in range(65)}
        self.assertEqual(len(memory_index.scores("synthetic", "一个问题", docs)), 65)
        self.constructor.assert_called_once()
        count = self.model.encode.call_count
        memory_index.scores("synthetic", "一个问题", docs)
        self.assertEqual(self.model.encode.call_count, count)
        docs.pop("c2")
        docs["c1"] = "新的内容版本"
        result = memory_index.scores("synthetic", "一个问题", docs)
        self.assertNotIn("c2", result)
        self.assertEqual(self.model.encode.call_count, count + 1)
        self.assertEqual(self.model.encode.call_args.args[0], ["新的内容版本"])

    def test_empty_index_does_not_load_and_missing_encoder_clears_cache(self):
        self.assertEqual(memory_index.scores("empty", "问题", {}), {})
        self.constructor.assert_not_called()
        memory_index.scores("synthetic", "问题", {"one": "内容"})
        self.assertIn("synthetic", memory_index.CACHE)
        embedder._text_model = None
        with patch.object(embedder, "TEXT_MODEL_ACTIVE", str(self.path / "missing")):
            self.assertEqual(memory_index.scores("synthetic", "问题", {"one": "内容"}), {})
        self.assertNotIn("synthetic", memory_index.CACHE)

    def test_index_can_start_before_embedder_import_without_general_loader(self):
        shared = SimpleNamespace(_text_model=None, get_local_text_embedder=Mock(return_value=self.model),
                                 get_text_embedder=Mock(side_effect=AssertionError("must not use network-capable loader")))
        with patch.dict("sys.modules", {"embedder": None}), patch.object(memory_index.importlib, "import_module", return_value=shared) as load:
            result = memory_index.scores("cold", "问题", {"c1": "内容"})
        self.assertEqual(result, {"c1": 1.0})
        load.assert_called_once_with("embedder")
        shared.get_local_text_embedder.assert_called_once()
        shared.get_text_embedder.assert_not_called()

    def test_component_import_failure_is_cached_and_falls_back(self):
        with patch.dict("sys.modules", {"embedder": None}), patch.object(memory_index.importlib, "import_module", side_effect=ImportError("synthetic missing")) as load, self.assertLogs(memory_index.logger, level="WARNING"):
            self.assertEqual(memory_index.scores("cold", "问题", {"c1": "内容"}), {})
            self.assertEqual(memory_index.scores("cold", "问题", {"c1": "内容"}), {})
        load.assert_called_once()


if __name__ == "__main__":
    unittest.main()
