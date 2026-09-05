"""Task evidence routing at the actual provider boundary, entirely synthetic."""
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from tests import test_task_routing as harness
from mindos.zhijun.context_bridge import attach_task_context
from mindos.zhijun.provider import ChatRequest
from mindos.zhijun.routing import GuardedProvider, Router, service_info, task_provider


PURPOSES = ("reply_assistance", "draft_turn", "decision_suggestions", "learning")


class ContextBridgeTests(unittest.TestCase):
    setUp = harness.RoutingTests.setUp
    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable
    claim = harness.RoutingTests.claim

    def request(self):
        return ChatRequest(system="合成任务：帮助用户表达，不替用户确认长期画像。",
            messages=[{"role": "user", "content": "星桥项目"}], debug={"userText": "星桥项目"})

    def test_four_task_paths_require_own_purpose_and_send_effective_context_once(self):
        self.enable()
        claim = self.claim()
        router = Router(self.onto, self.convs, self.cid)
        sources = router.resolve(router.ref("claim", claim["id"]))
        self.store.grant(router.scope, sources, service_info(self.online)["id"], "chat")
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[{**claim, "score": .9}]):
            for purpose in PURPOSES:
                with self.subTest(purpose=purpose):
                    request = self.request()
                    _, pending = task_provider(router, purpose, request, [], preview_only=True)
                    self.assertIn("claim:" + claim["id"], pending["missing"], "chat consent is not a new task grant")
                    count = len(self.online.requests)
                    with self.assertRaises(HTTPException):
                        task_provider(router, purpose, request, [], revision=pending["revision"])
                    self.assertEqual(len(self.online.requests), count)
                    self.store.grant(router.scope, pending["sources"], service_info(self.online)["id"], purpose)
                    _, approved = task_provider(router, purpose, request, [], preview_only=True)
                    self.assertEqual(approved["missing"], [])
                    guard, _ = task_provider(router, purpose, request, [], revision=approved["revision"])
                    guard.complete_json(request)
                    actual = self.online.requests[-1]
                    self.assertIn(claim["content"], actual.system)
                    self.assertEqual(actual.system.count("## 本轮实际提供的个人上下文与证据"), 1)
                    plan = actual.debug["contextPlan"]
                    self.assertEqual(plan["providedRefs"], [i["citationId"] for i in plan["background"] + plan["evidence"]])
                    self.assertIn(claim["id"], [r["id"] for r in guard.refs])
        self.assertEqual(self.convs.list_messages(self.cid), [], "candidate task context is not a user message")
        self.assertEqual(self.onto.get_claim(claim["id"])["trustState"], "confirmed")

    def test_attach_is_idempotent_and_does_not_mutate_original_request(self):
        router = Router(self.onto, self.convs, self.cid)
        claim = self.claim()
        original = self.request()
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[{**claim, "score": .9}]):
            once, refs = attach_task_context(router, "reply_assistance", original, [], self.local)
            twice, new_refs = attach_task_context(router, "reply_assistance", once, refs, self.local)
        self.assertEqual(twice, once)
        self.assertEqual(new_refs, refs)
        self.assertNotIn("contextPlan", original.debug)
        self.assertNotIn(claim["content"], original.system)

    def test_default_omit_excludes_denied_text_from_each_task_actual_request(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        claim = self.claim()
        self.store.set_handling(router.scope, enabled=True, action="omit", service=service_info(self.online)["id"], expected_revision=0)
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[{**claim, "score": .9}]):
            for purpose in PURPOSES:
                with self.subTest(purpose=purpose):
                    request = self.request()
                    _, preview = task_provider(router, purpose, request, [], preview_only=True)
                    self.assertEqual(preview["missing"], [])
                    guard, _ = task_provider(router, purpose, request, [], revision=preview["revision"])
                    guard.complete_json(request)
                    self.assertNotIn(claim["content"], self.online.requests[-1].system)
                    self.assertNotIn(claim["id"], [r["id"] for r in guard.refs])

    def test_cached_local_context_rejects_changed_source_version(self):
        router = Router(self.onto, self.convs, self.cid)
        claim = self.claim()
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[{**claim, "score": .9}]):
            request, refs = attach_task_context(router, "learning", self.request(), [], self.local)
        self.onto.add_evidence(claim["id"], [{"kind": "user_edit", "quote": "新增依据改变来源版本"}])
        with self.assertRaises(HTTPException) as raised:
            GuardedProvider(router, self.local, "learning", refs).complete_json(request)
        self.assertEqual(raised.exception.detail["code"], "SOURCE_CHANGED")
        self.assertEqual(self.local.requests, [])

    def test_cached_online_context_rechecks_revocation_and_service(self):
        self.enable()
        router = Router(self.onto, self.convs, self.cid)
        claim = self.claim()
        sources = router.resolve(router.ref("claim", claim["id"]))
        self.store.grant(router.scope, sources, service_info(self.online)["id"], "decision_suggestions")
        with patch("mindos.zhijun.memory_retrieval.retrieve_claims", return_value=[{**claim, "score": .9}]):
            request, refs = attach_task_context(router, "decision_suggestions", self.request(), [], self.online)
            preview = router.prepare("decision_suggestions", request, refs, self.online)
        self.store.revoke(router.scope, "claim:" + claim["id"])
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "decision_suggestions", refs, revision=preview["revision"]).complete_json(request)
        self.online._base_url = "https://another-synthetic.invalid/v1"
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "decision_suggestions", refs, revision=preview["revision"]).complete_json(request)
        self.assertEqual(self.online.requests, [])

    def test_task_focus_does_not_read_past_retry_upper_bound(self):
        router = Router(self.onto, self.convs, self.cid)
        self.convs.append_message(self.cid, "user", "我想找人合作做星桥项目", meta={"routingSources": []})
        current = self.convs.append_message(self.cid, "user", "还有呢", meta={"routingSources": []})
        self.convs.append_message(self.cid, "user", "星桥项目未来需要处理 FUTURE_SECRET", meta={"routingSources": []})
        router.context_before_seq = current["seq"]
        request = ChatRequest(system="合成任务", messages=[{"role": "user", "content": "还有呢"}], debug={"userText": "还有呢"})
        effective, refs = attach_task_context(router, "reply_assistance", request, [], self.local)
        self.assertNotIn("FUTURE_SECRET", effective.system)
        self.assertNotIn(current["id"], [r["id"] for r in refs])


if __name__ == "__main__":
    unittest.main()
