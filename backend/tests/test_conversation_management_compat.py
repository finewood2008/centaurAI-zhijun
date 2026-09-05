"""Conversation organization must not reset progress, forget, or grant consent."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import conversations, zhijun_home, zhijun_onboarding
from mindos.stores import conversation_store, growth_store, ontology_store
from mindos.stores.routing_store import RoutingStore
from mindos.zhijun import jobs
from mindos.zhijun.routing import Router, service_info
from tests.test_task_routing import Recording


class ConversationManagementCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        root = Path(self.tmp.name)
        self.onto = ontology_store.reset_for_tests(root / "ontology.db")
        self.convs = conversation_store.reset_for_tests(root / "conversations.db")
        self.growth = growth_store.reset_for_tests(root / "growth.db")
        self.routing = RoutingStore(self.onto)
        self.local = Recording(False)
        self.stack.enter_context(patch("mindos.zhijun.routing.local_provider", return_value=self.local))
        self.stack.enter_context(patch("mindos.zhijun.provider._open", side_effect=AssertionError("No network in synthetic tests")))
        self.now = datetime(2026, 9, 4, 8, tzinfo=timezone.utc)
        app = FastAPI()

        @app.middleware("http")
        async def device_scope(request, call_next):
            request.state.mindos_device_context = SimpleNamespace(device_id=request.headers.get("x-test-device"))
            return await call_next(request)

        app.include_router(conversations.router)
        app.include_router(zhijun_onboarding.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def conversation(self, *, days_ago=0, **kwargs):
        with patch.object(conversation_store, "utc_now", return_value=(self.now - timedelta(days=days_ago)).isoformat()):
            return self.convs.create_conversation(**kwargs)

    def metadata(self, conversation_id, **changes):
        return self.convs.update_metadata(conversation_id, expected_revision=self.convs.get_conversation(conversation_id)["metadataRevision"], **changes)

    def home(self, *, enqueue=False):
        return zhijun_home.build_home_overview(now=self.now, enqueue=enqueue, ontology=self.onto, conversations=self.convs, growth=self.growth)

    def confirmed_claim(self, conversation_id=None, message_id=None):
        return self.onto.create_claim(
            {"content": "合成案例：重要决定前先核对事实", "section": "principles", "layer": "self_declared"},
            [{"kind": "conversation_turn" if conversation_id else "user_edit", "quote": "重要决定前先核对事实",
              "conversation_id": conversation_id, "message_id": message_id}],
            trust_state="confirmed", trust_origin="user_created")

    def test_archived_old_first_meeting_migrates_and_resumes_same_conversation(self):
        initial = self.conversation(days_ago=30, mode="onboarding", title="第一次认识")
        self.convs.append_message(initial["id"], "assistant", "先聊聊眼下的处境。", meta={"kind": "onboarding_open"})
        self.convs.append_message(initial["id"], "user", "我在做一个合成项目。")
        self.metadata(initial["id"], status="archived")
        before_messages = self.convs.list_messages(initial["id"])

        progress = self.client.get("/api/mindos/zhijun/onboarding").json()
        self.assertEqual((progress["state"], progress["conversationId"], progress["migrated"]),
                         ("profile_building", initial["id"], True))
        for _ in range(2):
            response = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "start"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["conversationId"], initial["id"])
        self.assertEqual(len(self.convs.list_conversations(status="all")), 1)
        self.assertEqual(self.convs.list_messages(initial["id"]), before_messages)
        self.assertEqual(self.local.requests, [])

    def test_archived_first_meeting_beyond_recent_fifty_still_counts(self):
        initial = self.conversation(days_ago=60, mode="onboarding")
        self.metadata(initial["id"], status="archived")
        for index in range(55):
            self.conversation(days_ago=index)
        self.assertNotIn(initial["id"], [c["id"] for c in self.convs.list_conversations(limit=50, status="all")])
        progress = zhijun_onboarding.get_progress(ontology=self.onto, conversations=self.convs)
        self.assertEqual((progress["state"], progress["conversationId"]), ("profile_building", initial["id"]))
        home = self.home(enqueue=True)
        self.assertEqual(home["state"], "building")
        self.assertEqual(home["nextAction"]["targetId"], initial["id"])
        self.assertEqual(home["map"]["relationshipDays"], 61)
        self.assertEqual(self.onto.pending_jobs(), 0)

    def test_archived_completed_legacy_onboarding_does_not_restart(self):
        initial = self.conversation(mode="onboarding")
        self.convs.append_message(initial["id"], "assistant", "初次认识。", meta={"kind": "onboarding_open"})
        for index in range(7):
            self.convs.append_message(initial["id"], "user", f"这是第 {index + 1} 个合成回答。")
        self.metadata(initial["id"], status="archived")
        progress = zhijun_onboarding.get_progress(ontology=self.onto, conversations=self.convs)
        self.assertEqual((progress["state"], progress["conversationId"]), ("ready", initial["id"]))
        self.assertIsNotNone(progress["completedAt"])
        self.assertEqual(len(self.convs.list_conversations(status="all")), 1)

    def test_archive_preserves_home_relationship_and_onboarding_action(self):
        initial = self.conversation(days_ago=30, mode="onboarding")
        self.conversation(days_ago=1)
        before = self.home()
        self.metadata(initial["id"], status="archived")
        after = self.home(enqueue=True)
        for key in ("state", "map", "nextAction", "sourceHash"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["map"]["relationshipDays"], 31)
        self.assertEqual(self.onto.pending_jobs(), 0)

    def test_metadata_keeps_home_cache_hash_and_does_not_enqueue_model_work(self):
        self.confirmed_claim()
        self.conversation(days_ago=30)
        recent = self.conversation(days_ago=2)
        original = self.convs.get_conversation(recent["id"])
        before = self.home()
        self.onto.meta_set("zhijun_home_snapshot_v1", json.dumps({"sourceHash": before["sourceHash"], "brief": before["brief"]}))
        with patch.object(jobs, "enqueue_home_brief") as enqueue:
            for change in ({"title": "合成会话的新名字"}, {"pinned": True}, {"status": "archived"},
                           {"status": "active"}, {"pinned": False}):
                with self.subTest(change=change):
                    changed = self.metadata(recent["id"], **change)
                    self.assertEqual(changed["updatedAt"], original["updatedAt"])
                    self.assertEqual(changed["lastMessageAt"], original["lastMessageAt"])
                    after = self.home(enqueue=True)
                    self.assertEqual(after["sourceHash"], before["sourceHash"])
                    self.assertEqual(after["brief"]["status"], "ready")
                    self.assertEqual(after["brief"]["headline"], before["brief"]["headline"])
            enqueue.assert_not_called()
        self.assertEqual(self.local.requests, [])

    def test_organization_keeps_memories_grants_pending_tasks_and_followup(self):
        conv = self.conversation()
        cid = conv["id"]
        message = self.convs.append_message(cid, "user", "重要决定前先核对事实。")
        claim = self.confirmed_claim(cid, message["id"])
        self.routing.set_mode(cid, "local", "")
        source = {"key": "claim:" + claim["id"], "version": "synthetic-version"}
        self.routing.grant("global", [source], "synthetic-service", "chat")
        self.routing.pending(cid, "extract_turn", "synthetic-preview", "等待原有授权")
        queued = jobs.enqueue_summary(cid, store=self.onto)
        nudge = self.convs.create_nudge(kind="checkin", trigger_key="management-compat", trigger_ref={"conversationId": cid},
                                      why_now="约好的合成回访", message="回看之前的安排", scheduled_for=self.now.isoformat(), now=self.now.isoformat())
        snapshots = (self.convs.list_messages(cid), self.onto.get_claim(claim["id"]), self.routing.mode(cid),
                     self.routing.pending(cid), self.onto.get_job(queued), self.convs.get_nudge(nudge["id"]))
        for change in ({"title": "合成记录"}, {"pinned": True}, {"status": "archived"}):
            self.metadata(cid, **change)
        self.assertEqual((self.convs.list_messages(cid), self.onto.get_claim(claim["id"]), self.routing.mode(cid),
                          self.routing.pending(cid), self.onto.get_job(queued), self.convs.get_nudge(nudge["id"])), snapshots)
        self.assertTrue(self.routing.granted("global", source, "synthetic-service", "chat"))
        self.assertFalse(self.routing.granted("global", source, "other-service", "chat"))
        self.assertEqual(self.onto.pending_jobs_for_conversation(cid), 1)
        self.assertEqual(zhijun_home._next_action("established", None, [], [], self.convs, self.now)["targetId"], nudge["id"])

    def test_archived_summary_job_still_runs_through_original_local_router(self):
        conv = self.conversation()
        cid = conv["id"]
        self.routing.set_mode(cid, "local", "")
        self.convs.append_message(cid, "user", "合成案例：希望下周先完成一次验证。", meta={"routingSources": []})
        queued = jobs.enqueue_summary(cid, store=self.onto)
        self.metadata(cid, status="archived")
        result = jobs.run_job(self.onto.get_job(queued), store=self.onto, conv_store=self.convs)
        self.assertEqual(result["state"], "done")
        self.assertEqual(len(self.local.requests), 1)
        self.assertIn("希望下周先完成一次验证", self.local.requests[0].messages[0]["content"])
        self.assertEqual(self.routing.mode(cid)["mode"], "local")
        self.assertEqual(self.convs.get_conversation(cid)["status"], "archived")

    def test_archive_cannot_authorize_protected_history_for_background_summary(self):
        cid = self.conversation()["id"]
        claim = self.confirmed_claim()
        online = Recording()
        service = service_info(online)["id"]
        self.routing.set_mode(cid, "online", service)
        router = Router(self.onto, self.convs, cid)
        source = router.resolve(router.ref("claim", claim["id"]))[0]
        self.convs.append_message(cid, "assistant", "合成派生回答：重要决定前先核对事实。",
                                  meta={"routingSources": [source["ref"]]})
        queued = jobs.enqueue_summary(cid, store=self.onto)
        self.metadata(cid, status="archived")
        with patch("mindos.zhijun.routing.build_provider", return_value=online):
            result = jobs.run_job(self.onto.get_job(queued), store=self.onto, conv_store=self.convs)
        self.assertEqual((result["state"], result["reason"]), ("paused", "consent_required"))
        self.assertEqual(online.requests, [])
        self.assertFalse(self.routing.granted("global", source, service, "summarize_conversation"))
        self.assertEqual(self.routing.pending(cid)[0]["task_key"], "summarize_conversation")

    def test_management_api_pagination_search_conflict_and_device_scope(self):
        first = self.conversation(title="合成标题", device_scope="device:alpha")
        second = self.conversation(title="另一段对话", device_scope="device:alpha")
        message = self.convs.append_message(second["id"], "user", "正文也出现合成关键词。")
        self.conversation(title="合成：其他设备", device_scope="device:beta")
        headers = {"x-test-device": "alpha"}
        url = f"/api/mindos/conversations/{first['id']}"
        changed = self.client.patch(url, headers=headers, json={"expectedRevision": 0, "title": "合成标题改名", "pinned": True})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["metadataRevision"], 1)
        repeated = self.client.patch(url, headers=headers, json={"expectedRevision": 0, "title": "合成标题改名", "pinned": True})
        self.assertEqual(repeated.json()["metadataRevision"], 1)
        conflict = self.client.patch(url, headers=headers, json={"expectedRevision": 0, "title": "过期的修改"})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        denied = self.client.patch(url, headers={"x-test-device": "beta"}, json={"expectedRevision": 1, "status": "archived"})
        self.assertEqual(denied.status_code, 404, denied.text)
        archived = self.client.patch(url, headers=headers, json={"expectedRevision": 1, "status": "archived"})
        self.assertEqual(archived.status_code, 200, archived.text)

        page = self.client.get("/api/mindos/conversations", headers=headers, params={"status": "all", "q": "合成", "limit": 1}).json()
        self.assertEqual((page["total"], page["hasMore"]), (2, True))
        self.assertEqual(page["items"][0]["id"], first["id"])
        self.assertEqual(page["items"][0]["searchMatch"]["field"], "title")
        next_page = self.client.get("/api/mindos/conversations", headers=headers, params={"status": "all", "q": "合成", "limit": 1, "offset": 1}).json()
        self.assertFalse(next_page["hasMore"])
        self.assertEqual(next_page["items"][0]["searchMatch"]["messageId"], message["id"])
        self.assertIn("合成关键词", next_page["items"][0]["searchMatch"]["snippet"])
        active = self.client.get("/api/mindos/conversations", headers=headers).json()
        self.assertEqual([item["id"] for item in active["items"]], [second["id"]])
        self.assertEqual(self.local.requests, [])


if __name__ == "__main__":
    unittest.main()
