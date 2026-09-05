"""P0-3 三态读取与完整性校验测试（索引可靠性方案 §7.3 验收）。

覆盖：
- read_source_chunks 三态：ok / empty / read_error（read_error 绝不伪装成 empty）；
- get_source_chunks 兼容包装：read_error 返回 []（降级当轮结果，不落持久状态）；
- verify_source_index：正常 ok、缺中间块、chunk_index 重复、chunk_count 不符、
  hash 不一致、无记录 not_indexed、读取失败 read_error、纯图回落图片集合；
- get_source_hash：仅在完整性校验通过时返回 hash，否则 None（触发安全重建）；
- derived 派生链路（2026-08-22 事故验收化）：
  * read_error 且已有 ok 记录 → 不被覆盖（保持 ok）；
  * read_error 且无记录 → 写可重试的 unavailable，绝不写 skipped；
  * 真 empty（无 ok 记录）→ skipped（原有契约保持）。

全部 mock ChromaDB collection，不触碰真实数据目录。
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
from mindos import derived
from mindos.stores import derived_store


def _col_result(ids=None, metas=None, docs=None, embs=None):
    """构造 Chroma get() 的返回结构。

    修复2 契约：完整性校验要求每条记录都有非空 document 与有效 embedding，
    未显式给出时自动补齐（各测试聚焦 metadata 层面的完整性判定）。
    """
    ids = ids or []
    n = len(ids) or len(metas or [])
    if docs is None:
        docs = ["块文本"] * n
    if embs is None:
        embs = [[0.1] * 8 for _ in range(n)]
    return {
        "ids": ids,
        "metadatas": metas or [],
        "documents": docs,
        "embeddings": embs,
    }


def _chunk_meta(idx, n, content_hash="h1"):
    return {"chunk_index": idx, "chunk_count": n, "content_hash": content_hash,
            "source_path": "/x/a.md", "modality": "text"}


class TestReadSourceChunks(unittest.TestCase):
    """三态读取契约。"""

    def _patch_col(self, col):
        return patch.object(vs, "get_collection", return_value=col)

    def test_ok(self):
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["/x/a.md::0", "/x/a.md::1"],
            metas=[_chunk_meta(0, 2), _chunk_meta(1, 2)],
            docs=["第一块", "第二块"],
        )
        with self._patch_col(col):
            status, chunks = vs.read_source_chunks("/x/a.md")
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["text"], "第一块")

    def test_empty(self):
        col = MagicMock()
        col.get.return_value = _col_result()
        with self._patch_col(col):
            status, chunks = vs.read_source_chunks("/x/a.md")
        self.assertEqual(status, vs.READ_EMPTY)
        self.assertEqual(chunks, [])

    def test_read_error(self):
        col = MagicMock()
        col.get.side_effect = RuntimeError("hnsw index load failed")
        with self._patch_col(col):
            status, chunks = vs.read_source_chunks("/x/a.md")
        self.assertEqual(status, vs.READ_ERROR, "读取异常必须显式返回 read_error，不得伪装成 empty")
        self.assertEqual(chunks, [])

    def test_compat_wrapper_returns_empty_on_error(self):
        """兼容包装：read_error 返回 []（展示路径降级用），但三态接口可区分。"""
        col = MagicMock()
        col.get.side_effect = RuntimeError("db locked")
        with self._patch_col(col):
            self.assertEqual(vs.get_source_chunks("/x/a.md"), [])

    def test_chunks_sorted_by_start_time(self):
        col = MagicMock()
        m1 = _chunk_meta(1, 2); m1["start_time"] = 30.0
        m0 = _chunk_meta(0, 2); m0["start_time"] = 0.0
        col.get.return_value = _col_result(
            ids=["/x/a.md::1", "/x/a.md::0"], metas=[m1, m0], docs=["后", "前"]
        )
        with self._patch_col(col):
            _, chunks = vs.read_source_chunks("/x/a.md")
        self.assertEqual([c["text"] for c in chunks], ["前", "后"])


class TestEffectiveRead(unittest.TestCase):
    def test_numpy_style_embeddings_do_not_use_boolean_coercion(self):
        """Chroma 可返回 ndarray；聚合时只能检查 None，不能使用 ``or []``。"""
        class ArrayLike(list):
            def __bool__(self):
                raise ValueError("The truth value of an empty array is ambiguous")

        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0"], metas=[_chunk_meta(0, 1)], embs=ArrayLike([[0.1] * 8])
        )
        with patch.object(vs, "_union_collections", return_value=[col]):
            result = vs._read_effective("text", "/x/a.md")
        self.assertEqual(result["ids"], ["a::0"])
        self.assertEqual(result["embeddings"], [[0.1] * 8])


class TestVerifySourceIndex(unittest.TestCase):
    """完整性校验（方案 §7.3：缺块/重复/数量错/hash 错都必须 integrity_failed）。"""

    def _patch_cols(self, text_col, image_col):
        p1 = patch.object(vs, "get_collection", return_value=text_col)
        p2 = patch.object(vs, "get_image_collection", return_value=image_col)
        return p1, p2

    def _run(self, text_col, image_col=None, **kwargs):
        image_col = image_col if image_col is not None else MagicMock()
        with patch.object(vs, "get_collection", return_value=text_col), \
             patch.object(vs, "get_image_collection", return_value=image_col):
            return vs.verify_source_index("/x/a.md", **kwargs)

    def test_ok(self):
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::1", "a::2"],
            metas=[_chunk_meta(0, 3), _chunk_meta(1, 3), _chunk_meta(2, 3)],
        )
        self.assertEqual(self._run(col), vs.VERIFY_OK)

    def test_missing_middle_chunk(self):
        # 缺 chunk_index=1：3 块声明的 chunk_count=3 但实际 2 条且 index 跳号
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::2"], metas=[_chunk_meta(0, 3), _chunk_meta(2, 3)]
        )
        self.assertEqual(self._run(col), vs.VERIFY_INTEGRITY_FAILED)

    def test_duplicate_chunk_index(self):
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::0"], metas=[_chunk_meta(0, 2), _chunk_meta(0, 2)]
        )
        self.assertEqual(self._run(col), vs.VERIFY_INTEGRITY_FAILED)

    def test_chunk_count_mismatch(self):
        # 声明 chunk_count=5 实际 3 条 → 部分写入
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::1", "a::2"],
            metas=[_chunk_meta(0, 5), _chunk_meta(1, 5), _chunk_meta(2, 5)],
        )
        self.assertEqual(self._run(col), vs.VERIFY_INTEGRITY_FAILED)

    def test_inconsistent_hash(self):
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::1"],
            metas=[_chunk_meta(0, 2, "h1"), _chunk_meta(1, 2, "h2")],
        )
        self.assertEqual(self._run(col), vs.VERIFY_INTEGRITY_FAILED)

    def test_expected_hash_mismatch(self):
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::1"], metas=[_chunk_meta(0, 2), _chunk_meta(1, 2)]
        )
        self.assertEqual(
            self._run(col, expected_hash="other"), vs.VERIFY_INTEGRITY_FAILED
        )

    def test_expected_chunk_count_mismatch(self):
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::1"], metas=[_chunk_meta(0, 2), _chunk_meta(1, 2)]
        )
        # 第一次调用（文本校验）与第二次（块数校验）返回同一结构
        self.assertEqual(
            self._run(col, expected_chunk_count=5), vs.VERIFY_INTEGRITY_FAILED
        )

    def test_not_indexed(self):
        col = MagicMock()
        col.get.return_value = _col_result()
        img = MagicMock()
        img.get.return_value = _col_result()
        self.assertEqual(self._run(col, img), vs.VERIFY_NOT_INDEXED)

    def test_read_error(self):
        col = MagicMock()
        col.get.side_effect = RuntimeError("corrupt")
        self.assertEqual(self._run(col), vs.VERIFY_READ_ERROR)

    def test_pure_image_falls_back_to_image_collection(self):
        """纯图：文本集合无记录 → 图片集合有 → ok。"""
        col = MagicMock()
        col.get.return_value = _col_result()
        img = MagicMock()
        img.get.return_value = _col_result(metas=[{"content_hash": "img-h"}])
        self.assertEqual(self._run(col, img), vs.VERIFY_OK)

    def test_image_record_missing_hash(self):
        col = MagicMock()
        col.get.return_value = _col_result()
        img = MagicMock()
        img.get.return_value = _col_result(metas=[{"source_path": "/x/a.png"}])
        self.assertEqual(self._run(col, img), vs.VERIFY_INTEGRITY_FAILED)

    # ---------- 修复2：每条记录须有有效 document / embedding ----------

    def test_missing_embeddings(self):
        """记录有 metadata/document 但 embedding 缺失 → 损坏。"""
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::1"],
            metas=[_chunk_meta(0, 2), _chunk_meta(1, 2)],
            embs=[],
        )
        self.assertEqual(self._run(col), vs.VERIFY_INTEGRITY_FAILED)

    def test_empty_document(self):
        """document 为空字符串 → 损坏。"""
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::1"],
            metas=[_chunk_meta(0, 2), _chunk_meta(1, 2)],
            docs=["", "第二块"],
        )
        self.assertEqual(self._run(col), vs.VERIFY_INTEGRITY_FAILED)

    def test_inconsistent_embedding_dims(self):
        """同一源各块 embedding 维度不一致 → 写入异常。"""
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::1"],
            metas=[_chunk_meta(0, 2), _chunk_meta(1, 2)],
            embs=[[0.1] * 8, [0.1] * 16],
        )
        self.assertEqual(self._run(col), vs.VERIFY_INTEGRITY_FAILED)

    # ---------- 修复2：视频帧完整校验（逐帧而非只看第一条） ----------

    def _frame_meta(self, idx, n, content_hash="fh"):
        return {"source_path": "/x/v.mp4", "modality": "video_frame",
                "frame_index": idx, "frame_count": n, "content_hash": content_hash}

    def test_video_frames_ok(self):
        col = MagicMock()
        col.get.return_value = _col_result()
        img = MagicMock()
        img.get.return_value = _col_result(
            ids=["v::g1::frame::0", "v::g1::frame::1", "v::g1::frame::2"],
            metas=[self._frame_meta(0, 3), self._frame_meta(1, 3), self._frame_meta(2, 3)],
        )
        self.assertEqual(self._run(col, img), vs.VERIFY_OK)

    def test_video_frames_discontinuous_index(self):
        """帧序号跳号（0,1,3）→ 损坏，须重抽全部帧。"""
        col = MagicMock()
        col.get.return_value = _col_result()
        img = MagicMock()
        img.get.return_value = _col_result(
            ids=["v::g1::frame::0", "v::g1::frame::1", "v::g1::frame::3"],
            metas=[self._frame_meta(0, 3), self._frame_meta(1, 3), self._frame_meta(3, 3)],
        )
        self.assertEqual(self._run(col, img), vs.VERIFY_INTEGRITY_FAILED)

    def test_video_frames_count_mismatch(self):
        """声明 frame_count=4 实际 3 帧（部分写入）→ 损坏。"""
        col = MagicMock()
        col.get.return_value = _col_result()
        img = MagicMock()
        img.get.return_value = _col_result(
            ids=["v::g1::frame::0", "v::g1::frame::1", "v::g1::frame::2"],
            metas=[self._frame_meta(0, 4), self._frame_meta(1, 4), self._frame_meta(2, 4)],
        )
        self.assertEqual(self._run(col, img), vs.VERIFY_INTEGRITY_FAILED)

    def test_video_frames_missing_embedding(self):
        """部分帧缺 embedding → 损坏（不能只看第一条记录）。"""
        col = MagicMock()
        col.get.return_value = _col_result()
        img = MagicMock()
        img.get.return_value = _col_result(
            ids=["v::g1::frame::0", "v::g1::frame::1"],
            metas=[self._frame_meta(0, 2), self._frame_meta(1, 2)],
            embs=[[0.1] * 8],
        )
        self.assertEqual(self._run(col, img), vs.VERIFY_INTEGRITY_FAILED)


class TestGetSourceHashIntegrity(unittest.TestCase):
    """get_source_hash 仅在完整性校验通过时返回 hash（否则 None 触发安全重建）。"""

    def _hash_with(self, text_col, image_col=None):
        image_col = image_col if image_col is not None else MagicMock()
        with patch.object(vs, "get_collection", return_value=text_col), \
             patch.object(vs, "get_image_collection", return_value=image_col):
            return vs.get_source_hash("/x/a.md")

    def test_ok_returns_hash(self):
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::1"], metas=[_chunk_meta(0, 2, "abc"), _chunk_meta(1, 2, "abc")]
        )
        self.assertEqual(self._hash_with(col), "abc")

    def test_integrity_failed_returns_none(self):
        col = MagicMock()
        col.get.return_value = _col_result(
            ids=["a::0", "a::2"], metas=[_chunk_meta(0, 2), _chunk_meta(2, 2)]
        )
        self.assertIsNone(self._hash_with(col), "半成品索引必须返回 None 触发重建")

    def test_read_error_returns_none(self):
        col = MagicMock()
        col.get.side_effect = RuntimeError("db locked")
        self.assertIsNone(self._hash_with(col))

    def test_pure_image_returns_image_hash(self):
        col = MagicMock()
        col.get.return_value = _col_result()
        img = MagicMock()
        img.get.return_value = _col_result(metas=[{"content_hash": "img-h"}])
        self.assertEqual(self._hash_with(col, img), "img-h")


class TestDerivedReadErrorHandling(unittest.TestCase):
    """派生链路接入三态（2026-08-22 事故的验收化）。

    read_error 时：
    - 已有 ok 记录 → 不覆盖；
    - 无记录 → unavailable（可重试），绝不 skipped。
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()
        derived.reset_relation_task_flags()

    def tearDown(self):
        # 必须先恢复默认派生库路径再删临时目录：否则模块级 _DB_PATH 指向已删除
        # 目录，后续任何测试触发 DerivedStore.instance() 都会 "unable to open
        # database file"（全量回归时 p10/p13 曾因此连锁失败）
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _mock_read_error(self):
        return patch(
            "vector_store.read_source_chunks", return_value=(vs.READ_ERROR, [])
        )

    def _mock_empty(self):
        return patch(
            "vector_store.read_source_chunks", return_value=(vs.READ_EMPTY, [])
        )

    # ---------- 关系抽取（事故现场） ----------

    def test_relation_read_error_preserves_ok(self):
        """事故场景：关系已是 ok，向量库读取失败 → 记录必须保持 ok。"""
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "m1", derived.KIND_RELATION_EXTRACTION, "ok",
            {"items": [{"subject": {"name": "A"}, "predicate": "属于",
                        "object": {"name": "B"}, "confidence": 0.9}]},
            "oldhash", "gen",
        )
        with self._mock_read_error():
            derived._generate_relations("m1", "/x/m1.pdf", force=True)
        rec = self.store.get_derived_record(
            derived.OWNER_MATERIAL, "m1", derived.KIND_RELATION_EXTRACTION
        )
        self.assertEqual(rec["status"], "ok", "read_error 不得覆盖已有 ok 记录")
        self.assertEqual(rec["input_hash"], "oldhash")

    def test_relation_read_error_without_record_writes_unavailable(self):
        with self._mock_read_error():
            derived._generate_relations("m1", "/x/m1.pdf")
        rec = self.store.get_derived_record(
            derived.OWNER_MATERIAL, "m1", derived.KIND_RELATION_EXTRACTION
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "unavailable",
                         "read_error 且无历史记录 → 可重试的 unavailable")
        self.assertNotEqual(rec["status"], "skipped",
                            "read_error 绝不能写 skipped（2026-08-22 事故根因）")

    def test_relation_true_empty_writes_skipped(self):
        """真 empty（查询成功无记录）保持原契约：skipped。"""
        with self._mock_empty():
            derived._generate_relations("m1", "/x/m1.pdf")
        rec = self.store.get_derived_record(
            derived.OWNER_MATERIAL, "m1", derived.KIND_RELATION_EXTRACTION
        )
        self.assertEqual(rec["status"], "skipped")

    # ---------- 摘要 / 实体 ----------

    def test_summary_entities_read_error_preserves_ok(self):
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "m1", derived.KIND_SUMMARY, "ok",
            {"text": "旧摘要"}, "oldhash", "gen",
        )
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "m1", derived.KIND_ENTITY_EXTRACTION, "ok",
            {"items": []}, "oldhash", "gen",
        )
        with self._mock_read_error():
            derived._generate_summary_and_entities("m1", "/x/m1.pdf", force=True)
        s = self.store.get_derived_record(derived.OWNER_MATERIAL, "m1", derived.KIND_SUMMARY)
        e = self.store.get_derived_record(derived.OWNER_MATERIAL, "m1", derived.KIND_ENTITY_EXTRACTION)
        self.assertEqual(s["status"], "ok")
        self.assertEqual(e["status"], "ok")

    def test_summary_entities_read_error_writes_unavailable(self):
        with self._mock_read_error():
            derived._generate_summary_and_entities("m1", "/x/m1.pdf")
        s = self.store.get_derived_record(derived.OWNER_MATERIAL, "m1", derived.KIND_SUMMARY)
        e = self.store.get_derived_record(derived.OWNER_MATERIAL, "m1", derived.KIND_ENTITY_EXTRACTION)
        self.assertEqual(s["status"], "unavailable")
        self.assertEqual(e["status"], "unavailable")

    def test_entities_read_error_writes_unavailable(self):
        with self._mock_read_error():
            derived._generate_entities("m1", "/x/m1.pdf")
        e = self.store.get_derived_record(derived.OWNER_MATERIAL, "m1", derived.KIND_ENTITY_EXTRACTION)
        self.assertEqual(e["status"], "unavailable")

    # ---------- 标签候选 ----------

    def test_tags_read_error_writes_unavailable(self):
        with self._mock_read_error():
            derived._generate_tag_suggestions("m1", "/x/m1.pdf")
        t = self.store.get_derived_record(derived.OWNER_MATERIAL, "m1", derived.KIND_TAG_SUGGESTIONS)
        self.assertIsNotNone(t)
        self.assertEqual(t["status"], "unavailable")

    def test_tags_read_error_preserves_ok(self):
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "m1", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": [{"name": "t1"}]}, "oldhash", "gen",
        )
        with self._mock_read_error():
            derived._generate_tag_suggestions("m1", "/x/m1.pdf", force=True)
        t = self.store.get_derived_record(derived.OWNER_MATERIAL, "m1", derived.KIND_TAG_SUGGESTIONS)
        self.assertEqual(t["status"], "ok")

    # ---------- refresh_analysis 不因 read_error 崩溃 ----------

    def test_refresh_analysis_survives_read_error(self):
        """refresh_analysis（HTTP 上传链路调用）在 read_error 时不得抛异常。"""
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "m1", derived.KIND_ENTITY_EXTRACTION, "ok",
            {"items": []}, "h", "gen",
        )
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "m1", derived.KIND_RELATION_EXTRACTION, "ok",
            {"items": []}, "h2", "gen",
        )
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "m1", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": []}, "h3", "gen",
        )
        self.store.set_derived_record(
            derived.OWNER_MATERIAL, "m1", derived.KIND_SUMMARY, "ok",
            {"text": ""}, "h4", "gen",
        )
        with self._mock_read_error(), \
             patch.object(derived._ollama_scheduler, "submit", return_value=MagicMock()) as sub:
            try:
                derived.refresh_analysis("m1", "/x/m1.pdf")
            except derived._IndexReadError:
                self.fail("refresh_analysis 不得让 read_error 异常传播到 HTTP 层")
        # 实体/摘要 ok、关系 ok → 不触发重投（读取失败时保守跳过）
        sub.assert_not_called()


if __name__ == "__main__":
    unittest.main()
