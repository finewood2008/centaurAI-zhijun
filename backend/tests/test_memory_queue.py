"""Memory preference boundaries for queued work; synthetic stores/providers only."""
import unittest
from unittest.mock import Mock, patch

from tests import test_task_routing as harness
from mindos.stores.alignment_store import AlignmentStore
from mindos.zhijun import jobs
from mindos.zhijun.provider import ONBOARDING_QUESTIONS
from mindos.zhijun.turn import run_turn


class MemoryQueueTests(unittest.TestCase):
    setUp = harness.RoutingTests.setUp
    tearDown = harness.RoutingTests.tearDown
    preview = harness.RoutingTests.preview
    send = harness.RoutingTests.send

    def message_job(self, text="我长期负责合成项目研发，希望保留自主决定的空间。", *, role="user", status="complete", conversation_id=None):
        message = self.convs.append_message(conversation_id or self.cid, role, text, status=status)
        return {"kind": "extract_turn", "payload": {"conversationId": self.cid, "messageId": message["id"]}}, message

    def routed_turn(self, text):
        body, preview = self.preview(text)
        response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        return response.text

    def test_manual_policy_prevents_routed_extraction_enqueue_but_keeps_reply(self):
        with patch("mindos.zhijun.memory.extraction_allowed", return_value=False) as allowed, \
             patch.object(jobs, "extraction_enabled", return_value=True), \
             patch.object(jobs, "enqueue_extraction") as queue:
            response = self.routed_turn("我今天在考虑合成项目如何安排，希望先理清条件。")
        queue.assert_not_called()
        self.assertEqual(allowed.call_args.args[:3], (self.onto, self.convs, self.cid))
        self.assertIn('"reason": "memory_policy"', response)
        self.assertIn('event: message_done', response)
        self.assertEqual(len(self.local.requests), 1, "ordinary chat still works in manual memory mode")

    def test_manual_policy_prevents_legacy_extraction_enqueue_but_keeps_reply(self):
        with patch("mindos.zhijun.memory.extraction_allowed", return_value=False), \
             patch.object(jobs, "extraction_enabled", return_value=True), \
             patch.object(jobs, "enqueue_extraction") as queue:
            events = list(run_turn(self.cid, "我目前负责合成项目研发，希望先把工作安排理清。",
                                   provider=self.local, conv_store=self.convs, ontology=self.onto))
        queue.assert_not_called()
        self.assertEqual(next(data for event, data in events if event == "extraction")["reason"], "memory_policy")
        self.assertEqual(events[-1][0], "message_done")

    def test_explicit_request_can_queue_both_paths_without_confirming_records(self):
        # Request recognition/admission are tested in memory.py; the queue must
        # respect its answer, not treat a manual setting as a blanket refusal.
        text = "请记住：我长期认同做决定之前尊重当事人的意愿。"
        with patch("mindos.zhijun.memory.extraction_allowed", side_effect=lambda *args: args[-1].startswith("请记住")), \
             patch("mindos.zhijun.memory.automatic_allowed", return_value=False), \
             patch.object(jobs, "extraction_enabled", return_value=True), \
             patch.object(jobs, "enqueue_extraction", return_value="synthetic-job") as queue, \
             patch.object(jobs, "enqueue_alignment") as alignment:
            self.routed_turn(text)
            legacy = self.convs.create_conversation()["id"]
            list(run_turn(legacy, text, provider=self.local, conv_store=self.convs, ontology=self.onto))
        self.assertEqual(queue.call_count, 2)
        alignment.assert_not_called()
        self.assertEqual(self.onto.list_claims(trust_states=("confirmed",)), [])

    def test_already_queued_job_rechecks_policy_before_provider_or_gate(self):
        job, message = self.message_job()
        queued = jobs.enqueue_extraction(self.cid, message["id"], store=self.onto)
        with patch("mindos.zhijun.memory.extraction_allowed", return_value=False), \
             patch("mindos.zhijun.routing.Router.provider") as choose, \
             patch.object(jobs.provider_gate, "acquire") as acquire, \
             patch.object(jobs.extract, "run_extraction") as extract:
            result = jobs.run_job(self.onto.get_job(queued), store=self.onto, conv_store=self.convs)
        self.assertEqual(result, {"state": "skipped", "reason": "memory_policy"})
        choose.assert_not_called()
        acquire.assert_not_called()
        extract.assert_not_called()

    def test_explicit_job_is_allowed_but_manual_mode_does_not_spawn_alignment(self):
        job, message = self.message_job("请记住：我长期认同做决定前尊重当事人的意愿。")
        self.convs.append_message(self.cid, "assistant", "已收到这次明确的记忆请求。")
        choose = Mock(return_value=self.local)
        with patch("mindos.zhijun.memory.extraction_allowed", return_value=True), \
             patch("mindos.zhijun.memory.automatic_allowed", return_value=False), \
             patch.object(jobs.extract, "run_extraction", return_value={"created": ["new-working"], "promoted": []}) as extract, \
             patch.object(jobs, "enqueue_alignment") as alignment:
            result = jobs._run_job(job, store=self.onto, conv_store=self.convs, choose_provider=choose, managed=True)
        choose.assert_called_once()
        self.assertEqual(extract.call_args.kwargs["user_text"], message["content"])
        self.assertEqual(result["created"], ["new-working"])
        alignment.assert_not_called()

    def test_worker_rejects_cross_conversation_and_noncompleted_user_messages(self):
        other = self.convs.create_conversation()["id"]
        cases = [(self.message_job(conversation_id=other)[0], "message_conversation_mismatch"),
                 (self.message_job(role="assistant")[0], "not_completed_user_message"),
                 (self.message_job(status="aborted")[0], "not_completed_user_message")]
        for job, reason in cases:
            with self.subTest(reason=reason), patch("mindos.zhijun.memory.extraction_allowed") as allowed:
                choose = Mock()
                result = jobs._run_job(job, store=self.onto, conv_store=self.convs, choose_provider=choose, managed=True)
                self.assertEqual(result["reason"], reason)
                choose.assert_not_called()
                allowed.assert_not_called()

    def test_alignment_only_follows_created_or_promoted_memories(self):
        job, _ = self.message_job()
        answer = self.convs.append_message(self.cid, "assistant", "合成回复。")
        cases = [({"created": [], "promoted": [], "suppressed": 1}, False),
                 ({"created": [], "promoted": [], "contextualDrafts": ["contextual-1"]}, False),
                 ({"created": [], "promoted": [], "reaffirmed": ["same-1"]}, False),
                 ({"created": ["working-1"], "promoted": []}, True),
                 ({"created": [], "promoted": ["working-2"]}, True)]
        for result, expected in cases:
            with self.subTest(result=result), \
                 patch("mindos.zhijun.memory.extraction_allowed", return_value=True), \
                 patch("mindos.zhijun.memory.automatic_allowed", return_value=True), \
                 patch.object(jobs.extract, "run_extraction", return_value=result), \
                 patch.object(jobs, "enqueue_alignment") as alignment:
                jobs._run_job(job, store=self.onto, conv_store=self.convs, choose_provider=lambda: self.local, managed=True)
            self.assertEqual(alignment.call_count, int(expected))
            if expected:
                self.assertEqual(alignment.call_args.args[:2], (self.cid, answer["id"]))

    def test_policy_rechecked_after_extraction_before_alignment_enqueue(self):
        job, _ = self.message_job()
        self.convs.append_message(self.cid, "assistant", "合成回复。")
        mode = {"automatic": True}
        def finish(**_kwargs):
            mode["automatic"] = False  # user changed the setting while model was busy
            return {"created": ["working-1"], "promoted": []}
        with patch("mindos.zhijun.memory.extraction_allowed", return_value=True), \
             patch("mindos.zhijun.memory.automatic_allowed", side_effect=lambda *_args: mode["automatic"]), \
             patch.object(jobs.extract, "run_extraction", side_effect=finish), \
             patch.object(jobs, "enqueue_alignment") as alignment:
            jobs._run_job(job, store=self.onto, conv_store=self.convs, choose_provider=lambda: self.local, managed=True)
        alignment.assert_not_called()

    def test_queued_automatic_calibration_and_first_observation_respect_manual_mode(self):
        for kind in ("alignment", "first_observation"):
            with self.subTest(kind=kind), \
                 patch("mindos.zhijun.memory.automatic_allowed", return_value=False), \
                 patch("mindos.zhijun.alignment.run_job") as alignment, \
                 patch.object(jobs.extract, "first_observation") as observation, \
                 patch("mindos.zhijun.routing.Router.provider") as choose, \
                 patch.object(jobs.provider_gate, "acquire") as gate:
                result = jobs.run_job({"kind": kind, "payload": {"conversationId": self.cid}}, store=self.onto, conv_store=self.convs)
                self.assertEqual(result["reason"], "memory_policy")
                alignment.assert_not_called()
                observation.assert_not_called()
                choose.assert_not_called()
                gate.assert_not_called()

    def test_old_onboarding_does_not_queue_first_observation_in_manual_mode(self):
        cid = self.convs.create_conversation(mode="onboarding")["id"]
        for _ in ONBOARDING_QUESTIONS:
            self.convs.append_message(cid, "user", "合成的初始认识回答。")
        with patch("mindos.zhijun.memory.automatic_allowed", return_value=False), \
             patch("mindos.zhijun.memory.extraction_allowed", return_value=False), \
             patch.object(jobs, "enqueue_first_observation") as first:
            list(run_turn(cid, "我想先开始使用，以后再慢慢补充。", provider=self.local, conv_store=self.convs, ontology=self.onto))
        first.assert_not_called()

    def test_old_protected_conversation_does_not_queue_alignment_each_reply(self):
        AlignmentStore(self.onto).status(self.cid, local_only=True, status="paused")
        with patch("mindos.zhijun.memory.automatic_allowed", return_value=True), \
             patch("mindos.zhijun.memory.extraction_allowed", return_value=True), \
             patch.object(jobs, "enqueue_alignment") as alignment:
            events = list(run_turn(self.cid, "我想继续聊聊目前这个合成项目的安排。", provider=self.local, conv_store=self.convs, ontology=self.onto))
        alignment.assert_not_called()
        self.assertEqual(next(data for event, data in events if event == "extraction")["reason"], "private_profile")


if __name__ == "__main__":
    unittest.main()
