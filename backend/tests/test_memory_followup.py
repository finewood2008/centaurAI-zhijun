"""Follow-up save requests keep original user evidence and require human review."""
import unittest
from unittest.mock import patch

from tests import test_task_routing as harness
from mindos.stores.memory_store import MemoryStore
from mindos.zhijun import extract, jobs, memory
from mindos.zhijun.provider import ProviderError
from mindos.zhijun.routing import GuardedProvider, Router
from mindos.zhijun.turn import run_turn


IDENTITY = "我是一个珠海BNBU计算机科学与技术的学生"


class FollowupMemoryTests(unittest.TestCase):
    tearDown = harness.RoutingTests.tearDown
    preview = harness.RoutingTests.preview
    send = harness.RoutingTests.send
    enable = harness.RoutingTests.enable

    def setUp(self):
        harness.RoutingTests.setUp(self)
        self.ledger = MemoryStore(self.onto)
        self.ledger.set_policy("global", "manual", 0)
        self.local.result = {"entities": [], "claims": [{
            "section": "who", "layer": "self_declared", "predicate": "role", "subject": "me",
            "object": None, "content": IDENTITY, "quote": IDENTITY, "confidence": .99,
            "scope_hint": "long_term", "privacy_hint": "private", "merge_into": None,
            "why_it_matters": "课程学习与职业规划建议需要结合用户的学校和计算机专业。", "date": None,
        }]}

    def setup_request(self, text="记下来"):
        source = self.convs.append_message(self.cid, "user", IDENTITY)
        self.convs.append_message(self.cid, "assistant", "你是这所大学的计算机专业学生。要正式记下来吗？")
        request = self.convs.append_message(self.cid, "user", text)
        return source, request

    def job(self, request):
        return {"kind": "extract_turn", "payload": {"conversationId": self.cid, "messageId": request["id"]}}

    def run_request(self, request, provider=None):
        return jobs._run_job(self.job(request), store=self.onto, conv_store=self.convs,
                             choose_provider=lambda: provider or self.local, managed=True)

    def test_save_commands_pass_short_text_gate_but_declines_do_not(self):
        for text in ("记下来", "请记住", "帮我记下来", "把这条记下来", "请保存一下"):
            with self.subTest(text=text):
                self.assertTrue(extract.followup_memory_request(text))
                self.assertTrue(extract.should_extract(text)[0])
        for text in ("先别记", "不用记下来", "先别保存", "不要记住", "记住了吗？", "保存这个文件"):
            with self.subTest(text=text):
                self.assertFalse(extract.followup_memory_request(text))
        for text in ("暂不保存我的信息", "先不记下来，我只是随口说说"):
            self.assertTrue(extract.memory_request_declined(text))
            self.assertFalse(extract.should_extract(text)[0])

    def test_manual_mode_saves_original_student_statement_as_working_who(self):
        source, request = self.setup_request()
        result = self.run_request(request)
        self.assertEqual(len(result["created"]), 1)
        candidate = self.onto.get_claim(result["created"][0])
        self.assertEqual((candidate["section"], candidate["trustState"]), ("who", "working"))
        evidence = candidate["evidence"][0]
        self.assertEqual((evidence["messageId"], evidence["quote"]), (source["id"], IDENTITY))
        self.assertEqual(self.local.requests[0].debug["userText"], IDENTITY)
        self.assertNotIn("要正式记下来吗", self.local.requests[0].messages[0]["content"])
        self.assertEqual(self.ledger.admissions(self.cid)[0]["topic_id"], request["id"])
        self.assertEqual(self.ledger.admissions(self.cid)[0]["explicit"], 1)
        self.assertEqual(self.onto.list_claims(trust_states=("confirmed",)), [])

    def test_duplicate_requests_and_changed_model_wording_do_not_add_candidates(self):
        source, first = self.setup_request()
        self.run_request(first)
        self.local.result["claims"][0]["content"] = "我在珠海BNBU读计算机科学与技术专业"
        self.run_request(first)
        repeat = self.convs.append_message(self.cid, "user", "帮我记下来")
        self.run_request(repeat)
        candidates = self.onto.list_claims(trust_states=("working", "confirmed"))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["trustState"], "working")
        self.assertEqual(len(candidates[0]["evidence"]), 1)
        self.assertEqual(candidates[0]["evidence"][0]["messageId"], source["id"])

    def test_short_identity_answer_keeps_its_original_question_context(self):
        self.convs.append_message(self.cid, "assistant", "你的岗位是什么？")
        source = self.convs.append_message(self.cid, "user", "总经理")
        self.convs.append_message(self.cid, "assistant", "要记下来吗？")
        request = self.convs.append_message(self.cid, "user", "记下来")
        self.local.result["claims"][0].update(content="我是总经理", quote="总经理")
        result = self.run_request(request)
        self.assertEqual(len(result["created"]), 1)
        candidate = self.onto.get_claim(result["created"][0])
        self.assertEqual((candidate["section"], candidate["trustState"]), ("who", "working"))
        self.assertEqual(candidate["evidence"][0]["messageId"], source["id"])
        self.assertIn("你的岗位是什么", self.local.requests[0].messages[0]["content"])

    def test_rejected_candidate_is_not_recreated_by_another_save_command(self):
        _, request = self.setup_request()
        candidate_id = self.run_request(request)["created"][0]
        self.onto.transition(candidate_id, "reject", surface="conversation")
        self.local.result["claims"][0]["content"] = "我在珠海BNBU读计算机科学与技术专业"
        repeat = self.convs.append_message(self.cid, "user", "记下来")
        self.assertFalse(self.run_request(repeat)["created"])
        self.assertFalse(self.onto.list_claims(trust_states=("working", "confirmed")))

    def test_does_not_borrow_an_assistant_assertion_or_skip_an_intervening_user_turn(self):
        self.convs.append_message(self.cid, "assistant", IDENTITY)
        request = self.convs.append_message(self.cid, "user", "记下来")
        self.assertEqual(self.run_request(request)["reason"], "memory_source_missing")
        self.assertFalse(self.local.requests)
        for text, meta in (("先别保存", {}), ("不知道", {}),
                           (IDENTITY, {"materialRefs": ["file-1"]}),
                           ("请换一种说法", {"replyAssistance": {"kind": "control"}})):
            with self.subTest(text=text):
                self.convs.append_message(self.cid, "user", IDENTITY)
                self.convs.append_message(self.cid, "user", text, meta=meta)
                request = self.convs.append_message(self.cid, "user", "记下来")
                self.assertEqual(self.run_request(request)["state"], "skipped")
        self.assertFalse(self.local.requests)

    def test_refusal_after_save_request_while_model_runs_cancels_admission(self):
        _, request = self.setup_request()
        def generate(_req):
            self.convs.append_message(self.cid, "user", "不用记下来，我想先聊聊。")
            return self.local.result
        with patch.object(self.local, "complete_json", side_effect=generate):
            self.assertFalse(self.run_request(request)["created"])
        self.assertEqual(self.onto.inbox(), [])

    def test_cross_conversation_request_cannot_authorize_original_message(self):
        source, _ = self.setup_request()
        other = self.convs.create_conversation()["id"]
        request = self.convs.append_message(other, "user", "记下来")
        valid = extract.validate(self.local.result, user_text=IDENTITY, prev_assistant=None)
        result = memory.process_candidates(valid, [], store=self.onto, conversation_id=self.cid,
            message_id=source["id"], user_text=IDENTITY, request_message_id=request["id"])
        self.assertFalse(result["created"])

    def test_guarded_local_extraction_tracks_source_and_save_request(self):
        source, request = self.setup_request()
        provider = GuardedProvider(Router(self.onto, self.convs, self.cid), self.local,
                                   "extract_turn", [], background=True)
        result = self.run_request(request, provider)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual({ref["id"] for ref in provider.refs}, {source["id"], request["id"]})
        sources = self.onto.get_claim(result["created"][0])["evidence"][0]["locator"]["routingSources"]
        self.assertEqual({ref["id"] for ref in sources}, {source["id"], request["id"]})

    def test_online_save_command_does_not_grant_external_extraction_consent(self):
        self.enable()
        _, request = self.setup_request()
        result = jobs.run_job(self.job(request), store=self.onto, conv_store=self.convs)
        self.assertEqual((result["state"], result["reason"]), ("paused", "consent_required"))
        self.assertFalse(self.online.requests)
        self.assertFalse(self.onto.inbox())

    def test_both_chat_paths_enqueue_followup_in_manual_mode(self):
        for routed in (False, True):
            with self.subTest(routed=routed):
                self.convs.append_message(self.cid, "user", IDENTITY)
                self.convs.append_message(self.cid, "assistant", "要记下来吗？")
                with patch.object(jobs, "extraction_enabled", return_value=True), \
                     patch.object(jobs, "enqueue_extraction", return_value="save-job") as queue:
                    if routed:
                        body, preview = self.preview("记下来")
                        response = self.send(body, preview)
                        self.assertEqual(response.status_code, 200, response.text)
                    else:
                        list(run_turn(self.cid, "记下来", provider=self.local, conv_store=self.convs, ontology=self.onto))
                queue.assert_called_once()

    def test_provider_failure_and_lease_recovery_preserve_original_and_deduplicate(self):
        source, request = self.setup_request()
        queued = jobs.enqueue_extraction(self.cid, request["id"], store=self.onto)
        self.assertIsNone(jobs.enqueue_extraction(self.cid, request["id"], store=self.onto))
        worker = jobs.OntologyWorker()
        self.local.error = ProviderError("暂时不可用", code="PROVIDER_BUSY", retryable=True)
        with patch("mindos.zhijun.routing.Router.provider", return_value=self.local):
            first = self.onto.claim_next_job("first")
            worker.process(first, "first", store=self.onto, conv_store=self.convs)
            failed = self.onto.get_job(queued)
            self.assertEqual(failed["state"], "queued")
            self.assertEqual(failed["errorCode"], "PROVIDER_BUSY")
            self.assertEqual(self.convs.get_message(source["id"])["content"], IDENTITY)
            self.local.error = None
            second = self.onto.claim_next_job("second", lease_seconds=-1)
            # Simulate a crash after admission but before job acknowledgement.
            jobs.run_job(second, store=self.onto, conv_store=self.convs)
            self.assertEqual(self.onto.recover_expired_jobs(), 1)
            recovered = self.onto.claim_next_job("recovered")
            worker.process(recovered, "recovered", store=self.onto, conv_store=self.convs)
        self.assertEqual(self.onto.get_job(queued)["state"], "done")
        self.assertEqual(len(self.onto.inbox()), 1)
        self.assertEqual(self.onto.list_claims(trust_states=("confirmed",)), [])


if __name__ == "__main__":
    unittest.main()
