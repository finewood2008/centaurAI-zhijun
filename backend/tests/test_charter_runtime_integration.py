"""Synthetic charter checks across non-chat/background consumers; no network."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException

from tests.test_charter_policy import CharterPolicyTests
from mindos.stores.growth_store import GrowthStore
from mindos.stores.conversation_store import ConversationStore
from mindos.zhijun import charter_policy, deliberate, jobs, memory, nudges, growth_hooks
from mindos.zhijun.routing import Router, GuardedProvider
from mindos import zhijun_home


class CharterRuntimeTests(unittest.TestCase):
    setUp = CharterPolicyTests.setUp
    tearDown = CharterPolicyTests.tearDown
    clause = CharterPolicyTests.clause
    publish = CharterPolicyTests.publish

    def guard(self, task):
        self.store.set_mode(self.cid, "local", "")
        router = Router(self.onto, self.convs, self.cid)
        return GuardedProvider(router, self.local, task, router.history_refs(), background=True)

    def decision(self, scope="global", **extra):
        return GrowthStore.instance().create_decision({
            "scope": scope, "title": "合成选择", "context": "合成测试", "options": ["尝试", "暂缓"],
            "choice": "尝试", "rationale": "先获得反馈", "confidence": 60,
            "expectedOutcome": "了解真实情况", "reviewAt": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            "relatedEntityIds": [], "evidenceRefs": [], **extra})

    def test_manual_memory_controls_admission_and_summary_without_model_call(self):
        self.publish([self.clause(control="memory_manual", kind="boundary", text="只有我要求时才记录")])
        self.assertFalse(memory.automatic_allowed(self.onto, self.convs, self.cid))
        self.assertFalse(memory.extraction_allowed(self.onto, self.convs, self.cid, "我很喜欢按自己的节奏工作"))
        self.assertTrue(memory.extraction_allowed(self.onto, self.convs, self.cid, "请记住：我希望按自己的节奏工作"))
        self.convs.append_message(self.cid, "user", "这是合成消息")
        result = jobs.run_job({"kind": "summarize_conversation", "payload": {"conversationId": self.cid}},
                              store=self.onto, conv_store=self.convs)
        self.assertEqual(result["reason"], "charter_memory_manual")
        self.assertEqual(self.local.requests, [])
        self.assertIsNone(self.convs.latest_summary(self.cid))

    def test_summary_persists_actual_charter_lineage_and_survives_reopen(self):
        charter = self.publish()
        self.convs.append_message(self.cid, "user", "这是合成摘要来源", meta={"routingSources": []})
        result = jobs._run_job({"kind": "summarize_conversation", "payload": {"conversationId": self.cid}},
            store=self.onto, conv_store=self.convs, choose_provider=lambda: self.guard("summarize_conversation"), managed=True)
        self.assertEqual(result["state"], "done")
        saved = ConversationStore(self.convs._db_path).latest_summary(self.cid)
        self.assertEqual(saved["meta"]["charterBasis"]["version"], charter["version"])
        self.assertTrue(any(s["kind"] == "charter_clause" for s in saved["meta"]["routingSources"]))

    def test_changed_charter_during_summary_prevents_save(self):
        self.publish()
        self.convs.append_message(self.cid, "user", "合成摘要来源", meta={"routingSources": []})
        def changed(request):
            self.publish([self.clause(text="回答前先澄清必要条件")])
            return {"summary": "旧结果", "themes": [], "open_loops": []}
        with patch.object(self.local, "complete_json", side_effect=changed), self.assertRaises(HTTPException):
            jobs._run_job({"kind": "summarize_conversation", "payload": {"conversationId": self.cid}},
                store=self.onto, conv_store=self.convs, choose_provider=lambda: self.guard("summarize_conversation"), managed=True)
        self.assertIsNone(self.convs.latest_summary(self.cid))

    def draft(self):
        self.local.result = {"title": "合成选择", "context": "要不要开始", "options": ["尝试", "暂缓"],
            "choice": "尝试", "rationale": "先获得反馈", "confidence": 60, "expectedOutcome": "了解真实情况", "userQuotes": []}
        message = self.convs.append_message(self.cid, "user", "我要先尝试，理由是先获得反馈，预期了解真实情况，把握六成。", meta={"routingSources": []})
        return deliberate.run_draft(provider=self.guard("draft_turn"), conv_store=self.convs,
            conversation_id=self.cid, message_id=message["id"])[0]

    def confirm(self):
        return deliberate.confirm_draft(self.cid, {"choice": "尝试", "rationale": "先获得反馈", "confidence": 60,
            "expectedOutcome": "了解真实情况"}, conv_store=self.convs)["decision"]

    def test_decision_keeps_generation_version_not_confirmation_version(self):
        first = self.publish()
        draft = self.draft()
        self.assertEqual(draft["fields"]["charterBasis"]["charterId"], first["id"])
        latest = self.publish([self.clause(text="新的合作约定")])
        saved = self.confirm()
        self.assertEqual(saved["charterId"], first["id"])
        self.assertNotEqual(saved["charterVersion"], latest["version"])
        self.assertEqual(self.onto.list_claims(), [])

    def test_draft_without_charter_never_pretends_it_used_later_version(self):
        self.draft()
        self.publish()
        self.assertIsNone(self.confirm()["charterId"])

    def test_no_proactive_suppresses_existing_and_future_nudges_without_deleting(self):
        self.decision()
        self.assertGreater(nudges.scan(conv_store=self.convs)["created"], 0)
        self.publish([self.clause(control="no_proactive", kind="boundary", text="不要主动提醒我")])
        self.assertEqual(nudges.today(conv_store=self.convs)["items"], [])
        self.assertTrue(self.convs.list_nudges())
        self.decision()
        self.assertEqual(nudges.scan(conv_store=self.convs)["created"], 0)

    def test_home_does_not_queue_or_show_old_personalized_letter_when_disabled(self):
        self.publish([self.clause(control="no_proactive", kind="boundary", text="由我主动开始")])
        self.decision()
        home = zhijun_home.build_home_overview(ontology=self.onto, conversations=self.convs)
        self.assertFalse(home["proactiveAllowed"])
        self.assertEqual(home["brief"]["sourceRefs"], [])
        self.assertEqual(zhijun_home.generate_home_brief(home["sourceHash"], store=self.onto, conv_store=self.convs)["reason"], "charter_no_proactive")
        self.assertEqual(self.local.requests, [])

    def test_review_saved_but_no_automatic_claim_under_manual_only(self):
        self.publish([self.clause(control="memory_manual", kind="boundary")])
        decision = self.decision()
        result = growth_hooks.on_review({"id": "synthetic-review", "decisionId": decision["id"],
            "lessons": ["先尝试比一直计划更能给我反馈"]}, decision, store=self.onto)
        self.assertEqual(result["created"], [])
        self.assertEqual(self.onto.list_claims(), [])
        self.assertIsNotNone(GrowthStore.instance().get_decision(decision["id"]))

    def test_device_specific_charter_does_not_disable_other_devices(self):
        self.publish([self.clause(control="no_proactive", kind="boundary")], scope="device:one")
        self.assertTrue(charter_policy.check_action(charter_policy.scope_policy("global"), "proactive")["allowed"])
        blocked = self.decision(scope="device:one")
        self.assertEqual(charter_policy.record_scope(blocked, self.convs), "device:one")
        self.assertEqual(nudges.scan(conv_store=self.convs)["created"], 0)
        self.decision()
        self.assertEqual(nudges.scan(conv_store=self.convs)["created"], 1)

    def test_weekly_summary_never_aggregates_other_device_records(self):
        self.publish(scope="device:one")
        self.decision(scope="device:one")
        self.assertIsNone(nudges.weekly_review_candidate(conv_store=self.convs,
            growth=GrowthStore.instance(), now=datetime.now(timezone.utc)))
        self.decision()
        result = nudges.weekly_review_candidate(conv_store=self.convs,
            growth=GrowthStore.instance(), now=datetime.now(timezone.utc))
        self.assertIn("1 个判断", result["summary"])

    def test_candidate_lineage_survives_newer_draft_revision_and_confirmation(self):
        from mindos.zhijun.charter_artifacts import remember, recall_lineage
        charter = self.publish()
        draft = self.draft()
        router = Router(self.onto, self.convs, self.cid)
        source = router.resolve(router.ref("charter_clause", charter["id"] + ":guidance"))[0]
        remember(self.onto, self.cid, "decision_suggestions", draft["revision"],
                 {"sources": [source], "charterBasis": draft["fields"]["charterBasis"]})
        self.convs.upsert_draft(self.cid, draft["fields"])
        self.assertGreater(self.convs.get_draft(self.cid)["revision"], draft["revision"])
        saved = self.confirm()
        lineage = next(json.loads(ref) for ref in saved["evidenceRefs"] if json.loads(ref).get("kind") == "helper_lineage")
        self.assertIn(source["ref"], lineage["routingSources"])
        self.assertEqual(recall_lineage(self.onto, self.cid, "decision_suggestions")["sourceRevisions"], [draft["revision"]])


if __name__ == "__main__":
    unittest.main()
