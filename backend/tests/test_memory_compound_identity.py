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

    def assert_student_candidate(self, result, message):
        self.assertEqual(len(result["created"]), 1, result)
        student = self.onto.get_claim(result["created"][0])
        self.assertEqual(student["content"], STUDENT)
        self.assertEqual(student["trustState"], "working")
        self.assertEqual(student["scope"], "long_term")
        self.assertEqual(student["evidence"][0]["messageId"], message["id"])
        self.assertIn(student["evidence"][0]["quote"], message["content"])
        self.assertEqual(result["promoted"], [])
        self.assertEqual(result["reaffirmed"], [])
        self.assertEqual(memory.pending(self.onto, self.convs, self.cid)["total"], 1)

    def test_known_confirmed_programmer_does_not_swallow_new_student(self):
        existing = self.seed_programmer("confirmed")
        result, message = self.process(COMPOUND,
            raw_claim(PROGRAMMER, COMPOUND, .99), raw_claim(STUDENT, COMPOUND, .95))
        self.assert_student_candidate(result, message)
        self.assertEqual(self.onto.get_claim(existing["id"]), existing)

    def test_known_working_programmer_is_not_confirmed_by_repetition(self):
        existing = self.seed_programmer("working")
        result, message = self.process(COMPOUND,
            raw_claim(PROGRAMMER, "我不仅是一个程序员", .99),
            raw_claim(STUDENT, "还是一个大四的学生", .95))
        self.assert_student_candidate(result, message)
        self.assertEqual(self.onto.get_claim(existing["id"]), existing)
        self.assertEqual(self.onto.list_claims(trust_states=("confirmed",)), [])

    def test_duplicate_is_filtered_before_ordinary_one_candidate_budget(self):
        existing = self.seed_programmer("confirmed")
        text = "我是一名程序员。我是一名大四学生"
        result, message = self.process(text,
            raw_claim(PROGRAMMER, PROGRAMMER, .99), raw_claim(STUDENT, STUDENT, .95))
        self.assert_student_candidate(result, message)
        self.assertEqual(self.onto.get_claim(existing["id"]), existing)

    def test_two_new_identities_are_working_never_automatically_confirmed(self):
        result, message = self.process(COMPOUND,
            raw_claim(PROGRAMMER, "我不仅是一个程序员", .99),
            raw_claim(STUDENT, "还是一个大四的学生", .95))
        self.assertEqual(len(result["created"]), 2, result)
        values = self.onto.list_claims(trust_states=("working",))
        self.assertEqual({v["content"] for v in values}, {PROGRAMMER, STUDENT})
        self.assertEqual(self.onto.list_claims(trust_states=("confirmed",)), [])
        self.assertEqual(memory.pending(self.onto, self.convs, self.cid)["total"], 2)
        for value in values:
            self.assertEqual(value["evidence"][0]["messageId"], message["id"])

    def test_model_expansion_and_role_measure_words_do_not_duplicate_student(self):
        existing = self.onto.create_claim({"subject_entity_id": "ent_me", "section": "who",
            "layer": "self_declared", "predicate": "role", "content": STUDENT,
            "confidence": .95, "scope": "long_term"},
            [{"kind": "user_edit", "quote": STUDENT}], trust_state="working", trust_origin="model")
        result, _ = self.process(COMPOUND,
            raw_claim("我是大四学生，正处在毕业阶段", "还是一个大四的学生"))
        self.assertEqual(result["created"], [])
        self.assertEqual(result["filterReasons"]["existing"], 1)
        self.assertEqual(self.onto.get_claim(existing["id"]), existing)

    def test_wrong_model_merge_target_cannot_swallow_different_identity(self):
        existing = self.seed_programmer("confirmed")
        result, message = self.process(COMPOUND,
            raw_claim(STUDENT, COMPOUND, merge_into=existing["id"]),
            existing_ids={existing["id"]})
        self.assert_student_candidate(result, message)
        self.assertEqual(self.onto.get_claim(existing["id"]), existing)

    def test_other_device_scope_does_not_suppress_this_device_identity(self):
        existing = self.onto.create_claim({"subject_entity_id": "ent_me", "section": "who",
            "layer": "self_declared", "predicate": "role", "content": STUDENT,
            "confidence": .99, "scope": "long_term", "device_scope": "synthetic-other-device"},
            [{"kind": "user_edit", "quote": STUDENT}], trust_state="confirmed", trust_origin="user_created")
        result, message = self.process(STUDENT,
            raw_claim(STUDENT, STUDENT, merge_into=existing["id"]), existing_ids={existing["id"]})
        self.assert_student_candidate(result, message)
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
        self.assert_student_candidate(result, message)
        self.assertEqual(self.onto.get_claim(existing["id"]), existing)

    def test_same_content_for_other_subject_cannot_swallow_self_identity(self):
        person = self.onto.upsert_entity("合成同学", "person")
        existing = self.onto.create_claim({"subject_entity_id": person["id"], "section": "who",
            "layer": "self_declared", "predicate": "role", "content": STUDENT,
            "confidence": .99, "scope": "long_term"},
            [{"kind": "user_edit", "quote": STUDENT}], trust_state="confirmed", trust_origin="user_created")
        result, message = self.process(STUDENT,
            raw_claim(STUDENT, STUDENT, merge_into=existing["id"]), existing_ids={existing["id"]})
        self.assert_student_candidate(result, message)
        self.assertEqual(self.onto.get_claim(existing["id"]), existing)


if __name__ == "__main__":
    unittest.main()
