"""Compound self-identities retain exact evidence and remain reviewable candidates.

All stores and model-shaped payloads are synthetic; no model or live box is used.
"""
import unittest

from tests import test_task_routing as harness
from mindos.zhijun import extract, memory


COMPOUND = "我不仅是一个程序员，还是一个大四的学生"
PROGRAMMER = "我是一名程序员"
STUDENT = "我是一名大四学生"


def raw_claim(content, quote, confidence=.95, **overrides):
    value = {"section": "who", "layer": "self_declared", "predicate": "role",
        "subject": "me", "object": None, "content": content, "quote": quote,
        "confidence": confidence, "scope_hint": "long_term", "privacy_hint": "private",
        "merge_into": None, "why_it_matters": "职业和学习安排需要同时考虑毕业阶段与开发经验。"}
    value.update(overrides)
    return value


def validated(text, *items):
    return extract.validate({"claims": list(items)}, user_text=text, prev_assistant=None)


class CompoundIdentityValidationTests(unittest.TestCase):
    def test_model_added_graduation_clause_is_not_a_new_identity(self):
        values = validated(COMPOUND, raw_claim("我是大四学生，正处在毕业阶段", "还是一个大四的学生"))
        self.assertEqual(values[0].content, "我是一个大四的学生")
        self.assertEqual(values[0].quote, "还是一个大四的学生")

    def test_complete_original_quote_admits_student(self):
        values = validated(COMPOUND, raw_claim(STUDENT, COMPOUND))
        self.assertEqual(len(values), 1)
        durable, contextual = extract.admission(values, COMPOUND)
        self.assertEqual([v.content for v in durable], [STUDENT])
        self.assertEqual(contextual, [])
        self.assertEqual(durable[0].quote, COMPOUND)

    def test_second_clause_inherits_subject_without_rewriting_quote(self):
        quote = "还是一个大四的学生"
        values = validated(COMPOUND, raw_claim(STUDENT, quote))
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].layer, "self_declared")
        self.assertFalse(values[0].downgraded)
        durable, contextual = extract.admission(values, COMPOUND)
        self.assertEqual([v.content for v in durable], [STUDENT])
        self.assertEqual(contextual, [])
        self.assertEqual(durable[0].quote, quote)

    def test_two_explicit_identities_are_not_reduced_to_one_before_dedup(self):
        values = validated(COMPOUND,
            raw_claim(PROGRAMMER, "我不仅是一个程序员", .99),
            raw_claim(STUDENT, "还是一个大四的学生", .95))
        durable, contextual = extract.admission(values, COMPOUND)
        self.assertEqual({v.content for v in durable}, {PROGRAMMER, STUDENT})
        self.assertEqual(contextual, [])

    def test_supported_conjunctions_preserve_second_identity(self):
        for text, quote in (
            ("我不但是一个程序员，也是一名大四学生", "也是一名大四学生"),
            ("我既是一个程序员，又是一名大四学生", "又是一名大四学生"),
            ("我不仅是一个程序员，我还是一名大四学生", "我还是一名大四学生"),
        ):
            with self.subTest(text=text):
                durable, _ = extract.admission(validated(text, raw_claim(STUDENT, quote)), text)
                self.assertEqual([v.content for v in durable], [STUDENT])

    def test_reconstructed_quote_is_still_rejected(self):
        for quote in ("我是一个大四的学生", "我是一个程序员", "我还是一个大四的学生"):
            with self.subTest(quote=quote):
                self.assertEqual(validated(COMPOUND, raw_claim(STUDENT, quote)), [])

    def test_hypothetical_negative_third_person_and_quoted_identity_are_not_self_facts(self):
        for text, quote in (
            ("如果我不仅是一个程序员，还是一个大四的学生，会怎样？", "还是一个大四的学生"),
            ("假设我不仅是一个程序员，还是一个大四的学生", "还是一个大四的学生"),
            ("我不仅不是一个程序员，也不是一个大四的学生", "也不是一个大四的学生"),
            ("他不仅是一个程序员，还是一个大四的学生", "还是一个大四的学生"),
            ("我朋友不仅是一个程序员，还是一个大四的学生", "还是一个大四的学生"),
            ("他说：我不仅是一个程序员，还是一个大四的学生", "还是一个大四的学生"),
            ("请分析这句话：“我不仅是一个程序员，还是一个大四的学生”", COMPOUND),
            ("这只是示例：我不仅是一个程序员，还是一个大四的学生", COMPOUND),
        ):
            with self.subTest(text=text):
                durable, _ = extract.admission(validated(text, raw_claim(STUDENT, quote)), text)
                self.assertEqual(durable, [])

    def test_hypothesis_layer_cannot_be_promoted_by_compound_subject(self):
        values = validated(COMPOUND, raw_claim(STUDENT, COMPOUND, layer="hypothesis"))
        self.assertEqual(extract.admission(values, COMPOUND), ([], []))

    def test_unrelated_quote_cannot_inherit_compound_subject(self):
        text = COMPOUND + "。他是一名医生"
        values = validated(text, raw_claim("我是一名医生", "他是一名医生"))
        self.assertEqual(extract.admission(values, text), ([], []))

    def test_validation_diagnostics_contain_counts_not_user_or_model_text(self):
        diagnostics = {}
        values = extract.validate({"claims": [
            raw_claim(STUDENT, COMPOUND),
            raw_claim("不应被记录的模型正文", "不在用户原话中的私密文本"),
            raw_claim("", COMPOUND),
            raw_claim(STUDENT, COMPOUND, section="not_a_section"),
            raw_claim(STUDENT, COMPOUND, layer="observed"), None,
        ]}, user_text=COMPOUND, prev_assistant=None, diagnostics=diagnostics)
        self.assertEqual(len(values), 1)
        self.assertEqual(diagnostics, {"modelCandidates": 6, "invalidQuote": 1,
            "missingContent": 1, "invalidClassification": 1, "observedNotAllowed": 1,
            "invalidItem": 1})
        self.assertTrue(all(type(value) is int for value in diagnostics.values()))


