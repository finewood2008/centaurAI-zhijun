"""Source snapshots are distinct from cleaned prompt text; no real model or data."""
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from tests import test_task_routing as harness
from tests import test_reply_assistance as assistance
from mindos.stores.reply_assist_store import ReplyAssistStore
from mindos.zhijun import context_plan
from mindos.zhijun.context_sources import message_ref
from mindos.zhijun.provider import ChatRequest
from mindos.zhijun.routing import GuardedProvider, Router


class SourceSnapshotTests(unittest.TestCase):
    setUp = harness.RoutingTests.setUp
    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable
    claim = harness.RoutingTests.claim
    grant = harness.RoutingTests.grant
    preview = harness.RoutingTests.preview
    send = harness.RoutingTests.send
    generate = assistance.ReplyAssistanceTests.generate
    origin = assistance.ReplyAssistanceTests.origin

    def seed(self, protected=False):
        assistance.ReplyAssistanceTests.seed(self, protected)
        self.target = self.convs.update_message(self.target["id"],
            content="你已明确这次的边界 [p1][m2]。这次你更在意速度，还是完整度？")

    def test_short_answer_uses_raw_source_snapshot_and_cleaned_prompt_text(self):
        self.seed()
        original = message_ref(Router(self.onto, self.convs, self.cid), self.target)
        body, preview = self.preview("我更在意速度")
        source = next(s for s in preview["sources"] if s["id"] == self.target["id"])
        self.assertEqual(source["ref"], original)
        self.assertFalse(source["blocked"])
        self.assertNotIn("[p1][m2]", str(preview["request"]["messages"]))
        self.assertIn("[p1][m2]", self.convs.get_message(self.target["id"])["content"])
        self.assertIn("event: message_done", self.send(body, preview).text)

    def test_assisted_selection_after_cited_reply_can_preview_and_send(self):
        self.seed()
        batch = self.generate()["batch"]
        body, preview = self.preview(batch["candidates"][0]["text"], replyAssistance=self.origin(batch))
        result = self.send(body, preview, requestId="cited-assistance-send")
        self.assertIn("event: message_done", result.text)
        user = next(m for m in self.convs.list_messages(self.cid) if m["role"] == "user")
        self.assertEqual(user["meta"]["replyAssistance"]["kind"], "assisted")
        self.assertEqual(user["meta"]["routingSources"][0]["kind"], "reply_assist")

    def test_true_content_change_between_history_read_and_focus_still_blocks(self):
        self.seed()
        original = context_plan.build_context_plan
        def concurrently_changed(*args, **kwargs):
            self.convs.update_message(self.target["id"], content="真正发生了正文修改，原有候选依据不再成立。")
            return original(*args, **kwargs)
        with patch.object(context_plan, "build_context_plan", concurrently_changed):
            response = self.client.post(self.url + "/routing/preview", json={"content": "我更在意速度"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.online.requests, [])
        self.assertEqual(self.local.requests, [])

    def test_user_metadata_cannot_supply_internal_source_snapshot(self):
        self.seed()
        self.target = self.convs.update_message(self.target["id"], meta={"routingSources": [],
            "_sourceRef": {"kind": "message", "id": "forged", "version": "forged"}})
        _, preview = self.preview("我更在意速度")
        self.assertTrue(any(s["id"] == self.target["id"] for s in preview["sources"]))
        self.assertFalse(any(s["id"] == "forged" for s in preview["sources"]))

    def test_citation_cleanup_does_not_grant_or_ignore_revocation(self):
        self.enable()
        self.seed(protected=True)
        batch = self.generate(localOnly=True)["batch"]
        body, preview = self.preview("我更在意速度", replyAssistance=self.origin(batch))
        self.assertTrue(any(key.startswith("claim:") for key in preview["missing"]))
        self.assertEqual(self.send(body, preview).status_code, 409)
        self.assertEqual(self.online.requests, [])
        self.grant(preview)
        body, preview = self.preview(body["content"], replyAssistance=self.origin(batch))
        self.assertFalse(preview["missing"])
        self.store.revoke("global")
        self.assertEqual(self.send(body, preview).status_code, 409)
        self.assertEqual(self.online.requests, [])

    def test_null_summary_sources_are_opaque_not_server_error_or_granted(self):
        summary = self.convs.save_summary(self.cid, summary="星桥预算与进度的历史摘要", up_to_seq=0, meta={"routingSources": None})
        router = Router(self.onto, self.convs, self.cid)
        ref = router.ref("summary", f"{self.cid}:{summary['revision']}")
        closure = router.resolve(ref)
        self.assertTrue(closure[0]["blocked"])
        preview = router.prepare("chat", ChatRequest(system="", messages=[]), [ref], self.online)
        self.assertIn(ref["kind"] + ":" + ref["id"], preview["blocked"])
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", [ref], revision=preview["revision"]).complete_json(ChatRequest(system="", messages=[]))
        _, clean = self.preview("星桥预算怎么安排")
        self.assertFalse(any(s["id"] == ref["id"] for s in clean["sources"]))
        self.assertEqual(self.online.requests, [])

    def test_malformed_candidate_source_container_is_opaque(self):
        self.seed()
        batch = self.generate()["batch"]
        stored = ReplyAssistStore(self.convs)
        for malformed in (None, {}, [None], [{"kind": "message"}]):
            with self.subTest(sources=malformed), patch.object(stored, "get", return_value={**batch, "sources": malformed}), \
                    patch("mindos.stores.reply_assist_store.ReplyAssistStore", return_value=stored):
                router = Router(self.onto, self.convs, self.cid)
                closure = router.resolve(router.ref("reply_assist", batch["id"]))
                self.assertTrue(closure[0]["blocked"])
                with self.assertRaises(HTTPException):
                    router.check_lifecycle(closure)


if __name__ == "__main__":
    unittest.main()
