"""P1-2 维护操作与普通查询隔离测试（索引可靠性方案 §7.4 / 双集合隔离）。

覆盖场景：
- 重建开启：建好 __rebuild 集合、写目标切换、active 集合仍在线可检索；
- 写/读双目标：重建期间 add_file_chunks 写进 __rebuild 集合，read_source_chunks
  仍从 active 集合读旧数据（查询零中断）；generation 落在 {kind}::rebuild 隔离命名空间；
- 原子切换：commit 后 active 集合改为新数据，旧集合改 __obsolete 后删除，
  generation 由 text::rebuild 并回 text；
- 失败保持：commit 冒烟校验失败（rebuild 集合不可读）→ 不切换、旧集合在线；
- 中止清理：abort 删除 __rebuild 集合、丢弃隔离命名空间、active 集合不变；
- 状态机：idle/rebuilding 生命周期；重复 begin 拒绝；无重建时 commit 报错。

实现用内存 FakeClient/FakeCollection 模拟 ChromaDB（含 $and where 过滤、
modify 重命名、delete_collection），regeneration 用独立临时 SQLite，不触碰真实数据目录。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vector_store as vs
import generation_store


class FakeCollection:
    """单个 Chroma collection 内存模拟（含 rename/delete、$and 过滤）。"""

    def __init__(self, name: str, client=None):
        self.name = name
        self._client = client
        self.records: dict[str, dict] = {}

    def count(self) -> int:
        return len(self.records)

    def modify(self, name=None):
        if name and self._client:
            self._client.rename_collection(self.name, name)

    def add(self, ids, embeddings, documents, metadatas):
        for i in range(len(ids)):
            if ids[i] in self.records:
                raise ValueError(f"duplicate id: {ids[i]}")
            self.records[ids[i]] = {
                "embedding": list(embeddings[i]),
                "document": documents[i],
                "metadata": dict(metadatas[i]),
            }

    @staticmethod
    def _match(meta: dict, where) -> bool:
        if not where:
            return True
        if "$and" in where:
            return all(FakeCollection._match(meta, w) for w in where["$and"])
        if "$or" in where:
            return any(FakeCollection._match(meta, w) for w in where["$or"])
        return all(meta.get(k) == v for k, v in where.items())

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


class FaultyCollection(FakeCollection):
    """count() 抛错，用于模拟 rebuild 集合不可读。"""

    def count(self) -> int:
        raise RuntimeError("simulated unreadable rebuild collection")


class FakeClient:
    """模拟 chromadb PersistentClient：按名管理集合，支持重命名/删除。"""

    def __init__(self):
        self.collections: dict[str, FakeCollection] = {}

    def list_collections(self):
        # _apply_collection_switch 经 _existing_collection_names() 探测集合存在性
        return list(self.collections)

    def get_or_create_collection(self, name, metadata=None):
        if name not in self.collections:
            self.collections[name] = FakeCollection(name, self)
        return self.collections[name]

    def get_collection(self, name):
        return self.collections[name]

    def delete_collection(self, name):
        self.collections.pop(name, None)

    def rename_collection(self, old: str, new: str):
        col = self.collections.pop(old, None)
        if col is None:
            return
        col.name = new
        self.collections[new] = col


class RebuildIsolationTestBase(unittest.TestCase):
    """公共环境：独立注册表 DB + 内存 FakeClient 双集合。"""

    SP = "/data/docs/a.md"

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        generation_store.reset_for_tests(self._tmp / "gen.db")
        self.fake_client = FakeClient()
        # 统一工厂/句柄都走 base/delta client；不 patch get_collection，
        # 让真实工厂基于 FakeClient 维护 active/rebuild 集合（base/delta 同实例）。
        self._patch_base = patch.object(vs, "_get_base_client", return_value=self.fake_client)
        self._patch_delta = patch.object(vs, "_get_delta_client", return_value=self.fake_client)
        self._patch_base.start()
        self._patch_delta.start()
        self.addCleanup(self._patch_base.stop)
        self.addCleanup(self._patch_delta.stop)
        # 确保 active 文本/图片集合在 Chroma 端也存在（与真实启动态一致），
        # 否则 commit 的 atomic switch 对 image 会因 get_collection 缺集合而抛错。
        vs.get_collection()
        vs.get_image_collection()
        # 清除模块级单例句柄与重建状态，避免跨用例污染。
        vs._client = None
        vs._collection = None
        vs._image_collection = None
        vs._REBUILD_TARGETS.clear()
        from_ = list(vs._REGISTERED_COLLECTIONS.keys())
        for k in from_:
            vs._REGISTERED_COLLECTIONS.pop(k, None)
        self.addCleanup(self._reset_globals)
        self.addCleanup(lambda: generation_store.reset_for_tests(None))
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))

    def _reset_globals(self):
        vs._client = None
        vs._collection = None
        vs._image_collection = None
        vs._REBUILD_TARGETS.clear()
        for k in list(vs._REGISTERED_COLLECTIONS.keys()):
            vs._REGISTERED_COLLECTIONS.pop(k, None)

    # ---- 便捷写入 / 读取 ----

    def _write(self, sp: str, texts: list[str], content_hash: str = "h") -> bool:
        return vs.add_file_chunks(
            sp, "text", texts, [[0.1, 0.2] for _ in texts],
            {"file_name": Path(sp).name, "content_hash": content_hash},
        )

    def _read_texts(self, sp: str) -> str:
        status, chunks = vs.read_source_chunks(sp)
        return status if status != vs.READ_OK else "|".join(c["text"] for c in chunks)

    def _rebuild_names(self) -> dict:
        return {
            "text": vs._rebuild_physical_name(vs.CHROMA_COLLECTION),
            "image": vs._rebuild_physical_name(vs.IMAGE_COLLECTION),
        }


@unittest.skip("C1 已替换为目录级 building base，不再创建 __rebuild collection")
class TestRebuildLifecycle(RebuildIsolationTestBase):
    """begin / commit / abort / status 状态机。"""

    def test_status_idle_by_default(self):
        self.assertEqual(vs.rebuild_status(), vs.REBUILD_STATE_IDLE)
        self.assertFalse(vs.rebuilding())

    def test_begin_creates_rebuild_collections_and_flips_write_target(self):
        r = vs.begin_rebuild()
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], vs.REBUILD_STATE_FULL)
        self.assertTrue(vs.rebuilding())
        self.assertEqual(vs.rebuild_status(), vs.REBUILD_STATE_FULL)
        names = self._rebuild_names()
        for n in names.values():
            self.assertIn(n, self.fake_client.collections)

    def test_double_begin_rejected(self):
        self.assertTrue(vs.begin_rebuild()["ok"])
        second = vs.begin_rebuild()
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "already_rebuilding")

    def test_begin_failure_keeps_idle(self):
        # 让 rebuild 集合创建失败：直接让 get_base_collection 抛异常以模拟 Chroma 创建失败。
        def boom(name, space="cosine"):
            raise RuntimeError("simulated create failure")

        with patch.object(vs, "get_base_collection", side_effect=boom):
            r = vs.begin_rebuild()
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], vs.REBUILD_STATE_IDLE)
        self.assertFalse(vs.rebuilding())

    def test_commit_without_rebuild_errors(self):
        r = vs.commit_rebuild()
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "no_rebuild_in_progress")


@unittest.skip("C1 已替换为目录级 building base，不再创建 __rebuild collection")
class TestWriteReadIsolation(RebuildIsolationTestBase):
    """重建期间：写进 __rebuild，读仍返回 active 旧数据。"""

    def test_write_target_switches_to_rebuild_while_read_keeps_old(self):
        self._write(self.SP, ["旧数据"])
        self.assertEqual(self._read_texts(self.SP), "旧数据")
        assert vs.begin_rebuild()["ok"]

        # 重建期间写入 → 进 __rebuild 集合
        self.assertTrue(self._write(self.SP, ["新数据"]))
        text_col = self.fake_client.collections[vs.CHROMA_COLLECTION]
        rebuild = self.fake_client.collections[self._rebuild_names()["text"]]
        self.assertEqual(self._active_texts(text_col), ["旧数据"])
        self.assertEqual(self._active_texts(rebuild), ["新数据"])

        # 读路径仍走 active → 仍见旧数据
        self.assertEqual(self._read_texts(self.SP), "旧数据")

        # generation 落到隔离命名空间，不污染 active 代数
        namespace = vs._gen_namespace("text")
        self.assertEqual(namespace, "text::rebuild")
        self.assertGreater(
            generation_store.current_generation("text::rebuild", self.SP), 0)

    @staticmethod
    def _active_texts(col):
        return [r["document"] for _, r in sorted(
            col.records.items(), key=lambda kv: kv[1]["metadata"].get("chunk_index", 0))]


@unittest.skip("C1 已替换为 registry 路由切换，不再 rename collection")
class TestAtomicSwitch(RebuildIsolationTestBase):
    """commit：active 切换为新数据，旧集合下线，generation 并回正式 token。"""

    def test_commit_switches_active_to_new_data(self):
        self._write(self.SP, ["旧数据"])
        assert vs.begin_rebuild()["ok"]
        self.assertTrue(self._write(self.SP, ["新数据"]))

        r = vs.commit_rebuild()
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], vs.REBUILD_STATE_IDLE)
        self.assertFalse(vs.rebuilding())

        # active 集合现在是新数据
        self.assertEqual(self._read_texts(self.SP), "新数据")

        # 物理集合层面：documents 保留（= 新集合），旧改 __obsolete 后被删，
        # __rebuild 集合也随改名消失。
        colls = list(self.fake_client.collections.keys())
        self.assertIn(vs.CHROMA_COLLECTION, colls)
        self.assertNotIn(self._rebuild_names()["text"], colls)
        self.assertNotIn(f"{vs.CHROMA_COLLECTION}__obsolete", colls)

        # generation 并回正式 token，隔离命名空间已清
        self.assertGreater(
            generation_store.current_generation("text", self.SP), 0)
        self.assertEqual(
            generation_store.current_generation("text::rebuild", self.SP), 0)


@unittest.skip("C1 已替换为目录级 building base，不再创建 __rebuild collection")
class TestCommitHoldBack(RebuildIsolationTestBase):
    """commit 冒烟校验失败：不切换，active 旧数据保持在线。"""

    def test_unreadable_rebuild_holds_back(self):
        self._write(self.SP, ["旧数据"])
        assert vs.begin_rebuild()["ok"]
        # 使 rebuild 集合不可读（count 抛错）
        rebuild_name = self._rebuild_names()["text"]
        self.fake_client.collections[rebuild_name] = FaultyCollection(rebuild_name)

        r = vs.commit_rebuild()
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("error", "").startswith("rebuild_validation_failed"))

        # 未切换、未清状态 → 旧数据仍在线可检索
        self.assertEqual(self._read_texts(self.SP), "旧数据")
        self.assertTrue(vs.rebuilding())

        # 中止清理后回到 idle，active 仍为旧数据
        vs.abort_rebuild()
        self.assertFalse(vs.rebuilding())
        self.assertEqual(self._read_texts(self.SP), "旧数据")


@unittest.skip("C1 已替换为目录级 building base，不再创建 __rebuild collection")
class TestAbortRebuild(RebuildIsolationTestBase):
    """abort：删 __rebuild 集合、丢隔离命名空间、active 不变。"""

    def test_abort_cleans_rebuild_keeps_active(self):
        self._write(self.SP, ["旧数据"])
        assert vs.begin_rebuild()["ok"]
        self.assertTrue(self._write(self.SP, ["新数据"]))
        # 重建命名空间已有代数
        self.assertGreater(
            generation_store.current_generation("text::rebuild", self.SP), 0)

        r = vs.abort_rebuild()
        self.assertTrue(r["ok"])
        self.assertFalse(vs.rebuilding())
        self.assertEqual(vs.rebuild_status(), vs.REBUILD_STATE_IDLE)

        # __rebuild 集合已删
        self.assertNotIn(self._rebuild_names()["text"], self.fake_client.collections)
        # 隔离命名空间已清，active 代数不受影响
        self.assertEqual(
            generation_store.current_generation("text::rebuild", self.SP), 0)
        self.assertEqual(self._read_texts(self.SP), "旧数据")


@unittest.skip("C1 使用 building generation，旧 generation token 用例已替代")
class TestFreshRebuildClearsStaleGeneration(RebuildIsolationTestBase):
    """崩溃遗留的 rebuild 注册表不能污染下一轮正式 generation。"""

    def test_begin_clears_stale_rebuild_generation_tokens(self):
        self._write(self.SP, ["旧数据"])
        generation_store.set_generation("text::rebuild", self.SP, 99)
        generation_store.set_generation("image::rebuild", self.SP, 88)

        self.assertTrue(vs.begin_rebuild()["ok"])

        self.assertEqual(generation_store.current_generation("text::rebuild", self.SP), 0)
        self.assertEqual(generation_store.current_generation("image::rebuild", self.SP), 0)


if __name__ == "__main__":
    unittest.main()