class CompoundIdentityPersistenceTests(unittest.TestCase):
    setUp = harness.RoutingTests.setUp
    tearDown = harness.RoutingTests.tearDown

    def process(self, text, *items, existing_ids=None):
        message = self.convs.append_message(self.cid, "user", text, meta={"routingSources": []})
        values = extract.validate({"claims": list(items)}, user_text=text,
            prev_assistant=None, existing_ids=existing_ids)
        result = memory.process_candidates(values, [], store=self.onto,
            conversation_id=self.cid, message_id=message["id"], user_text=text, routing_sources=[])
        return result, message

    def seed_programmer(self, trust_state):
        return self.onto.create_claim({"subject_entity_id": "ent_me", "section": "who",
            "layer": "self_declared", "predicate": "role", "content": PROGRAMMER,
            "confidence": .99, "scope": "long_term"},
            [{"kind": "user_edit", "quote": PROGRAMMER}], trust_state=trust_state,
            trust_origin="user_created" if trust_state == "confirmed" else "model")

    def assert_student_candidate(self, result, message, *, reaffirmed=(), promoted=()):
        # 「还是一个大四的学生」这段原话没有第一人称，不满足直接记住的守卫（V3 拍板 4），仍是待确认候选。
        self.assertEqual(len(result["created"]), 1, result)
        student = self.onto.get_claim(result["created"][0])
        self.assertEqual(student["content"], STUDENT)
        self.assertEqual((student["trustState"], student["trustOrigin"]), ("working", "model"))
        self.assertEqual(student["scope"], "long_term")
        self.assertEqual(student["evidence"][0]["messageId"], message["id"])
        self.assertIn(student["evidence"][0]["quote"], message["content"])
        self.assertEqual(result["promoted"], list(promoted))
        self.assertEqual(result["reaffirmed"], list(reaffirmed))
        self.assertEqual(result["autoConfirmed"], list(promoted))
        self.assertEqual(memory.pending(self.onto, self.convs, self.cid)["total"], 1)

    def assert_student_confirmed(self, result, message, *, reaffirmed=()):
        # 原话本身是第一人称、精确引用、高置信的自述 → 直接记住（trustOrigin=utterance，可撤回）。
        self.assertEqual(len(result["created"]), 1, result)
        student = self.onto.get_claim(result["created"][0])
        self.assertEqual(student["content"], STUDENT)
        self.assertEqual((student["trustState"], student["trustOrigin"]), ("confirmed", "utterance"))
        self.assertEqual(student["evidence"][0]["messageId"], message["id"])
        self.assertIn(student["evidence"][0]["quote"], message["content"])
        self.assertEqual(result["autoConfirmed"], result["created"])
        self.assertEqual(result["promoted"], [])
        self.assertEqual(result["reaffirmed"], list(reaffirmed))
        self.assertEqual(memory.pending(self.onto, self.convs, self.cid)["total"], 0)

    def assert_reaffirmed(self, existing, message, *, trust_state):
        refreshed = self.onto.get_claim(existing["id"])
        self.assertEqual(refreshed["trustState"], trust_state)
        self.assertEqual(len(refreshed["evidence"]), len(existing["evidence"]) + 1)
        self.assertEqual(refreshed["evidence"][-1]["messageId"], message["id"])
        self.assertGreaterEqual(refreshed["lastReaffirmed"], existing["lastReaffirmed"])
        self.assertEqual({k: v for k, v in refreshed.items() if k not in ("evidence", "lastReaffirmed", "updatedAt", "trustState", "trustOrigin", "selfAlignment")},
                         {k: v for k, v in existing.items() if k not in ("evidence", "lastReaffirmed", "updatedAt", "trustState", "trustOrigin", "selfAlignment")})

    def test_known_confirmed_programmer_does_not_swallow_new_student(self):
        existing = self.seed_programmer("confirmed")
        result, message = self.process(COMPOUND,
            raw_claim(PROGRAMMER, COMPOUND, .99), raw_claim(STUDENT, COMPOUND, .95))
        # 整句原话是第一人称的精确引用 → 学生身份直接记住；已确认的程序员身份只追加证据并重申，不改写记录。
        self.assert_student_confirmed(result, message, reaffirmed=[existing["id"]])
        self.assert_reaffirmed(existing, message, trust_state="confirmed")

    def test_known_working_programmer_is_confirmed_by_own_restatement(self):
        existing = self.seed_programmer("working")
        result, message = self.process(COMPOUND,
            raw_claim(PROGRAMMER, "我不仅是一个程序员", .99),
            raw_claim(STUDENT, "还是一个大四的学生", .95))
        # 待确认的程序员身份被本人再次亲口说到（第一人称片段）→ 确认；学生片段没有第一人称 → 仍待确认。
        self.assert_student_candidate(result, message, reaffirmed=[existing["id"]], promoted=[existing["id"]])
        self.assert_reaffirmed(existing, message, trust_state="confirmed")
        self.assertEqual([c["id"] for c in self.onto.list_claims(trust_states=("confirmed",))], [existing["id"]])

    def test_duplicate_is_filtered_before_ordinary_one_candidate_budget(self):
        existing = self.seed_programmer("confirmed")
        text = "我是一名程序员。我是一名大四学生"
        result, message = self.process(text,
            raw_claim(PROGRAMMER, PROGRAMMER, .99), raw_claim(STUDENT, STUDENT, .95))
        self.assert_student_confirmed(result, message, reaffirmed=[existing["id"]])
        self.assert_reaffirmed(existing, message, trust_state="confirmed")

    def test_compound_identity_confirms_only_the_first_person_clause(self):
        result, message = self.process(COMPOUND,
            raw_claim(PROGRAMMER, "我不仅是一个程序员", .99),
            raw_claim(STUDENT, "还是一个大四的学生", .95))
        self.assertEqual(len(result["created"]), 2, result)
        by_content = {c["content"]: c for c in self.onto.list_claims(trust_states=("working", "confirmed"))}
        self.assertEqual((by_content[PROGRAMMER]["trustState"], by_content[PROGRAMMER]["trustOrigin"]), ("confirmed", "utterance"))
        self.assertEqual((by_content[STUDENT]["trustState"], by_content[STUDENT]["trustOrigin"]), ("working", "model"))
        self.assertEqual(result["autoConfirmed"], [by_content[PROGRAMMER]["id"]])
        self.assertEqual(memory.pending(self.onto, self.convs, self.cid)["total"], 1)
        for value in by_content.values():
            self.assertEqual(value["evidence"][0]["messageId"], message["id"])

    def test_model_expansion_and_role_measure_words_do_not_duplicate_student(self):
        existing = self.onto.create_claim({"subject_entity_id": "ent_me", "section": "who",
            "layer": "self_declared", "predicate": "role", "content": STUDENT,
            "confidence": .95, "scope": "long_term"},
            [{"kind": "user_edit", "quote": STUDENT}], trust_state="working", trust_origin="model")
        result, message = self.process(COMPOUND,
            raw_claim("我是大四学生，正处在毕业阶段", "还是一个大四的学生"))
        self.assertEqual(result["created"], [])
        self.assertEqual(result["filterReasons"]["existing"], 1)
        # 重复命中：追加本条消息为证据并重申；片段没有第一人称，待确认理解不因此确认。
        self.assertEqual((result["reaffirmed"], result["promoted"]), ([existing["id"]], []))
        self.assert_reaffirmed(existing, message, trust_state="working")

    def test_wrong_model_merge_target_cannot_swallow_different_identity(self):
        existing = self.seed_programmer("confirmed")
        result, message = self.process(COMPOUND,
            raw_claim(STUDENT, COMPOUND, merge_into=existing["id"]),
            existing_ids={existing["id"]})
        self.assert_student_confirmed(result, message)
        self.assertEqual(self.onto.get_claim(existing["id"]), existing)

    def test_other_device_scope_does_not_suppress_this_device_identity(self):
        existing = self.onto.create_claim({"subject_entity_id": "ent_me", "section": "who",
            "layer": "self_declared", "predicate": "role", "content": STUDENT,
            "confidence": .99, "scope": "long_term", "device_scope": "synthetic-other-device"},
            [{"kind": "user_edit", "quote": STUDENT}], trust_state="confirmed", trust_origin="user_created")
        result, message = self.process(STUDENT,
            raw_claim(STUDENT, STUDENT, merge_into=existing["id"]), existing_ids={existing["id"]})
        self.assert_student_confirmed(result, message)
        self.assertEqual(self.onto.get_claim(existing["id"]), existing)

    def test_invisible_source_cannot_consume_candidate_budget(self):
        foreign = self.convs.create_conversation(device_scope="synthetic-other-device")
        source = self.convs.append_message(foreign["id"], "user", STUDENT)
        # Legacy global claim with an out-of-scope source; background and role
        # intentionally differ so storage hash uniqueness cannot mask visibility.
        existing = self.onto.create_claim({"subject_entity_id": "ent_me", "section": "who",
            "layer": "self_declared", "predicate": "background", "content": STUDENT,
            "confidence": .99, "scope": "long_term"},
            [{"kind": "conversation_turn", "conversation_id": foreign["id"],
              "message_id": source["id"], "quote": STUDENT}],
            trust_state="confirmed", trust_origin="user_created")
        result, message = self.process(STUDENT,
            raw_claim(STUDENT, STUDENT, merge_into=existing["id"]), existing_ids={existing["id"]})
        self.assert_student_confirmed(result, message)
        self.assertEqual(self.onto.get_claim(existing["id"]), existing)

    def test_same_content_for_other_subject_cannot_swallow_self_identity(self):
        person = self.onto.upsert_entity("合成同学", "person")
        existing = self.onto.create_claim({"subject_entity_id": person["id"], "section": "who",
            "layer": "self_declared", "predicate": "role", "content": STUDENT,
            "confidence": .99, "scope": "long_term"},
            [{"kind": "user_edit", "quote": STUDENT}], trust_state="confirmed", trust_origin="user_created")
        result, message = self.process(STUDENT,
            raw_claim(STUDENT, STUDENT, merge_into=existing["id"]), existing_ids={existing["id"]})
        self.assert_student_confirmed(result, message)
        self.assertEqual(self.onto.get_claim(existing["id"]), existing)


if __name__ == "__main__":
    unittest.main()
