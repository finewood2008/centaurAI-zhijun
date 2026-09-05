"""P0-2 原子替换测试（索引可靠性方案 §7.1 / §7.2 验收）。

覆盖：
- 单文件失败恢复（§7.1）：Chroma add 失败 / 部分写入（新代校验失败）→ 旧索引
  原样保留仍可检索；重试成功后才切换到新代；
- 音视频失败恢复（§7.2）：ASR / CLIP / 向量化 / 帧写入失败 → 旧转写块与视觉块
  保留、处理开始前不再调用 delete_file、不产生孤儿帧目录；
- 存量兼容：无 generation 字段的旧记录读取口径不变；重索引后原子替换；
- 残留代治理：上次失败的残留代在下一次写入前被清理；
- 读取过滤：search / list_documents / BM25 补取只统计当前有效代；
- 注册表单元行为：set/next/clear/clear_all。

全部使用内存 FakeCollection 模拟 ChromaDB（含 $and where 过滤），注册表指向
独立临时 SQLite，不触碰真实数据目录。
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
import watcher


class FakeCollection:
    """Chroma collection 内存模拟：支持 {"k": v} / {"$and": [...]} where 过滤。

    fail_on_add=True 模拟 Chroma add 抛异常；add_drop_count=N 模拟部分写入
    （add 静默丢弃最后 N 条，用于触发新代完整性校验失败）。
    """

    def __init__(self):
        self.records: dict[str, dict] = {}
        self.fail_on_add = False
        self.add_drop_count = 0

    def count(self) -> int:
        return len(self.records)

    def add(self, ids, embeddings, documents, metadatas):
        if self.fail_on_add:
            raise RuntimeError("simulated chroma add failure")
        for i in range(len(ids) - self.add_drop_count):
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
        rows = [(cid, r) for cid, r in self.records.items() if self._match(r["metadata"], where)]
        rows = rows[:n_results]
        return {
            "ids": [[cid for cid, _ in rows]],
            "documents": [[r["document"] for _, r in rows]],
            "metadatas": [[r["metadata"] for _, r in rows]],
            "distances": [[0.1] * len(rows)],
        }


def _legacy_chunk(sp: str, i: int, n: int, text: str, content_hash: str = "h0"):
    """构造迁移前存量记录（无 generation 字段、旧 id 格式）。"""
    return (
        f"{sp}::{i}",
        {
            "embedding": [0.1, 0.1],
            "document": text,
            "metadata": {
                "source_path": sp,
                "file_type": "text",
                "chunk_index": i,
                "chunk_count": n,
                "content_hash": content_hash,
                "modality": "text",
                "schema_version": vs.SCHEMA_VERSION,
                "model_id": vs.TEXT_MODEL_ID,
            },
        },
    )


class AtomicReplaceTestBase(unittest.TestCase):
    """公共环境：独立注册表 DB + Fake 双集合 + 隔离帧目录。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        generation_store.reset_for_tests(self._tmp / "gen_registry.db")
        self.text_col = FakeCollection()
        self.image_col = FakeCollection()
        for p in (
            patch.object(vs, "get_collection", return_value=self.text_col),
            patch.object(vs, "get_image_collection", return_value=self.image_col),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.frames_root = self._tmp / "video_frames"
        self.frames_root.mkdir(parents=True)
        p = patch.object(vs, "VIDEO_FRAMES_DIR", str(self.frames_root))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        generation_store.reset_for_tests(None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- 便捷写入 ----

    def _write_chunks(self, sp: str, texts: list[str], content_hash: str = "h-new") -> bool:
        return vs.add_file_chunks(
            sp, "text", texts, [[0.1, 0.2] for _ in texts],
            {"file_name": Path(sp).name, "content_hash": content_hash},
        )

    def _write_frames(self, sp: str, frame_dir: Path, n: int = 2) -> bool:
        frame_dir.mkdir(parents=True, exist_ok=True)
        metas = [{"frame_path": str(frame_dir / f"f{k}.jpg"), "start_time": float(k)}
                 for k in range(n)]
        return vs.add_image_frames(
            sp, [[0.1, 0.2] for _ in range(n)], metas,
            {"file_name": Path(sp).name, "content_hash": "fh"},
        )


class TestGenerationStore(unittest.TestCase):
    """注册表单元行为。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        generation_store.reset_for_tests(self._tmp / "gen.db")

    def tearDown(self):
        generation_store.reset_for_tests(None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_missing_returns_zero(self):
        self.assertEqual(generation_store.current_generation("text", "/x/a.md"), 0)

    def test_set_and_next(self):
        generation_store.set_generation("text", "/x/a.md", 1)
        self.assertEqual(generation_store.current_generation("text", "/x/a.md"), 1)
        self.assertEqual(generation_store.next_generation("text", "/x/a.md"), 2)

    def test_collections_independent(self):
        """文本/图片集合各自计数（视频流程分两次写互不干扰）。"""
        generation_store.set_generation("text", "/x/v.mp4", 3)
        self.assertEqual(generation_store.current_generation("image", "/x/v.mp4"), 0)

    def test_clear_and_clear_all(self):
        generation_store.set_generation("text", "/x/a.md", 1)
        generation_store.set_generation("image", "/x/a.md", 2)
        generation_store.set_generation("text", "/x/b.md", 1)
        generation_store.clear_source("/x/a.md")
        self.assertEqual(generation_store.current_generation("text", "/x/a.md"), 0)
        self.assertEqual(generation_store.current_generation("image", "/x/a.md"), 0)
        self.assertEqual(generation_store.current_generation("text", "/x/b.md"), 1)
        generation_store.clear_all()
        self.assertEqual(generation_store.current_generation("text", "/x/b.md"), 0)

    def test_current_generations_batch(self):
        generation_store.set_generation("text", "/x/a.md", 1)
        generation_store.set_generation("text", "/x/b.md", 2)
        generation_store.set_generation("image", "/x/a.md", 5)
        self.assertEqual(
            generation_store.current_generations("text"), {"/x/a.md": 1, "/x/b.md": 2}
        )
        self.assertEqual(generation_store.current_generations("image"), {"/x/a.md": 5})


class TestAtomicFileChunks(AtomicReplaceTestBase):
    """§7.1 单文件失败恢复。"""

    SP = "/data/docs/a.md"

    def test_first_write_registers_generation_1(self):
        ok = self._write_chunks(self.SP, ["第一块", "第二块"])
        self.assertTrue(ok)
        self.assertEqual(
            generation_store.current_generation("text", self.SP), 1)
        status, chunks = vs.read_source_chunks(self.SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual([c["text"] for c in chunks], ["第一块", "第二块"])
        # 新代 id 携带代数标识
        self.assertTrue(all("::g1::" in c["id"] for c in chunks))

    def test_add_failure_preserves_old_index(self):
        """§7.1：Chroma add 失败 → 旧索引原样保留、仍可检索、注册表不切。"""
        self.assertTrue(self._write_chunks(self.SP, ["旧一", "旧二"], content_hash="h-old"))
        self.text_col.fail_on_add = True
        self.assertFalse(self._write_chunks(self.SP, ["新一", "新二"]))
        self.text_col.fail_on_add = False
        # 旧索引仍可检索
        status, chunks = vs.read_source_chunks(self.SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual([c["text"] for c in chunks], ["旧一", "旧二"])
        # 注册表未切换；get_source_hash 校验通过返回旧 hash
        self.assertEqual(generation_store.current_generation("text", self.SP), 1)
        self.assertEqual(vs.get_source_hash(self.SP), "h-old")

    def test_partial_write_detected_and_old_index_preserved(self):
        """§7.1：部分写入（新代缺块）→ 校验失败 → 清新代残留、旧索引保留。"""
        self.assertTrue(self._write_chunks(self.SP, ["旧一", "旧二"], content_hash="h-old"))
        self.text_col.add_drop_count = 1  # add 静默丢最后 1 条 → 新代只有 1/2 块
        self.assertFalse(self._write_chunks(self.SP, ["新一", "新二"]))
        self.text_col.add_drop_count = 0
        status, chunks = vs.read_source_chunks(self.SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual([c["text"] for c in chunks], ["旧一", "旧二"])
        self.assertEqual(generation_store.current_generation("text", self.SP), 1)

    def test_retry_success_switches_to_new_index(self):
        """§7.1：失败后重试成功 → 切换到新代，旧代记录被删除。"""
        self.assertTrue(self._write_chunks(self.SP, ["旧一", "旧二"], content_hash="h-old"))
        self.text_col.fail_on_add = True
        self.assertFalse(self._write_chunks(self.SP, ["新一"]))
        self.text_col.fail_on_add = False
        self.assertTrue(self._write_chunks(self.SP, ["新一", "新二", "新三"], content_hash="h-new"))
        status, chunks = vs.read_source_chunks(self.SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual([c["text"] for c in chunks], ["新一", "新二", "新三"])
        self.assertEqual(generation_store.current_generation("text", self.SP), 2)
        # 旧代记录已删（集合内只剩 g2 三条）
        self.assertEqual(len(self.text_col.records), 3)
        self.assertEqual(vs.get_source_hash(self.SP), "h-new")

    def test_stale_generation_purged_before_next_write(self):
        """上次失败的残留代在下次写入前清理，不随切换累积。"""
        self.assertTrue(self._write_chunks(self.SP, ["旧一", "旧二"]))
        # 人为注入「失败残留」：g2 记录存在但注册表仍指向 g1
        self.text_col.records[f"{self.SP}::g2::0"] = {
            "embedding": [0.0], "document": "残留",
            "metadata": {"source_path": self.SP, "generation": 2,
                         "chunk_index": 0, "chunk_count": 1, "content_hash": "bad"},
        }
        self.assertTrue(self._write_chunks(self.SP, ["新内容"]))
        status, chunks = vs.read_source_chunks(self.SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual([c["text"] for c in chunks], ["新内容"])
        # 残留 g2 与旧代 g1 均已清除，只剩新代 g3 一条
        self.assertEqual(len(self.text_col.records), 1)

    def test_empty_embeddings_fails_fast(self):
        self.assertFalse(
            vs.add_file_chunks(self.SP, "text", ["x"], [], {"content_hash": "h"})
        )


class TestLegacyCompatibility(AtomicReplaceTestBase):
    """迁移前存量数据（无 generation 字段）零迁移兼容。"""

    SP = "/data/docs/legacy.md"

    def _seed_legacy(self):
        for i, text in enumerate(["旧A", "旧B"]):
            cid, rec = _legacy_chunk(self.SP, i, 2, text, content_hash="h-legacy")
            self.text_col.records[cid] = rec

    def test_legacy_records_readable_without_registry(self):
        self._seed_legacy()
        status, chunks = vs.read_source_chunks(self.SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual([c["text"] for c in chunks], ["旧A", "旧B"])
        self.assertEqual(vs.get_source_hash(self.SP), "h-legacy")

    def test_legacy_reindex_replaces_atomically(self):
        self._seed_legacy()
        self.assertTrue(self._write_chunks(self.SP, ["新内容"], content_hash="h-v2"))
        status, chunks = vs.read_source_chunks(self.SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual([c["text"] for c in chunks], ["新内容"])
        # 存量旧记录（无 generation 字段）已随切换删除
        self.assertEqual(len(self.text_col.records), 1)
        self.assertEqual(vs.get_source_hash(self.SP), "h-v2")


class TestAtomicImages(AtomicReplaceTestBase):
    """纯图向量与视频帧的原子替换（§7.2 视觉侧）。"""

    SP = "/data/media/v.mp4"

    def test_image_vector_failure_preserves_old(self):
        self.assertTrue(vs.add_image_vector(self.SP, [0.1, 0.2], {"content_hash": "ih1"}))
        self.image_col.fail_on_add = True
        self.assertFalse(vs.add_image_vector(self.SP, [0.3, 0.4], {"content_hash": "ih2"}))
        self.image_col.fail_on_add = False
        self.assertEqual(generation_store.current_generation("image", self.SP), 1)
        res = self.image_col.get(where={"source_path": self.SP})
        self.assertEqual(len(res["ids"]), 1)  # 旧向量仍在

    def test_image_vector_success_switches(self):
        self.assertTrue(vs.add_image_vector(self.SP, [0.1, 0.2], {"content_hash": "ih1"}))
        self.assertTrue(vs.add_image_vector(self.SP, [0.3, 0.4], {"content_hash": "ih2"}))
        self.assertEqual(generation_store.current_generation("image", self.SP), 2)
        self.assertEqual(len(self.image_col.records), 1)  # 旧代已删

    def test_frames_failure_keeps_old_and_cleans_new_dir(self):
        """§7.2：帧写入失败 → 旧帧记录/目录保留，新抽帧目录不孤儿。"""
        old_dir = self.frames_root / "oldsha"
        self.assertTrue(self._write_frames(self.SP, old_dir, n=2))
        new_dir = self.frames_root / "newsha"
        new_dir.mkdir()
        (new_dir / "f0.jpg").write_bytes(b"x")
        self.image_col.fail_on_add = True
        metas = [{"frame_path": str(new_dir / f"f{k}.jpg"), "start_time": float(k)}
                 for k in range(2)]
        self.assertFalse(vs.add_image_frames(
            self.SP, [[0.1, 0.2]] * 2, metas, {"content_hash": "fh2"}))
        self.image_col.fail_on_add = False
        # 旧帧记录仍在（gen1），注册表未切
        self.assertEqual(generation_store.current_generation("image", self.SP), 1)
        res = self.image_col.get(where={"source_path": self.SP})
        self.assertEqual(len(res["ids"]), 2)
        # 新抽帧目录已清理（孤儿清理）；旧帧目录保留
        self.assertFalse(new_dir.exists())
        self.assertTrue(old_dir.exists())

    def test_frames_partial_write_preserves_old(self):
        old_dir = self.frames_root / "oldsha"
        self.assertTrue(self._write_frames(self.SP, old_dir, n=2))
        self.image_col.add_drop_count = 1
        self.assertFalse(self._write_frames(self.SP, self.frames_root / "newsha", n=2))
        self.image_col.add_drop_count = 0
        self.assertEqual(generation_store.current_generation("image", self.SP), 1)
        self.assertEqual(len(self.image_col.get(where={"source_path": self.SP})["ids"]), 2)

    def test_frames_success_replaces_and_cleans_old_dir(self):
        old_dir = self.frames_root / "oldsha"
        self.assertTrue(self._write_frames(self.SP, old_dir, n=2))
        new_dir = self.frames_root / "newsha"
        self.assertTrue(self._write_frames(self.SP, new_dir, n=3))
        self.assertEqual(generation_store.current_generation("image", self.SP), 2)
        # 只剩新代 3 条帧记录；旧帧目录清理、新帧目录保留
        self.assertEqual(len(self.image_col.records), 3)
        self.assertFalse(old_dir.exists())
        self.assertTrue(new_dir.exists())

    def test_frames_same_dir_reuse_not_deleted(self):
        """同内容重索引（能力指纹变化）复用同一帧目录：成功后目录不得被删。"""
        shared_dir = self.frames_root / "sha1"
        self.assertTrue(self._write_frames(self.SP, shared_dir, n=2))
        self.assertTrue(self._write_frames(self.SP, shared_dir, n=2))
        self.assertTrue(shared_dir.exists())
        self.assertEqual(len(self.image_col.records), 2)

    def test_delete_file_clears_registry_and_records(self):
        self._write_chunks(self.SP, ["t1"])
        self._write_frames(self.SP, self.frames_root / "sha", n=1)
        self.assertTrue(vs.delete_file(self.SP))
        self.assertEqual(generation_store.current_generation("text", self.SP), 0)
        self.assertEqual(generation_store.current_generation("image", self.SP), 0)
        self.assertEqual(len(self.text_col.records), 0)
        self.assertEqual(len(self.image_col.records), 0)


class TestReadPathFiltering(AtomicReplaceTestBase):
    """读取路径只统计当前有效代（删旧失败留下的孤儿记录不外泄）。"""

    SP = "/data/docs/a.md"

    def _seed_two_generations(self):
        self._write_chunks(self.SP, ["旧块内容"], content_hash="h1")
        # 二次写入成功 → 注册表指向 g2、g1 记录已随切换删除
        self._write_chunks(self.SP, ["新块内容1", "新块内容2"], content_hash="h2")
        # 手动补一条 g1 记录，模拟「g2 已切换但 g1 删失败」的孤儿状态
        g1_id, g1_rec = _legacy_chunk(self.SP, 0, 1, "旧块内容", "h1")
        g1_rec["metadata"]["generation"] = 1
        g1_rec["metadata"]["content_hash"] = "h1"
        self.text_col.records[g1_id] = g1_rec
        self.assertEqual(len(self.text_col.records), 3)  # g2×2 + 孤儿 g1×1

    def test_search_filters_stale_generation(self):
        self._seed_two_generations()
        items = vs.search([0.1, 0.2], n_results=10)
        texts = [it["text"] for it in items]
        self.assertNotIn("旧块内容", texts)
        self.assertIn("新块内容1", texts)

    def test_read_source_chunks_filters_stale_generation(self):
        self._seed_two_generations()
        status, chunks = vs.read_source_chunks(self.SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual([c["text"] for c in chunks], ["新块内容1", "新块内容2"])

    def test_list_documents_counts_current_generation_only(self):
        self._seed_two_generations()
        result = vs.list_documents(limit=None)
        docs = [d for d in result["items"] if d["id"] == self.SP]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["chunk_count"], 2)

    def test_get_chunks_by_ids_filters_stale(self):
        self._seed_two_generations()
        stale_id = next(
            cid for cid, r in self.text_col.records.items()
            if r["metadata"].get("generation") == 1
        )
        current_id = next(
            cid for cid, r in self.text_col.records.items()
            if r["metadata"].get("generation") == 2
        )
        items = vs.get_chunks_by_ids([stale_id, current_id])
        self.assertEqual([it["id"] for it in items], [current_id])


class TestVideoAudioFailureRecovery(AtomicReplaceTestBase):
    """§7.2 音视频失败恢复：处理前不删旧索引，失败保留旧转写/旧帧。"""

    AUDIO_SP = "/data/media/meeting.mp3"
    VIDEO_SP = "/data/media/clip.mp4"

    def _seed_old_transcript(self, sp: str):
        self._write_chunks(sp, ["旧转写一", "旧转写二"], content_hash="h-old")

    def _watcher_patches(self):
        """watcher 音视频流程的外部依赖 mock（ASR/CLIP/摘要提交全部隔离）。"""
        mocks = {
            "WHISPER_ENABLED": True,
            "VIDEO_FRAME_OCR_ENABLED": False,
            "CLIP_ENABLED": False,
            "whisper_available": MagicMock(return_value=True),
            "transcribe_audio": MagicMock(return_value=[]),
            "embed_image_clip": MagicMock(return_value=None),
            "embed_batch_texts": MagicMock(side_effect=lambda texts: [[0.1, 0.2] for _ in texts]),
            "_submit_material_summary": MagicMock(),
            "_submit_material_analysis": MagicMock(),
            "annotations": MagicMock(**{"caption_of.return_value": ""}),
            "delete_file": MagicMock(),
        }
        entered = [patch.object(watcher, k, v) for k, v in mocks.items()]
        for p in entered:
            p.start()
            self.addCleanup(p.stop)
        return {k: getattr(watcher, k) for k in mocks}

    def _video_patches(self, extract_frames=None, probe=None):
        import video
        entered = [
            patch.object(video, "ffmpeg_available", MagicMock(return_value=True)),
            patch.object(video, "probe", MagicMock(return_value=probe or {
                "has_video": False, "has_audio": True, "duration": 10.0,
            })),
            patch.object(video, "extract_audio_16k_mono", MagicMock(return_value="/tmp/wav")),
            patch.object(video, "extract_frames", extract_frames or MagicMock(return_value=[])),
        ]
        for p in entered:
            p.start()
            self.addCleanup(p.stop)

    def _base_meta(self, content_hash: str) -> dict:
        return {
            "file_name": "media.bin",
            "content_hash": content_hash,
            "rag_transcript_chunk_sec": 30,
        }

    def test_audio_embed_failure_preserves_old_transcript(self):
        """§7.2：音频向量化失败 → 旧转写保留、仍可检索、不提前 delete_file。"""
        self._seed_old_transcript(self.AUDIO_SP)
        patches = self._watcher_patches()
        patches["embed_batch_texts"].side_effect = lambda texts: []  # 向量化失败
        self._video_patches()
        ok = watcher._index_audio(self.AUDIO_SP, self.AUDIO_SP, self._base_meta("h-new:cap"))
        self.assertFalse(ok)
        patches["delete_file"].assert_not_called()  # 处理开始前绝不删旧索引
        status, chunks = vs.read_source_chunks(self.AUDIO_SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual([c["text"] for c in chunks], ["旧转写一", "旧转写二"])

    def test_audio_asr_empty_preserves_old_transcript(self):
        """§7.2：ASR 无产出 → 旧转写保留。"""
        self._seed_old_transcript(self.AUDIO_SP)
        patches = self._watcher_patches()
        patches["transcribe_audio"].return_value = []  # 无转写产出
        self._video_patches()
        ok = watcher._index_audio(self.AUDIO_SP, self.AUDIO_SP, self._base_meta("h-new:cap"))
        self.assertFalse(ok)
        patches["delete_file"].assert_not_called()
        status, chunks = vs.read_source_chunks(self.AUDIO_SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual(len(chunks), 2)

    def test_audio_success_replaces_transcript(self):
        """音频重索引成功 → 原子切换到新转写。"""
        self._seed_old_transcript(self.AUDIO_SP)
        patches = self._watcher_patches()
        patches["transcribe_audio"].return_value = [
            {"start": 0.0, "end": 40.0, "text": "新转写内容很长超过分块窗口"},
        ]
        self._video_patches()
        ok = watcher._index_audio(self.AUDIO_SP, self.AUDIO_SP, self._base_meta("h-new:cap"))
        self.assertTrue(ok)
        status, chunks = vs.read_source_chunks(self.AUDIO_SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertIn("新转写内容", chunks[0]["text"])
        self.assertEqual(len(self.text_col.records), 1)  # 旧代已删

    def test_video_all_steps_fail_preserves_old(self):
        """§7.2：视频 ASR/CLIP 全失败 → 旧转写与旧帧索引保留、delete_file 未被调用。"""
        self._seed_old_transcript(self.VIDEO_SP)
        old_dir = self.frames_root / "oldsha"
        self._write_frames(self.VIDEO_SP, old_dir, n=2)
        patches = self._watcher_patches()
        self._video_patches()
        ok = watcher._index_video(self.VIDEO_SP, self.VIDEO_SP, self._base_meta("h-new:cap"))
        self.assertFalse(ok)
        patches["delete_file"].assert_not_called()
        status, chunks = vs.read_source_chunks(self.VIDEO_SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertEqual([c["text"] for c in chunks], ["旧转写一", "旧转写二"])
        self.assertEqual(len(self.image_col.get(where={"source_path": self.VIDEO_SP})["ids"]), 2)
        self.assertTrue(old_dir.exists())  # 旧帧目录保留

    def test_video_frame_write_failure_no_orphan_dir(self):
        """§7.2：帧向量写入失败 → 旧帧保留，新抽帧目录不孤儿。"""
        old_dir = self.frames_root / "oldsha"
        self._write_frames(self.VIDEO_SP, old_dir, n=2)
        new_dir = self.frames_root / "newsha"
        new_dir.mkdir()
        frames = [{"path": str(new_dir / f"f{k}.jpg"), "timestamp": float(k)} for k in range(2)]
        patches = self._watcher_patches()
        with patch.object(watcher, "CLIP_ENABLED", True), \
             patch.object(watcher, "embed_image_clip", MagicMock(return_value=[0.1, 0.2])):
            self._video_patches(
                extract_frames=MagicMock(return_value=frames),
                probe={"has_video": True, "has_audio": True, "duration": 10.0},
            )
            self.image_col.fail_on_add = True
            ok = watcher._index_video(self.VIDEO_SP, self.VIDEO_SP, self._base_meta("h-new:cap"))
            self.image_col.fail_on_add = False
            self.assertFalse(ok)
        patches["delete_file"].assert_not_called()
        # 旧帧记录与目录保留；新帧目录已清理
        self.assertEqual(len(self.image_col.get(where={"source_path": self.VIDEO_SP})["ids"]), 2)
        self.assertTrue(old_dir.exists())
        self.assertFalse(new_dir.exists())

    def test_video_success_replaces_both_collections(self):
        """视频成功重索引：帧走新代、转写走新代，两集合各自原子切换。"""
        self._seed_old_transcript(self.VIDEO_SP)
        old_dir = self.frames_root / "oldsha"
        self._write_frames(self.VIDEO_SP, old_dir, n=2)
        new_dir = self.frames_root / "newsha"
        frames = [{"path": str(new_dir / f"f{k}.jpg"), "timestamp": float(k)} for k in range(2)]
        patches = self._watcher_patches()
        patches["transcribe_audio"].return_value = [
            {"start": 0.0, "end": 40.0, "text": "新视频转写内容超过窗口长度"},
        ]
        with patch.object(watcher, "CLIP_ENABLED", True), \
             patch.object(watcher, "embed_image_clip", MagicMock(return_value=[0.1, 0.2])):
            self._video_patches(
                extract_frames=MagicMock(return_value=frames),
                probe={"has_video": True, "has_audio": True, "duration": 10.0},
            )
            new_dir.mkdir()
            for fr in frames:
                Path(fr["path"]).write_bytes(b"x")
            ok = watcher._index_video(self.VIDEO_SP, self.VIDEO_SP, self._base_meta("h-new:cap"))
            self.assertTrue(ok)
        # 文本：新代转写生效、旧代删除
        status, chunks = vs.read_source_chunks(self.VIDEO_SP)
        self.assertEqual(status, vs.READ_OK)
        self.assertIn("新视频转写", chunks[0]["text"])
        self.assertEqual(len(self.text_col.records), 1)
        # 图片：新代帧生效、旧代记录与目录清理
        self.assertEqual(len(self.image_col.records), 2)
        self.assertFalse(old_dir.exists())
        self.assertTrue(new_dir.exists())
        patches["delete_file"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
