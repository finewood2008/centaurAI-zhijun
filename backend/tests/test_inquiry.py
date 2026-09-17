"""求知引擎（V3 M4-v1）：各 kind、问句可被 _answer_section 识别、静默词、no_proactive、7 天冷却、优先级、提示词块与授权。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mindos.stores import conversation_store as conversation_store_module
from mindos.stores import growth_store as growth_store_module
from mindos.stores import ontology_store as ontology_store_module
from mindos.stores.charter_draft_store import render_document
from mindos.zhijun import inquiry
from mindos.zhijun.extract import _answer_section


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class InquiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.onto = ontology_store_module.reset_for_tests(root / "ontology.db")
        self.convs = conversation_store_module.reset_for_tests(root / "conversations.db")
        self.growth = growth_store_module.reset_for_tests(root / "growth.db")
        self._env = patch.dict(os.environ, {"ZHIJUN_PROVIDER": "fake", "ZHIJUN_MATERIAL_EVIDENCE": "0"})
        self._env.start()
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def claim(self, content, *, section="who", predicate=None, trust="confirmed", privacy="private", valid_to=None, layer=None):
        payload = {"content": content, "section": section, "layer": layer or ("self_declared" if trust == "confirmed" else "hypothesis"),
                   "privacy_level": privacy, "valid_to": valid_to}
        if predicate:
            payload["predicate"] = predicate
        return self.onto.create_claim(payload, [{"kind": "user_edit", "quote": content}], trust_state=trust,
                                      trust_origin="user_created" if trust == "confirmed" else "model")

    def targets(self, scope="global", **kwargs):
        return inquiry.targets(self.onto, self.convs, self.growth, scope, now=self.now, **kwargs)

    def publish(self, clauses):
        return self.growth.create_charter({"document": render_document(clauses), "clauses": clauses, "expectedVersion": 0,
                                           "workspaceId": "synthetic-test-workspace", "metadata": {"scope": "global", "origin": "workspace", "sources": []}})

    def test_gap_questions_map_back_to_their_sections(self) -> None:
        for section, question in inquiry.GAP_QUESTIONS.items():
            self.assertEqual(_answer_section(question), section, section)
        self.assertEqual(self.targets(), [])  # 空本体没有线索，不问
        self.claim("我是产品负责人", predicate="role")
        gaps = [t for t in self.targets() if t["kind"] == "gap"]
        self.assertEqual({t["targetId"] for t in gaps}, {"who", "people", "matters", "principles", "ways", "direction"})
        self.assertTrue(all(t["key"] == "gap:" + t["targetId"] and t["refs"] == [] for t in gaps))
        self.claim("我坚持先看数据再拍板", section="principles")
        self.claim("我做决定前会先和团队核对", section="principles")
        self.assertNotIn("principles", {t["targetId"] for t in self.targets() if t["kind"] == "gap"})

    def test_stale_tension_open_loop_nod_and_onboarding_topic(self) -> None:
        stale = self.claim("我是一个很久没提过的身份", predicate="role")
        with self.onto._connect() as db:
            db.execute("UPDATE claims SET last_reaffirmed=?, first_seen=? WHERE id=?", (_iso(self.now - timedelta(days=90)), _iso(self.now - timedelta(days=90)), stale["id"]))
        fresh = self.claim("我最近说过的身份", predicate="role")
        a = self.claim("我坚持周末不工作", section="principles")
        b = self.claim("我最近周末都在加班", section="ways")
        conflict = self.onto.create_conflict(a["id"], b["id"], kind="tension", note="张力")
        due = self.growth.create_decision({"title": "要不要外包测试", "context": "c", "options": ["a", "b"], "choice": "a", "rationale": "r", "confidence": 60,
                                           "expectedOutcome": "e", "reviewAt": _iso(self.now - timedelta(days=1)), "relatedEntityIds": [], "evidenceRefs": []})
        self.growth.create_decision({"title": "以后再看", "context": "c", "options": ["a", "b"], "choice": "a", "rationale": "r", "confidence": 60,
                                     "expectedOutcome": "e", "reviewAt": _iso(self.now + timedelta(days=10)), "relatedEntityIds": [], "evidenceRefs": []})
        commit = self.claim("我承诺这周交出方案", section="matters", predicate="committed_to", valid_to=_iso(self.now - timedelta(hours=1)))
        conv = self.convs.create_conversation()
        self.convs.append_message(conv["id"], "user", "x")
        self.convs.save_summary(conv["id"], up_to_seq=1, summary="s", key_points=["主题", "待办：约投资人"], meta={"routingSources": []})
        nod = self.claim("我可能更适合上午处理难题", predicate="has_trait", trust="working")
        ready = self.claim("我大概是个直接的人", predicate="has_trait", trust="working")
        self.onto.set_promotion_ready(ready["id"], True)
        onboarding = self.convs.create_conversation(mode="onboarding")
        self.convs.append_message(onboarding["id"], "assistant", "开场", meta={"kind": "onboarding_open", "onboardingTopic": "situation"})
        self.convs.append_message(onboarding["id"], "user", "我是产品负责人，最近在做远川项目")

        found = self.targets(limit=50)
        by_key = {t["key"]: t for t in found}
        self.assertIn("stale:" + stale["id"], by_key)
        self.assertNotIn("stale:" + fresh["id"], by_key)
        self.assertEqual(by_key["stale:" + stale["id"]]["question"], "上次你说「我是一个很久没提过的身份」，现在还是这样吗？")
        self.assertEqual(by_key["tension:" + conflict["id"]]["refs"], [{"kind": "claim", "id": a["id"]}, {"kind": "claim", "id": b["id"]}])
        self.assertIn("是原则变了，还是这次情况特殊", by_key["tension:" + conflict["id"]]["question"])
        self.assertEqual(by_key["due:" + due["id"]]["targetType"], "decision")
        self.assertNotIn("due:" + [d for d in self.growth.list_decisions() if d["title"] == "以后再看"][0]["id"], by_key)
        self.assertEqual(by_key["commit:" + commit["id"]]["kind"], "open_loop")
        self.assertEqual(by_key[f"loop:{conv['id']}:1:1"]["question"], "上次你说要「约投资人」，后来怎么样了？")
        self.assertEqual(by_key[f"loop:{conv['id']}:1:1"]["refs"], [{"kind": "summary", "id": f"{conv['id']}:1"}])
        nods = [t for t in found if t["kind"] == "nod"]
        self.assertEqual(nods[0]["targetId"], ready["id"])
        self.assertEqual(nods[1]["question"], "我印象里我可能更适合上午处理难题，对吗？")
        topic = next(t for t in found if t["kind"] == "onboarding_topic")
        self.assertEqual(topic["targetId"], "direction")  # 那句自述同时命中「当前处境」与「眼下在意的事」
        kinds = [t["kind"] for t in found]
        self.assertEqual(kinds, sorted(kinds, key=lambda k: inquiry._PRIORITY[k]))
        self.assertEqual(found[0]["kind"], "open_loop")
        picked = inquiry.pick(self.onto, self.convs, self.growth, "global", now=self.now)
        self.assertEqual(picked["key"], found[0]["key"])

    def test_sensitive_claims_are_never_targets(self) -> None:
        self.claim("我是产品负责人", predicate="role")
        self.claim("我有一段敏感的经历", predicate="background", privacy="sensitive", trust="working")
        secret = self.claim("我承诺处理一件敏感的事", section="matters", predicate="committed_to", privacy="sensitive", valid_to=_iso(self.now - timedelta(hours=1)))
        keys = {t["key"] for t in self.targets(limit=50)}
        self.assertFalse(any("敏感" in t["question"] for t in self.targets(limit=50)))
        self.assertNotIn("commit:" + secret["id"], keys)

    def test_quiet_words_drop_targets(self) -> None:
        self.claim("我是产品负责人", predicate="role")
        self.claim("我不希望AI主动提起健康这些话题", section="principles", predicate="boundary")
        self.claim("我最近健康状况不太好", predicate="has_trait", trust="working")
        self.claim("我大概更喜欢上午处理难题", predicate="has_trait", trust="working")
        nods = [t["question"] for t in self.targets(limit=50) if t["kind"] == "nod"]
        self.assertEqual(len(nods), 1)
        self.assertNotIn("健康", nods[0])

    def test_no_proactive_charter_keeps_only_gap_and_nod(self) -> None:
        self.claim("我是产品负责人", predicate="role")
        self.claim("我可能更适合上午处理难题", predicate="has_trait", trust="working")
        self.growth.create_decision({"title": "要不要外包测试", "context": "c", "options": ["a", "b"], "choice": "a", "rationale": "r", "confidence": 60,
                                     "expectedOutcome": "e", "reviewAt": _iso(self.now - timedelta(days=1)), "relatedEntityIds": [], "evidenceRefs": []})
        self.assertIn("open_loop", {t["kind"] for t in self.targets(limit=50)})
        self.publish([{"id": "quiet", "section": "与知君合作", "text": "不要主动提醒我", "kind": "boundary", "scope": "global", "context": "",
                       "control": "no_proactive", "sources": [], "quote": "不要主动提醒我"}])
        kinds = {t["kind"] for t in self.targets(limit=50)}
        self.assertTrue(kinds)
        self.assertTrue(kinds <= {"gap", "nod"}, kinds)

    def test_cooldown_and_bounded_recent_keys(self) -> None:
        self.claim("我是产品负责人", predicate="role")
        first = inquiry.pick(self.onto, self.convs, self.growth, "global", now=self.now)
        inquiry.mark_asked(self.onto, "global", first["key"], now=self.now)
        second = inquiry.pick(self.onto, self.convs, self.growth, "global", now=self.now)
        self.assertNotEqual(first["key"], second["key"])
        self.assertEqual(inquiry.pick(self.onto, self.convs, self.growth, "global", now=self.now + timedelta(days=8))["key"], first["key"])
        for index in range(30):
            inquiry.mark_asked(self.onto, "global", f"k{index}", now=self.now + timedelta(seconds=index))
        recent = json.loads(self.onto.meta_get("zhijun_inquiry_recent_v1"))
        self.assertEqual(len(recent), inquiry.RECENT_LIMIT)
        self.assertIn("k29", recent)
        self.assertNotIn("k0", recent)
        inquiry.mark_asked(self.onto, "dev-b", "scoped", now=self.now)
        self.assertIn("scoped", inquiry.recent_keys(self.onto, "dev-b", now=self.now))
        self.assertNotIn("scoped", inquiry.recent_keys(self.onto, "global", now=self.now))

    def test_render_is_bounded_and_question_detection(self) -> None:
        block = inquiry.render({"question": "问" * 200, "why": "因" * 100})
        self.assertLessEqual(len(block), inquiry.BLOCK_LIMIT)
        self.assertTrue(block.startswith(inquiry.HEADING))
        for text in ("你怎么看？", "帮我想想方案", "这样可以吗", "为什么会这样"):
            self.assertTrue(inquiry.is_question(text), text)
        for text in ("我今天很累。", "上周把定价表改完了"):
            self.assertFalse(inquiry.is_question(text), text)


class InquiryRoutingTests(unittest.TestCase):
    """提示词块：只在普通对话、非深入、用户本句非问句时注入；引用的来源必须已授权。"""
    from tests import test_task_routing as _harness
    setUp = _harness.RoutingTests.setUp
    tearDown = _harness.RoutingTests.tearDown
    enable = _harness.RoutingTests.enable
    claim = _harness.RoutingTests.claim

    def plan(self, text, **kwargs):
        from mindos.zhijun.routing import Router, prepare_chat
        return prepare_chat(Router(self.onto, self.convs, self.cid), text, **kwargs)

    def test_block_injected_after_context_plan_only_for_plain_statements(self) -> None:
        self.claim()
        plan = self.plan("我今天把定价表改完了。")
        system = plan.assembled.system
        self.assertIn(inquiry.HEADING, system)
        self.assertEqual(plan.assembled.provenance["inquiry"]["kind"], "gap")
        self.assertGreater(system.index(inquiry.HEADING), system.index("## 核心画像"))
        self.assertTrue(system.rstrip().endswith("）") or system.rstrip().endswith("？"))
        for text, kwargs in (("你觉得我该怎么办？", {}), ("我今天把定价表改完了。", {"depth": "deep"}), ("我今天把定价表改完了。", {"mode": "deliberate"}), ("我今天把定价表改完了。", {"omit": True})):
            other = self.plan(text, **kwargs)
            self.assertNotIn(inquiry.HEADING, other.assembled.system, (text, kwargs))
            self.assertIsNone(other.assembled.provenance["inquiry"])

    def test_unauthorized_target_is_skipped_not_blocking(self) -> None:
        c = self.claim()
        working = self.onto.create_claim({"subject_entity_id": "ent_me", "section": "who", "layer": "hypothesis", "predicate": "has_trait", "content": "我可能更适合上午处理难题"},
                                         [{"kind": "user_edit", "quote": "x"}], trust_state="working", trust_origin="model")
        self.onto.set_promotion_ready(working["id"], True)
        self.enable()
        plan = self.plan("我今天把定价表改完了。")
        self.assertEqual(plan.preview["missing"], [])
        info = plan.assembled.provenance["inquiry"]
        self.assertIsNotNone(info)
        self.assertNotEqual(info["targetId"], working["id"])
        self.assertNotIn("我可能更适合上午处理难题", json.dumps(plan.preview["request"], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
