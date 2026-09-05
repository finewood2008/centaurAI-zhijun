"""Synthetic-only regression tests for optional AI drafting, not auto decisions."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import conversations
from mindos.stores import conversation_store, growth_store, ontology_store
from mindos.stores.alignment_store import AlignmentStore
from mindos.zhijun import alignment, decision_suggestions, deliberate, history
from mindos.zhijun.provider import FakeProvider, ProviderError


SAMPLE = {"candidates": [
    {"title": "先保留现状", "choice": "暂不扩张，先改善现有产品", "rationale": "把资源用在已知问题上，但会推迟探索机会。", "expectedOutcome": "观察现有用户的问题是否减少。"},
    {"title": "小范围验证", "choice": "先做一个可撤回的小试点", "rationale": "用有限投入验证需求，接受进度较慢的代价。", "expectedOutcome": "观察是否出现明确的使用意愿，再决定下一步。"},
    {"title": "条件满足后推进", "choice": "先明确预算和退出条件，满足后再扩张", "rationale": "如果资源允许，可以争取机会；也要能承受未达预期。", "expectedOutcome": "核对资源是否够用，以及新用户是否持续使用。"},
]}


class SuggestionsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.onto = ontology_store.reset_for_tests(root / "onto.db")
        self.convs = conversation_store.reset_for_tests(root / "conv.db")
        self.growth = growth_store.reset_for_tests(root / "growth.db")
        self.conv = self.convs.create_conversation(title="合成案例：是否扩张")
        self.convs.append_message(self.conv["id"], "user", "合成案例：我在考虑扩大产品范围，但还没决定。")
        self.draft = self.convs.upsert_draft(self.conv["id"], {**deliberate.default_fields(), "title": "是否扩张", "context": "考虑扩大产品范围", "options": ["扩张", "保持现状"]})
        self.payload = {"draftId": self.draft["id"], "expectedRevision": self.draft["revision"], "current": {}, "avoidChoices": []}
        self.url = f"/api/mindos/conversations/{self.conv['id']}/decision-draft"
        self.provider = Mock(name="local-test")
        self.provider.name, self.provider.model, self.provider.external = "ollama", "synthetic", False
        self.provider.complete_json.return_value = copy.deepcopy(SAMPLE)
        self.provider_patch = patch.object(decision_suggestions, "local_provider", return_value=self.provider)
        self.factory = self.provider_patch.start()
        app = FastAPI()
        app.include_router(conversations.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.provider_patch.stop()
        self.tmp.cleanup()

    def generate(self, payload=None):
        return self.client.post(self.url + "/suggestions", json=payload or self.payload)

    def test_generation_is_read_only_and_never_fills_confidence(self):
        before = self.convs.list_messages(self.conv["id"])
        response = self.generate()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["candidates"], SAMPLE["candidates"])
        self.assertFalse(response.json()["external"])
        self.assertEqual(self.convs.get_draft(self.conv["id"]), self.draft)
        self.assertEqual(before, self.convs.list_messages(self.conv["id"]))
        self.assertEqual(self.growth.list_decisions(), [])
        self.factory.assert_called_once_with(num_ctx=8192, timeout=55)
        self.assertNotIn('"confidence"', self.provider.complete_json.call_args.args[0].messages[0]["content"])

    def test_confirmation_remains_explicit_and_records_assistance(self):
        self.generate()
        self.assertEqual(self.client.post(self.url + "/confirm", json={}).status_code, 400)
        chosen = {k: v for k, v in SAMPLE["candidates"][1].items() if k != "title"}
        chosen.update(confidence=0, assistedFields=["choice", "rationale", "expectedOutcome"])
        response = self.client.post(self.url + "/confirm", json=chosen)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["decision"]["choice"], chosen["choice"])
        self.assertEqual(response.json()["decision"]["confidence"], 0)
        self.assertEqual(response.json()["draft"]["fields"]["assistedFields"], chosen["assistedFields"])
        self.assertEqual(response.json()["draft"]["fields"]["userQuotes"], [])
        self.assertEqual(self.generate().status_code, 409)

    def test_request_validation(self):
        for change in ({"expectedRevision": 0}, {"expectedRevision": True}, {"expectedRevision": "1"}, {"current": {"confidence": 99}}, {"avoidChoices": ["x"] * 4}):
            with self.subTest(change=change):
                self.assertEqual(self.generate({**self.payload, **change}).status_code, 422)
        self.provider.complete_json.assert_not_called()

    def test_scope_and_missing_conversation(self):
        with patch.object(decision_suggestions, "_device_scope_of", return_value="other-device"):
            self.assertEqual(self.generate().status_code, 404)
        response = self.client.post("/api/mindos/conversations/missing/decision-draft/suggestions", json=self.payload)
        self.assertEqual(response.status_code, 404)
        self.provider.complete_json.assert_not_called()

    def test_stale_revision_never_calls_model(self):
        self.convs.upsert_draft(self.conv["id"], self.draft["fields"])
        self.assertEqual(self.generate().status_code, 409)
        self.provider.complete_json.assert_not_called()

    def test_delayed_result_rejected_after_draft_change(self):
        def reply(_):
            self.convs.upsert_draft(self.conv["id"], {**self.draft["fields"], "choice": "用户新修改"})
            return SAMPLE
        self.provider.complete_json.side_effect = reply
        self.assertEqual(self.generate().status_code, 409)
        self.assertEqual(self.convs.get_draft(self.conv["id"])["fields"]["choice"], "用户新修改")

    def test_delayed_result_rejected_after_new_message(self):
        def reply(_):
            self.convs.append_message(self.conv["id"], "user", "我改变主意了")
            return SAMPLE
        self.provider.complete_json.side_effect = reply
        self.assertEqual(self.generate().status_code, 409)

    def test_invalid_candidates_are_not_silently_used(self):
        bad = [None, {}, {"candidates": SAMPLE["candidates"][:1]}, {"candidates": [SAMPLE["candidates"][0]] * 3},
               {"candidates": [{**c, "confidence": 80} for c in SAMPLE["candidates"]]},
               {"candidates": [{**c, "choice": "   "} for c in SAMPLE["candidates"]]}]
        for raw in bad:
            with self.subTest(raw=raw):
                self.provider.complete_json.return_value = raw
                self.assertEqual(self.generate().status_code, 502)
                self.assertEqual(self.convs.get_draft(self.conv["id"]), self.draft)

    def test_no_external_fallback_and_gate_released_after_failure(self):
        self.provider.external = True
        self.assertEqual(self.generate().status_code, 503)
        self.provider.complete_json.assert_not_called()
        self.provider.external = False
        self.provider.complete_json.side_effect = ProviderError("local unavailable")
        self.assertEqual(self.generate().status_code, 503)
        self.provider.complete_json.side_effect = None
        self.assertEqual(self.generate().status_code, 200)

    def test_busy_local_model_does_not_lock_draft(self):
        with patch.object(decision_suggestions.provider_gate, "acquire", return_value=False):
            self.assertEqual(self.generate().status_code, 429)
        self.assertEqual(self.generate().status_code, 200)

    def test_regenerate_uses_current_edits_and_excludes_assistant_as_user(self):
        self.convs.append_message(self.conv["id"], "assistant", "这不是用户原话")
        self.generate({**self.payload, "current": {"choice": "我现在选择先不扩张"}, "avoidChoices": ["旧方向"]})
        req = self.provider.complete_json.call_args.args[0]
        self.assertIn("我现在选择先不扩张", req.messages[0]["content"])
        self.assertIn("旧方向", req.messages[0]["content"])
        self.assertNotIn("这不是用户原话", req.messages[0]["content"])

    def test_private_derived_selection_cannot_escape_via_history_or_review(self):
        AlignmentStore(self.onto).status(self.conv["id"], status="calibrated")
        self.assertEqual(self.generate().status_code, 200)
        chosen = {k: v for k, v in SAMPLE["candidates"][0].items() if k != "title"}
        result = self.client.post(self.url + "/confirm", json={**chosen, "confidence": 70, "assistedFields": ["choice"]}).json()
        decision = result["decision"]
        self.assertTrue(history.local_only_decision(decision))
        self.assertEqual(history.similar_decisions("扩大产品范围", growth=self.growth), [])
        review = self.client.post("/api/mindos/conversations", json={"mode": "review", "decisionId": decision["id"]}).json()
        self.assertTrue(alignment.protected(review["id"], self.convs, self.onto))
        external = FakeProvider()
        external.external = True
        with patch("mindos.chat_imports.local_provider", return_value=self.provider):
            selected = alignment.select_provider(review["id"], "怎么看", external, self.onto, self.convs)
        self.assertIs(selected, self.provider)
        self.assertTrue(any(json.loads(r).get("kind") == "local_only_decision" for r in decision["evidenceRefs"]))


if __name__ == "__main__":
    unittest.main()
