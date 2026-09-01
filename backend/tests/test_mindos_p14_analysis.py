"""MindOS P14-04 自动关键词、实体与标签候选确认回归测试。

覆盖：
- 候选标签（TAG_SUGGESTIONS）：空文本→skipped、hash 未变不重复生成、
  hash 变更重生成且保留已确认标记、suggestionId=tag:{name}、候选不自动转正式；
- 实体抽取（ENTITY_EXTRACTION）：严格 JSON schema 校验、不合法输出绝不保存
  模型原始文本（走正则/jieba 降级或 failed）、连接错误→unavailable、空数组→ok；
- analysis_of / tag_suggestions_of / entities_of 视图及状态；
- submit / refresh：同输入 hash 只生成一次、缺失/失败才补调度、skipped 不重复；
- confirm_tag_suggestion：标记已确认、已确认幂等；
- HTTP 接口：analysis GET 聚合、tag-suggestions GET 读缓存 + refresh、
  confirm POST 的 404/409/幂等/成功（写正式标签 + 审计）；
- watcher：索引成功/空文本早退路径都提交分析任务。

依赖项目 .venv，可独立于 server 运行：
    .venv\\Scripts\\python.exe -m unittest test_mindos_p14_analysis -v
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
from mindos.stores import derived_store
from mindos.services import ingestion
from mindos import uploads


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class EntityJsonTests(unittest.TestCase):
    """实体 JSON 解析 / 严格校验 / 去重。"""

    def test_parse_fenced_json_array(self):
        answer = '```json\n[{"type":"term","name":"算法"}]```'
        self.assertEqual(derived._parse_entity_json(answer)[0]["name"], "算法")

    def test_parse_wrapped_dict(self):
        answer = '{"entities": [{"type":"person","name":"张三"}]}'
        data = derived._parse_entity_json(answer)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["type"], "person")

    def test_parse_empty_list_ok(self):
        self.assertEqual(derived._parse_entity_json("[]"), [])

    def test_parse_invalid_returns_none(self):
        self.assertIsNone(derived._parse_entity_json("我无法提取实体"))
        self.assertIsNone(derived._parse_entity_json(""))

    def test_parse_summary_entities_json_object(self):
        answer = json.dumps({
            "summary": "一段摘要。",
            "entities": [{"type": "term", "name": "算法", "confidence": 0.8, "evidence": "x"}],
        }, ensure_ascii=False)
        summary, entities = derived._parse_summary_entities(answer, "本文介绍算法的实现")
        self.assertEqual(summary, "一段摘要。")
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["name"], "算法")

    def test_parse_summary_entities_plain_text(self):
        # 模型未按 JSON 指令输出 → 整段按摘要保留，实体为 None（走降级）
        summary, entities = derived._parse_summary_entities("纯文本摘要内容", "内容甲")
        self.assertEqual(summary, "纯文本摘要内容")
        self.assertIsNone(entities)

    def test_parse_summary_entities_drops_symbols(self):
        # LLM 误把 markdown 符号当术语 → 符号过滤剔除，仅保留自然语言实体
        answer = json.dumps({
            "summary": "摘要。",
            "entities": [
                {"type": "term", "name": "###", "confidence": 0.9, "evidence": "###"},
                {"type": "term", "name": "算法", "confidence": 0.8, "evidence": "x"},
            ],
        }, ensure_ascii=False)
        summary, entities = derived._parse_summary_entities(answer, "### 标题\n本文介绍算法的实现")
        self.assertEqual(summary, "摘要。")
        self.assertEqual([it["name"] for it in entities], ["算法"])

    def test_normalize_rejects_symbol_only_names(self):
        # ### / --- 等纯符号不是自然语言实体，一律拒绝
        src = "### 标题\n---\n正文介绍算法"
        self.assertIsNone(derived._normalize_entity(
            {"type": "term", "name": "###", "confidence": 0.9, "evidence": "###"}, src,
        ))
        self.assertIsNone(derived._normalize_entity(
            {"type": "term", "name": "---", "confidence": 0.9, "evidence": "---"}, src,
        ))
        # 含自然语言成分的实体不受影响
        self.assertIsNotNone(derived._normalize_entity(
            {"type": "term", "name": "算法", "confidence": 0.9, "evidence": "x"}, src,
        ))

    def test_normalize_rejects_bad_type_and_name(self):
        src = "张三在北京发表了讲话"
        self.assertIsNone(derived._normalize_entity({"type": "badtype", "name": "X"}, src))
        self.assertIsNone(derived._normalize_entity({"type": "person", "name": " "}, src))
        self.assertIsNone(derived._normalize_entity({"type": "term", "name": "字" * 65}, src))
        self.assertIsNone(derived._normalize_entity(None, src))

    def test_normalize_rejects_entity_not_in_source(self):
        # 模型返回的实体名不在原文中 → 视为幻觉，直接丢弃
        self.assertIsNone(derived._normalize_entity(
            {"type": "person", "name": "张三", "confidence": 0.9, "evidence": "张三发言"},
            "输入正文是内容甲",
        ))
        self.assertIsNone(derived._normalize_entity(
            {"type": "place", "name": "北京", "confidence": 0.8, "evidence": "在北京"},
            "原文未提及任何城市名",
        ))

    def test_normalize_regenerates_evidence_from_source(self):
        # 不信任模型传回的 evidence：统一由服务端从原文截取
        src = "会议讨论了托克维尔在《论美国的民主》中的观点"
        it = derived._normalize_entity(
            {"type": "person", "name": "托克维尔", "confidence": 0.9, "evidence": "伪造片段"},
            src,
        )
        self.assertEqual(it["evidence"], "会议讨论了托克维尔在《论美国的民主》中的观点")
        self.assertNotIn("伪造片段", it["evidence"])
        it = derived._normalize_entity(
            {"type": "person", "name": "托克维尔", "confidence": 0.9, "evidence": "x"},
            "原文没有出现这个名字",
        )
        self.assertIsNone(it)  # 名字未命中 → 丢弃

    def test_normalize_coerces_confidence(self):
        src = "介绍了深度学习的算法原理"
        it = derived._normalize_entity(
            {"type": "term", "name": "算法", "confidence": 5, "evidence": "新型算法"},
            src,
        )
        self.assertEqual(it["confidence"], 0.6)
        it = derived._normalize_entity(
            {"type": "term", "name": "算法", "confidence": "x", "evidence": ""},
            src,
        )
        self.assertEqual(it["confidence"], 0.6)

    def test_make_entity_id_and_evidence_cap(self):
        it = derived._make_entity("place", "北京", 0.8, "文" * 500)
        self.assertEqual(it["entityId"], "entity:place:北京")
        self.assertLessEqual(len(it["evidence"]), derived.MAX_EVIDENCE_CHARS)

    def test_dedupe_keeps_max_confidence(self):
        items = [
            {"entityId": "entity:term:算法", "type": "term", "name": "算法", "confidence": 0.4, "evidence": ""},
            {"entityId": "entity:term:算法", "type": "term", "name": "算法", "confidence": 0.9, "evidence": ""},
        ]
        result = derived._dedupe_entities(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["confidence"], 0.9)

    def test_entities_from_llm_drops_invalid_rows(self):
        answer = json.dumps([
            {"type": "person", "name": "张三", "confidence": 0.9, "evidence": "伪造证据"},
            {"type": "badtype", "name": "坏行", "confidence": 0.5, "evidence": "x"},
            {"type": "term", "name": "算法", "confidence": 0.7, "evidence": "模型编造的片段"},
        ])
        items = derived._entities_from_llm(answer, source_text="张三介绍了算法的实现")
        self.assertIsNotNone(items)
        self.assertEqual(len(items), 2)
        # evidence 一律来自原文，绝不保留模型提供的片段
        for it in items:
            self.assertIn(it["name"], it["evidence"])
        self.assertNotIn("伪造证据", items[0]["evidence"])
        self.assertNotIn("模型编造的片段", items[1]["evidence"])

    def test_entities_from_llm_hallucinated_all_return_none(self):
        # 输出合法但全部实体不在原文 → 返回 None（调用方走 fallback / failed）
        answer = json.dumps([
            {"type": "person", "name": "张三", "confidence": 0.9, "evidence": "张三发言"},
            {"type": "place", "name": "北京", "confidence": 0.8, "evidence": "在北京"},
        ])
        self.assertIsNone(derived._entities_from_llm(answer, source_text="内容甲"))

    def test_entities_from_llm_empty_array_ok(self):
        # 模型明确输出空数组 → 合法空
        self.assertEqual(derived._entities_from_llm("[]", source_text="内容甲"), [])

    def test_entities_from_llm_invalid_returns_none(self):
        self.assertIsNone(derived._entities_from_llm("模型说：无法回答", source_text="内容甲"))


class GenerateTagSuggestionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _record(self):
        return self.store.get_derived_record(
            "material", "mindos_t1", derived.KIND_TAG_SUGGESTIONS
        )

    @patch("mindos.tag_suggest.suggest_tags_with_source")
    @patch.object(derived, "_input_text", return_value="   ")
    def test_empty_text_skipped_without_suggest(self, _input, suggest):
        derived._generate_tag_suggestions("mindos_t1", "/tmp/a.pdf")
        suggest.assert_not_called()
        rec = self._record()
        self.assertEqual(rec["status"], "skipped")
        self.assertEqual(rec["content"]["items"], [])
        self.assertEqual(rec["content"]["source"], "fallback")

    @patch("mindos.tag_suggest.suggest_tags_with_source",
           return_value={"items": ["AI", "数据库"], "source": "llm"})
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_success_saves_candidates_source_llm(self, _input, suggest):
        derived._generate_tag_suggestions("mindos_t1", "/tmp/a.pdf")
        rec = self._record()
        self.assertEqual(rec["status"], "ok")
        items = rec["content"]["items"]
        self.assertEqual([it["name"] for it in items], ["AI", "数据库"])
        self.assertEqual(items[0]["suggestionId"], "tag:AI")
        self.assertFalse(items[0]["confirmed"])
        self.assertEqual(rec["content"]["source"], "llm")
        self.assertEqual(rec["input_hash"], _hash("内容甲"))

    @patch("mindos.tag_suggest.suggest_tags_with_source",
           return_value={"items": ["降级标签"], "source": "fallback"})
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_fallback_source_recorded(self, _input, suggest):
        derived._generate_tag_suggestions("mindos_t1", "/tmp/a.pdf")
        rec = self._record()
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["content"]["source"], "fallback")

    @patch("mindos.tag_suggest.suggest_tags_with_source")
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_unchanged_hash_does_not_regenerate(self, _input, suggest):
        gen = derived._generator_name(derived.get_provider().get_local_snapshot())
        self.store.set_derived_record(
            "material", "mindos_t1", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": [{"suggestionId": "tag:旧", "name": "旧", "confirmed": False}]},
            _hash("内容甲"), gen,
        )
        derived._generate_tag_suggestions("mindos_t1", "/tmp/a.pdf")
        suggest.assert_not_called()
        self.assertEqual(self._record()["content"]["items"][0]["name"], "旧")

    @patch("mindos.tag_suggest.suggest_tags_with_source",
           return_value={"items": ["AI", "新标签"], "source": "llm"})
    @patch.object(derived, "_input_text", return_value="内容乙")
    def test_changed_hash_keeps_confirmed_flag(self, _input, suggest):
        self.store.set_derived_record(
            "material", "mindos_t1", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": [
                {"suggestionId": "tag:AI", "name": "AI", "confirmed": True},
                {"suggestionId": "tag:旧", "name": "旧", "confirmed": False},
            ]},
            "oldhash", "g",
        )
        derived._generate_tag_suggestions("mindos_t1", "/tmp/a.pdf")
        items = self._record()["content"]["items"]
        by_name = {it["name"]: it for it in items}
        self.assertTrue(by_name["AI"]["confirmed"])       # 已确认的候选被保留
        self.assertFalse(by_name["新标签"]["confirmed"])  # 新候选未确认
        self.assertNotIn("旧", by_name)

    @patch("mindos.tag_suggest.suggest_tags_with_source",
           return_value={"items": [], "source": "fallback"})
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_empty_suggestions_marks_failed(self, _input, suggest):
        derived._generate_tag_suggestions("mindos_t1", "/tmp/a.pdf")
        self.assertEqual(self._record()["status"], "failed")


class GenerateEntitiesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _record(self):
        return self.store.get_derived_record(
            "material", "mindos_e1", derived.KIND_ENTITY_EXTRACTION
        )

    @patch.object(derived, "_call_entity_model")
    @patch.object(derived, "_input_text", return_value="   ")
    def test_empty_text_skipped_without_model(self, _input, call_model):
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        call_model.assert_not_called()
        self.assertEqual(self._record()["status"], "skipped")

    @patch.object(derived, "_call_entity_model", return_value=json.dumps([
        {"type": "person", "name": "张三", "confidence": 0.9, "evidence": "模型编造的片段A"},
        {"type": "place", "name": "北京", "confidence": 0.8, "evidence": "模型编造的片段B"},
    ]))
    @patch.object(derived, "_input_text", return_value="张三在北京参加了会议，讨论了算法。")
    def test_valid_json_ok_source_llm(self, _input, call_model):
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        rec = self._record()
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["content"]["source"], "llm")
        self.assertEqual(len(rec["content"]["items"]), 2)
        # evidence 由服务端从原文生成，模型提供的片段绝不落库
        for it in rec["content"]["items"]:
            self.assertIn(it["name"], it["evidence"])
        serialized = json.dumps(rec["content"], ensure_ascii=False)
        self.assertNotIn("模型编造的片段", serialized)

    @patch.object(derived, "_call_entity_model", return_value=json.dumps([
        {"type": "person", "name": "张三", "confidence": 0.9, "evidence": "张三发言"},
        {"type": "place", "name": "北京", "confidence": 0.8, "evidence": "在北京"},
    ]))
    @patch.object(derived, "_entity_fallback", return_value=[])
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_hallucinated_entities_fall_back_to_failed(self, _input, fallback, call_model):
        """模型返回的实体全部不在原文 → 绝不作为 ok 落库，走 fallback/未果则 failed。"""
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        rec = self._record()
        self.assertEqual(rec["status"], "failed")
        self.assertEqual(rec["content"]["items"], [])
        self.assertNotEqual(rec["content"].get("source"), "llm")

    @patch.object(derived, "_call_entity_model", return_value=json.dumps([
        {"type": "person", "name": "张三", "confidence": 0.9, "evidence": "张三发言"},
        {"type": "place", "name": "北京", "confidence": 0.8, "evidence": "在北京"},
    ]))
    @patch.object(derived, "_entity_fallback", return_value=[
        {"entityId": "entity:term:内容", "type": "term",
         "name": "内容", "confidence": 0.5, "evidence": "内容甲"},
    ])
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_hallucinated_entities_fall_back_to_local(self, _input, fallback, call_model):
        """模型输出了原文不存在的实体 → 丢弃并降级，source 必须为 fallback、绝无 llm。"""
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        rec = self._record()
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["content"]["source"], "fallback")
        serialized = json.dumps(rec["content"], ensure_ascii=False)
        self.assertNotIn("张", serialized)  # 模型幻觉与伪造证据都不落库

    @patch.object(derived, "_call_entity_model", return_value="[]")
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_empty_array_ok(self, _input, call_model):
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        rec = self._record()
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["content"]["source"], "llm")
        self.assertEqual(rec["content"]["items"], [])

    @patch.object(derived, "_call_entity_model", return_value="模型说：无法回答")
    @patch.object(derived, "_entity_fallback", return_value=[])
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_invalid_output_never_saves_raw_text_and_failed(self, _input, fallback, call_model):
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        rec = self._record()
        self.assertEqual(rec["status"], "failed")
        # 绝不保存模型原始文本（任何字段都不得出现）
        serialized = json.dumps(rec["content"], ensure_ascii=False)
        self.assertNotIn("模型说", serialized)
        self.assertEqual(rec["content"]["items"], [])

    @patch.object(derived, "_call_entity_model", return_value="胡说八道")
    @patch.object(derived, "_entity_fallback", return_value=[
        {"entityId": "entity:organization:腾讯", "type": "organization",
         "name": "腾讯", "confidence": 0.45, "evidence": "腾讯公司"},
    ])
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_invalid_output_falls_back_to_fallback(self, _input, fallback, call_model):
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        rec = self._record()
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["content"]["source"], "fallback")
        self.assertEqual(rec["content"]["items"][0]["name"], "腾讯")

    @patch.object(
        derived, "_call_entity_model",
        side_effect=urllib.error.URLError("conn refused"),
    )
    @patch.object(derived, "_entity_fallback", return_value=[])
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_connection_error_unavailable(self, _input, fallback, call_model):
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        self.assertEqual(self._record()["status"], "unavailable")

    @patch.object(
        derived, "_call_entity_model",
        side_effect=urllib.error.URLError("conn refused"),
    )
    @patch.object(derived, "_entity_fallback", return_value=[
        {"entityId": "entity:term:算法", "type": "term",
         "name": "算法", "confidence": 0.5, "evidence": "算法"},
    ])
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_connection_error_with_fallback_ok(self, _input, fallback, call_model):
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        rec = self._record()
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["content"]["source"], "fallback")

    @patch.object(
        derived, "_call_entity_model", side_effect=ValueError("bad response"),
    )
    @patch.object(derived, "_entity_fallback", return_value=[])
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_other_error_marks_failed(self, _input, fallback, call_model):
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        self.assertEqual(self._record()["status"], "failed")

    @patch.object(derived, "_call_entity_model", return_value=json.dumps([
        {"type": "term", "name": "算法", "confidence": 0.7, "evidence": "算法介绍"},
        {"type": "term", "name": "算法", "confidence": 0.9, "evidence": "算法实现"},
    ]))
    @patch.object(derived, "_input_text", return_value="本文重点介绍算法的实现方式")
    def test_dedupe_and_unchanged_hash(self, _input, call_model):
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        items = self._record()["content"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["confidence"], 0.9)
        # hash 未变 → 再次生成不调用模型
        call_model.reset_mock()
        derived._generate_entities("mindos_e1", "/tmp/a.pdf")
        call_model.assert_not_called()

    def test_entity_fallback_filters_markdown_symbols(self):
        # jieba 会把 markdown 的 ###/--- 排到关键词前列，符号过滤必须剔除
        items = derived._entity_fallback(
            "# P14 智能解析\n### 目标\n---\n### 结论\n正文介绍算法的实现方式"
        )
        names = [it["name"] for it in items]
        self.assertNotIn("###", names)
        self.assertNotIn("---", names)
        self.assertIn("算法", names)


class AnalysisViewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_views_pending_without_record(self):
        self.assertEqual(derived.tag_suggestions_of("m")["status"], "pending")
        self.assertEqual(derived.entities_of("m")["status"], "pending")
        self.assertEqual(derived.relations_of("m")["status"], "pending")
        view = derived.analysis_of("m")
        self.assertEqual(
            set(view.keys()),
            {"summary", "tagSuggestions", "entities", "relations"},
        )
        self.assertEqual(view["summary"]["status"], "pending")
        self.assertEqual(view["relations"]["status"], "pending")

    def test_views_reflect_records(self):
        self.store.set_derived_record(
            "material", "m", derived.KIND_SUMMARY, "ok", {"text": "摘要"}, "h", "g",
        )
        self.store.set_derived_record(
            "material", "m", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": [{"suggestionId": "tag:A", "name": "A", "confirmed": False}],
             "source": "fallback"},
            "h", "g",
        )
        self.store.set_derived_record(
            "material", "m", derived.KIND_ENTITY_EXTRACTION, "ok",
            {"items": [{"entityId": "entity:person:张三", "type": "person",
                        "name": "张三", "confidence": 0.9, "evidence": "张"}],
             "source": "llm"},
            "h", "g",
        )
        view = derived.analysis_of("m")
        self.assertEqual(view["summary"]["text"], "摘要")
        self.assertEqual(view["tagSuggestions"]["items"][0]["suggestionId"], "tag:A")
        self.assertEqual(view["entities"]["items"][0]["name"], "张三")
        self.assertIsNotNone(view["summary"]["generatedAt"])
        self.assertIsNotNone(view["entities"]["generatedAt"])
        # P1#3：降级来源必须透传到公开视图
        self.assertEqual(view["tagSuggestions"]["source"], "fallback")
        self.assertEqual(view["entities"]["source"], "llm")

    def test_views_source_null_when_absent(self):
        self.store.set_derived_record(
            "material", "m", derived.KIND_ENTITY_EXTRACTION, "ok",
            {"items": []}, "h", "g",
        )
        self.assertIsNone(derived.entities_of("m")["source"])
        self.assertIsNone(derived.tag_suggestions_of("missing")["source"])
        # confirm 重写记录后 source 仍保留
        self.store.set_derived_record(
            "material", "m", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": [{"suggestionId": "tag:A", "name": "A", "confirmed": False}],
             "source": "fallback"},
            "h", "g",
        )
        derived.confirm_tag_suggestion("m", "tag:A")
        self.assertEqual(
            self.store.get_derived_record("material", "m", derived.KIND_TAG_SUGGESTIONS)
            ["content"]["source"], "fallback",
        )


class SubmitRefreshAnalysisTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        derived.reset_derived_task_flags()
        derived.reset_relation_task_flags()
        self.store = derived_store.DerivedStore.instance()

    def tearDown(self):
        derived.reset_derived_task_flags()
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    @patch.object(derived, "_ollama_scheduler")
    def test_submit_analysis_submits_tag_suggestions_and_relations(self, scheduler):
        # P14-04/P0-1：分析路径补标签候选 + 关系三元组两个任务
        # （实体已随摘要合并生成，故不在此再提交实体任务）。统一调度器提交。
        derived.submit_analysis("mindos_s1", "/tmp/a.pdf")
        self.assertEqual(scheduler.submit.call_count, 2)
        kinds = [call.kwargs["kind"] for call in scheduler.submit.call_args_list]
        self.assertIn(derived.KIND_TAG_SUGGESTIONS.lower(), kinds)
        self.assertIn(derived.KIND_RELATION_EXTRACTION.lower(), kinds)

    @patch.object(derived, "_ollama_scheduler")
    def test_force_submit_analysis_also_refreshes_entities(self, scheduler):
        """用户手动重生成必须补实体，避免关系因无端点而不调用模型。"""
        derived.submit_analysis("mindos_s1", "/tmp/a.pdf", force=True)
        self.assertEqual(scheduler.submit.call_count, 3)
        kinds = [call.kwargs["kind"] for call in scheduler.submit.call_args_list]
        self.assertEqual(kinds, [
            derived.KIND_TAG_SUGGESTIONS.lower(),
            derived.KIND_ENTITY_EXTRACTION.lower(),
            derived.KIND_RELATION_EXTRACTION.lower(),
        ])

    @patch.object(derived, "submit_analysis")
    @patch.object(derived, "submit_summary")
    def test_reparse_all_marks_existing_outputs_pending_before_submission(self, submit_summary, submit_analysis):
        for kind, content in (
            (derived.KIND_SUMMARY, {"text": "旧摘要"}),
            (derived.KIND_TAG_SUGGESTIONS, {"items": []}),
            (derived.KIND_ENTITY_EXTRACTION, {"items": []}),
            (derived.KIND_RELATION_EXTRACTION, {"items": []}),
        ):
            self.store.set_derived_record("material", "mindos_s1", kind, "ok", content, "h", "g")

        result = derived.reparse_all("mindos_s1", "/tmp/a.pdf")

        self.assertEqual(result["summary"]["status"], "pending")
        self.assertEqual(result["tagSuggestions"]["status"], "pending")
        self.assertEqual(result["entities"]["status"], "pending")
        self.assertEqual(result["relations"]["status"], "pending")
        submit_summary.assert_called_once_with("mindos_s1", "/tmp/a.pdf", force=True)
        submit_analysis.assert_called_once_with("mindos_s1", "/tmp/a.pdf", force=True)

    @patch.object(derived, "_submit_relations")
    @patch.object(derived, "_call_entity_model", return_value="[]")
    @patch.object(derived, "_input_text", return_value="内容甲")
    def test_forced_entities_keep_force_for_chained_relations(self, _input, _model, submit_relations):
        """实体完成后重放关系任务时，不能丢失用户的强制重生成语义。"""
        derived._generate_entities("mindos_s1", "/tmp/a.pdf", force=True)
        submit_relations.assert_called_once_with("mindos_s1", "/tmp/a.pdf", True)

    @patch.object(derived, "_ollama_scheduler")
    def test_refresh_analysis_submits_when_missing(self, scheduler):
        # 四条记录都缺失 → 摘要、标签、实体、关系各补一次（统一调度器）
        derived.refresh_analysis("mindos_s1", "/tmp/a.pdf")
        self.assertEqual(scheduler.submit.call_count, 4)

    @patch.object(derived, "_ollama_scheduler")
    def test_refresh_analysis_skips_ok_and_skipped(self, scheduler):
        # 三条记录均 ok 且关系 hash 与当前实体一致 → 全部跳过
        text = "资料正文"
        with patch.object(derived, "_input_text", return_value=text):
            ent_rec = {"content": {"items": [{"type": "term", "name": "A"}]}, "status": "ok",
                       "generator": "g", "source": "llm"}
            for kind, rec in (
                (derived.KIND_SUMMARY, {"content": {"text": "摘要"}, "status": "ok"}),
                (derived.KIND_TAG_SUGGESTIONS, {"content": {"items": []}, "status": "ok"}),
                (derived.KIND_ENTITY_EXTRACTION, ent_rec),
                (derived.KIND_RELATION_EXTRACTION, {
                    "content": {"items": []}, "status": "ok",
                    "input_hash": derived._relation_input_hash(text, ent_rec)}),
            ):
                self.store.set_derived_record("material", "m", kind, rec["status"],
                                              rec["content"], rec.get("input_hash", "h"), "g")
            derived.refresh_analysis("m", "/tmp/a.pdf")
            scheduler.submit.assert_not_called()
        # 当前正文仍为空时，skipped 是稳定终态，不重复调度。
        for kind in (derived.KIND_SUMMARY, derived.KIND_TAG_SUGGESTIONS, derived.KIND_ENTITY_EXTRACTION,
                      derived.KIND_RELATION_EXTRACTION):
            self.store.set_derived_record("material", "m", kind, "skipped", {"items": []}, "", "g")
        with patch.object(derived, "_input_text", return_value="   "):
            derived.refresh_analysis("m", "/tmp/a.pdf")
        scheduler.submit.assert_not_called()

    @patch.object(derived, "_ollama_scheduler")
    def test_refresh_analysis_recovers_skipped_after_snapshot_text_is_available(self, scheduler):
        """快照晚于派生产物就绪时，旧 skipped 必须恢复为可生成任务。"""
        for kind in (derived.KIND_SUMMARY, derived.KIND_TAG_SUGGESTIONS, derived.KIND_ENTITY_EXTRACTION,
                     derived.KIND_RELATION_EXTRACTION):
            self.store.set_derived_record("material", "m", kind, "skipped", {"items": []}, "", "g")
        with patch.object(derived, "_input_text", return_value="已补齐的资料正文"):
            result = derived.refresh_analysis("m", "/tmp/a.pdf")
        self.assertEqual(scheduler.submit.call_count, 4)
        self.assertTrue(result["summaryScheduled"])
        self.assertTrue(result["tagScheduled"])
        self.assertTrue(result["entityScheduled"])
        self.assertTrue(result["relationScheduled"])
        for kind in (derived.KIND_SUMMARY, derived.KIND_TAG_SUGGESTIONS, derived.KIND_ENTITY_EXTRACTION,
                     derived.KIND_RELATION_EXTRACTION):
            self.assertEqual(self.store.get_derived_record("material", "m", kind)["status"], "pending")

    @patch.object(derived, "_ollama_scheduler")
    def test_refresh_analysis_resubmits_failed(self, scheduler):
        for kind in (derived.KIND_SUMMARY, derived.KIND_TAG_SUGGESTIONS, derived.KIND_ENTITY_EXTRACTION,
                      derived.KIND_RELATION_EXTRACTION):
            self.store.set_derived_record(
                "material", "m", kind, "failed", {"items": []}, "h", "g",
            )
        derived.refresh_analysis("m", "/tmp/a.pdf")
        self.assertEqual(scheduler.submit.call_count, 4)

    @patch.object(derived, "_ollama_scheduler")
    def test_refresh_analysis_respects_failure_cooldown(self, scheduler):
        future = __import__("time").time() + 300
        for kind, content in (
            (derived.KIND_SUMMARY, {"text": "", "retryAfter": future}),
            (derived.KIND_TAG_SUGGESTIONS, {"items": [], "retryAfter": future}),
            (derived.KIND_ENTITY_EXTRACTION, {"items": [], "retryAfter": future}),
            (derived.KIND_RELATION_EXTRACTION, {"items": [], "retryAfter": future}),
        ):
            self.store.set_derived_record("material", "m", kind, "unavailable", content, "h", "g")
        derived.refresh_analysis("m", "/tmp/a.pdf")
        scheduler.submit.assert_not_called()

    @patch.object(derived, "_ollama_scheduler")
    def test_refresh_analysis_resubmits_relation_when_hash_stale(self, scheduler):
        # 实体产物迭代后，ok 关系记录的复合 hash 过期 → 必须重算
        with patch.object(derived, "_input_text", return_value="资料正文") as inp:
            ent_rec = {"content": {"items": [{"type": "term", "name": "A"}]}, "status": "ok",
                       "generator": "g", "source": "llm"}
            self.store.set_derived_record("material", "m", derived.KIND_TAG_SUGGESTIONS,
                                          "ok", {"items": []}, "h", "g")
            self.store.set_derived_record("material", "m", derived.KIND_SUMMARY,
                                          "ok", {"text": "摘要"}, "h", "g")
            self.store.set_derived_record("material", "m", derived.KIND_ENTITY_EXTRACTION,
                                          "ok", ent_rec["content"], "h", "g")
            # 关系记录 hash 与当前实体产物不匹配 → 视为过期
            self.store.set_derived_record(
                "material", "m", derived.KIND_RELATION_EXTRACTION, "ok",
                {"items": []}, "stale-hash", "g",
            )
            derived.refresh_analysis("m", "/tmp/a.pdf")
        scheduler.submit.assert_called_once()
        self.assertEqual(
            scheduler.submit.call_args.kwargs["kind"],
            derived.KIND_RELATION_EXTRACTION.lower(),
        )


class ConfirmTagSuggestionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed(self, confirmed=False):
        self.store.set_derived_record(
            "material", "mindos_c1", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": [{"suggestionId": "tag:AI", "name": "AI", "confirmed": confirmed}]},
            "h", "g",
        )

    def test_confirm_marks_candidate(self):
        self._seed(confirmed=False)
        derived.confirm_tag_suggestion("mindos_c1", "tag:AI")
        items = self.store.get_derived_record(
            "material", "mindos_c1", derived.KIND_TAG_SUGGESTIONS
        )["content"]["items"]
        self.assertTrue(items[0]["confirmed"])

    def test_confirm_idempotent_no_rewrite(self):
        self._seed(confirmed=True)
        with patch.object(self.store, "set_derived_record", wraps=self.store.set_derived_record) as sdr:
            derived.confirm_tag_suggestion("mindos_c1", "tag:AI")
        sdr.assert_not_called()  # 已确认 → 不重复写库

    def test_confirm_noop_when_status_not_ok(self):
        self.store.set_derived_record(
            "material", "mindos_c2", derived.KIND_TAG_SUGGESTIONS, "failed",
            {"items": []}, "h", "g",
        )
        derived.confirm_tag_suggestion("mindos_c2", "tag:X")  # 不抛异常、不写库


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()
        self.source = self._tmp / "report.md"
        self.source.write_text("内容", encoding="utf-8")
        self.rec = {
            "material_id": "mindos_r1",
            "file_name": "report.md",
            "file_type": "document",
            "source_path": str(self.source),
        }
        self._job = patch.object(
            ingestion.JobStore, "instance",
            return_value=MagicMock(get=lambda _m: self.rec),
        )
        self._sp = patch.object(uploads.ingestion, "source_path_of", return_value=str(self.source))

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _record(self, kind):
        return self.store.get_derived_record("material", "mindos_r1", kind)

    def test_analysis_endpoint_aggregates_and_refreshes(self):
        self.store.set_derived_record(
            "material", "mindos_r1", derived.KIND_SUMMARY,
            "ok", {"text": "摘要"}, "h", "g",
        )
        with self._job, self._sp, patch.object(
            uploads.derived_svc, "refresh_analysis"
        ) as refresh:
            result = uploads.mindos_material_analysis("mindos_r1")
        refresh.assert_called_once_with("mindos_r1", str(self.source))
        self.assertEqual(result["materialId"], "mindos_r1")
        self.assertEqual(result["summary"]["text"], "摘要")
        self.assertEqual(result["tagSuggestions"]["status"], "pending")
        self.assertEqual(result["entities"]["status"], "pending")

    def test_regenerate_analysis_reparses_all_outputs(self):
        pending = {
            "summary": {"status": "pending"},
            "tagSuggestions": {"status": "pending"},
            "entities": {"status": "pending"},
            "relations": {"status": "pending"},
        }
        with self._job, self._sp, patch.object(uploads.derived_svc, "reparse_all", return_value=pending) as reparse:
            result = uploads.mindos_material_regenerate(
                "mindos_r1", uploads.RegenerateRequest(item="parse"),
            )
        reparse.assert_called_once_with("mindos_r1", str(self.source))
        self.assertEqual(result["materialId"], "mindos_r1")
        self.assertEqual(result["item"], "parse")

    def test_tag_suggestions_endpoint_reads_cache_and_refreshes(self):
        self.store.set_derived_record(
            "material", "mindos_r1", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": [{"suggestionId": "tag:AI", "name": "AI", "confirmed": False}],
             "source": "fallback"},
            "h", "g",
        )
        with self._job, self._sp, patch.object(
            uploads.derived_svc, "refresh_analysis"
        ) as refresh:
            result = uploads.mindos_material_tag_suggestions("mindos_r1")
        refresh.assert_called_once()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "fallback")
        self.assertEqual(result["items"][0]["suggestionId"], "tag:AI")

    def test_confirm_missing_candidate_404(self):
        self.store.set_derived_record(
            "material", "mindos_r1", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": [{"suggestionId": "tag:AI", "name": "AI", "confirmed": False}]},
            "h", "g",
        )
        with self._job, self._sp, self.assertRaises(Exception) as cm:
            uploads.mindos_material_tag_suggestion_confirm("mindos_r1", "tag:不存在")
        self.assertEqual(cm.exception.status_code, 404)

    def test_confirm_status_not_ok_409(self):
        # 无记录 → status=pending
        with self._job, self._sp, self.assertRaises(Exception) as cm:
            uploads.mindos_material_tag_suggestion_confirm("mindos_r1", "tag:AI")
        self.assertEqual(cm.exception.status_code, 409)

    def test_confirm_success_writes_tag_and_audit(self):
        self.store.set_derived_record(
            "material", "mindos_r1", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": [{"suggestionId": "tag:AI", "name": "AI", "confirmed": False}]},
            "h", "g",
        )
        with self._job, self._sp, patch.object(
            uploads.ingestion, "set_material_tags", return_value=["AI"]
        ) as set_tags, patch("annotations.add_audit") as audit:
            result = uploads.mindos_material_tag_suggestion_confirm("mindos_r1", "tag:AI")
        self.assertEqual(result["confirmed"], True)
        self.assertEqual(result["tags"], ["AI"])
        set_tags.assert_called_once_with("mindos_r1", ["AI"], "add")
        audit.assert_called_once()
        payload = audit.call_args.kwargs.get("payload") or audit.call_args.args[1].get("payload", {})
        self.assertEqual(payload.get("tag"), "AI")
        # 候选被标记为已确认
        items = self._record(derived.KIND_TAG_SUGGESTIONS)["content"]["items"]
        self.assertTrue(items[0]["confirmed"])

    def test_confirm_already_confirmed_is_idempotent(self):
        self.store.set_derived_record(
            "material", "mindos_r1", derived.KIND_TAG_SUGGESTIONS, "ok",
            {"items": [{"suggestionId": "tag:AI", "name": "AI", "confirmed": True}]},
            "h", "g",
        )
        with self._job, self._sp, patch.object(
            uploads.ingestion, "material_tags", return_value=["AI"]
        ), patch.object(uploads.ingestion, "set_material_tags") as set_tags, patch(
            "annotations.add_audit"
        ) as audit:
            result = uploads.mindos_material_tag_suggestion_confirm("mindos_r1", "tag:AI")
        self.assertEqual(result["confirmed"], True)
        set_tags.assert_not_called()  # 不重复写正式标签
        audit.assert_not_called()     # 不重复审计


class WatcherAnalysisSubmitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        derived_store.reset_for_tests(self._tmp / "derived.db")
        self.store = derived_store.DerivedStore.instance()
        self.src = self._tmp / "report.txt"

    def tearDown(self):
        derived_store.reset_for_tests()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_submit_material_analysis_resolves_and_submits(self):
        with patch.object(
            watcher.derived_store, "material_id_for_source", return_value="mindos_w1"
        ), patch("mindos.derived.submit_analysis") as submit:
            watcher._submit_material_analysis("/tmp/a.pdf")
        submit.assert_called_once_with("mindos_w1", "/tmp/a.pdf")

    def test_submit_material_analysis_skips_unknown_source(self):
        with patch.object(
            watcher.derived_store, "material_id_for_source", return_value=None
        ), patch("mindos.derived.submit_analysis") as submit:
            watcher._submit_material_analysis("/tmp/a.pdf")
        submit.assert_not_called()

    def test_empty_text_early_return_submits_analysis(self):
        """空文本早退路径：清旧 chunks + 提交摘要 + 提交分析（候选/实体落 skipped）。"""
        self.src.write_text("   ", encoding="utf-8")
        with patch.object(watcher, "_index_fingerprint", return_value="h"), patch.object(
            watcher, "get_source_hash", return_value=None
        ), patch.object(watcher.annotations, "get_rag_override", return_value=None), patch.object(
            watcher.annotations, "caption_of", return_value=""
        ), patch.object(watcher, "delete_text_chunks") as delete_chunks, patch.object(
            watcher, "_submit_material_summary"
        ), patch.object(watcher, "_submit_material_analysis") as submit_analysis:
            result = watcher.index_file(str(self.src), force=True)
        self.assertFalse(result)
        delete_chunks.assert_called_once_with(str(self.src))
        submit_analysis.assert_called_once_with(str(self.src))


if __name__ == "__main__":
    unittest.main(verbosity=2)
