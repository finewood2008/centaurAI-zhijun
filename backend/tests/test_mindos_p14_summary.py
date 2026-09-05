"""MindOS P14-03 自动 200 字摘要回归测试。

覆盖：
- derived_records：upsert / 读取 / 幂等（摘要存为派生数据，不改写原材料或知识卡片）；
- 摘要输入：读取已索引文本，排除用户「说明」（caption）块；
- 200 字截断：句末安全截断；
- 生成逻辑：空文本不调用模型（skipped）、输入 hash 未变不重复调用、
  hash 变化重新生成、连接错误→unavailable、其它错误→failed、空输出→failed；
- summary_of / detail_of：返回 summary 对象与 excerpt 纯预览；
- 重试接口：force 提交。

依赖项目 .venv，可独立于 server 运行：
    .venv\\Scripts\\python.exe -m unittest test_mindos_p14_summary -v
"""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import watcher
from mindos import derived
from mindos import related
from mindos.stores import derived_store
from mindos.services import ingestion
from mindos import uploads
from vector_store import READ_EMPTY, READ_OK


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class DerivedRecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_set_and_get_derived_record(self):
        self.store.set_derived_record(
            "material", "m1", "SUMMARY", "ok", {"text": "摘要"}, "hash1", "ollama:qwen3:1.7b"
        )
        rec = self.store.get_derived_record("material", "m1", "SUMMARY")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["content"]["text"], "摘要")
        self.assertEqual(rec["input_hash"], "hash1")
        self.assertEqual(rec["generator"], "ollama:qwen3:1.7b")

    def test_missing_returns_none(self):
        self.assertIsNone(self.store.get_derived_record("material", "m1", "SUMMARY"))


class InputTextTests(unittest.TestCase):
    @patch("vector_store.read_source_chunks")
    def test_excludes_caption_and_empty_chunks(self, read_chunks):
        read_chunks.return_value = (
            READ_OK,
            [
                {"text": "正文第一段", "metadata": {"modality": "text"}},
                {"text": "说明：用户备注", "metadata": {"modality": "caption"}},
                {"text": "转写内容", "metadata": {"modality": "transcript"}},
                {"text": "", "metadata": {"modality": "text"}},
            ],
        )
        text = derived._input_text("/tmp/a.pdf")
        self.assertIn("正文第一段", text)
        self.assertIn("转写内容", text)
        self.assertNotIn("用户备注", text)
        self.assertNotIn("说明：", text)


class TruncateSummaryTests(unittest.TestCase):
    def test_under_limit_unchanged(self):
        self.assertEqual(derived._truncate_summary("短摘要"), "短摘要")

    def test_over_limit_truncates_at_sentence_end(self):
        base = "这是第一句内容。" * 30  # 240 字
        truncated = derived._truncate_summary(base, limit=200)
        self.assertLessEqual(len(truncated), 200)
        self.assertTrue(truncated.endswith("。"))
        self.assertGreater(len(truncated), 100)

    def test_no_sentence_end_hard_cut(self):
        base = "无标点" * 100  # 300 字、无标点
        truncated = derived._truncate_summary(base, limit=200)
        self.assertEqual(len(truncated), 200)


