"""A charter governs memory locally without becoming unrelated model evidence."""
import unittest
from unittest.mock import patch

from tests import test_charter_policy as charter_harness
from mindos.zhijun import jobs
from mindos.zhijun.routing import Router, service_info


class MemoryCharterExtractionTests(unittest.TestCase):
    setUp = charter_harness.CharterPolicyTests.setUp
    tearDown = charter_harness.CharterPolicyTests.tearDown
    clause = charter_harness.CharterPolicyTests.clause
    publish = charter_harness.CharterPolicyTests.publish
    enable = charter_harness.CharterPolicyTests.enable

    def setup_online(self, clauses=None):
        charter = self.publish(clauses)
        self.enable()
        self.service = service_info(self.online)["id"]
        self.policy = self.store.set_policy("global", enabled=True, service=self.service,
            service_name="Synthetic", include_files=False, include_charter=False,
            purposes=["chat", "extract_turn"], auto_egress=True, expected_revision=0)
        return charter

    def add_user(self, text="我是程序员"):
        return self.convs.append_message(self.cid, "user", text,
            meta={"routingOrigin": {"service": self.service}, "routingSources": []})

    def add_previous(self, charter, text="你的职业是什么？"):
        return self.convs.append_message(self.cid, "assistant", text,
            meta={"routingOrigin": {"service": self.service}, "routingSources": [
                {"kind": "charter_clause", "id": charter["id"] + ":" + charter["clauses"][0]["id"]}]})

    def model_result(self, quote):
        return {"entities": [], "claims": [{"section": "who", "layer": "self_declared",
            "predicate": "role", "subject": "me", "object": None,
            "content": "我是一名程序员", "quote": quote, "confidence": 0.95,
            "scope_hint": "long_term", "privacy_hint": "private", "merge_into": None,
            "why_it_matters": "讨论技术问题时可以直接围绕实际开发约束和代码展开。", "date": None}]}

    def run_extract(self, message):
        self.online.result = self.model_result(message["content"])
        return jobs.run_job({"kind": "extract_turn", "payload": {
            "conversationId": self.cid, "messageId": message["id"]}},
            store=self.onto, conv_store=self.convs)

    def assert_candidate(self, result, trust_state="confirmed"):
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(len(result["created"]), 1, result)
        # V3（拍板 4）：「我是程序员」是亲口说的、原话精确引用的自述 → 直接记为已确认（可撤回），不再进待确认队列；
        # 原话没有第一人称（「程序员」）或章程只允许手动整理时仍是 working。
        other = "working" if trust_state == "confirmed" else "confirmed"
        self.assertEqual(self.onto.list_claims(trust_states=(other,)), [])
        claims = self.onto.list_claims(trust_states=(trust_state,))
        self.assertEqual([c["id"] for c in claims], result["created"])
        self.assertEqual(claims[0]["trustOrigin"], "utterance" if trust_state == "confirmed" else "model")
        self.assertEqual(result["autoConfirmed"], result["created"] if trust_state == "confirmed" else [])
        self.assertEqual(self.store.policy("global"), self.policy, "background must not widen consent")

    def test_independent_statement_does_not_send_unapproved_charter(self):
        charter = self.setup_online()
        result = self.run_extract(self.add_user())
        self.assert_candidate(result)
        self.assertEqual(len(self.online.requests), 1)
        request = self.online.requests[0]
        self.assertNotIn(charter["clauses"][0]["text"], request.system)
        self.assertEqual(request.debug["charterPolicy"]["version"], charter["version"])

    def test_independent_statement_does_not_inherit_previous_assistant_charter(self):
        charter = self.setup_online()
        previous = self.add_previous(charter, "先按章程核对边界。你的职业是什么？")
        result = self.run_extract(self.add_user())
        self.assert_candidate(result)
        sent = self.online.requests[0]
        self.assertNotIn(previous["content"], str(sent.messages))
        claim = self.onto.list_claims(trust_states=("confirmed",))[0]
        sources = claim["evidence"][0]["locator"]["routingSources"]
        self.assertFalse(any(r["kind"].startswith("charter") or r["id"] == previous["id"] for r in sources))

    def test_short_answer_keeps_previous_question_permission_boundary(self):
        charter = self.setup_online()
        previous = self.add_previous(charter)
        message = self.add_user("程序员")
        result = self.run_extract(message)
        self.assertEqual(result["state"], "paused", result)
        self.assertEqual(result["reason"], "consent_required", result)
        self.assertEqual(self.online.requests, [])
        preview = self.store.get_preview(result["previewId"], self.cid)
        self.assertTrue(any(s["id"] == previous["id"] for s in preview["sources"]))
        self.assertTrue(any(s["kind"].startswith("charter") and s["key"] in preview["missing"] for s in preview["sources"]))
        self.assertEqual(self.onto.list_claims(trust_states=("working", "confirmed")), [])
        Router(self.onto, self.convs, self.cid).authorize(preview, preview["missing"])
        self.assert_candidate(self.run_extract(message), trust_state="working")  # 「程序员」不含第一人称
        self.assertIn(previous["content"], str(self.online.requests[0].messages))

    def test_long_slot_answer_still_preserves_previous_question_permission(self):
        charter = self.setup_online()
        previous = self.add_previous(charter)
        result = self.run_extract(self.add_user("程序员，负责团队内部的系统开发与维护"))
        self.assertEqual(result["state"], "paused", result)
        preview = self.store.get_preview(result["previewId"], self.cid)
        self.assertTrue(any(s["id"] == previous["id"] for s in preview["sources"]))
        self.assertEqual(self.online.requests, [])

    def test_manual_charter_blocks_automatic_but_allows_explicit_request(self):
        self.setup_online([self.clause(control="memory_manual", kind="boundary", text="只有我要求时才记录")])
        result = self.run_extract(self.add_user())
        self.assertEqual(result, {"state": "skipped", "reason": "memory_policy"})
        self.assertEqual(self.online.requests, [])
        self.assert_candidate(self.run_extract(self.add_user("请记住：我是程序员")), trust_state="working")  # 章程手动整理：不自动确认

    def test_local_only_charter_still_blocks_external_extraction(self):
        self.setup_online([self.clause(control="local_only", kind="boundary", text="仅在本地处理")])
        result = self.run_extract(self.add_user())
        self.assertEqual(result["state"], "paused", result)
        self.assertEqual(self.online.requests, [])
        self.assertEqual(self.onto.list_claims(trust_states=("working", "confirmed")), [])

    def test_charter_change_during_model_call_blocks_candidate_write(self):
        self.setup_online()
        message = self.add_user()
        def changed(_request):
            self.publish([self.clause(control="memory_manual", kind="boundary", text="仅明确请求才整理")])
            return self.model_result(message["content"])
        with patch.object(self.online, "complete_json", side_effect=changed):
            result = self.run_extract(message)
        self.assertEqual(result["state"], "paused", result)
        self.assertEqual(result["reason"], "charter_changed", result)
        self.assertEqual(self.onto.list_claims(trust_states=("working", "confirmed")), [])


if __name__ == "__main__":
    unittest.main()
