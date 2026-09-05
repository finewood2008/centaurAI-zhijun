"""Conversation-aware retrieval must preserve source consent and honest receipts.

All databases and providers are synthetic; assertions inspect the actual model
boundary, not merely the route preview or the displayed permission message.
"""
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from tests import test_task_routing as routing_fixture
from mindos import growth
from mindos.stores.alignment_store import AlignmentStore
from mindos.stores.charter_draft_store import FIELDS
from mindos.stores.routing_store import RoutingStore
from mindos.zhijun.provider import ChatRequest
from mindos.zhijun.routing import GuardedProvider, Router, prepare_chat, service_info
from mindos.zhijun import memory_retrieval


class MemoryContextRoutingTests(unittest.TestCase):
    setUp = routing_fixture.RoutingTests.setUp
    tearDown = routing_fixture.RoutingTests.tearDown
    enable = routing_fixture.RoutingTests.enable
    claim = routing_fixture.RoutingTests.claim
    preview = routing_fixture.RoutingTests.preview
    grant = routing_fixture.RoutingTests.grant
    send = routing_fixture.RoutingTests.send

    def charter(self, **values):
        return growth.create_charter(growth.CharterCreate(
            roles=["合成项目开发者"], goals=["今年完成一次小尝试"], **values))

    def plan(self, text="人生章程里还有哪些栏目没有填写？", **kwargs):
        return prepare_chat(Router(self.onto, self.convs, self.cid), text, **kwargs)

    def execute(self, plan):
        return list(GuardedProvider(plan.router, plan.provider, "chat", plan.refs,
            revision=plan.preview["revision"], excluded=plan.preview["excluded"]).stream(
                ChatRequest(**plan.preview["request"])))

    def allow_refs(self, refs):
        r = Router(self.onto, self.convs, self.cid)
        preview = r.prepare("chat", ChatRequest(system="synthetic grant", messages=[]), refs, self.online)
        r.authorize(preview, preview["missing"])

    def ordinary_message(self, text, *, role="user", sources=None, status="complete"):
        return self.convs.append_message(self.cid, role, text, status=status, meta={
            "routingOrigin": {"service": service_info(self.online)["id"]},
            "routingSources": sources or []})

    def test_exact_charter_state_includes_unfilled_fields_with_versioned_refs(self):
        c = self.charter()
        plan = self.plan()
        charter_refs = [s for s in plan.preview["sources"] if s["kind"] == "charter"]
        self.assertEqual({s["id"].rsplit(":", 1)[1] for s in charter_refs}, set(FIELDS))
        self.assertTrue(all(s["version"] != "unavailable" for s in charter_refs))
        self.assertEqual(plan.preview["request"]["debug"]["charterSnapshot"], c["version"])
        self.execute(plan)
        payload = self.local.requests[-1].system
        self.assertIn("合成项目开发者", payload)
        self.assertIn("今年完成一次小尝试", payload)
        for field in ("vision", "principles", "challengeStyle", "boundaries", "quietDomains"):
            self.assertIn(FIELDS[field] + "：待完善（尚未填写，不代表没有限制）", payload)
        receipt = plan.assembled.provenance["memoryContext"]
        self.assertEqual(receipt["intent"], "charter")
        self.assertTrue(receipt["charterChecked"])
        self.assertTrue(receipt["charterComplete"])

    def test_empty_field_status_is_not_exempt_from_online_authorization(self):
        self.charter(); self.enable()
        plan = self.plan()
        empty = [s for s in plan.preview["sources"] if s["id"].endswith(":boundaries")]
        self.assertEqual(len(empty), 1)
        self.assertIn(empty[0]["key"], plan.preview["missing"])
        with self.assertRaises(HTTPException): self.execute(plan)
        self.assertEqual(self.online.requests, [])
        self.grant(plan.preview)
        fresh = self.plan()
        self.assertEqual(fresh.preview["missing"], [])
        self.execute(fresh)
        self.assertIn(FIELDS["boundaries"] + "：待完善", self.online.requests[-1].system)

    def test_omit_preference_does_not_describe_unapproved_fields_as_empty(self):
        self.charter(); self.enable()
        self.store.set_handling("global", enabled=True, action="omit",
            service=service_info(self.online)["id"], expected_revision=0)
        plan = self.plan()
        self.assertFalse(plan.preview["missing"])
        self.execute(plan)
        self.assertNotIn("合成项目开发者", self.online.requests[-1].system)
        self.assertNotIn(FIELDS["boundaries"] + "：待完善", self.online.requests[-1].system)
        self.assertFalse(plan.assembled.provenance["memoryContext"]["charterComplete"])

    def test_charter_revoke_between_preview_and_actual_request_blocks(self):
        self.charter(); self.enable(); self.grant(self.plan().preview)
        plan = self.plan()
        self.store.revoke("global")
        with self.assertRaises(HTTPException): self.execute(plan)
        self.assertEqual(self.online.requests, [])

    def test_new_current_charter_invalidates_prepared_state_snapshot(self):
        self.charter(); self.enable(); self.grant(self.plan().preview)
        plan = self.plan()
        self.charter(boundaries=["新边界"], expectedVersion=1)
        with self.assertRaises(HTTPException): self.execute(plan)
        self.assertEqual(self.online.requests, [])
        self.assertTrue(self.plan().preview["missing"])

    def test_current_charter_snapshot_also_rechecked_before_local_call(self):
        self.charter()
        plan = self.plan()
        self.charter(boundaries=["新边界"], expectedVersion=1)
        with self.assertRaises(HTTPException): self.execute(plan)
        self.assertEqual(self.local.requests, [])

    def test_changed_service_cannot_receive_previously_authorized_charter_state(self):
        self.charter(); self.enable(); self.grant(self.plan().preview)
        plan = self.plan()
        self.online._base_url = "https://another-synthetic.invalid/v1"
        self.enable()
        with self.assertRaises(HTTPException): self.execute(plan)
        self.assertEqual(self.online.requests, [])
        self.assertTrue(self.plan().preview["missing"])

    def test_allowed_recent_topic_enables_short_followup_retrieval(self):
        c = self.claim(); self.enable()
        self.allow_refs([{"kind": "claim", "id": c["id"]}])
        self.ordinary_message("我想讨论星桥项目被安排的工作与个人追求之间的关系")
        plan = self.plan("那这件事该怎么办？")
        self.assertIn(c["id"], [x["id"] for x in plan.assembled.provenance["confirmedClaims"]])
        self.execute(plan)
        self.assertIn(c["content"], self.online.requests[-1].system)

    def test_cutoff_topic_is_never_recovered_for_short_followup(self):
        c = self.claim()
        self.convs.append_message(self.cid, "user", "PRIVATE_CUTOFF_TOPIC 星桥项目被安排的工作与个人追求")
        self.enable(fresh=True)
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", wraps=memory_retrieval.retrieve_claims) as search:
            plan = self.plan("那这件事该怎么办？")
        self.assertTrue(search.called)
        searches = json.dumps([call.args[1:] for call in search.call_args_list], ensure_ascii=False)
        self.assertNotIn("PRIVATE_CUTOFF_TOPIC", searches)
        self.assertNotIn(c["id"], [x["id"] for x in plan.assembled.provenance["confirmedClaims"]])
        self.execute(plan)
        self.assertNotIn("PRIVATE_CUTOFF_TOPIC", json.dumps(self.online.requests[-1].messages))

    def test_unapproved_or_incomplete_history_cannot_supply_retrieval_terms(self):
        c = self.claim(); self.enable()
        self.convs.append_message(self.cid, "user", "PRIVATE_UNAPPROVED_TOPIC 星桥项目工作安排", meta={"routingSources": []})
        self.ordinary_message("PRIVATE_INCOMPLETE_TOPIC 星桥项目工作安排", status="aborted")
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", wraps=memory_retrieval.retrieve_claims) as search:
            plan = self.plan("那这件事该怎么办？")
        self.assertTrue(search.called)
        searches = json.dumps([call.args[1:] for call in search.call_args_list], ensure_ascii=False)
        self.assertNotIn("PRIVATE_UNAPPROVED_TOPIC", searches)
        self.assertNotIn("PRIVATE_INCOMPLETE_TOPIC", searches)
        self.assertNotIn(c["id"], [x["id"] for x in plan.assembled.provenance["confirmedClaims"]])

    def test_blocked_legacy_history_does_not_restore_charter_intent(self):
        self.charter(); self.enable()
        self.convs.append_message(self.cid, "user", "PRIVATE_LEGACY_TOPIC 我想修改人生章程", meta={"localOnlyDerived": True})
        AlignmentStore(self.onto).status(self.cid, local_only=True, status="paused")
        plan = self.plan("还有哪些空着？")
        self.assertEqual(plan.assembled.provenance["memoryContext"]["intent"], "conversation")
        self.assertFalse(any(s["kind"] == "charter" for s in plan.preview["sources"]))
        self.assertNotIn("PRIVATE_LEGACY_TOPIC", json.dumps(plan.preview["request"], ensure_ascii=False))

    def test_history_ancestry_is_inherited_not_reported_as_direct_read(self):
        c = self.claim(); self.enable()
        r = Router(self.onto, self.convs, self.cid)
        ref = r.resolve(r.ref("claim", c["id"]))[0]["ref"]
        self.allow_refs([ref])
        self.ordinary_message("ALLOWED_DERIVED_ANSWER", role="assistant", sources=[ref])
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[]):
            plan = self.plan("如何整理明天的行李？")
        self.assertEqual(plan.assembled.provenance["confirmedClaims"], [])
        memory = plan.assembled.provenance["memoryContext"]
        self.assertEqual(memory["directCount"], 0)
        self.assertEqual(memory["inheritedCount"], 1)
        self.assertEqual(memory["status"], "inherited")
        self.execute(plan)
        self.assertIn("ALLOWED_DERIVED_ANSWER", json.dumps(self.online.requests[-1].messages))

    def test_changed_claim_ancestor_is_not_counted_as_inherited(self):
        c = self.claim(); self.enable()
        r = Router(self.onto, self.convs, self.cid)
        ref = r.resolve(r.ref("claim", c["id"]))[0]["ref"]
        self.allow_refs([ref])
        self.ordinary_message("PRIVATE_STALE_DERIVED_ANSWER", role="assistant", sources=[ref])
        self.onto.add_evidence(c["id"], [{"kind": "user_edit", "quote": "新的独立修正"}])
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[]):
            plan = self.plan("如何整理明天的行李？")
        self.assertEqual(plan.assembled.provenance["memoryContext"]["inheritedCount"], 0)
        self.execute(plan)
        self.assertNotIn("PRIVATE_STALE_DERIVED_ANSWER", json.dumps(self.online.requests[-1].messages))

    def test_direct_and_inherited_same_claim_count_once_and_receipt_persists(self):
        c = self.claim(); self.enable()
        r = Router(self.onto, self.convs, self.cid)
        ref = r.resolve(r.ref("claim", c["id"]))[0]["ref"]
        self.allow_refs([ref])
        self.ordinary_message("这段资料谈到了星桥项目的工作安排", role="assistant", sources=[ref, ref])
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[c]):
            body, preview = self.preview("星桥项目工作安排")
            response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        assistant = self.convs.list_messages(self.cid)[-1]
        memory = assistant["meta"]["routingProvenance"]["memoryContext"]
        self.assertEqual(memory["directCount"], 1)
        self.assertEqual(memory["inheritedCount"], 0)
        self.assertEqual(memory["status"], "direct")
        self.assertEqual(self.convs.get_message(assistant["id"])["meta"]["routingProvenance"]["memoryContext"], memory)

    def test_partially_authorized_charter_does_not_claim_full_check(self):
        c = self.charter(); self.enable()
        self.allow_refs([{"kind": "charter", "id": c["id"] + ":roles"}])
        self.store.set_handling("global", enabled=True, action="omit",
            service=service_info(self.online)["id"], expected_revision=0)
        plan = self.plan()
        self.execute(plan)
        memory = plan.assembled.provenance["memoryContext"]
        self.assertTrue(memory["charterChecked"])
        self.assertFalse(memory["charterComplete"])
        self.assertIn("合成项目开发者", self.online.requests[-1].system)
        self.assertNotIn("今年完成一次小尝试", self.online.requests[-1].system)
        self.assertIn(FIELDS["boundaries"] + "：本轮未读取", self.online.requests[-1].system)

    def test_explicit_topic_switch_does_not_keep_requesting_charter(self):
        self.charter(); self.enable()
        self.store.set_task(self.cid, "charter")
        self.ordinary_message("我想修改人生章程")
        switched = self.plan("换个话题，明天怎么打包行李？")
        self.assertEqual(switched.assembled.provenance["memoryContext"]["intent"], "conversation")
        self.assertFalse(any(s["kind"] == "charter" for s in switched.preview["sources"]))
        self.ordinary_message("换个话题，明天怎么打包行李？")
        followup = self.plan("还有哪些？")
        self.assertEqual(followup.assembled.provenance["memoryContext"]["intent"], "conversation")
        self.assertFalse(any(s["kind"] == "charter" for s in followup.preview["sources"]))

    def test_self_contained_question_does_not_inherit_charter_from_short_length(self):
        self.charter(); self.enable()
        self.store.set_task(self.cid, "charter")
        self.ordinary_message("我想修改人生章程")
        plan = self.plan("明天怎么打包行李？")
        self.assertEqual(plan.assembled.provenance["memoryContext"]["intent"], "conversation")
        self.assertFalse(any(s["kind"] == "charter" for s in plan.preview["sources"]))
        self.ordinary_message("明天怎么打包行李？")
        followup = self.plan("还有哪些？")
        self.assertEqual(followup.assembled.provenance["memoryContext"]["intent"], "conversation")
        self.assertFalse(any(s["kind"] == "charter" for s in followup.preview["sources"]))

    def test_followup_reopens_original_claim_instead_of_only_reading_summary(self):
        c = self.claim(); self.enable()
        r = Router(self.onto, self.convs, self.cid)
        ref = r.resolve(r.ref("claim", c["id"]))[0]["ref"]
        self.allow_refs([ref])
        assistant = self.ordinary_message("我们可以先区分责任和真正认同的方向。", role="assistant", sources=[ref])
        self.convs.update_message(assistant["id"], meta={**assistant["meta"], "routingProvenance": {
            "confirmedClaims": [{"id": c["id"], "content": c["content"]}], "workingClaims": []}})
        # No lexical clue in the follow-up or previous summary. The previous
        # answer's verified direct sources must be read again from the store.
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[]):
            plan = self.plan("那怎么办？")
        self.assertIn(c["id"], [x["id"] for x in plan.assembled.provenance["confirmedClaims"]])
        self.assertEqual(plan.assembled.provenance["memoryContext"]["directCount"], 1)
        self.execute(plan)
        self.assertIn(c["content"], self.online.requests[-1].system)

    def test_followup_does_not_reopen_every_old_ancestor_as_latest_evidence(self):
        old = self.claim()
        current = self.onto.create_claim({"subject_entity_id": "ent_me", "section": "principles",
            "layer": "self_declared", "predicate": "holds_principle", "content": "我重视每周留出稳定的家庭时间", "confidence": .9},
            [{"kind": "user_edit", "quote": "家庭时间对我重要"}],
            trust_state="confirmed", trust_origin="user_created")
        self.enable()
        r = Router(self.onto, self.convs, self.cid)
        refs = [r.resolve(r.ref("claim", c["id"]))[0]["ref"] for c in (old, current)]
        self.allow_refs(refs)
        assistant = self.ordinary_message("我们可以从一个可持续的小安排开始。", role="assistant", sources=refs)
        self.convs.update_message(assistant["id"], meta={**assistant["meta"], "routingProvenance": {
            "confirmedClaims": [{"id": current["id"], "content": current["content"]}], "workingClaims": []}})
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[]):
            plan = self.plan("那怎么办？")
        direct = [c["id"] for c in plan.assembled.provenance["confirmedClaims"]]
        self.assertEqual(direct, [current["id"]])
        self.assertEqual(plan.assembled.provenance["memoryContext"]["inheritedCount"], 1)

    def test_explicit_charter_task_survives_fresh_context_without_copying_history(self):
        self.charter()
        response = self.client.post("/api/mindos/conversations", json={"taskContext": "charter"})
        self.assertEqual(response.status_code, 200, response.text)
        self.cid = response.json()["id"]
        self.url = f"/api/mindos/conversations/{self.cid}"
        self.convs.append_message(self.cid, "user", "PRIVATE_OLD_DIRECTION 我想修改人生章程")
        self.enable(fresh=True)
        reopened = RoutingStore(self.onto)
        with reopened.ontology._connect() as db:
            rows = db.execute("SELECT * FROM routing_tasks").fetchall()
        self.assertNotIn("PRIVATE_OLD_DIRECTION", str([tuple(row) for row in rows]))
        plan = self.plan("还有哪些空着？")
        self.assertEqual(plan.assembled.provenance["memoryContext"]["intent"], "charter")
        self.assertNotIn("PRIVATE_OLD_DIRECTION", json.dumps(plan.preview["request"], ensure_ascii=False))
        self.assertEqual(len([s for s in plan.preview["sources"] if s["kind"] == "charter"]), 7)


if __name__ == "__main__":
    unittest.main()