class GenerateSummaryTests(unittest.TestCase):
    """P14-03 摘要 + P14-04 实体合并生成（一次 LLM 调用产出两条派生记录）。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _record(self, kind=derived.KIND_SUMMARY):
        return self.store.get_derived_record("material", "mindos_s1", kind)

    @patch.object(derived, "_call_summary_entities_model")
    @patch.object(derived, "_input_text", return_value="   ")
    def test_empty_text_skipped_without_model_call(self, _input, call_model):
        derived._generate_summary_and_entities("mindos_s1", "/tmp/a.pdf")
        call_model.assert_not_called()
        rec = self._record()
        self.assertEqual(rec["status"], "skipped")
        self.assertEqual(rec["content"]["text"], "")
        # 实体同样落 skipped，不调用模型
        ent = self._record(derived.KIND_ENTITY_EXTRACTION)
        self.assertEqual(ent["status"], "skipped")
        self.assertEqual(ent["content"]["items"], [])

    @patch.object(derived, "_call_summary_entities_model", return_value=json.dumps({
        "summary": "这是生成的摘要。",
        "entities": [{"type": "term", "name": "算法", "confidence": 0.8, "evidence": "x"}],
    }, ensure_ascii=False))
    @patch.object(derived, "_input_text", return_value="本文重点介绍算法的实现")
    def test_success_saves_both_records(self, _input, call_model):
        derived._generate_summary_and_entities("mindos_s1", "/tmp/a.pdf")
        call_model.assert_called_once()
        rec = self._record()
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["content"]["text"], "这是生成的摘要。")
        self.assertEqual(rec["input_hash"], _hash("本文重点介绍算法的实现"))
        # 实体与摘要同源一次调用生成，来源为 llm
        ent = self._record(derived.KIND_ENTITY_EXTRACTION)
        self.assertEqual(ent["status"], "ok")
        self.assertEqual(ent["content"]["source"], "llm")
        self.assertEqual(ent["input_hash"], _hash("本文重点介绍算法的实现"))
        self.assertEqual(ent["content"]["items"][0]["name"], "算法")

    @patch.object(derived, "_call_summary_entities_model")
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_unchanged_hash_does_not_call_model(self, _input, call_model):
        gen = derived._generator_name(derived.get_provider().get_local_snapshot())
        self.store.set_derived_record(
            "material", "mindos_s1", derived.KIND_SUMMARY, "ok",
            {"text": "旧摘要"}, _hash("内容甲"), gen,
        )
        self.store.set_derived_record(
            "material", "mindos_s1", derived.KIND_ENTITY_EXTRACTION, "ok",
            {"items": []}, _hash("内容甲"), gen,
        )
        derived._generate_summary_and_entities("mindos_s1", "/tmp/a.pdf")
        call_model.assert_not_called()

    @patch.object(derived, "_call_summary_entities_model", return_value=json.dumps({
        "summary": "新摘要。", "entities": [],
    }, ensure_ascii=False))
    @patch.object(derived, "_input_text", return_value="内容乙")
    def test_changed_hash_recalls_model(self, _input, call_model):
        self.store.set_derived_record(
            "material", "mindos_s1", derived.KIND_SUMMARY, "ok",
            {"text": "旧摘要"}, "oldhash", "g",
        )
        derived._generate_summary_and_entities("mindos_s1", "/tmp/a.pdf")
        call_model.assert_called_once()
        self.assertEqual(self._record()["content"]["text"], "新摘要。")

    @patch.object(derived, "_call_summary_entities_model", side_effect=urllib.error.URLError("conn refused"))
    @patch.object(derived, "_entity_fallback", return_value=[])
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_connection_error_marks_unavailable(self, _input, fallback, call_model):
        derived._generate_summary_and_entities("mindos_s1", "/tmp/a.pdf")
        self.assertEqual(self._record()["status"], "unavailable")
        # 实体无降级结果时与摘要同状态
        self.assertEqual(self._record(derived.KIND_ENTITY_EXTRACTION)["status"], "unavailable")

    @patch.object(derived, "_call_summary_entities_model", side_effect=ValueError("bad response"))
    @patch.object(derived, "_entity_fallback", return_value=[])
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_other_error_marks_failed(self, _input, fallback, call_model):
        derived._generate_summary_and_entities("mindos_s1", "/tmp/a.pdf")
        self.assertEqual(self._record()["status"], "failed")

    @patch.object(derived, "_call_summary_entities_model", return_value="")
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_empty_model_output_marks_failed(self, _input, call_model):
        derived._generate_summary_and_entities("mindos_s1", "/tmp/a.pdf")
        self.assertEqual(self._record()["status"], "failed")

    @patch.object(derived, "_call_summary_entities_model", return_value="这是纯文本摘要，不是 JSON。")
    @patch.object(derived, "_entity_fallback", return_value=[])
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_plain_text_answer_kept_as_summary_entity_fallback(self, _input, fallback, call_model):
        # 模型未按 JSON 指令输出 → 整段按摘要保留，实体走降级
        derived._generate_summary_and_entities("mindos_s1", "/tmp/a.pdf")
        self.assertEqual(self._record()["status"], "ok")
        self.assertEqual(self._record()["content"]["text"], "这是纯文本摘要，不是 JSON。")
        fallback.assert_called_once()

    def test_summary_of_no_record_is_pending(self):
        self.assertEqual(derived.summary_of("mindos_none")["status"], "pending")

    @patch.object(derived, "_ollama_scheduler")
    def test_submit_summary_submits_task(self, scheduler):
        derived.reset_derived_task_flags()
        derived.submit_summary("mindos_s1", "/tmp/a.pdf", force=True)
        scheduler.submit.assert_called_once()
        # submit(priority, task_fn, material_id=..., kind=...)：
        # force=True → 最高优先级，任务体为闭包包装的 _generate_summary_and_entities。
        self.assertIs(scheduler.submit.call_args.args[0], derived.PRIORITY_MANUAL_REGENERATE)
        self.assertEqual(scheduler.submit.call_args.kwargs["material_id"], "mindos_s1")
        self.assertEqual(scheduler.submit.call_args.kwargs["kind"], derived.KIND_SUMMARY.lower())


class DetailSummaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()
        self.source = self._tmp / "report.md"
        self.source.write_text("内容", encoding="utf-8")

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _detail(self, material_id):
        rec = {
            "material_id": material_id,
            "file_name": "report.md",
            "file_type": "document",
            "source_path": str(self.source),
            "job_id": f"job_{material_id}",
            "created_at": 1700000000.0,
            "folder": "未分类",
        }
        body = "这是一段很长的正文内容" * 20
        with patch.object(
            ingestion.JobStore, "instance",
            return_value=MagicMock(get=lambda _m: rec, is_canceled=lambda _m: False),
        ), patch.object(ingestion, "get_job", return_value={"state": "done"}), patch.object(
            ingestion, "get_source_chunks", return_value=[]
        ), patch.object(ingestion, "_ann_get", return_value={"tags": []}), patch.object(
            ingestion, "parse_file", return_value={"text": body, "parts": []}
        ):
            return ingestion.detail_of(material_id)

    def test_detail_summary_object_and_excerpt(self):
        detail = self._detail("mindos_ds1")
        # 无摘要记录 → pending，不伪装成正文截断
        self.assertEqual(detail["summary"]["status"], "pending")
        self.assertEqual(detail["summary"]["text"], "")
        self.assertEqual(detail["excerpt"], ("这是一段很长的正文内容" * 20)[:200])

    def test_detail_summary_ok_from_derived(self):
        self.store.set_derived_record(
            "material", "mindos_ds1", "SUMMARY", "ok", {"text": "自动摘要文本"}, "h", "g"
        )
        detail = self._detail("mindos_ds1")
        self.assertEqual(detail["summary"]["status"], "ok")
        self.assertEqual(detail["summary"]["text"], "自动摘要文本")
        self.assertIsNotNone(detail["summary"]["generatedAt"])


class RetryEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()
        self.source = self._tmp / "report.md"
        self.source.write_text("内容", encoding="utf-8")

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_retry_submits_force(self):
        rec = {
            "material_id": "mindos_rs1", "file_name": "report.md",
            "file_type": "document", "source_path": str(self.source),
        }
        with patch.object(
            ingestion.JobStore, "instance",
            return_value=MagicMock(get=lambda _m: rec),
        ), patch.object(uploads.ingestion, "source_path_of", return_value=str(self.source)), patch.object(
            uploads.derived_svc, "submit_summary"
        ) as submit:
            result = uploads.mindos_material_summary_retry("mindos_rs1")
        submit.assert_called_once_with("mindos_rs1", str(self.source), force=True)
        self.assertEqual(result["materialId"], "mindos_rs1")
        self.assertEqual(result["status"], "pending")


class SummaryObjectConsumersTests(unittest.TestCase):
    """P1 回归：summary 改为对象后，关联推荐 / 标签推荐不得再按字符串使用。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_material_related_handles_summary_object(self):
        detail = {
            "summary": {"text": "自动摘要", "status": "ok", "generatedAt": None},
            "fileName": "plan.pdf",
        }
        with patch.object(ingestion, "source_path_of", return_value="/tmp/plan.pdf"), patch.object(
            ingestion, "material_tags", return_value=[]
        ), patch.object(related, "get_source_embedding", return_value=[]), patch.object(
            ingestion, "detail_of", return_value=detail
        ), patch.object(related, "_similar_materials", return_value=[]), patch.object(
            related, "_shared_tag_materials", return_value=[]
        ), patch.object(related, "_keyword_materials", return_value=[]), patch.object(
            related, "_similar_knowledge", return_value=[]
        ), patch.object(related, "_shared_tag_knowledge", return_value=[]), patch.object(
            related, "_keyword_knowledge", return_value=[]
        ):
            result = related.material_related("mindos_r1")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)

    def test_tag_suggestions_reads_derived_cache(self):
        # P14-04：候选标签改为异步生成后从派生缓存读取，请求内不再同步调用模型；
        # 返回结构为 {status, items, generatedAt}（原 {suggestions} 已废弃）。
        self.store.set_derived_record(
            "material", "mindos_t1", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": [{"suggestionId": "tag:AI", "name": "AI", "confirmed": False}]},
            "h", "g",
        )
        rec = {"material_id": "mindos_t1", "source_path": "/tmp/plan.pdf"}
        with patch.object(
            ingestion.JobStore, "instance", return_value=MagicMock(get=lambda _m: rec),
        ), patch.object(
            ingestion, "source_path_of", return_value="/tmp/plan.pdf"
        ), patch.object(
            uploads.derived_svc, "refresh_analysis"
        ) as refresh:
            result = uploads.mindos_material_tag_suggestions("mindos_t1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["items"][0]["suggestionId"], "tag:AI")
        self.assertFalse(result["items"][0]["confirmed"])
        refresh.assert_called_once_with("mindos_t1", "/tmp/plan.pdf")

    def test_summary_text_of_backward_compat(self):
        # 对象形态
        self.assertEqual(ingestion.summary_text_of({"summary": {"text": "对象摘要"}}), "对象摘要")
        self.assertEqual(ingestion.summary_text_of({"summary": {"text": "", "status": "ok"}}), "")
        # 旧字符串形态
        self.assertEqual(ingestion.summary_text_of({"summary": "旧字符串摘要"}), "旧字符串摘要")
        self.assertEqual(ingestion.summary_text_of({"summary": None}), "")


class WatcherEarlyReturnSummaryTests(unittest.TestCase):
    """P2 回归：空文本等早退路径要清理旧 chunks 并落为 skipped 摘要（而非复用旧摘要）。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()
        self.src = self._tmp / "report.txt"

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_empty_text_early_return_clears_chunks_and_submits_summary(self):
        self.src.write_text("   ", encoding="utf-8")
        with patch.object(watcher, "_index_fingerprint", return_value="h"), patch.object(
            watcher, "get_source_hash", return_value=None
        ), patch.object(watcher.annotations, "get_rag_override", return_value=None), patch.object(
            watcher.annotations, "caption_of", return_value=""
        ), patch.object(watcher, "delete_text_chunks") as delete_chunks, patch.object(
            watcher, "_submit_material_summary"
        ) as submit:
            result = watcher.index_file(str(self.src), force=True)
        self.assertFalse(result)
        delete_chunks.assert_called_once_with(str(self.src))
        # 空文本早退路径以 force 提交：明确判定「合法空」而非读取故障
        submit.assert_called_once_with(str(self.src), force=True)

    def test_reprocess_to_empty_clears_chunks_and_skips_summary(self):
        """有旧摘要 + 旧 chunks → 重处理为空文本 → 旧 chunks 被清、摘要 skipped、不调用模型。"""
        # 预置旧摘要记录（模拟之前有正文时已生成）
        self.store.set_derived_record(
            "material", "mindos_e1", "SUMMARY", "ok", {"text": "旧摘要"}, "oldhash", "g"
        )
        self.src.write_text("   ", encoding="utf-8")
        with patch.object(watcher, "_submit_material_analysis"), patch.object(watcher, "_index_fingerprint", return_value="h"), patch.object(
            watcher, "get_source_hash", return_value=None
        ), patch.object(watcher.annotations, "get_rag_override", return_value=None), patch.object(
            watcher.annotations, "caption_of", return_value=""
        ), patch.object(
            watcher.derived_store, "material_id_for_source", return_value="mindos_e1"
        ), patch.object(
            derived, "submit_summary",
            side_effect=lambda material_id, source_path, force=False: derived._generate_summary_and_entities(
                material_id, source_path, force
            ),
        ), patch.object(
            derived, "_call_summary_entities_model", side_effect=AssertionError("空文本不应调用模型")
        ), patch.object(watcher, "delete_text_chunks") as delete_chunks, patch(
            "vector_store.read_source_chunks", return_value=(READ_EMPTY, [])
        ):
            result = watcher.index_file(str(self.src), force=True)
        self.assertFalse(result)
        # 旧 chunks 已被清除
        delete_chunks.assert_called_once_with(str(self.src))
        # 摘要落为 skipped，不再沿用旧摘要
        rec = self.store.get_derived_record("material", "mindos_e1", "SUMMARY")
        self.assertEqual(rec["status"], "skipped")
        self.assertEqual(rec["content"]["text"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
