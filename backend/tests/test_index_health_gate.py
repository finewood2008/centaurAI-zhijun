"""阶段B：索引健康闸门测试（D4 损坏策略 + 巡检禁重入队）。

覆盖：
- 健康状态默认 unknown；corrupted 时写路径抛 IndexCorruptedError（闸门生效）；
- verify_chroma_health 在空库（集合未创建）时判定 healthy、不误报损坏；
- verify_chroma_health 在集合读取失败时判定 corrupted 并持久化到 index_registry；
- watcher.submit_index 在 corrupted 时拒绝入队（返回 False）；
- index_registry 路由与 storage_status 不泄露内部绝对路径。

用内存 FakeClient/FakeCollection 模拟 ChromaDB，不触碰真实数据目录。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index_registry
import vector_store as vs
import watcher


class FailingCollection:
    """count/get 抛异常，模拟 HNSW 损坏后的读取失败。"""

    def count(self) -> int:
        raise RuntimeError("HNSW read failure")

    def get(self, *args, **kwargs):
        raise RuntimeError("HNSW read failure")


class HealthyCollection:
    def __init__(self, name: str):
        self.name = name
        self.records: dict[str, dict] = {}

    def count(self) -> int:
        return len(self.records)

    def get(self, ids=None, where=None, limit=None, include=None):
        return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}

    def query(self, *args, **kwargs):
        return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}


class FakeClient:
    def __init__(self, collections: dict | None = None, fail_list: bool = False):
        self._cols = collections or {}
        self.fail_list = fail_list
        self.closed = False

    def list_collections(self):
        if self.fail_list:
            raise RuntimeError("list_collections failure")
        return list(self._cols.keys())

    def get_collection(self, name):
        return self._cols[name]

    def get_or_create_collection(self, name, metadata=None):
        if name not in self._cols:
            self._cols[name] = HealthyCollection(name)
        return self._cols[name]

    def close(self):
        self.closed = True


def _patch_client(fake: FakeClient):
    """同时替换 base/delta client，返回 (base_patch, delta_patch) 供 addCleanup。"""
    p_base = patch.object(vs, "_get_base_client", return_value=fake)
    p_delta = patch.object(vs, "_get_delta_client", return_value=fake)
    p_base.start()
    p_delta.start()
    return p_base, p_delta


class IndexHealthGateTestBase(unittest.TestCase):
    def setUp(self):
        vs._client = None
        vs._collection = None
        vs._image_collection = None
        vs._ACTIVE_OPS = 0
        vs._REGISTERED_COLLECTIONS.clear()
        vs._REBUILD_TARGETS.clear()
        vs.reset_index_health()
        # 独立注册表，避免测试间污染
        index_registry.reset_for_tests()

    def tearDown(self):
        vs.reset_index_health()
        index_registry.reset_for_tests()


class TestHealthState(IndexHealthGateTestBase):
    def test_default_unknown_not_blocked(self):
        self.assertEqual(vs.index_health_state(), vs.INDEX_STATE_UNKNOWN)
        self.assertFalse(vs.index_health_blocked())

    def test_corrupted_blocks_writes(self):
        vs.set_index_health_state(vs.INDEX_STATE_CORRUPTED)
        self.assertTrue(vs.index_health_blocked())
        with self.assertRaises(vs.IndexCorruptedError):
            vs.add_file_chunks("/x/a.txt", "text", ["c"], [[0.1]], {"file_name": "a"})

    def test_healthy_not_blocked(self):
        vs.set_index_health_state(vs.INDEX_STATE_HEALTHY)
        self.assertFalse(vs.index_health_blocked())

    def test_runtime_hnsw_failure_marks_index_corrupted(self):
        """不能只依赖启动自检；运行中的 HNSW 故障必须立即关闭后续入口。"""
        changed = vs.record_index_operation_failure(
            RuntimeError("Error loading hnsw index"), "add_file_chunks"
        )
        self.assertTrue(changed)
        self.assertTrue(vs.index_health_blocked())
        self.assertEqual(
            index_registry.storage_status()["base_status"],
            index_registry.STATUS_CORRUPTED,
        )

    def test_collection_factory_refuses_access_when_corrupted(self):
        vs.set_index_health_state(vs.INDEX_STATE_CORRUPTED)
        with self.assertRaises(vs.IndexCorruptedError):
            vs.get_or_create_collection("memory")


class TestHealthCheckGates(IndexHealthGateTestBase):
    def test_empty_db_is_healthy_not_corrupted(self):
        # 全新数据目录：受管集合全部未创建 -> ok（不得误报损坏）。
        fake = FakeClient()
        patcher = _patch_client(fake)
        for p in patcher:
            self.addCleanup(p.stop)
        result = vs.verify_chroma_health()
        self.assertTrue(result["ok"])
        self.assertEqual(vs.index_health_state(), vs.INDEX_STATE_HEALTHY)

    def test_corrupted_collection_flips_state_and_persists(self):
        fake = FakeClient({vs.CHROMA_COLLECTION: FailingCollection()})
        patcher = _patch_client(fake)
        for p in patcher:
            self.addCleanup(p.stop)
        result = vs.verify_chroma_health()
        self.assertFalse(result["ok"])
        self.assertEqual(vs.index_health_state(), vs.INDEX_STATE_CORRUPTED)
        # 持久化到注册表：活跃 base 代际标记为 corrupted
        status = index_registry.storage_status()
        self.assertEqual(status["base_status"], index_registry.STATUS_CORRUPTED)

    def test_list_collections_failure_is_corrupted(self):
        fake = FakeClient(fail_list=True)
        patcher = _patch_client(fake)
        for p in patcher:
            self.addCleanup(p.stop)
        result = vs.verify_chroma_health()
        self.assertFalse(result["ok"])
        self.assertEqual(vs.index_health_state(), vs.INDEX_STATE_CORRUPTED)

    def test_write_gate_on_corrupted(self):
        vs.set_index_health_state(vs.INDEX_STATE_CORRUPTED)
        with self.assertRaises(vs.IndexCorruptedError):
            vs.delete_file("/x/a.txt")
        with self.assertRaises(vs.IndexCorruptedError):
            vs.add_image_vector("/x/a.png", [0.1, 0.2], {"file_name": "a"})


class TestWatcherGate(IndexHealthGateTestBase):
    def test_submit_index_refused_when_corrupted(self):
        vs.set_index_health_state(vs.INDEX_STATE_CORRUPTED)
        self.assertFalse(watcher.submit_index("C:/tmp/not-exist.pdf"))

    def test_index_file_refused_when_corrupted(self):
        vs.set_index_health_state(vs.INDEX_STATE_CORRUPTED)
        with self.assertRaises(vs.IndexCorruptedError):
            watcher.index_file("C:/tmp/not-exist.pdf")


class TestRegistryView(IndexHealthGateTestBase):
    def test_storage_status_hides_absolute_path(self):
        index_registry.ensure_registry()
        status = index_registry.storage_status()
        # 代际标识允许暴露，但内部绝对路径与密钥不得出现
        self.assertIsNotNone(status["base_generation_id"])
        self.assertNotIn("path", status)
        blob = str(status)
        self.assertNotIn("chroma_data", blob)
        self.assertNotIn("\\\\", blob)  # 反斜杠路径分隔符不泄漏
        self.assertNotIn("index_registry.db", blob)

    def test_generation_status_persists(self):
        routing = index_registry.ensure_registry()
        base_id = routing["base_generation_id"]
        index_registry.set_generation_status(base_id, index_registry.STATUS_CORRUPTED)
        gen = index_registry.get_generation(base_id)
        self.assertEqual(gen["status"], index_registry.STATUS_CORRUPTED)


if __name__ == "__main__":
    unittest.main()
