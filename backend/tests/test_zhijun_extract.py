"""知君抽取器：入口校验、去重与晋升、墓碑抑制、跳过规则、演示模型的规则抽取。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mindos.stores.ontology_store import ME_ENTITY_ID, OntologyStore
from mindos.zhijun import extract
from mindos.zhijun.provider import FakeProvider, fake_extract


def _raw(**overrides) -> dict:
    item = {
        "section": "matters",
        "layer": "self_declared",
        "predicate": "working_on",
        "subject": "me",
        "object": None,
        "content": "我在做远川项目",
        "quote": "我在做远川项目",
        "confidence": 0.9,
        "scope_hint": "long_term",
        "privacy_hint": "private",
    }
    item.update(overrides)
    return item


USER_TEXT = "我在做远川项目，压力很大。我想明年把公司做到盈利。"


class ValidateTests(unittest.TestCase):
    def test_quote_must_be_exact_substring(self) -> None:
        valid = extract.validate({"claims": [_raw(quote="我在做远川的项目")]}, user_text=USER_TEXT, prev_assistant=None)
        self.assertEqual(valid, [])
        valid = extract.validate({"claims": [_raw(quote="我在做远川项目")]}, user_text=USER_TEXT, prev_assistant=None)
        self.assertEqual(len(valid), 1)

    def test_observed_from_conversation_is_dropped(self) -> None:
        self.assertEqual(extract.validate({"claims": [_raw(layer="observed")]}, user_text=USER_TEXT, prev_assistant=None), [])

    def test_self_declared_without_first_person_becomes_hypothesis(self) -> None:
        text = "远川项目压力很大"
        valid = extract.validate({"claims": [_raw(content="远川项目压力很大", quote=text)]}, user_text=text, prev_assistant=None)
        self.assertEqual(valid[0].layer, "hypothesis")
        self.assertTrue(valid[0].downgraded)
        valid = extract.validate({"claims": [_raw(content="远川项目压力很大", quote=text)]}, user_text=text, prev_assistant="你最近在忙什么？")
        self.assertEqual(valid[0].layer, "self_declared")

    def test_predicate_fallback_and_cap(self) -> None:
        items = [_raw(content=f"我在做第{i}件事", quote="我在做远川项目", confidence=0.5 + i * 0.05, predicate="bogus") for i in range(6)]
        valid = extract.validate({"claims": items}, user_text=USER_TEXT, prev_assistant=None)
        self.assertEqual(len(valid), extract.MAX_CLAIMS_PER_TURN)
        self.assertEqual(valid[0].predicate, "working_on")
        self.assertTrue(all(c.downgraded for c in valid))
        self.assertGreaterEqual(valid[0].confidence, valid[-1].confidence)

    def test_aspirational_requires_wish_words(self) -> None:
        valid = extract.validate(
            {"claims": [_raw(section="direction", layer="aspirational", predicate="wants_to", content="我想明年盈利", quote="我想明年把公司做到盈利")]},
            user_text=USER_TEXT,
            prev_assistant=None,
        )
        self.assertEqual(valid[0].layer, "aspirational")
        valid = extract.validate(
            {"claims": [_raw(section="direction", layer="aspirational", predicate="wants_to", content="x", quote="我在做远川项目")]},
            user_text=USER_TEXT,
            prev_assistant=None,
        )
        self.assertEqual(valid[0].layer, "self_declared")

    def test_should_extract_rules(self) -> None:
        self.assertEqual(extract.should_extract("好的"), (False, "too_short"))
        self.assertEqual(extract.should_extract("远川项目下一步怎么办？"), (False, "pure_question"))
        self.assertEqual(extract.should_extract("我该怎么办？"), (True, "ok"))
        self.assertEqual(extract.should_extract(USER_TEXT), (True, "ok"))

    def test_fake_extract_heuristics(self) -> None:
        raw = fake_extract(USER_TEXT)
        sections = {c["section"]: c for c in raw["claims"]}
        self.assertIn("matters", sections)
        self.assertIn("direction", sections)
        self.assertEqual(sections["direction"]["layer"], "aspirational")
        for claim in raw["claims"]:
            self.assertIn(claim["quote"], USER_TEXT)
        self.assertEqual(fake_extract("远川项目下一步怎么办")["claims"], [])


class PersistTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OntologyStore(Path(self._tmp.name) / "ontology.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, text: str, message_id: str, prev: str | None = None) -> dict:
        return extract.run_extraction(
            provider=FakeProvider(),
            store=self.store,
            conversation_id="conv_1",
            message_id=message_id,
            user_text=text,
            prev_assistant=prev,
        )

    def test_utterance_is_confirmed_and_aspiration_stays_working(self) -> None:
        result = self._run(USER_TEXT, "msg_1")
        self.assertEqual(result["state"], "done")
        self.assertEqual(len(result["created"]), 2)
        claims = {c["content"]: c for c in self.store.list_claims(trust_states=("working", "confirmed"))}
        matters = claims["我在做远川项目，压力很大"] if "我在做远川项目，压力很大" in claims else claims["我在做远川项目"]
        self.assertEqual(matters["trustState"], "confirmed")
        self.assertEqual(matters["trustOrigin"], "utterance")
        self.assertEqual(matters["evidence"][0]["messageId"], "msg_1")
        direction = next(c for c in claims.values() if c["section"] == "direction")
        self.assertEqual(direction["trustState"], "working")
        self.assertEqual(direction["layer"], "aspirational")
        self.assertEqual(len(self.store.inbox()), 1)

    def test_restating_adds_evidence_and_promotes_working(self) -> None:
        working = self.store.create_claim(
            {"content": "我在做远川项目", "section": "matters", "layer": "self_declared", "confidence": 0.6},
            [{"kind": "conversation_turn", "conversation_id": "conv_0", "message_id": "msg_0", "quote": "x"}],
            trust_state="working",
            trust_origin="model",
        )
        result = self._run("我在做远川项目", "msg_2")
        self.assertIn(working["id"], result["reaffirmed"])
        self.assertIn(working["id"], result["promoted"])
        refreshed = self.store.get_claim(working["id"])
        self.assertEqual(refreshed["trustState"], "confirmed")
        self.assertEqual(len(refreshed["evidence"]), 2)

    def test_tombstone_suppresses_model_candidates_but_not_user_restatement(self) -> None:
        text = "我想明年把公司做到盈利"
        first = self._run(text, "msg_3")
        claim_id = first["created"][0]
        self.store.transition(claim_id, "reject", surface="conversation")
        second = self._run(text, "msg_4")
        self.assertEqual(second["suppressed"], 1)
        self.assertEqual(second["created"], [])
        restated = self._run("我在做远川项目", "msg_5")
        self.assertEqual(len(restated["created"]), 1)
        self.store.transition(restated["created"][0], "retract", surface="ontology_page")
        third = self._run("我在做远川项目", "msg_6")
        self.assertEqual(len(third["created"]), 1)
        self.assertEqual(self.store.get_claim(third["created"][0])["supersedesId"], restated["created"][0])

    def test_skipped_inputs_do_not_call_model(self) -> None:
        result = self._run("好的", "msg_7")
        self.assertEqual(result["state"], "skipped")
        self.assertEqual(self.store.stats()["claims"], {"working": 0, "confirmed": 0, "retracted": 0, "superseded": 0})

    def test_entities_from_extraction_are_upserted(self) -> None:
        valid = extract.validate(
            {"claims": [_raw(section="people", predicate="works_with", subject="me", object="林岚", content="我和林岚一起做远川项目", quote="我在做远川项目")]},
            user_text=USER_TEXT,
            prev_assistant=None,
        )
        summary = extract.persist(valid, [{"name": "林岚", "type": "person", "aliases": ["岚姐"]}], store=self.store, conversation_id="conv_1", message_id="msg_8")
        self.assertEqual(len(summary["created"]), 1)
        claim = self.store.get_claim(summary["created"][0])
        self.assertEqual(claim["objectName"], "林岚")
        self.assertEqual(claim["subjectEntityId"], ME_ENTITY_ID)
        self.assertEqual(self.store.entity_names_for_conversation("conv_1"), ["林岚"])


if __name__ == "__main__":
    unittest.main()
