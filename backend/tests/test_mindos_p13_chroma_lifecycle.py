"""P1-3 Chroma 访问生命周期测试（索引可靠性方案 §P1-3）。

覆盖：
- 引用计数：默认 0；with operation() 进出增减；被 _tracked_operation 装饰的
  公开入口（如 read_source_chunks）调用后计数归零、不泄漏；
- 释放把关：有活跃操作时 release_chroma() 返回 False 并保持 client 不关；
  无活跃操作时返回 True、清空句柄、调用 client.close()；
- 释放后惰性重建：release 后再调用 get_collection() 自动重建连接并可用。

实现用内存 FakeClient/FakeCollection 模拟 ChromaDB，不触碰真实数据目录。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vector_store as vs


class FakeCollection:
    def __init__(self, name: str, client=None):
        self.name = name
        self._client = client
        self.records: dict[str, dict] = {}

    def count(self) -> int:
        return len(self.records)

    def get(self, ids=None, where=None, limit=None, include=None):
        return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}


class FakeClient:
    def __init__(self):
        self.collections: dict[str, FakeCollection] = {}

    def get_or_create_collection(self, name, metadata=None):
        if name not in self.collections:
            self.collections[name] = FakeCollection(name, self)
        return self.collections[name]

    def get_collection(self, name):
        return self.collections[name]

    def close(self):
        pass


class ChromaLifecycleTestBase(unittest.TestCase):
    def setUp(self):
        vs._client = None
        vs._base_client = None
        vs._delta_client = None
        vs._collection = None
        vs._image_collection = None
        vs._ACTIVE_OPS = 0
        self.fake_client = FakeClient()
        self._patch_base = patch.object(vs, "_get_base_client", return_value=self.fake_client)
        self._patch_delta = patch.object(vs, "_get_delta_client", return_value=self.fake_client)
        self._patch_base.start()
        self._patch_delta.start()
        self.addCleanup(self._patch_base.stop)
        self.addCleanup(self._patch_delta.stop)

    def tearDown(self):
        vs._client = None
        vs._base_client = None
        vs._delta_client = None
        vs._collection = None
        vs._image_collection = None
        vs._ACTIVE_OPS = 0


class TestReferenceCounting(ChromaLifecycleTestBase):
    def test_default_zero(self):
        self.assertEqual(vs.active_operations(), 0)

    def test_context_manager_bounds(self):
        self.assertEqual(vs.active_operations(), 0)
        with vs.operation():
            self.assertEqual(vs.active_operations(), 1)
            with vs.operation():
                self.assertEqual(vs.active_operations(), 2)
            self.assertEqual(vs.active_operations(), 1)
        self.assertEqual(vs.active_operations(), 0)

    def test_context_manager_releases_on_exc(self):
        with self.assertRaises(RuntimeError):
            with vs.operation():
                self.assertEqual(vs.active_operations(), 1)
                raise RuntimeError("boom")
        self.assertEqual(vs.active_operations(), 0)

    def test_decorated_entry_counts_then_releases(self):
        # 装饰过的公开入口调用期间持引用，结束后归零、不泄漏。
        status, chunks = vs.read_source_chunks("/x/absent.md")
        self.assertEqual(status, vs.READ_EMPTY)
        self.assertEqual(chunks, [])
        self.assertEqual(vs.active_operations(), 0)


class TestReleaseGate(ChromaLifecycleTestBase):
    def test_release_skips_when_busy(self):
        vs._client = MagicMock()
        with vs.operation():
            self.assertFalse(vs.release_chroma())
            self.assertIsNotNone(vs._client)

    def test_release_when_idle(self):
        client = MagicMock()
        vs._base_client = client
        vs._collection = object()
        self.assertTrue(vs.release_chroma())
        self.assertIsNone(vs._client)
        self.assertIsNone(vs._base_client)
        self.assertIsNone(vs._collection)
        client.close.assert_called_once()

    def test_release_nothing_to_close_still_true_when_idle(self):
        self.assertIsNone(vs._client)
        self.assertTrue(vs.release_chroma())


class TestLazyReinitAfterRelease(ChromaLifecycleTestBase):
    def test_get_collection_recreates_after_release(self):
        self.assertTrue(vs.release_chroma())
        self.assertIsNone(vs._client)
        # release 后惰性重建：get_collection 经统一工厂从 FakeClient 取集合，可用。
        col = vs.get_collection()
        self.assertIn(vs.CHROMA_COLLECTION, self.fake_client.collections)
        self.assertEqual(col.count(), 0)
        self.assertEqual(vs.active_operations(), 0)


if __name__ == "__main__":
    unittest.main()