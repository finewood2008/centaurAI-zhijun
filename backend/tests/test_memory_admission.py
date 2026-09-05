"""Memory value admission uses synthetic utterances, without a model or live DB."""
from __future__ import annotations

from dataclasses import replace
import unittest

from mindos.zhijun import extract


def claim(text: str, **overrides) -> extract.ValidatedClaim:
    fields = dict(
        section="who", layer="self_declared", predicate="role", subject="me", object=None,
        content=text, quote=text, confidence=.95, scope="long_term", privacy_level="private",
        why_it_matters="职业建议需要考虑用户的岗位职责。",
    )
    fields.update(overrides)
    return extract.ValidatedClaim(**fields)


class MemoryRequestTests(unittest.TestCase):
    def test_only_explicit_positive_save_requests(self):
        for text in (
            "请记住我是产品经理。", "你帮我记下来：我很重视自主决定。", "帮我把这次活动的目标记下来。",
            "我希望你记住我刚才的选择。", "这点请记住：明天先看看作品。", "请保存一下这段内容。",
        ):
            with self.subTest(text=text):
                self.assertTrue(extract.explicit_memory_request(text))

    def test_discussing_memory_negation_and_forgetting_are_not_save_requests(self):
        for text in (
            "为什么你什么都想记住？", "我不想让你记住这些。", "不要把这件事记下来。",
            "先别记住我的临时计划。", "不必保存这条。", "请删除这条我让你记住的内容。",
            "把这条记忆忘掉。", "我昨天让你记住了什么？", "不要记住活动安排，但这件事请记住。",
            "记住是什么意思？",
        ):
            with self.subTest(text=text):
                self.assertFalse(extract.explicit_memory_request(text))

    def test_declined_memory_can_gate_automatic_extraction(self):
        for text in ("不要记住这件事，我明天只是试一试。", "先别保存这条理解。", "不用记下来，我只是随口说说。"):
            with self.subTest(text=text):
                self.assertTrue(extract.memory_request_declined(text))
        self.assertFalse(extract.memory_request_declined("不用保存这个文件，我只是想看看内容。"))
        self.assertTrue(extract.memory_request_declined("不用保存这个文件，也不要记住我刚才说的事。"))
        self.assertFalse(extract.memory_request_declined("请记住，我重视自主决定。"))


