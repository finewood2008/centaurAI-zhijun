"""C1：base+delta 双代际最小模型测试（generation 覆盖 + tombstone 删除）。

用两个独立 FakeClient 分别模拟 base（只读存量）与 delta（可写增量），验证：
- 写入只进 delta，base 不被修改；
- 读取 union base+delta：base 存量可见；
- 更新（delta 新代）按 generation 覆盖 base 旧代；
- 删除通过 tombstone 挡住 base 旧 chunk（防删除后复活）；
- index_registry 创建 delta 目录并递增 routing_epoch。

不触碰真实 Chroma 数据目录。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vector_store as vs
import generation_store
import index_registry


class FakeCollection:
    def __init__(self, name: str, client=None):
        self.name = name
        self._client = client
        self.records: dict[str, dict] = {}

    def count(self) -> int:
        return len(self.records)

    @staticmethod
    def _match(meta: dict, where) -> bool:
        if not where:
            return True
        if "$and" in where:
            return all(FakeCollection._match(meta, w) for w in where["$and"])
        return all(meta.get(k) == v for k, v in where.items())

    def add(self, ids, embeddings, documents, metadatas):
        for i in range(len(ids)):
            self.records[ids[i]] = {
                "embedding": list(embeddings[i]),
                "document": documents[i],
                "metadata": dict(metadatas[i]),
            }

    def get(self, ids=None, where=None, limit=None, include=None):
        rows = [
            (cid, r) for cid, r in self.records.items()
            if (ids is None or cid in ids) and self._match(r["metadata"], where)
        ]
        if limit is not None:
            rows = rows[:limit]
        return {
            "ids": [cid for cid, _ in rows],
            "documents": [r["document"] for _, r in rows],
            "metadatas": [r["metadata"] for _, r in rows],
            "embeddings": [r["embedding"] for _, r in rows],
        }

    def delete(self, ids=None, where=None):
        targets = [
            cid for cid, r in self.records.items()
            if (ids is None or cid in ids) and self._match(r["metadata"], where)
        ]
        for cid in targets:
            self.records.pop(cid, None)

    def query(self, query_embeddings, n_results, where=None, include=None):
        rows = [(cid, r) for cid, r in self.records.items()
                if self._match(r["metadata"], where)]
        rows = rows[:n_results]
        return {
            "ids": [[cid for cid, _ in rows]],
            "documents": [[r["document"] for _, r in rows]],
            "metadatas": [[r["metadata"] for _, r in rows]],
            "distances": [[0.1] * len(rows)],
        }


class FakeClient:
    def __init__(self):
        self.collections: dict[str, FakeCollection] = {}

    def list_collections(self):
        return list(self.collections)

    def get_or_create_collection(self, name, metadata=None):
        if name not in self.collections:
            self.collections[name] = FakeCollection(name, self)
        return self.collections[name]

    def get_collection(self, name):
        return self.collections[name]


class BaseDeltaTest(unittest.TestCase):
    SP = "/data/docs/a.md"

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        generation_store.reset_for_tests(self._tmp / "gen.db")
        index_registry.reset_for_tests(self._tmp / "index_registry.db")
        vs._REGISTERED_COLLECTIONS.clear()
        vs._REBUILD_TARGETS.clear()
        vs._collection = None
        vs._image_collection = None
        vs._base_client = None
        vs._delta_client = None
        vs.reset_index_health()
        self.base_client = FakeClient()
        self.delta_client = FakeClient()
        self._pb = patch.object(vs, "_get_base_client", return_value=self.base_client)
        self._pd = patch.object(vs, "_get_delta_client", return_value=self.delta_client)
        self._pb.start()
        self._pd.start()
        self.addCleanup(self._pb.stop)
        self.addCleanup(self._pd.stop)
        self.addCleanup(self._reset)
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))

    def _reset(self):
        vs._REGISTERED_COLLECTIONS.clear()
        vs._REBUILD_TARGETS.clear()
        vs._collection = None
        vs._image_collection = None
        vs._base_client = None
        vs._delta_client = None
        vs.reset_index_health()
        generation_store.reset_for_tests(None)
        index_registry.reset_for_tests(None)

    def _base_col(self):
        return self.base_client.get_or_create_collection(vs.CHROMA_COLLECTION)

    def _delta_col(self):
        return self.delta_client.get_or_create_collection(vs.CHROMA_COLLECTION)

    def _seed_base(self, sp: str, text: str, gen: int, content_hash: str = "h"):
        """直接在 base 侧放置一条当前代的存量记录。"""
        col = self._base_col()
        col.records[f"{sp}::g{gen}::0"] = {
            "embedding": [0.1, 0.2],
            "document": text,
            "metadata": {"source_path": sp, "generation": gen, "chunk_index": 0,
                         "chunk_count": 1, "content_hash": content_hash,
                         "file_type": "text", "schema_version": vs.SCHEMA_VERSION,
                         "model_id": vs.TEXT_MODEL_ID},
        }
        generation_store.set_generation(generation_store.COLLECTION_TEXT, sp, gen)

    def test_write_goes_to_delta_only(self):
        ok = vs.add_file_chunks(
            self.SP, "text", ["新写入"], [[0.1, 0.2]],
            {"file_name": "a.md", "content_hash": "h1"},
        )
        self.assertTrue(ok)
        self.assertEqual(self._delta_col().count(), 1)
        # base 只读：未被写入
        self.assertEqual(self._base_col().count(), 0)

    def test_union_read_sees_base_snapshot(self):
        self._seed_base(self.SP, "base存量", 1)
        status, chunks = vs.read_source_chunks(self.SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual(chunks[0]["text"], "base存量")

    def test_update_supersedes_base_by_generation(self):
        self._seed_base(self.SP, "base旧版", 1)
        ok = vs.add_file_chunks(
            self.SP, "text", ["delta新版"], [[0.1, 0.2]],
            {"file_name": "a.md", "content_hash": "h2"},
        )
        self.assertTrue(ok)
        # 读取只返回 delta 新版（base 旧代按 generation 过滤）
        status, chunks = vs.read_source_chunks(self.SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual(chunks[0]["text"], "delta新版")
        # 检索同样不泄漏 base 旧代
        hits = vs.search([0.1, 0.2], n_results=10)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["text"], "delta新版")

    def test_delete_tombstones_hides_base(self):
        self._seed_base(self.SP, "base存量", 1)
        ok = vs.delete_file(self.SP)
        self.assertTrue(ok)
        # tombstone 挡住 base 旧 chunk：读取为空、检索无结果
        status, chunks = vs.read_source_chunks(self.SP)
        self.assertEqual(status, vs.READ_EMPTY)
        self.assertEqual(chunks, [])
        self.assertTrue(index_registry.is_tombstoned(self.SP))
        self.assertEqual(vs.search([0.1, 0.2], n_results=10), [])
        self.assertEqual(vs.list_documents()["total"], 0)
        self.assertEqual(vs.get_stats()["total_documents"], 0)

    def test_registry_creates_delta_and_bumps_epoch(self):
        r1 = index_registry.ensure_delta()
        self.assertTrue(r1["ok"])
        self.assertTrue(r1["delta_path"])
        delta_dir = Path(r1["delta_path"])
        self.assertTrue(delta_dir.is_dir())
        r2 = index_registry.ensure_delta()  # 幂等复用
        self.assertEqual(r2["delta_generation_id"], r1["delta_generation_id"])
        # 再次 create_delta 应新建下一代（供 C2 合并切换用），epoch 递增
        r3 = index_registry.create_delta()
        self.assertNotEqual(r3["delta_generation_id"], r1["delta_generation_id"])
        self.assertGreater(r3["routing_epoch"], r2["routing_epoch"])

    def test_delta_corruption_detected_by_health(self):
        """delta 损坏必须被健康检查探测到并记录（不因 ensure_delta 静默替换而丢失观测）。"""
        # 预置：注册表已有 delta 代际。
        index_registry.ensure_delta()
        # base 侧健康集合：让 base 探测通过。
        self.base_client.get_or_create_collection(vs.CHROMA_COLLECTION)
        # delta 探测 client：读取抛错（HNSW 损坏）。
        failing = MagicMock()
        failing.count.side_effect = RuntimeError("hnsw read failure")
        failing.get.side_effect = RuntimeError("hnsw read failure")
        probe = MagicMock()
        probe.list_collections.return_value = [vs.CHROMA_COLLECTION]
        probe.get_collection.return_value = failing
        with patch.object(vs, "_probe_delta_client", return_value=probe):
            result = vs.verify_chroma_health()
        # base 正常 → 整体 ok；delta 损坏 → delta_ok=False 且被记录
        self.assertTrue(result["ok"])
        self.assertFalse(result["delta_ok"])
        self.assertEqual(vs.index_health_state(), vs.INDEX_STATE_HEALTHY)
        status = index_registry.storage_status()
        self.assertEqual(status["delta_status"], index_registry.STATUS_CORRUPTED)

    def test_corrupted_delta_replaced_on_next_ensure(self):
        """损坏 delta 在下一次 ensure_delta 时被替换为新代，旧代状态保留（可观测）。"""
        r1 = index_registry.ensure_delta()
        self.assertTrue(r1["ok"])
        index_registry.set_generation_status(r1["delta_generation_id"], index_registry.STATUS_CORRUPTED)
        r2 = index_registry.ensure_delta()
        self.assertNotEqual(r2["delta_generation_id"], r1["delta_generation_id"])
        self.assertGreater(r2["routing_epoch"], r1["routing_epoch"])
        # 旧损坏代际状态保留（不因替换而丢观测），且不再被路由。
        old = index_registry.get_generation(r1["delta_generation_id"])
        self.assertEqual(old["status"], index_registry.STATUS_CORRUPTED)
        routing = index_registry.get_routing()
        self.assertEqual(routing["delta_generation_id"], r2["delta_generation_id"])

    def test_carry_over_copies_memory_and_knowledge_only(self):
        """搬运 helper：只搬记忆/知识卡片，不搬 text/image（text/image 由重建重建）。"""
        old = FakeClient()
        old.get_or_create_collection(vs.MEMORY_COLLECTION).add(
            ids=["mem-1", "mem-2"], embeddings=[[0.1, 0.2], [0.3, 0.4]],
            documents=["记忆a", "记忆b"], metadatas=[{"source": "memory/USER.md"}] * 2,
        )
        old.get_or_create_collection(vs.KNOWLEDGE_CARDS_COLLECTION).add(
            ids=["kc-1"], embeddings=[[0.5, 0.6]],
            documents=["知识卡片"], metadatas=[{"title": "t"}],
        )
        # text 集合不应被搬运（全量重建会重建 text/image）
        old.get_or_create_collection(vs.CHROMA_COLLECTION).add(
            ids=["t-1"], embeddings=[[0.7, 0.8]], documents=["文本"], metadatas=[{"source_path": "/x/a"}],
        )
        new = FakeClient()
        carried = vs._carry_over_delta_collections(old, new)
        self.assertIn(vs.MEMORY_COLLECTION, carried)
        self.assertIn(vs.KNOWLEDGE_CARDS_COLLECTION, carried)
        self.assertEqual(new.get_or_create_collection(vs.MEMORY_COLLECTION).count(), 2)
        self.assertEqual(new.get_or_create_collection(vs.KNOWLEDGE_CARDS_COLLECTION).count(), 1)
        # text 不搬运
        self.assertNotIn(vs.CHROMA_COLLECTION, new.collections)

    def test_carry_over_merges_base_and_delta_with_delta_priority(self):
        delta = FakeClient()
        base = FakeClient()
        delta.get_or_create_collection(vs.MEMORY_COLLECTION).add(
            ids=["shared"], embeddings=[[0.9, 0.8]], documents=["delta"],
            metadatas=[{"source": "memory/new.md"}],
        )
        base.get_or_create_collection(vs.MEMORY_COLLECTION).add(
            ids=["shared", "base-only"], embeddings=[[0.1, 0.2], [0.3, 0.4]],
            documents=["base-old", "base-only"],
            metadatas=[{"source": "memory/old.md"}, {"source": "memory/base.md"}],
        )
        new = FakeClient()

        vs._carry_over_delta_collections([delta, base], new)

        records = new.get_collection(vs.MEMORY_COLLECTION).records
        self.assertEqual(records["shared"]["document"], "delta")
        self.assertEqual(records["base-only"]["document"], "base-only")

    def test_carry_over_read_failure_aborts_switch(self):
        old = FakeClient()
        failing = MagicMock()
        failing.get.side_effect = RuntimeError("read failed")
        old.collections[vs.MEMORY_COLLECTION] = failing

        with self.assertRaisesRegex(Exception, "carry_read_failed"):
            vs._carry_over_delta_collections([old], FakeClient())


class RealRebuildCarryTest(unittest.TestCase):
    """端到端：全量重建（begin→commit）后，记忆/知识卡片向量不丢失（搬运到新 delta）。

    使用真实 Chroma 临时目录，验证问题 1 的修复闭环。
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        generation_store.reset_for_tests(self._tmp / "gen.db")
        index_registry.reset_for_tests(self._tmp / "index_registry.db")
        vs._base_client = vs._delta_client = None
        vs._base_client_generation_id = vs._delta_client_generation_id = None
        vs._collection = vs._image_collection = None
        vs._REBUILD_TARGETS.clear()
        vs._REGISTERED_COLLECTIONS.clear()
        vs.reset_index_health()
        self.addCleanup(self._reset_globals)
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))

    def _reset_globals(self):
        vs._base_client = vs._delta_client = None
        vs._base_client_generation_id = vs._delta_client_generation_id = None
        vs._collection = vs._image_collection = None
        vs._rebuild_client = None
        vs._rebuild_generation_id = None
        vs._REBUILD_TARGETS.clear()
        vs._REGISTERED_COLLECTIONS.clear()
        vs.reset_index_health()
        generation_store.reset_for_tests(None)
        index_registry.reset_for_tests(None)

    def test_rebuild_preserves_memory_and_knowledge(self):
        # 往当前 delta 写入一条记忆与一条知识卡片向量
        vs.get_or_create_collection(vs.MEMORY_COLLECTION).add(
            ids=["mem-1"], embeddings=[[0.1, 0.2]],
            documents=["记忆内容"], metadatas=[{"source": "memory/USER.md"}],
        )
        vs.get_or_create_collection(vs.KNOWLEDGE_CARDS_COLLECTION).add(
            ids=["kc-1"], embeddings=[[0.3, 0.4]],
            documents=["知识卡片"], metadatas=[{"title": "t"}],
        )

        # 全量重建
        self.assertTrue(vs.begin_rebuild()["ok"])
        self.assertTrue(vs.commit_rebuild()["ok"])

        # 提交后：新 delta 应包含搬运来的记忆/知识卡片
        self.assertEqual(
            vs._get_delta_client().get_collection(vs.MEMORY_COLLECTION).count(), 1)
        self.assertEqual(
            vs._get_delta_client().get_collection(vs.KNOWLEDGE_CARDS_COLLECTION).count(), 1)
        # base（新重建目录）只预建空占位集合；旧数据必须经搬运进入新 delta，而非留在 base
        self.assertEqual(vs._get_base_client().get_collection(vs.MEMORY_COLLECTION).count(), 0)


if __name__ == "__main__":
    unittest.main()
