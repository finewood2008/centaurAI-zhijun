"""Synthetic executives start independently, without importing another profile."""
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from mindos import zhijun_onboarding as onboarding
from mindos.stores import ontology_store, conversation_store


class ExecutiveOnboardingIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.onto = ontology_store.reset_for_tests(root / "ontology.db")
        self.convs = conversation_store.reset_for_tests(root / "conversations.db")

    def tearDown(self):
        self.tmp.cleanup()

    def progress(self, scope):
        return onboarding.get_progress(ontology=self.onto, conversations=self.convs, scope=scope)

    def action(self, scope, action):
        return onboarding.apply_action(onboarding.OnboardingCommand(action=action),
            ontology=self.onto, conversations=self.convs, scope=scope)

    def test_two_executives_start_resume_skip_and_restart_independently(self):
        a = self.action("device:lin", "start")
        self.convs.append_message(a["conversationId"], "user", "我是林舟，一家制造企业的运营负责人")
        self.assertEqual(self.progress("device:zhou")["state"], "welcome")
        self.assertIsNone(self.progress("device:zhou")["conversationId"])
        b = self.action("device:zhou", "start")
        self.assertNotEqual(a["conversationId"], b["conversationId"])
        self.assertEqual(self.action("device:lin", "start")["conversationId"], a["conversationId"])
        self.action("device:lin", "skip")
        self.assertEqual(self.progress("device:lin")["state"], "ready")
        self.assertEqual(self.progress("device:zhou")["state"], "profile_building")
        self.action("device:zhou", "restart")
        self.assertEqual(self.progress("device:lin")["state"], "ready")
        self.assertEqual(self.progress("global")["state"], "welcome")

    def test_global_legacy_profile_does_not_skip_new_device_welcome(self):
        self.onto.create_claim({"content": "合成旧用户是企业负责人", "section": "who", "layer": "self_declared"},
            [{"kind": "user_edit", "quote": "合成旧用户"}], trust_state="confirmed", trust_origin="user_created")
        self.assertEqual(self.progress("global")["state"], "ready")
        self.assertEqual(self.progress("device:new")["state"], "welcome")

    def test_archived_own_onboarding_is_reused_without_foreign_history(self):
        a = self.action("device:lin", "start")
        self.convs.update_metadata(a["conversationId"], expected_revision=0, status="archived", device_scope="device:lin")
        self.assertEqual(self.action("device:lin", "start")["conversationId"], a["conversationId"])
        self.assertIsNone(self.progress("device:zhou")["conversationId"])

    def test_opening_is_explicit_local_template_not_opaque_legacy_history(self):
        started = self.action("device:lin", "start")
        opening = self.convs.list_messages(started["conversationId"])[0]
        self.assertEqual(opening["provider"], "template")
        self.assertEqual(opening["meta"]["routingOrigin"], {"service": "", "external": False})
        self.assertEqual(opening["meta"]["routingSources"], [])


if __name__ == "__main__":
    unittest.main()
