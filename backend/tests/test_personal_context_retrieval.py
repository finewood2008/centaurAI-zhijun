"""Synthetic context/recall coverage; fake encoders only and isolated databases."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mindos.stores import conversation_store, ontology_store
from mindos.zhijun.memory_context import build_focus, conversation_intent
from mindos.zhijun import memory_retrieval as recall
from mindos.zhijun.memory_index import CACHE, scores


class FocusTests(unittest.TestCase):
    def test_slot_answer_keeps_event_and_last_question_without_inventing_facts(self):
        history = [{"role": "user", "content": "我在考虑换工作"},
                   {"role": "assistant", "content": "你最担心薪资还是时间？"},
                   {"role": "user", "content": "时间"},
                   {"role": "assistant", "content": "能留出多久做决定？"}]
        focus = build_focus("三个月", history)
        self.assertTrue(focus["continuation"])
        self.assertIn("换工作", focus["query"])
        self.assertIn("能留出多久", focus["question"])
        self.assertIn("三个月", focus["query"])
        self.assertEqual(focus["historyUsed"], [0, 2, 3])

    def test_topic_switch_and_following_slot_do_not_reopen_previous_event(self):
        history = [{"role": "user", "content": "PRIVATE_OLD_WORK 我在考虑换工作"},
                   {"role": "assistant", "content": "还担心什么？"},
                   {"role": "user", "content": "换个话题，安排周末徒步"},
                   {"role": "assistant", "content": "想走多长时间？"}]
        focus = build_focus("两小时", history)
        self.assertIn("徒步", focus["query"])
        self.assertNotIn("PRIVATE_OLD_WORK", focus["query"])
        self.assertEqual(build_focus("换个话题，聊摄影", history)["historyUsed"], [])

    def test_multiple_switches_use_latest_boundary(self):
        history = [{"role": "user", "content": "换个话题，SECRET_OLDER"},
                   {"role": "user", "content": "再换个话题，去海边"},
                   {"role": "assistant", "content": "想什么时候出发？"}]
        self.assertNotIn("SECRET_OLDER", build_focus("周六", history)["query"])

    def test_no_summary_or_incomplete_reply_becomes_working_evidence(self):
        history = [{"role": "user", "content": "我在考虑一场分享"},
                   {"role": "assistant", "content": "我推测你害怕失败。"},
                   {"role": "assistant", "content": "PRIVATE_PARTIAL?", "status": "streaming"}]
        focus = build_focus("继续", history)
        self.assertNotIn("害怕失败", focus["query"])
        self.assertNotIn("PRIVATE_PARTIAL", focus["query"])

    def test_self_contained_question_and_courtesy_do_not_inherit(self):
        history = [{"role": "user", "content": "我在考虑换工作"},
                   {"role": "assistant", "content": "最担心什么？"}]
        for text in ("明天天气如何？", "谢谢"):
            self.assertEqual(build_focus(text, history)["query"], text)

    def test_charter_context_distinguished_from_authoritative_document(self):
        self.assertEqual(conversation_intent("人生章程写了哪些内容？", []), "charter")
        self.assertEqual(conversation_intent("结合我的经历，看看章程适不适合我", []), "charter_context")
        history = [{"role": "user", "content": "结合我的本体讨论章程"}]
        self.assertEqual(conversation_intent("那怎么办？", history), "charter_context")
        self.assertEqual(conversation_intent("最近公司给我安排了一个新项目", history, "charter"), "conversation")

    def test_retrospective_is_explicit_and_bounded(self):
        self.assertEqual(build_focus("回顾我当时的工作安排", [])["mode"], "retrospective")
        self.assertEqual(build_focus("2020年的工作是什么", [])["mode"], "retrospective")
        self.assertEqual(build_focus("产品需求发生变化怎么办", [])["mode"], "current")
        self.assertLessEqual(len(build_focus("还有" + "字"*10000, [{"role": "user", "content": "字"*10000}]*9)["query"]), 2322)


class PersonalRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.onto = ontology_store.reset_for_tests(Path(self.tmp.name)/"ontology.db")
        self.convs = conversation_store.reset_for_tests(Path(self.tmp.name)/"conversations.db")
        self.module = SimpleNamespace(_text_model=None, get_text_embedder=Mock(side_effect=AssertionError("no model loading")))
        self.patch = patch.dict("sys.modules", {"embedder": self.module})
        self.patch.start()
        CACHE.clear()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def claim(self, content, *, scope="global", section="matters", state="confirmed", claim_scope="long_term", **fields):
        conv = self.convs.create_conversation(device_scope=scope)
        msg = self.convs.append_message(conv["id"], "user", content)
        return self.onto.create_claim({"section": section, "layer": "self_declared", "content": content,
            "subject_entity_id": "ent_me", "device_scope": scope, "scope": claim_scope, **fields},
            [{"kind": "conversation_turn", "conversation_id": conv["id"], "message_id": msg["id"], "quote": content}],
            trust_state=state, trust_origin="user_created")

    def recall(self, text, **kwargs):
        return recall.retrieve_claims(self.onto, text, [], conversations=self.convs, scope="global", **kwargs)

    def test_scope_filter_precedes_result_limit_and_index(self):
        foreign = self.claim("星桥项目研发安排", scope="foreign")
        mine = self.claim("星桥项目由我负责")
        result = self.recall("星桥项目研发安排", limit=1)
        self.assertEqual([c["id"] for c in result], [mine["id"]])
        encoder = Mock(side_effect=lambda texts, **kw: [[1.0, 0.0] for _ in texts])
        self.module._text_model = SimpleNamespace(encode=encoder)
        self.recall("星桥项目")
        self.assertNotIn(foreign["id"], next(iter(CACHE.values()))["rows"])

    def test_all_candidates_before_cap_include_old_scoped_record(self):
        mine = self.claim("这是我的星桥项目")
        rows = [{**mine, "id": f"foreign{i}", "evidence": [{"conversationId": "missing-device"}]} for i in range(2100)]
        with patch.object(self.onto, "list_claims", return_value=[*rows, mine]) as listing:
            self.assertEqual(self.recall("星桥项目", limit=1)[0]["id"], mine["id"])
        self.assertEqual(listing.call_args.kwargs["limit"], -1)

    def test_deleted_origin_or_message_not_recalled_and_unknown_scope_fails_closed(self):
        item = self.claim("我负责星桥项目")
        evidence = item["evidence"][0]
        with self.convs._connect() as db:
            db.execute("DELETE FROM messages WHERE id=?", (evidence["messageId"],))
        self.assertEqual(self.recall("星桥项目"), [])
        self.assertEqual(recall.retrieve_claims(self.onto, "星桥项目", [], scope="global"), [])

    def test_recycled_material_excluded_before_limit(self):
        item = self.claim("星桥项目安排")
        rows = [{**item, "evidence": [*item["evidence"], {"materialId": "gone"}]}]
        with patch.object(self.onto, "list_claims", return_value=rows), patch("mindos.chat_imports.require_material", side_effect=ValueError("recycled")):
            self.assertEqual(self.recall("星桥项目"), [])

    def test_context_and_exception_can_recall_without_claim_body_match(self):
        item = self.claim("我愿意尝试", scope="global")
        contextual = {"situation": "熟悉的主题，有准备时间", "exceptions": "陌生听众且需要即兴回答时尚不确定"}
        with patch.object(self.onto, "list_claims", return_value=[{**item, "contextual": contextual}]):
            result = self.recall("陌生听众，即兴回答", limit=1)
        self.assertEqual(result[0]["id"], item["id"])
        self.assertIn("exceptions", result[0]["matchedFields"])
        self.assertEqual(result[0]["contextual"], contextual)

    def test_entity_alias_finds_relationship_claim(self):
        entity = self.onto.upsert_entity("王海", "person", aliases=["老王"])
        item = self.claim("他是与我长期合作的搭档", section="people", object_entity_id=entity["id"])
        result = self.recall("老王", limit=1)
        self.assertEqual(result[0]["id"], item["id"])
        self.assertIn("entities", result[0]["matchedFields"])

    def test_expired_and_superseded_only_explicit_retrospective(self):
        past = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        old = self.claim("我负责星桥项目", valid_to=past)
        self.assertEqual(self.recall("星桥项目"), [])
        self.assertEqual(self.recall("回顾星桥项目")[0]["temporalStatus"], "historical")
        with self.onto._connect() as db:
            db.execute("UPDATE claims SET trust_state='superseded',valid_to=NULL WHERE id=?", (old["id"],))
        self.assertEqual(self.recall("星桥项目"), [])
        self.assertEqual(self.recall("过去的星桥项目")[0]["id"], old["id"])
        with self.onto._connect() as db:
            db.execute("UPDATE claims SET trust_state='retracted' WHERE id=?", (old["id"],))
        self.assertEqual(self.recall("过去的星桥项目"), [])

    def test_generic_question_without_event_does_not_recall(self):
        self.claim("我担心会不会错过一个机会", section="ways")
        self.assertEqual(self.recall("会不会？"), [])
        self.assertEqual(self.recall("那会不会这样？"), [])

    def test_uncalibrated_and_low_alignment_facts_are_eligible(self):
        item = self.claim("星桥项目只是公司安排的工作")
        self.assertIsNone(self.recall("星桥项目")[0]["selfAlignment"]["level"])
        with patch.object(self.onto, "list_claims", return_value=[{**item, "selfAlignment": {"level": 0, "framing": "long_term"}}]):
            self.assertEqual(self.recall("星桥项目")[0]["id"], item["id"])

    def test_background_caps_and_excludes_traits_aspirations_and_context_only(self):
        for i in range(6):
            self.claim("我的工作角色" + str(i), section="who", predicate="role")
        self.claim("我希望成为艺术家", section="who", layer="aspirational")
        self.claim("我可能擅长公开表达", section="who", predicate="has_trait")
        self.claim("我当时是代班负责人", section="who", claim_scope="context_only", predicate="role")
        other = self.onto.upsert_entity("合成同事", "person")
        self.claim("合成同事是工程师", section="who", subject_entity_id=other["id"])
        rows = recall.confirmed_background(self.onto, conversations=self.convs, scope="global")
        self.assertLessEqual(len(rows), 4)
        self.assertLessEqual(sum(len(c["content"]) for c in rows), 600)
        self.assertTrue(all(c["subjectEntityId"] == "ent_me" and c["predicate"] in ("is", "role", "background") and c["layer"] != "aspirational" and c["scope"] != "context_only" for c in rows))
        self.assertGreater(len(recall.confirmed_background(self.onto, conversations=self.convs, scope="global", limit=32, budget=4800)), 4)


class FullIndexTests(unittest.TestCase):
    def setUp(self):
        CACHE.clear()

    def test_full_index_semantic_hit_beyond_old_sample(self):
        def encode(texts, **kwargs):
            return [[1.0, 0.0] if text in ("寻找独立决策", "只有第九十九条是对应概念") else [0.0, 1.0] for text in texts]
        model = SimpleNamespace(encode=Mock(side_effect=encode))
        documents = {f"c{i:03}": str(i) for i in range(120)}
        documents["c099"] = "只有第九十九条是对应概念"
        with patch.dict("sys.modules", {"embedder": SimpleNamespace(_text_model=model)}):
            first = scores(("db", "one"), "寻找独立决策", documents)
            calls = model.encode.call_count
            self.assertEqual(scores(("db", "one"), "寻找独立决策", documents), first)
        self.assertEqual(len(first), 120)
        self.assertEqual(max(first, key=first.get), "c099")
        self.assertEqual(model.encode.call_count, calls)
        self.assertTrue(all(len(c.args[0]) <= 33 for c in model.encode.call_args_list))

    def test_version_change_deletion_and_scope_namespace(self):
        model = SimpleNamespace(encode=Mock(side_effect=lambda texts, **kw: [[1.0, 0.0] for _ in texts]))
        with patch.dict("sys.modules", {"embedder": SimpleNamespace(_text_model=model)}):
            scores(("db", "a"), "q", {"one": "old", "gone": "secret"})
            scores(("db", "b"), "q", {"foreign": "another device"})
            scores(("db", "a"), "q", {"one": "new"})
        self.assertEqual(set(CACHE[("db", "a")]["rows"]), {"one"})
        self.assertEqual(set(CACHE[("db", "b")]["rows"]), {"foreign"})
        self.assertEqual(model.encode.call_args.args[0], ["new"])

    def test_failed_refresh_does_not_retain_old_version(self):
        model = SimpleNamespace(encode=Mock(side_effect=lambda texts, **kw: [[1.0, 0.0] for _ in texts]))
        with patch.dict("sys.modules", {"embedder": SimpleNamespace(_text_model=model)}):
            scores(("db", "a"), "q", {"one": "old", "gone": "secret"})
            model.encode.side_effect = RuntimeError("unavailable")
            self.assertEqual(scores(("db", "a"), "q", {"one": "new"}), {})
        self.assertEqual(CACHE[("db", "a")]["rows"], {})

    def test_same_text_with_changed_source_version_reindexes(self):
        model = SimpleNamespace(encode=Mock(side_effect=lambda texts, **kw: [[1.0, 0.0] for _ in texts]))
        with patch.dict("sys.modules", {"embedder": SimpleNamespace(_text_model=model)}):
            scores(("db", "a"), "q", {"one": "same text"}, {"one": "v1"})
            old = CACHE[("db", "a")]["rows"]["one"][0]
            scores(("db", "a"), "q", {"one": "same text"}, {"one": "v2"})
        self.assertNotEqual(CACHE[("db", "a")]["rows"]["one"][0], old)
        self.assertEqual(model.encode.call_count, 2)
