"""知君本体存储：状态机、活跃唯一、墓碑、检索、inbox、实体、后台任务。"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from mindos.stores.ontology_store import (
    ME_ENTITY_ID,
    OntologyConflictError,
    OntologyError,
    OntologyNotFoundError,
    OntologyStore,
    content_hash,
    tokenize,
)
from runtime_paths import DB_ROOT, ONTOLOGY_DB_PATH


def _claim(content: str = "我在做远川项目", section: str = "matters", layer: str = "self_declared", **extra) -> dict:
    payload = {"content": content, "section": section, "layer": layer, "confidence": 0.9}
    payload.update(extra)
    return payload


def _evidence(quote: str = "我在做远川项目") -> list[dict]:
    return [{"kind": "conversation_turn", "conversation_id": "conv_1", "message_id": "msg_1", "quote": quote}]


class OntologyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OntologyStore(Path(self._tmp.name) / "ontology.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_path_is_under_runtime_db_root(self) -> None:
        self.assertEqual(ONTOLOGY_DB_PATH.parent, DB_ROOT)

    def test_me_entity_exists_after_init(self) -> None:
        me = self.store.get_entity(ME_ENTITY_ID)
        self.assertIsNotNone(me)
        self.assertEqual(me["type"], "me")

    def test_create_claim_defaults_and_evidence(self) -> None:
        claim = self.store.create_claim(_claim(), _evidence(), trust_state="working", trust_origin="model")
        self.assertEqual(claim["trustState"], "working")
        self.assertEqual(claim["subjectEntityId"], ME_ENTITY_ID)
        self.assertEqual(claim["predicate"], "working_on")
        self.assertEqual(len(claim["evidence"]), 1)
        self.assertEqual(claim["evidence"][0]["quote"], "我在做远川项目")
        self.assertEqual(self.store.stats()["claims"]["working"], 1)

    def test_active_hash_is_unique_but_tombstones_do_not_block(self) -> None:
        first = self.store.create_claim(_claim(), _evidence(), trust_state="working", trust_origin="model")
        with self.assertRaises(OntologyConflictError):
            self.store.create_claim(_claim(), _evidence(), trust_state="working", trust_origin="model")
        self.store.transition(first["id"], "reject", surface="conversation")
        self.assertIsNone(self.store.find_active_by_hash(ME_ENTITY_ID, "working_on", "我在做远川项目"))
        tombstone = self.store.find_tombstone_by_hash(ME_ENTITY_ID, "working_on", "我在做远川项目")
        self.assertEqual(tombstone["id"], first["id"])
        again = self.store.create_claim(
            _claim(), _evidence(), trust_state="confirmed", trust_origin="utterance", supersedes_id=first["id"]
        )
        self.assertEqual(again["supersedesId"], first["id"])

    def test_hypothesis_cannot_be_written_as_confirmed(self) -> None:
        with self.assertRaises(OntologyError):
            self.store.create_claim(_claim(layer="hypothesis"), _evidence(), trust_state="confirmed", trust_origin="utterance")

    def test_invalid_section_or_predicate_rejected(self) -> None:
        with self.assertRaises(OntologyError):
            self.store.create_claim(_claim(section="nope"), _evidence())
        with self.assertRaises(OntologyError):
            self.store.create_claim(_claim(predicate="knows"), _evidence())

    def test_state_machine_transitions(self) -> None:
        working = self.store.create_claim(_claim("我喜欢早起", "ways"), _evidence("我喜欢早起"))
        confirmed = self.store.transition(working["id"], "confirm", surface="conversation")["claim"]
        self.assertEqual(confirmed["trustState"], "confirmed")
        self.assertEqual(confirmed["trustOrigin"], "user_confirm")
        with self.assertRaises(OntologyConflictError):
            self.store.transition(working["id"], "confirm", surface="conversation")
        reaffirmed = self.store.transition(working["id"], "reaffirm", surface="ontology_page")["claim"]
        self.assertGreaterEqual(reaffirmed["lastReaffirmed"], confirmed["lastReaffirmed"])
        retracted = self.store.transition(working["id"], "retract", surface="ontology_page")["claim"]
        self.assertEqual(retracted["trustState"], "retracted")
        self.assertEqual(retracted["retractionReason"], "user_retracted")
        with self.assertRaises(OntologyConflictError):
            self.store.transition(working["id"], "reaffirm", surface="ontology_page")

        ctx = self.store.create_claim(_claim("这次我先不扩张", "principles"), _evidence("这次我先不扩张"))
        ctx = self.store.transition(ctx["id"], "context_only", surface="conversation", conversation_id="conv_9")["claim"]
        self.assertEqual(ctx["trustState"], "confirmed")
        self.assertEqual(ctx["scope"], "context_only")
        self.assertEqual(ctx["contextRef"], "conv_9")

        deferred = self.store.create_claim(_claim("我可能偏内向", "who", "hypothesis"), _evidence("我可能偏内向"))
        deferred = self.store.transition(deferred["id"], "defer", surface="conversation")["claim"]
        self.assertEqual(deferred["trustState"], "working")
        self.assertIsNotNone(deferred["deferredUntil"])
        self.assertNotIn(deferred["id"], [c["id"] for c in self.store.inbox()])

        with self.assertRaises(OntologyConflictError):
            self.store.transition(deferred["id"], "retract", surface="conversation")
        with self.assertRaises(OntologyNotFoundError):
            self.store.transition("clm_missing", "confirm", surface="conversation")

    def test_partial_creates_replacement_and_supersedes_old(self) -> None:
        old = self.store.create_claim(_claim(), _evidence())
        result = self.store.transition(
            old["id"], "partial", surface="conversation", edited_content="我在带远川项目的产品线", conversation_id="conv_1"
        )
        self.assertEqual(result["claim"]["trustState"], "superseded")
        new = result["replacedBy"]
        self.assertEqual(new["trustState"], "confirmed")
        self.assertEqual(new["trustOrigin"], "user_edit")
        self.assertEqual(new["supersedesId"], old["id"])
        self.assertEqual(result["claim"]["supersededById"], new["id"])
        kinds = {ev["kind"] for ev in new["evidence"]}
        self.assertEqual(kinds, {"conversation_turn", "user_edit"})
        with self.assertRaises(OntologyError):
            self.store.transition(new["id"], "partial", surface="conversation", edited_content="我在带远川项目的产品线")

    def test_review_events_recorded(self) -> None:
        claim = self.store.create_claim(_claim(), _evidence())
        self.store.transition(claim["id"], "confirm", surface="conversation", conversation_id="conv_1", note="ok")
        events = self.store.review_events(claim["id"])
        self.assertEqual([e["action"] for e in events], ["confirm", "create"])
        self.assertEqual(events[0]["surface"], "conversation")
        self.assertEqual(events[0]["before"]["trustState"], "working")
        self.assertEqual(events[0]["after"]["trustState"], "confirmed")

    def test_search_prefers_lexical_overlap_and_respects_trust_states(self) -> None:
        a = self.store.create_claim(_claim("我在做远川项目"), _evidence(), trust_state="confirmed", trust_origin="utterance")
        b = self.store.create_claim(_claim("我喜欢早起跑步", "ways"), _evidence("我喜欢早起跑步"), trust_state="confirmed", trust_origin="utterance")
        hits = self.store.search_claims("远川项目最近怎么样", k=5, trust_states=("confirmed",))
        self.assertEqual(hits[0]["id"], a["id"])
        self.assertTrue(all("score" in h for h in hits))
        self.store.transition(a["id"], "retract", surface="ontology_page")
        self.assertEqual([h["id"] for h in self.store.search_claims("远川项目", k=5, trust_states=("confirmed",))], [b["id"]])
        tomb = self.store.search_claims("远川项目", k=5, trust_states=("retracted", "superseded"), include_hidden=True, min_score=0.35)
        self.assertEqual([h["id"] for h in tomb], [a["id"]])
        self.assertEqual(self.store.search_claims("完全无关的话题", k=5, trust_states=("retracted",), min_score=0.35), [])

    def test_tokenize_handles_cjk_and_ascii(self) -> None:
        tokens = tokenize("我在做远川项目 project-X")
        self.assertIn("project", tokens)
        self.assertTrue(any("远川" in t for t in tokens))
        self.assertEqual(content_hash("ent_me", "is", "我是产品负责人"), content_hash("ent_me", "is", "我 是 产品负责人。"))

    def test_similar_active_detects_near_duplicates(self) -> None:
        self.store.create_claim(_claim("我在做远川项目"), _evidence())
        self.assertIsNotNone(self.store.find_similar_active("我在做远川项目。", threshold=0.9))
        self.assertIsNone(self.store.find_similar_active("我今天心情不错", threshold=0.9))

    def test_entities_upsert_alias_and_lookup(self) -> None:
        lin = self.store.upsert_entity("林岚", "person", aliases=["岚姐"])
        again = self.store.upsert_entity("岚姐", "person")
        self.assertEqual(lin["id"], again["id"])
        self.assertIn("岚姐", again["aliases"])
        self.assertEqual(self.store.find_entity("林岚")["id"], lin["id"])
        me = self.store.upsert_entity("我", "person")
        self.assertEqual(me["id"], ME_ENTITY_ID)
        claim = self.store.create_claim(
            _claim("我和林岚一起做远川项目", "people", predicate="works_with", object_entity_id=lin["id"]),
            [{"kind": "conversation_turn", "conversation_id": "conv_x", "message_id": "m", "quote": "我和林岚一起做远川项目"}],
        )
        self.assertEqual(claim["objectName"], "林岚")
        self.assertEqual(self.store.entity_names_for_conversation("conv_x"), ["林岚"])
        self.assertEqual(self.store.entity_names_for_conversation("conv_other"), [])
        listed = {e["canonicalName"]: e for e in self.store.list_entities()}
        self.assertEqual(listed["林岚"]["claimCount"], 1)
        with self.assertRaises(OntologyNotFoundError):
            self.store.create_claim(_claim("x", "people", object_entity_id="ent_missing"), [])

    def test_inbox_orders_newest_first_and_hides_challenged(self) -> None:
        first = self.store.create_claim(_claim("我可能偏内向", "who", "hypothesis"), _evidence("x"))
        time.sleep(0.01)
        second = self.store.create_claim(_claim("我大概更信数据", "ways", "hypothesis"), _evidence("y"))
        self.assertEqual([c["id"] for c in self.store.inbox()], [second["id"], first["id"]])
        self.assertEqual(self.store.stats()["inbox"], 2)

    def test_jobs_lifecycle(self) -> None:
        job_id = self.store.enqueue_job("extract_turn", "msg_1", payload={"a": 1}, priority=5)
        self.assertIsNotNone(job_id)
        self.assertIsNone(self.store.enqueue_job("extract_turn", "msg_1"))
        self.assertEqual(self.store.pending_jobs(), 1)
        job = self.store.claim_next_job("owner-a", lease_seconds=60)
        self.assertEqual(job["jobId"], job_id)
        self.assertEqual(job["state"], "running")
        self.assertIsNone(self.store.claim_next_job("owner-b", lease_seconds=60))
        self.assertTrue(self.store.heartbeat_job(job_id, "owner-a"))
        self.assertFalse(self.store.finish_job(job_id, "owner-b"))
        self.assertTrue(self.store.fail_job(job_id, "owner-a", failure_class="transient", error_code="E", retry=True))
        self.assertEqual(self.store.get_job(job_id)["state"], "queued")
        job = self.store.claim_next_job("owner-a", lease_seconds=0)
        time.sleep(0.01)
        self.assertEqual(self.store.recover_expired_jobs(), 1)
        job = self.store.claim_next_job("owner-c", lease_seconds=60)
        self.assertTrue(self.store.finish_job(job["jobId"], "owner-c", result={"ok": True}))
        self.assertEqual(self.store.get_job(job_id)["result"], {"ok": True})
        self.assertEqual(self.store.pending_jobs(), 0)
        with self.assertRaises(OntologyError):
            self.store.enqueue_job("unknown_kind", "x")


if __name__ == "__main__":
    unittest.main()
