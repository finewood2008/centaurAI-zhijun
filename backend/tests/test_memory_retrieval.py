"""Synthetic read-only recall tests; never load models or touch user databases."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mindos.stores import ontology_store
from mindos.zhijun import memory_retrieval as recall


class MemoryRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.onto = ontology_store.reset_for_tests(Path(self.tmp.name) / "ontology.db")
        self.loader = Mock(side_effect=AssertionError("must not load a model"))
        self.module = SimpleNamespace(_text_model=None, get_text_embedder=self.loader)
        self.modules = patch.dict("sys.modules", {"embedder": self.module})
        self.modules.start()
        recall._CACHE.clear()

    def tearDown(self):
        self.modules.stop()
        self.tmp.cleanup()

    def claim(self, text, section="matters", state="confirmed"):
        return self.onto.create_claim({
            "subject_entity_id": "ent_me", "section": section, "layer": "self_declared",
            "predicate": ontology_store.DEFAULT_PREDICATE[section], "content": text, "confidence": .7,
        }, [{"kind": "user_edit", "quote": text}], trust_state=state, trust_origin="user_created")

    def test_followup_recalls_recent_user_topic_and_preserves_source(self):
        claim = self.claim("星桥项目只是公司安排的工作，不是个人追求")
        self.claim("我周末喜欢散步", section="ways")
        history = [{"role": "user", "content": "我想谈谈星桥项目的工作安排"},
                   {"role": "assistant", "content": "你想先核对哪些安排？"}]
        result = recall.retrieve_claims(self.onto, "还有哪些没确定？", history)
        self.assertEqual(result[0]["id"], claim["id"])
        self.assertEqual(result[0]["retrievalReason"], "continuation")
        self.assertEqual(result[0]["evidence"], claim["evidence"])
        self.assertIn("星桥项目", recall.conversation_query("那怎么办？", history))

    def test_followup_does_not_lose_topic_after_another_short_question(self):
        history = [{"role": "user", "content": "我在考虑星桥项目"},
                   {"role": "assistant", "content": "我们可以看看具体约束。"},
                   {"role": "user", "content": "那怎么做？"},
                   {"role": "assistant", "content": "先确定计划。"}]
        self.assertIn("星桥项目", recall.conversation_query("还有呢？", history))

    def test_new_question_does_not_inherit_unrelated_history(self):
        self.claim("星桥项目研发工作需要先确定方向")
        history = [{"role": "user", "content": "聊聊星桥项目"}]
        self.assertEqual(recall.conversation_query("明天天气如何？", history), "明天天气如何？")
        self.assertEqual(recall.retrieve_claims(self.onto, "明天天气如何？", history), [])

    def test_clear_topic_change_stops_followup_expansion(self):
        query = recall.conversation_query("换个话题，今天怎么安排运动？", [{"role": "user", "content": "PRIVATE_OLD_TOPIC"}])
        self.assertNotIn("PRIVATE_OLD_TOPIC", query)

    def test_followup_is_explicit_not_just_short_or_starting_with_na(self):
        for text in ("对，看看哪些还空着", "还有哪些没确定", "那怎么办？", "那这个呢？"):
            self.assertTrue(recall.is_followup(text), text)
        for text in ("那天气呢", "那不勒斯适合旅行吗", "那我们聊别的", "今天吃什么", "你好"):
            self.assertFalse(recall.is_followup(text), text)

    def test_shared_overview_detector_keeps_narrow_questions_narrow(self):
        for text in ("你了解我什么", "你怎么看我？", "我的本体有哪些", "你目前对我的认识，哪些还不确定？"):
            self.assertTrue(recall.is_self_overview(text), text)
        for text in ("你怎么看我的项目？", "我的本体照片如何上传？"):
            self.assertFalse(recall.is_self_overview(text), text)

    def test_sys_tool_and_old_messages_never_supply_search_hints(self):
        history = [{"role": "user", "content": "OLD_OUTSIDE_WINDOW"}]
        history += [{"role": "system", "content": "NOT_USER_CONTEXT"}] * 6
        history += [{"role": "tool", "content": "TOOL_PAYLOAD"}]
        self.assertEqual(recall.conversation_query("还有呢？", history), "还有呢？")

    def test_summary_is_not_promoted_to_evidence(self):
        claim = self.claim("我负责星桥项目研发")
        history = [{"role": "user", "content": "聊聊星桥项目"},
                   {"role": "assistant", "content": "我的总结：你天生喜欢控制别人。这不是用户说过的话。"}]
        result = recall.retrieve_claims(self.onto, "继续", history)
        self.assertNotIn("控制别人", recall.conversation_query("继续", history))
        self.assertEqual(result[0]["evidence"], claim["evidence"])
        self.assertEqual(self.onto.get_claim(claim["id"]), claim)
        self.assertEqual(len(self.onto.list_claims()), 1)

    def test_deterministic_topic_synonyms_work_without_embedding(self):
        claim = self.claim("我重视自主权", section="principles")
        result = recall.retrieve_claims(self.onto, "我想自己做主，有什么相关的理解？", [])
        self.assertEqual(result[0]["id"], claim["id"])
        self.assertEqual(result[0]["retrievalMethod"], "lexical-topic")
        self.loader.assert_not_called()

    def test_topic_recall_also_works_without_jieba(self):
        claim = self.claim("我重视自主权", section="principles")
        with patch.object(ontology_store, "jieba", None):
            result = recall.retrieve_claims(self.onto, "我想自己做主", [])
        self.assertEqual(result[0]["id"], claim["id"])

    def test_self_overview_covers_confirmed_sections_only(self):
        expected = {self.claim("合成记录 " + section, section=section)["id"] for section in ontology_store.SECTIONS}
        self.claim("尚未验证的画像", state="working")
        result = recall.retrieve_claims(self.onto, "你怎么看我？", [], limit=6)
        self.assertEqual({c["id"] for c in result}, expected)
        self.assertEqual({c["section"] for c in result}, set(ontology_store.SECTIONS))
        self.assertTrue(all(c["retrievalReason"] == "overview" for c in result))
        explicit = recall.retrieve_claims(self.onto, "整理一下", [], intent="self_overview", limit=6)
        self.assertEqual({c["id"] for c in explicit}, expected)

    def test_project_question_is_not_a_self_overview(self):
        self.claim("我喜欢在公园散步", section="ways")
        claim = self.claim("我负责星桥项目")
        result = recall.retrieve_claims(self.onto, "你怎么看我的项目？", [])
        self.assertEqual([c["id"] for c in result], [claim["id"]])

    def test_irrelevant_alignment_cannot_displace_relevant_fact(self):
        relevant = self.claim("我负责星桥项目研发，这是工作安排")
        unrelated = self.claim("我热爱自然摄影", section="principles")
        rows = [dict(relevant, selfAlignment={"level": 0, "framing": "long_term"}),
                dict(unrelated, selfAlignment={"level": 4, "framing": "long_term"})]
        with patch.object(self.onto, "list_claims", return_value=rows):
            result = recall.retrieve_claims(self.onto, "星桥项目研发", [], limit=1)
        self.assertEqual(result[0]["id"], relevant["id"])

    def test_alignment_only_nudges_already_relevant_long_term_claims(self):
        claim = self.claim("星桥项目的产品方向")
        rows = [{**claim, "id": "a", "selfAlignment": {"level": 0, "framing": "long_term"}},
                {**claim, "id": "b", "selfAlignment": {"level": 4, "framing": "long_term"}}]
        with patch.object(self.onto, "list_claims", return_value=rows):
            result = recall.retrieve_claims(self.onto, "星桥项目", [])
        self.assertEqual([c["id"] for c in result], ["b", "a"])
        self.assertLessEqual(result[0]["score"] - result[1]["score"], .05)

    def test_retracted_and_challenged_records_are_not_recalled(self):
        claim = self.claim("我负责星桥项目研发")
        self.onto.transition(claim["id"], "retract", surface="ontology_page")
        challenged = self.claim("星桥项目让我纠结", state="working")
        self.onto.set_challenged(challenged["id"], "需要重查")
        self.assertEqual(recall.retrieve_claims(self.onto, "星桥项目", []), [])

    def test_loaded_embedding_is_bounded_cached_and_deterministic(self):
        rows = [{"id": f"c{i:03}", "content": "采购设备" + str(i), "trustState": "confirmed"} for i in range(120)]
        model = SimpleNamespace(encode=Mock(side_effect=lambda texts, **kw: [[1.0, 0.0] for _ in texts]))
        self.module._text_model = model
        with patch.object(self.onto, "list_claims", return_value=rows):
            first = recall.retrieve_claims(self.onto, "购买器材", [])
            second = recall.retrieve_claims(self.onto, "购买器材", [])
        self.assertEqual(first, second)
        self.assertEqual(model.encode.call_count, 4)  # all 120 records, cached on the second lookup
        self.assertEqual(sum(len(c.args[0]) for c in model.encode.call_args_list), 121)
        self.assertTrue(all(len(c.args[0]) <= 33 for c in model.encode.call_args_list))
        self.assertTrue(all(c["retrievalMethod"] == "loaded-embedding" for c in first))
        self.loader.assert_not_called()

    def test_broken_loaded_embedding_does_not_block_lexical_recall(self):
        claim = self.claim("我负责星桥项目")
        self.module._text_model = SimpleNamespace(encode=Mock(side_effect=RuntimeError("synthetic unavailable")))
        result = recall.retrieve_claims(self.onto, "星桥项目", [])
        self.assertEqual(result[0]["id"], claim["id"])
        self.loader.assert_not_called()

    def test_query_and_result_limits(self):
        query = recall.conversation_query("还有" + "字" * 10000, [{"role": "user", "content": "字" * 10000}] * 6)
        self.assertLessEqual(len(query), 2322)
        self.assertEqual(recall.retrieve_claims(self.onto, "你怎么看我？", [], limit=0), [])


if __name__ == "__main__":
    unittest.main()