class MemoryAdmissionTests(unittest.TestCase):
    def test_executive_factual_premise_in_a_question_is_preserved(self):
        for text, quote in (
            ("我主要负责产品研发，你建议我怎样安排精力？", "我主要负责产品研发"),
            ("我一直很看重团队的自主权，你觉得这次该怎么办？", "我一直很看重团队的自主权"),
            ("我是总经理，如果下属不认同该怎么办？", "我是总经理"),
        ):
            section = "ways" if "自主权" in quote else "matters" if "研发" in quote else "who"
            candidate = claim(quote, section=section)
            with self.subTest(text=text):
                long_term, context = extract.admission([candidate], text)
                self.assertEqual(long_term, [candidate])
                self.assertFalse(context)

    def test_questions_and_hypothetical_positions_are_not_facts(self):
        for text, quote in (
            ("我是不是适合做总经理，你怎么看？", "做总经理"),
            ("如果我是总经理，你会怎么建议？", "我是总经理"),
            ("假如公司请我，我负责产品研发会不会更合适？", "我负责产品研发"),
            ("我是总经理吗？", "我是总经理"),
        ):
            with self.subTest(text=text):
                self.assertEqual(extract.admission([claim(quote)], text), ([], []))

    def test_recurring_weekend_routine_is_not_a_one_off_arrangement(self):
        for text in ("我每个周末都陪女儿运动", "我每周都和团队做一次复盘", "我每天留半小时独立思考"):
            candidate = claim(text, section="ways", predicate="tends_to")
            with self.subTest(text=text):
                self.assertEqual(extract.admission([candidate], text), ([candidate], []))
        for text in ("我本周末陪女儿运动", "明天我每小时都要开一次会", "我暂时每个周末都参加培训"):
            with self.subTest(text=text):
                long_term, context = extract.admission([claim(text, section="ways")], text)
                self.assertFalse(long_term)
                self.assertEqual(len(context), 1)

    def test_explicit_executive_role_values_and_preferences_do_not_require_always(self):
        for section, text in (("who", "我在一家制造企业任总经理"), ("matters", "我主要分管产品和研发"),
                              ("principles", "对我来说诚信最重要"), ("ways", "我更喜欢先听团队不同的意见"),
                              ("people", "我女儿叫小雨")):
            candidate = claim(text, section=section)
            with self.subTest(text=text):
                self.assertEqual(extract.admission([candidate], text), ([candidate], []))

    def test_short_substantive_answer_uses_explicit_question_not_generic_context(self):
        for question, text, section in (("你的岗位是什么？", "总经理", "who"),
            ("我该怎么称呼你？", "林远", "who"), ("你最在意的是谁？", "女儿", "people"),
            ("你有什么不愿退让的原则？", "诚信", "principles")):
            candidate = claim(text, section=section)
            with self.subTest(text=text):
                self.assertTrue(extract.should_extract(text, question)[0])
                self.assertEqual(extract.admission([candidate], text, prev_assistant=question), ([candidate], []))
                self.assertFalse(extract.should_extract(text)[0])
        for text in ("嗯", "好的", "不知道", "不确定", "跳过", "都行"):
            with self.subTest(text=text):
                self.assertFalse(extract.should_extract(text, "你的岗位是什么？")[0])
        self.assertFalse(extract.should_extract("总经理", "还有别的吗？")[0])
        long_term, context = extract.admission([claim("明天的主持人")], "明天的主持人", prev_assistant="你的岗位是什么？")
        self.assertFalse(long_term)
        self.assertEqual(len(context), 1)

    def test_new_guided_flow_never_forces_a_turn_number_into_a_fixed_section(self):
        request = extract.build_request("我的底线是不向第三方出售客户资料", "有哪些边界？", [], debug={"onboardingStep": 4})
        self.assertNotIn("本轮不要输出 claims", request.system)
        self.assertNotIn("只能使用 section=", request.system)

    def test_clear_identity_is_still_a_candidate(self):
        text = "我是产品经理"
        candidate = claim(text)
        long_term, context = extract.admission([candidate], text)
        self.assertEqual(long_term, [candidate])
        self.assertEqual(context, [])

    def test_confirmed_extraction_confidence_does_not_make_an_event_permanent(self):
        text = "明天我想去黑客松现场寻找优秀人才"
        candidate = claim(text, section="direction", predicate="wants_to", layer="aspirational",
                          why_it_matters="本次活动建议应围绕发现合适的合作人才。")
        long_term, context = extract.admission([candidate], text)
        self.assertEqual(long_term, [])
        self.assertEqual([c.scope for c in context], ["context_only"])
        self.assertEqual(candidate.scope, "long_term", "admission must not mutate existing values")

    def test_event_details_remain_context_not_stable_preferences(self):
        samples = (
            ("我想先探探现场有什么样的人，再决定要不要合作", "direction", "wants_to"),
            ("我想找一些背景好的年轻人", "direction", "wants_to"),
            ("我筛选人才时既看硬标签也看实际能力，但硬标签能帮我更快筛掉一部分人", "ways", "decides_by"),
            ("明天我打算专门看看作品完整的人，对比筛选误差", "direction", "wants_to"),
        )
        for text, section, predicate in samples:
            with self.subTest(text=text):
                long_term, context = extract.admission([claim(text, section=section, predicate=predicate,
                    why_it_matters="帮助比较本次招募活动的候选人筛选办法。")], text)
                self.assertEqual(long_term, [])
                self.assertEqual(len(context), 1)

    def test_explicit_principle_relationship_and_long_horizon_goal(self):
        samples = (
            claim("我的原则是不因短期利益牺牲诚实", section="principles", predicate="holds_principle"),
            claim("我的女儿是小雨", section="people", predicate="relationship"),
            claim("我希望三年后创办一所学校", section="direction", predicate="wants_to", layer="aspirational"),
            claim("我目前负责公司的产品研发", section="matters", predicate="working_on"),
        )
        for candidate in samples:
            with self.subTest(text=candidate.quote):
                long_term, context = extract.admission([candidate], candidate.quote)
                self.assertEqual(long_term, [candidate])
                self.assertEqual(context, [])

    def test_same_message_different_clause_does_not_make_identity_ephemeral(self):
        text = "我是产品经理，明天去参加活动"
        long_term, context = extract.admission([claim("我是产品经理")], text)
        self.assertEqual(len(long_term), 1)
        self.assertFalse(context)

    def test_time_qualifier_outside_short_quote_is_preserved(self):
        text = "明天我是活动负责人"
        for quote in ("我是活动负责人", "我是 活动负责人"):
            with self.subTest(quote=quote):
                long_term, context = extract.admission([claim(quote)], text)
                self.assertFalse(long_term)
                self.assertEqual(context[0].scope, "context_only")

    def test_missing_generic_and_repeated_reasons_are_rejected(self):
        for reason in ("", "很重要", "有助于了解用户", "以后有帮助", "我是产品经理"):
            with self.subTest(reason=reason):
                self.assertEqual(extract.admission([claim("我是产品经理", why_it_matters=reason)], "我是产品经理"), ([], []))

    def test_question_is_not_a_personal_fact_even_with_first_person(self):
        text = "我是不是更适合当产品经理？"
        candidate = claim("我是不是更适合当产品经理", content="我适合当产品经理")
        self.assertEqual(extract.admission([candidate], text), ([], []))

    def test_model_hypothesis_never_uses_single_turn_as_stable_evidence(self):
        candidate = claim("我昨天拒绝了一次请求", content="我一直擅长坚持边界", layer="hypothesis",
                          section="ways", predicate="tends_to", why_it_matters="帮助用户未来处理人际边界与额外请求。")
        self.assertEqual(extract.admission([candidate], candidate.quote), ([], []))

    def test_assisted_answer_not_a_stable_preference_even_when_edited(self):
        candidate = claim("我一直更看重自主决定", section="ways", predicate="prefers")
        origin = {"kind": "assisted", "batchId": "synthetic", "edited": True}
        long_term, context = extract.admission([candidate], candidate.quote, origin)
        self.assertFalse(long_term)
        self.assertEqual(context[0].scope, "context_only")

    def test_explicit_save_does_not_change_context_into_long_term(self):
        text = "请记住：明天我去活动现场看看"
        self.assertTrue(extract.explicit_memory_request(text))
        long_term, context = extract.admission([claim("明天我去活动现场看看", section="matters", predicate="happened")], text)
        self.assertFalse(long_term)
        self.assertEqual(context[0].scope, "context_only")

    def test_unknown_scope_is_not_invented_from_clear_phrase(self):
        candidate = claim("我是产品经理", scope="context_only")
        long_term, context = extract.admission([candidate], candidate.quote)
        self.assertFalse(long_term)
        self.assertEqual(context, [candidate])

    def test_limits_and_duplicate_content(self):
        candidates = [claim("我是产品经理"), claim("我叫小远")]
        candidates += [claim(f"明天我会看第{n}个作品", section="matters", predicate="happened") for n in range(4)]
        candidates.insert(3, replace(candidates[2]))
        text = "。".join(c.quote for c in candidates)
        long_term, context = extract.admission(candidates, text)
        self.assertEqual(len(long_term), 1)
        self.assertEqual(len(context), 2)
        self.assertEqual(len({c.content for c in context}), 2)

    def test_prompt_asks_for_sparse_explicit_evidence(self):
        req = extract.build_request("我是产品经理", None, [])
        self.assertIn("长期候选最多 1 条", req.system)
        self.assertIn("情境片段最多 2 条", req.system)
        self.assertIn("本任务不输出 hypothesis", req.system)
        self.assertIn("不是三个稳定偏好", req.system)


if __name__ == "__main__":
    unittest.main()
