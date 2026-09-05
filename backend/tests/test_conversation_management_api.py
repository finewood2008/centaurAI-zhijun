"""Conversation organization changes neither personal knowledge nor consent."""
from types import SimpleNamespace
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests import test_task_routing as harness
from mindos import conversations
from mindos.stores.growth_store import GrowthStore
from mindos.zhijun.provider import FakeProvider
from mindos.zhijun.turn import run_turn


class ConversationManagementApiTests(unittest.TestCase):
    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable
    preview = harness.RoutingTests.preview
    send = harness.RoutingTests.send
    claim = harness.RoutingTests.claim

    def setUp(self):
        harness.RoutingTests.setUp(self)
        app = FastAPI()

        @app.middleware("http")
        async def device_context(request, call_next):
            request.state.mindos_device_context = SimpleNamespace(device_id=request.headers.get("x-test-device"))
            return await call_next(request)

        app.include_router(conversations._build_router())
        self.client = TestClient(app)

    def update(self, cid=None, revision=None, device=None, **fields):
        cid = cid or self.cid
        if revision is None:
            revision = self.convs.get_conversation(cid)["metadataRevision"]
        return self.client.patch(f"/api/mindos/conversations/{cid}", json={"expectedRevision": revision, **fields},
                                 headers={"x-test-device": device} if device else {})

    def listing(self, **params):
        response = self.client.get("/api/mindos/conversations", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_title_validation_and_manual_title_survives_next_user_message(self):
        for body in ({"expectedRevision": 0}, {"expectedRevision": 0, "title": " "},
                     {"expectedRevision": 0, "title": "长" * 81}, {"expectedRevision": 0, "title": None},
                     {"expectedRevision": 0, "pinned": "true"}, {"expectedRevision": 0, "status": None}):
            self.assertEqual(self.client.patch(self.url, json=body).status_code, 422)
        named = self.update(title="  合成项目复盘  ")
        self.assertEqual(named.status_code, 200, named.text)
        self.assertEqual(named.json()["title"], "合成项目复盘")
        self.convs.append_message(self.cid, "user", "新消息不改标题")
        restored = self.client.get(self.url).json()["conversation"]
        self.assertEqual(restored["title"], "合成项目复盘")

    def test_conflicting_rename_and_duplicate_archive_are_safe(self):
        first = self.update(revision=0, title="第一个名称")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.update(revision=0, title="另一个名称").status_code, 409)
        archived = self.update(status="archived")
        repeated = self.update(revision=first.json()["metadataRevision"], status="archived")
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["metadataRevision"], archived.json()["metadataRevision"])
        self.assertEqual(repeated.json()["title"], "第一个名称")

    def test_lists_are_paged_and_total_is_not_the_page_length(self):
        for i in range(54):
            self.convs.create_conversation(title=f"合成对话 {i}")
        first = self.listing(limit=30)
        second = self.listing(limit=30, offset=30)
        self.assertEqual(first["total"], 55)
        self.assertEqual(len(first["items"]), 30)
        self.assertTrue(first["hasMore"])
        self.assertEqual(len(second["items"]), 25)
        self.assertFalse(second["hasMore"])
        self.assertFalse({c["id"] for c in first["items"]} & {c["id"] for c in second["items"]})
        self.assertEqual(self.listing(offset=90)["items"], [])
        self.assertEqual(len(self.listing(limit=200)["items"]), 55)

    def test_invalid_filters_and_page_bounds_fail_before_query(self):
        for params in ({"status": "deleted"}, {"limit": 0}, {"limit": 201},
                       {"offset": -1}, {"offset": 2**63}, {"q": "长" * 101}):
            response = self.client.get("/api/mindos/conversations", params=params)
            self.assertEqual(response.status_code, 422, response.text)

    def test_archive_filters_keep_pin_and_direct_read_does_not_restore(self):
        self.update(pinned=True)
        before = self.convs.get_conversation(self.cid)
        self.update(status="archived")
        self.assertEqual(self.listing()["total"], 0)
        archived = self.listing(status="archived")["items"][0]
        self.assertEqual(archived["pinnedAt"], before["pinnedAt"])
        read = self.client.get(self.url).json()["conversation"]
        self.assertEqual(read["status"], "archived")
        self.update(status="active")
        self.assertEqual(self.listing()["items"][0]["pinnedAt"], before["pinnedAt"])

    def test_search_all_records_and_archived_body_returns_message_location(self):
        match = self.convs.append_message(self.cid, "user", "我之前讨论过合成独特线索，可以通过正文找回")
        self.update(title="没有搜索词的标题", status="archived")
        for i in range(55):
            self.convs.create_conversation(title=f"干扰项 {i}")
        result = self.listing(status="all", q="合成独特线索")
        self.assertEqual(result["total"], 1)
        found = result["items"][0]
        self.assertEqual(found["id"], self.cid)
        self.assertEqual(found["status"], "archived")
        self.assertEqual(found["searchMatch"]["field"], "message")
        self.assertEqual(found["searchMatch"]["messageId"], match["id"])
        self.assertIn("合成独特线索", found["searchMatch"]["snippet"])
        self.assertEqual(self.listing(q="合成独特线索")["total"], 0)
        self.assertEqual(self.online.requests + self.local.requests, [])

    def test_search_does_not_match_system_notes_and_handles_literal_wildcards(self):
        self.convs.append_message(self.cid, "system", "系统内部标记秘钥")
        self.convs.append_message(self.cid, "assistant", "案例折扣 20%_literal，保留为文字 <script>alert(1)</script>")
        self.assertEqual(self.listing(q="系统内部标记秘钥")["total"], 0)
        found = self.listing(q="20%_literal")["items"]
        self.assertEqual(len(found), 1)
        self.assertIn("20%_literal", found[0]["searchMatch"]["snippet"])
        self.assertEqual(self.listing(q="20%nomatch")["total"], 0)

    def test_other_device_cannot_list_read_or_modify_conversation(self):
        self.convs.append_message(self.cid, "user", "独立设备的内容")
        headers = {"x-test-device": "other"}
        for path in ("/api/mindos/conversations?status=all", "/api/mindos/conversations?status=all&q=独立设备"):
            response = self.client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], 0)
        self.assertEqual(self.client.get(self.url, headers=headers).status_code, 404)
        for fields in ({"title": "恶意改名"}, {"status": "archived"}, {"pinned": True}):
            self.assertEqual(self.update(device="other", **fields).status_code, 404)

    def test_patch_is_covered_by_the_existing_write_guard(self):
        def deny():
            raise HTTPException(403, "write denied")
        app = FastAPI()
        app.include_router(conversations._build_router(deny))
        response = TestClient(app).patch(self.url, json={"expectedRevision": 0, "title": "不允许改名"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.convs.get_conversation(self.cid)["metadataRevision"], 0)

    def test_new_message_restores_but_validation_failure_and_background_reply_do_not(self):
        self.update(status="archived")
        self.assertEqual(self.client.post(self.url + "/messages", json={"content": ""}).status_code, 400)
        self.assertEqual(self.convs.get_conversation(self.cid)["status"], "archived")
        self.convs.append_message(self.cid, "assistant", "后台迟到的回复")
        self.assertEqual(self.convs.get_conversation(self.cid)["status"], "archived")
        before = self.convs.get_conversation(self.cid)["metadataRevision"]
        body, preview = self.preview("继续聊合成的普通问题")
        response = self.send(body, preview)
        self.assertEqual(response.status_code, 200, response.text)
        current = self.convs.get_conversation(self.cid)
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["metadataRevision"], before + 1)

    def test_unapproved_send_never_restores_or_saves_new_message(self):
        claim = self.claim()
        self.enable()
        self.update(status="archived")
        before = self.convs.get_conversation(self.cid)
        body, preview = self.preview("星桥项目为什么迟迟不想推进？")
        self.assertIn("claim:" + claim["id"], preview["missing"])
        self.assertEqual(self.send(body, preview).status_code, 409)
        after = self.convs.get_conversation(self.cid)
        self.assertEqual(after, before)
        self.assertEqual(self.online.requests, [])

    def test_archive_during_generation_does_not_stop_reply_or_restore_on_retry(self):
        stream = run_turn(self.cid, "合成问题", conv_store=self.convs, ontology=self.onto,
                          provider=FakeProvider(), request_id="management-stream-1")
        first = next(stream)
        self.assertEqual(first[0], "meta")
        self.assertEqual(self.update(status="archived").status_code, 200)
        remaining = list(stream)
        self.assertTrue(any(name == "message_done" for name, _ in remaining))
        self.assertEqual(self.convs.get_conversation(self.cid)["status"], "archived")
        original_count = self.convs.get_conversation(self.cid)["messageCount"]
        repeated = list(run_turn(self.cid, "合成问题", conv_store=self.convs, ontology=self.onto,
                                provider=FakeProvider(), request_id="management-stream-1"))
        self.assertTrue(repeated[0][1].get("replayed"))
        self.assertEqual(self.convs.get_conversation(self.cid)["status"], "archived")
        self.assertEqual(self.convs.get_conversation(self.cid)["messageCount"], original_count)

    def test_archived_review_reused_without_cross_device_session_leak(self):
        decision = GrowthStore.instance().create_decision({"title": "合成判断", "context": "测试",
            "options": ["a", "b"], "choice": "a", "rationale": "测试", "confidence": 50,
            "expectedOutcome": "验证选择", "relatedEntityIds": [], "evidenceRefs": []})
        body = {"mode": "review", "decisionId": decision["id"]}
        first = self.client.post("/api/mindos/conversations", json=body).json()
        self.update(first["id"], status="archived")
        again = self.client.post("/api/mindos/conversations", json=body).json()
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["reused"])
        self.assertEqual(again["status"], "archived")
        other = self.client.post("/api/mindos/conversations", json=body, headers={"x-test-device": "other"}).json()
        self.assertNotEqual(other["id"], first["id"])


if __name__ == "__main__":
    unittest.main()
