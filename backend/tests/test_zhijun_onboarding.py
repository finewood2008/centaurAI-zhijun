from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import zhijun_onboarding
from mindos.stores import conversation_store as conversation_store_module
from mindos.stores import ontology_store as ontology_store_module


class ZhijunOnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.onto = ontology_store_module.reset_for_tests(root / "ontology.db")
        self.convs = conversation_store_module.reset_for_tests(root / "conversations.db")
        app = FastAPI()
        app.include_router(zhijun_onboarding.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clean_install_starts_with_explicit_welcome(self) -> None:
        progress = self.client.get("/api/mindos/zhijun/onboarding").json()
        self.assertEqual(progress["state"], "welcome")
        self.assertFalse(progress["migrated"])
        self.assertEqual(self.onto.meta_get("zhijun_onboarding_state_v1") is not None, True)

    def test_start_creates_assistant_opening_without_fake_user_message(self) -> None:
        started = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "start"}).json()
        self.assertEqual(started["state"], "profile_building")
        messages = self.convs.list_messages(started["conversationId"])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "assistant")
        self.assertEqual(messages[0]["meta"]["kind"], "onboarding_open")
        self.assertNotEqual(messages[0]["content"], "你好，我们开始吧")
        again = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "start"}).json()
        self.assertEqual(again["conversationId"], started["conversationId"])

    def test_skip_is_available_before_and_during_profile_building(self) -> None:
        skipped = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "skip"}).json()
        self.assertEqual(skipped["state"], "ready")
        self.assertEqual((skipped["starterImport"], skipped["sourceConnect"]), ("skipped", "skipped"))
        self.client.post("/api/mindos/zhijun/onboarding", json={"action": "restart"})
        started = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "start"}).json()
        self.convs.append_message(started["conversationId"], "user", "我叫阿澈")
        skipped_building = self.client.post(
            "/api/mindos/zhijun/onboarding",
            json={"action": "skip", "conversationId": started["conversationId"]},
        ).json()
        self.assertEqual(skipped_building["state"], "ready")
        self.assertEqual(self.convs.count_messages(started["conversationId"], role="user"), 1)

    def test_progress_is_resumable_and_transitions_are_guarded(self) -> None:
        started = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "start"}).json()
        early = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "profile_ready", "conversationId": started["conversationId"]})
        self.assertEqual(early.status_code, 200)
        self.convs.append_message(started["conversationId"], "user", "我叫阿澈，希望今年更从容一些")
        review = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "profile_ready", "conversationId": started["conversationId"]}).json()
        self.assertEqual(review["state"], "profile_review")
        imported = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "profile_confirmed"}).json()
        self.assertEqual(imported["state"], "ready")
        self.assertEqual(imported["starterImport"], "pending")
        self.assertEqual(imported["sourceConnect"], "pending")
        ready = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "finish"}).json()
        self.assertEqual(ready["state"], "ready")
        self.assertIsNotNone(ready["completedAt"])

    def test_existing_user_is_migrated_to_ready(self) -> None:
        self.onto.create_claim(
            {"content": "我在做一个产品", "section": "matters", "layer": "self_declared"},
            [{"kind": "user_edit", "quote": "我在做一个产品"}],
            trust_state="confirmed",
            trust_origin="user_created",
        )
        progress = self.client.get("/api/mindos/zhijun/onboarding").json()
        self.assertEqual((progress["state"], progress["migrated"]), ("ready", True))
        restarted = self.client.post("/api/mindos/zhijun/onboarding", json={"action": "restart"}).json()
        self.assertEqual(restarted["state"], "welcome")
        self.assertTrue(self.onto.stats()["hasOntology"])

    def test_purge_all_returns_the_product_to_first_run(self) -> None:
        self.onto.create_claim(
            {"content": "我在做一个产品", "section": "matters", "layer": "self_declared"},
            [{"kind": "user_edit", "quote": "我在做一个产品"}],
            trust_state="confirmed",
            trust_origin="user_created",
        )
        self.assertEqual(self.client.get("/api/mindos/zhijun/onboarding").json()["state"], "ready")
        self.onto.meta_set("zhijun_home_snapshot_v1", "{\"stale\":true}")
        self.onto.purge_all()
        self.convs.purge_all()
        progress = self.client.get("/api/mindos/zhijun/onboarding").json()
        self.assertEqual(progress["state"], "welcome")
        self.assertIsNone(self.onto.meta_get("zhijun_home_snapshot_v1"))


if __name__ == "__main__":
    unittest.main()
