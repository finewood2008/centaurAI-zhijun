"""知君「良师」内核：相似历史判断进商量、复盘经验 → 原则候选、抽取合并与期限、第一次观察、带主题的摘要、异步草稿。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import conversations, growth, nudges, ontology
from mindos.stores import conversation_store as conversation_store_module
from mindos.stores import growth_store as growth_store_module
from mindos.stores import ontology_store as ontology_store_module
from mindos.stores.ontology_store import ME_ENTITY_ID
from mindos.zhijun import context as context_module
from mindos.zhijun import extract, growth_hooks, history, jobs, persona
from mindos.zhijun.provider import FakeProvider, ONBOARDING_QUESTIONS, ProviderError


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decision(title: str, **extra) -> dict:
    payload = {
        "title": title,
        "context": "背景",
        "options": ["外包", "自己做"],
        "choice": "自己做",
        "rationale": "因为控制力更强",
        "confidence": 60,
        "expectedOutcome": "两周内见效",
        "reviewAt": _iso(datetime.now(timezone.utc) + timedelta(days=10)),
        "relatedEntityIds": [],
        "evidenceRefs": [],
    }
    payload.update(extra)
    return payload


class MentorCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.onto = ontology_store_module.reset_for_tests(root / "ontology.db")
        self.convs = conversation_store_module.reset_for_tests(root / "conversations.db")
        self.growth = growth_store_module.reset_for_tests(root / "growth.db")
        self._env = patch.dict(os.environ, {"ZHIJUN_PROVIDER": "fake", "ZHIJUN_MATERIAL_EVIDENCE": "0"})
        self._env.start()
        app = FastAPI()
        for module in (conversations, ontology, nudges, growth):
            app.include_router(module.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_persona_uses_bounded_partner_identity(self) -> None:
        self.assertIn("有记忆边界、可核对、不会替用户决定的 AI 长期思考伙伴", persona.PERSONA_CORE)
        self.assertNotIn("AI 良师益友", persona.PERSONA_CORE)

    # ---- 历史判断进商量
    def test_similar_decisions_prefer_reviewed_and_feed_context(self) -> None:
        reviewed = self.growth.create_decision(_decision("远川项目测试要不要外包"))
        self.growth.record_outcome(reviewed["id"], {"result": "自己做，慢了两周但质量稳", "notes": "", "evidenceRefs": []})
        self.growth.create_review({"decisionId": reviewed["id"], "reflection": "低估了周期", "lessons": ["先小范围验证再全量"], "nextAction": "下次预留缓冲"})
        self.growth.create_decision(_decision("给团队放一周假", context="大家太累"))
        picked = history.similar_decisions("远川项目的测试是外包还是自己做", k=3, growth=self.growth)
        self.assertEqual(picked[0]["id"], reviewed["id"])
        block = persona.past_decisions_block(picked)
        self.assertIn("你过去类似的判断", block)
        self.assertIn("先小范围验证再全量", block)
        conv = self.convs.create_conversation(mode="chat")
        assembled = context_module.assemble(
            conversation=conv, user_text="远川项目的测试是外包还是自己做", depth="brief", provider=FakeProvider(), ontology=self.onto,
            recent_messages=[{"role": "user", "content": "远川项目的测试是外包还是自己做"}], user_turns=1, turn_mode="deliberate", past_decisions=picked,
        )
        self.assertIn("你过去类似的判断", assembled.system)
        self.assertEqual(assembled.debug["pastDecisions"][0]["id"], reviewed["id"])

    def test_principles_always_anchor_deliberation(self) -> None:
        self.onto.create_claim({"content": "我坚持先看数据再拍板", "section": "principles", "layer": "self_declared"}, [], trust_state="confirmed", trust_origin="utterance")
        # 再塞 13 条更近的已确认理解，把原则挤出词面检索的前 12 名——商量时仍要被带上。
        for i in range(13):
            self.onto.create_claim({"content": f"我第{i}件近况是在忙远川项目", "section": "who", "layer": "self_declared"}, [], trust_state="confirmed", trust_origin="utterance")
        conv = self.convs.create_conversation(mode="chat")
        assembled = context_module.assemble(
            conversation=conv, user_text="要不要给老客户涨价", depth="brief", provider=FakeProvider(), ontology=self.onto,
            recent_messages=[{"role": "user", "content": "要不要给老客户涨价"}], user_turns=1, turn_mode="deliberate",
        )
        self.assertIn("先看数据再拍板", assembled.system)
        plain = context_module.assemble(
            conversation=conv, user_text="要不要给老客户涨价", depth="brief", provider=FakeProvider(), ontology=self.onto,
            recent_messages=[{"role": "user", "content": "要不要给老客户涨价"}], user_turns=1, turn_mode="chat",
        )
        self.assertNotIn("先看数据再拍板", plain.system)

    # ---- 复盘经验 → 原则候选
    def test_review_lessons_become_principle_candidates(self) -> None:
        decision = self.growth.create_decision(_decision("要不要涨价"))
        self.growth.record_outcome(decision["id"], {"result": "涨了一成没流失", "notes": "", "evidenceRefs": []})
        res = self.client.post("/api/mindos/growth/reviews", json={"decisionId": decision["id"], "reflection": "低估了客户接受度", "lessons": ["先小范围试再全量", "定价前先问三个老客户"], "nextAction": "下次先试点"})
        self.assertEqual(res.status_code, 200, res.text)
        inbox = self.onto.inbox()
        contents = {c["content"] for c in inbox}
        self.assertIn("先小范围试再全量", contents)
        cand = next(c for c in inbox if c["content"] == "先小范围试再全量")
        self.assertEqual((cand["section"], cand["layer"], cand["trustState"]), ("principles", "aspirational", "working"))
        self.assertEqual(cand["evidence"][0]["kind"], "review")
        second = self.growth.create_decision(_decision("要不要换供应商"))
        self.growth.record_outcome(second["id"], {"result": "换了", "notes": "", "evidenceRefs": []})
        growth_hooks.on_review({"decisionId": second["id"], "lessons": ["先小范围试一下再全量推"]}, second, store=self.onto)
        refreshed = self.onto.get_claim(cand["id"])
        self.assertEqual(len(refreshed["evidence"]), 2)
        self.assertTrue(refreshed["promotionReady"])

    # ---- 抽取：merge_into 与期限
    def test_extraction_merge_into_and_commitment_date(self) -> None:
        existing = self.onto.create_claim({"content": "我在做远川项目", "section": "matters", "layer": "self_declared"}, [{"kind": "conversation_turn", "conversation_id": "c0", "message_id": "m0", "quote": "x"}], trust_state="confirmed", trust_origin="utterance")
        raw = {
            "claims": [
                {"section": "matters", "layer": "self_declared", "predicate": "working_on", "subject": "me", "object": None, "content": "我正在带远川项目", "quote": "我正在带远川项目", "confidence": 0.9, "scope_hint": "long_term", "privacy_hint": "private", "merge_into": existing["id"], "why_it_matters": "当前主线", "date": None},
                {"section": "matters", "layer": "self_declared", "predicate": "committed_to", "subject": "me", "object": None, "content": "我承诺三个月内把团队招齐", "quote": "三个月内把团队招齐", "confidence": 0.9, "scope_hint": "long_term", "privacy_hint": "private", "merge_into": "clm_bogus", "why_it_matters": "承诺要回访", "date": "2026-12-02"},
            ],
            "entities": [],
        }
        text = "我正在带远川项目，三个月内把团队招齐。"
        valid = extract.validate(raw, user_text=text, prev_assistant=None, existing_ids={existing["id"]})
        self.assertEqual(valid[0].merge_into, existing["id"])
        self.assertIsNone(valid[1].merge_into)
        self.assertTrue(valid[1].valid_to.startswith("2026-12-02"))
        summary = extract.persist(valid, [], store=self.onto, conversation_id="c1", message_id="m1")
        self.assertIn(existing["id"], summary["reaffirmed"])
        self.assertEqual(len(summary["created"]), 1)
        created = self.onto.get_claim(summary["created"][0])
        self.assertEqual(created["predicate"], "committed_to")
        self.assertTrue(created["validTo"].startswith("2026-12-02"))
        self.assertEqual(len(self.onto.get_claim(existing["id"])["evidence"]), 2)

    def test_aspiration_checked_on_whole_sentence(self) -> None:
        text = "接下来一两年，我想把公司做到盈亏平衡，然后能把周末还给家里。"
        raw = {"claims": [{"section": "direction", "layer": "aspirational", "predicate": "wants_to", "subject": "me", "object": None, "content": "我希望把周末还给家里", "quote": "能把周末还给家里", "confidence": 0.9, "scope_hint": "long_term", "privacy_hint": "private"}], "entities": []}
        valid = extract.validate(raw, user_text=text, prev_assistant=None)
        self.assertEqual(valid[0].layer, "aspirational")

    def test_entity_filter_drops_self_name_and_generic_groups(self) -> None:
        ents = [{"name": "阿远", "type": "person"}, {"name": "5人小组", "type": "person"}, {"name": "老周", "type": "person"}, {"name": "远川项目", "type": "project"}, {"name": "团队", "type": "organization"}]
        kept = extract.filter_entities(ents, user_text="叫我阿远就行。我在带一个 5 人的小组做远川项目，老周负责销售。")
        self.assertEqual([e["name"] for e in kept], ["老周", "远川项目"])
        kept = extract.filter_entities([{"name": "阿远", "type": "person"}], user_text="我和阿远一起吃饭")
        self.assertEqual(len(kept), 1)

    # ---- 建档收尾：第一次观察
    def test_onboarding_wrap_up_queues_first_observation(self) -> None:
        conv = self.client.post("/api/mindos/conversations", json={"mode": "onboarding"}).json()
        answers = ["你好，我们开始吧", "叫我阿远，我是一家小公司的创始人", "我在带远川项目", "我最在意我太太和合伙人老周", "最近我拒了一个大客户", "我坚持先看数据再拍板", "我想明年做到盈亏平衡", "健康话题不用主动提"]
        for a in answers:
            res = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": a})
            self.assertEqual(res.status_code, 200)
        jobs.drain(store=self.onto, conv_store=self.convs, max_jobs=60)
        hypotheses = [c for c in self.onto.list_claims(trust_states=("working",), limit=100) if c["layer"] == "hypothesis"]
        self.assertEqual(len(hypotheses), 1)
        self.assertIn("我猜你", hypotheses[0]["content"])
        self.assertEqual(hypotheses[0]["evidence"][0]["kind"], "conversation_turn")
        events = self.onto.review_events(hypotheses[0]["id"])
        self.assertEqual(events[-1]["surface"], "onboarding")

    # ---- 摘要带主题
    def test_summary_keeps_themes_and_open_loops(self) -> None:
        conv = self.convs.create_conversation()
        for i in range(8):
            self.convs.append_message(conv["id"], "user", f"第{i}轮我在准备远川项目上线，下周要给客户演示。")
            self.convs.append_message(conv["id"], "assistant", "记下了")
        jobs.enqueue_summary(conv["id"], store=self.onto)
        jobs.drain(store=self.onto, conv_store=self.convs)
        summary = self.convs.latest_summary(conv["id"])
        self.assertIsNotNone(summary)
        block = persona.themes_block(summary)
        self.assertIn("反复出现", block)

    # ---- 商量：演示模型同步草稿事件带 state=ready
    def test_deliberate_draft_event_state(self) -> None:
        conv = self.client.post("/api/mindos/conversations", json={}).json()
        res = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": "我在纠结要不要外包还是自己做。", "mode": "deliberate"})
        self.assertIn('"state": "ready"', res.text)
        self.assertIn("relatedDecisionIds", res.text)

    # ---- DeepSeek 思考开关：低强度关、中高强度开并放宽预算；非 DeepSeek 不发未知参数
    def test_openai_compatible_thinking_control(self) -> None:
        from mindos.zhijun.provider import JSON_TASK_MIN_TOKENS, ChatRequest, OpenAICompatibleProvider

        ds = OpenAICompatibleProvider("https://api.deepseek.com/v1", "deepseek-v4-pro", "k", timeout=5)
        body = {"max_tokens": 1024}
        ds._apply_thinking(body, ChatRequest(system="", messages=[], effort="low"))
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_tokens"], 1024)
        body = {"max_tokens": 1024}
        ds._apply_thinking(body, ChatRequest(system="", messages=[], effort="medium"))
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["max_tokens"], JSON_TASK_MIN_TOKENS)
        other = OpenAICompatibleProvider("https://example.com/v1", "m", "k", timeout=5)
        body = {"max_tokens": 1024}
        other._apply_thinking(body, ChatRequest(system="", messages=[], effort="medium"))
        self.assertNotIn("thinking", body)
        forced = OpenAICompatibleProvider("https://example.com/v1", "m", "k", timeout=5, thinking="deepseek")
        forced._apply_thinking(body, ChatRequest(system="", messages=[], effort="low"))
        self.assertEqual(body["thinking"], {"type": "disabled"})

    # ---- 出处显式化：provenance 带 pastDecisions 与 anchorClaimIds
    @staticmethod
    def _provenance(sse_text: str) -> dict:
        for frame in sse_text.split("\n\n"):
            lines = frame.strip().split("\n")
            if lines and lines[0] == "event: provenance":
                return json.loads("\n".join(l[5:].strip() for l in lines[1:] if l.startswith("data:")))
        raise AssertionError("没有 provenance 事件")

    def test_provenance_carries_past_decisions_and_anchors(self) -> None:
        reviewed = self.growth.create_decision(_decision("远川项目测试要不要外包"))
        self.growth.record_outcome(reviewed["id"], {"result": "自己做，慢了两周但质量稳", "notes": "", "evidenceRefs": []})
        principle = self.onto.create_claim({"content": "我坚持先看数据再拍板", "section": "principles", "layer": "self_declared"}, [], trust_state="confirmed", trust_origin="utterance")
        conv = self.client.post("/api/mindos/conversations", json={}).json()
        # 商量：过去判断与原则锚点都在出处里，且锚点 id 也出现在 confirmedClaims 中
        res = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": "远川项目的测试是外包还是自己做", "mode": "deliberate"})
        self.assertEqual(res.status_code, 200, res.text)
        prov = self._provenance(res.text)
        self.assertEqual(prov["pastDecisions"][0]["id"], reviewed["id"])
        self.assertEqual(set(prov["pastDecisions"][0]), {"id", "title", "choice", "status", "createdAt"})
        self.assertEqual(prov["pastDecisions"][0]["status"], "outcome_recorded")
        self.assertIn(principle["id"], prov["anchorClaimIds"])
        self.assertIn(principle["id"], [c["id"] for c in prov["confirmedClaims"]])
        # 普通聊天：一句值得记的话也带上相似判断；锚点只在商量 / 回访 / 深入时带
        res = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": "我在想远川项目的测试是外包还是自己做，最近压力挺大。"})
        prov = self._provenance(res.text)
        self.assertEqual([d["id"] for d in prov["pastDecisions"]], [reviewed["id"]])
        self.assertEqual(prov["anchorClaimIds"], [])
        # 纯提问（没有第一人称、问号结尾）不过抽取门，也不查历史
        res = self.client.post(f"/api/mindos/conversations/{conv['id']}/messages", json={"content": "远川项目测试外包好吗？"})
        self.assertEqual(self._provenance(res.text)["pastDecisions"], [])

    # ---- 手写补一条可省略分区
    def test_create_claim_without_section_classifies(self) -> None:
        res = self.client.post("/api/mindos/ontology/claims", json={"content": "我希望明年把公司做到盈利"})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual((body["section"], body["layer"]), ("direction", "aspirational"))
        res = self.client.post("/api/mindos/ontology/claims", json={"content": "我和老周一起做远川项目"})
        self.assertEqual(res.json()["section"], "people")


if __name__ == "__main__":
    unittest.main()
