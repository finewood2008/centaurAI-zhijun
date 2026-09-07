"""Independent synthetic checks for multi-hop work-product provenance and races."""
from concurrent.futures import ThreadPoolExecutor
import json
import unittest

from fastapi import HTTPException

from tests import test_matters as fixture
from mindos.stores.ontology_store import OntologyConflictError
from mindos.zhijun.provider import ChatRequest
from mindos.zhijun.routing import GuardedProvider, Router, service_info


class MattersIndependentTests(unittest.TestCase):
    setUp = fixture.MattersTests.setUp
    tearDown = fixture.MattersTests.tearDown
    enable = fixture.MattersTests.enable
    create = fixture.MattersTests.create
    artifact = fixture.MattersTests.artifact
    grant = fixture.MattersTests.grant

    def default_policy(self):
        with self.onto._connect() as db:
            db.execute("INSERT INTO routing_auto_consent(scope,enabled,service,service_name,include_files,purposes_json,revision,updated_at) VALUES('global',1,?,'合成服务',1,?,1,'2026-01-01')",
                       (service_info(self.online)["id"], json.dumps(["chat", "summarize_conversation", "extract_turn"])))

    def test_new_artifact_cannot_inherit_an_old_default_or_original_online_mode(self):
        self.enable()
        self.default_policy()
        matter, _ = self.create()
        product, _ = self.artifact(matter, meta={"routingSources": [], "routingOrigin": {"service": service_info(self.online)["id"]}})
        router = Router(self.onto, self.convs, self.cid)
        ref = router.ref("artifact", product["id"])
        request = ChatRequest(system=product["markdown"], messages=[])
        preview = router.prepare("chat", request, [ref], self.online)
        self.assertIn("artifact:" + product["id"], preview["missing"])
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", [ref], revision=preview["revision"]).complete_json(request)
        self.assertEqual(self.online.requests, [])

    def test_chat_grant_does_not_authorize_background_summary_or_derived_summary(self):
        self.enable()
        self.default_policy()
        matter, _ = self.create()
        product, _ = self.artifact(matter)
        router = Router(self.onto, self.convs, self.cid)
        source = router.resolve(router.ref("artifact", product["id"]))[0]["ref"]
        self.grant(router, [source], "chat")
        derived = self.convs.append_message(self.cid, "assistant", "基于成果的合成回答", meta={"routingSources": [source], "routingOrigin": {"service": service_info(self.online)["id"]}})
        message_source = router.resolve(router.ref("message", derived["id"]))[0]["ref"]
        request = ChatRequest(system="汇总已授权的内容", messages=[{"role": "assistant", "content": derived["content"]}])
        for purpose in ("summarize_conversation", "extract_turn"):
            with self.assertRaises(HTTPException):
                GuardedProvider(router, self.online, purpose, [message_source], background=True).complete_json(request)
        summary = self.convs.save_summary(self.cid, up_to_seq=derived["seq"], summary="成果的派生摘要", key_points=[], generated_by="synthetic", meta={"routingSources": [message_source]})
        summary_ref = router.ref("summary", f"{self.cid}:{summary['revision']}")
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "extract_turn", [summary_ref], background=True).complete_json(request)
        self.assertEqual(self.online.requests, [])
        self.grant(router, [source], "summarize_conversation")
        GuardedProvider(router, self.online, "summarize_conversation", [message_source], background=True).complete_json(request)
        self.assertEqual(len(self.online.requests), 1)

    def test_two_hop_edited_product_keeps_stale_ancestor_block(self):
        self.enable()
        matter, _ = self.create()
        first, _ = self.artifact(matter)
        router = Router(self.onto, self.convs, self.cid)
        first_source = router.resolve(router.ref("artifact", first["id"]))[0]["ref"]
        derived = self.convs.append_message(self.cid, "assistant", "第二份成果", meta={"routingSources": [first_source]})
        response = self.client.post(self.base + f"/{matter['id']}/artifacts", json={"requestId": "save-second-product", "conversationId": self.cid, "messageId": derived["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        second = response.json()
        edited = self.work.edit_artifact(second["id"], "global", {"markdown": "用户重新编辑后的第二份成果"}, 1, "edit-second-product")
        self.assertEqual(edited["sources"], second["sources"])
        self.grant(router, [router.ref("artifact", second["id"])])
        self.work.edit_artifact(first["id"], "global", {"markdown": "源成果已改版"}, 1, "change-first-product")
        refs = [router.ref("artifact", second["id"])]
        closure = router.resolve(refs[0])
        self.assertTrue(any(s["key"] == "artifact:" + first["id"] and s["blocked"] for s in closure))
        with self.assertRaises(HTTPException):
            GuardedProvider(router, self.online, "chat", refs).complete_json(ChatRequest(system=edited["markdown"], messages=[]))
        self.assertEqual(self.online.requests, [])

    def test_parallel_edits_have_one_winner_and_exact_idempotent_snapshot(self):
        matter, _ = self.create()
        product, _ = self.artifact(matter)

        def edit(number):
            text, request_id = f"合成编辑 {number}", f"parallel-edit-{number}"
            try:
                value = self.work.edit_artifact(product["id"], "global", {"markdown": text}, 1, request_id)
                return value, text, request_id
            except OntologyConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, (1, 2)))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        value, text, request_id = winners[0]
        self.assertEqual(value["revision"], 2)
        self.assertEqual(self.work.edit_artifact(product["id"], "global", {"markdown": text}, 1, request_id), value)
        self.assertEqual(len(self.work.history("artifact", product["id"], "global")), 2)
        self.assertEqual(self.work.artifact(product["id"], "global"), value)


if __name__ == "__main__":
    unittest.main()
