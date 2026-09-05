"""Synthetic calibration, egress, source lineage and optimistic-update tests."""
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindos import ontology, conversations
from mindos.chat_imports import service_info
from mindos.stores import ontology_store, conversation_store
from mindos.stores.alignment_store import AlignmentStore, LEVELS
from mindos.zhijun import alignment, context, jobs, context_pack, projection
from mindos.zhijun.provider import TextDelta, Done, ProviderError
from mindos.zhijun.turn import run_turn


class RecordingProvider:
    def __init__(self, external=False, name="test-local", base="http://model.test/v1"):
        self.external, self.name, self.model, self._base_url = external, name, "synthetic-model", base
        self.requests = []
        self.json_result = None

    def stream(self, req):
        self.requests.append(req)
        yield TextDelta("这段合成回复保留事实，但不把工作安排当作你的核心意愿。")
        yield Done("stop")

    def complete_json(self, req):
        self.requests.append(req)
        if isinstance(self.json_result, Exception):
            raise self.json_result
        return self.json_result


class SelfAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.onto = ontology_store.reset_for_tests(self.root / "ontology.db")
        self.convs = conversation_store.reset_for_tests(self.root / "conversations.db")
        self.store = AlignmentStore(self.onto)
        self.conv = self.convs.create_conversation()["id"]
        self.local, self.external = RecordingProvider(), RecordingProvider(True, "external")
        self.stack.enter_context(patch.dict(os.environ, {"ZHIJUN_MATERIAL_EVIDENCE": "0", "ZHIJUN_EXTRACTION": "0"}))
        self.stack.enter_context(patch("mindos.chat_imports.local_provider", return_value=self.local))
        self.stack.enter_context(patch("mindos.alignment_routes.build_provider", return_value=self.external))
        app = FastAPI()
        app.include_router(ontology.router)
        app.include_router(conversations.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def claim(self, text="我负责星桥项目，这是公司安排的工作", **over):
        payload = {"subject_entity_id": "ent_me", "section": "matters", "layer": "self_declared", "predicate": "working_on", "content": text, "confidence": .99, **over}
        return self.onto.create_claim(payload, [{"kind": "user_edit", "quote": text}], trust_state="confirmed", trust_origin="user_created")

    def payload(self, claim, level=0, **over):
        a = claim["selfAlignment"]
        return {"requestId": str(uuid.uuid4()), "expectedRevision": a["revision"], "claimVersion": a["claimVersion"],
                "evidenceVersion": a["evidenceVersion"], "action": "calibrate", "level": level, "framing": "long_term", "note": "合成隐私：我更想研究自然摄影，而不是负责这个项目", **over}

    def review(self, claim, level=0, **over):
        res = self.client.post(f"/api/mindos/ontology/claims/{claim['id']}/alignment", json=self.payload(claim, level, **over))
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def source(self, claim):
        return alignment.source(claim, self.convs, "global")

    def assemble(self, provider, text="星桥项目工作", **over):
        return context.assemble(conversation=self.convs.get_conversation(self.conv), user_text=text,
            depth="brief", provider=provider, ontology=self.onto, conversation_store=self.convs,
            recent_messages=[], user_turns=1, **over)

    def test_old_and_new_claims_start_unknown_not_confidence(self):
        c = self.claim()
        self.assertIsNone(c["selfAlignment"]["level"])
        self.assertEqual(c["confidence"], .99)
        self.assertEqual(c["selfAlignment"]["revision"], 0)
        reopened = ontology_store.OntologyStore(self.onto.db_path)
        self.assertIsNone(reopened.get_claim(c["id"])["selfAlignment"]["level"])

    def test_zero_is_valid_and_keeps_fact_and_trust(self):
        c = self.review(self.claim(), 0)
        self.assertEqual(c["selfAlignment"]["level"], 0)
        self.assertEqual(c["trustState"], "confirmed")
        self.assertEqual(c["confidence"], .99)
        assembled = self.assemble(self.local)
        self.assertIn(c["content"], assembled.system)
        self.assertIn("不代表我", assembled.system)
        self.assertIn("不是事实真假", assembled.system)
        self.assertEqual(assembled.provenance["alignmentSources"][0]["revision"], 1)

    def test_all_five_levels(self):
        for level in range(5):
            c = self.review(self.claim(f"我负责星桥项目{level}"), level)
            self.assertEqual(c["selfAlignment"]["level"], level)
        self.assertEqual(len(LEVELS), 5)

    def test_idempotency_conflicting_request_and_stale_revision(self):
        c = self.claim()
        payload = self.payload(c)
        url = f"/api/mindos/ontology/claims/{c['id']}/alignment"
        self.assertEqual(self.client.post(url, json=payload).status_code, 200)
        self.assertEqual(self.client.post(url, json=payload).json()["selfAlignment"]["revision"], 1)
        self.assertEqual(self.client.post(url, json={**payload, "level": 4}).status_code, 409)
        self.assertEqual(self.client.post(url, json={**payload, "requestId": str(uuid.uuid4())}).status_code, 409)
        self.assertEqual(len(self.onto.review_events(target_id=c["id"])), 2)

    def test_invalid_levels_do_not_write(self):
        c = self.claim()
        for level in (-1, 5, True, 1.5, "4"):
            res = self.client.post(f"/api/mindos/ontology/claims/{c['id']}/alignment", json=self.payload(c, level))
            self.assertEqual(res.status_code, 422)
        self.assertEqual(self.onto.get_claim(c["id"])["selfAlignment"]["revision"], 0)

    def test_reaffirm_time_does_not_raise_or_decay_level(self):
        c = self.review(self.claim(), 1)
        before = c["selfAlignment"]
        c = self.onto.transition(c["id"], "reaffirm", surface="ontology_page")["claim"]
        self.assertEqual(c["selfAlignment"], before)
        with self.onto._connect() as db:
            db.execute("UPDATE claims SET last_reaffirmed='2020-01-01T00:00:00Z' WHERE id=?", (c["id"],))
        self.assertEqual(self.onto.get_claim(c["id"])["selfAlignment"]["level"], 1)

    def test_replacement_does_not_inherit_alignment(self):
        c = self.review(self.claim(), 4)
        result = self.onto.transition(c["id"], "partial", edited_content="我希望自己选择星桥项目", surface="ontology_page")
        self.assertIsNone(result["replacedBy"]["selfAlignment"]["level"])
        self.assertIsNone(result["claim"]["selfAlignment"]["level"])
        self.assertTrue(result["claim"]["selfAlignment"]["history"])

    def test_context_and_aspiration_do_not_become_long_term_trait(self):
        c = self.review(self.claim(scope="context_only"), 4)
        self.assertEqual(c["selfAlignment"]["framing"], "context_only")
        c = self.review(self.claim("我希望自主选择星桥项目", layer="aspirational"), 4)
        self.assertEqual(c["selfAlignment"]["framing"], "aspirational")
        self.assertIn("不代表已经做到", self.assemble(self.local).system)

    def test_new_evidence_keeps_level_invalidates_old_consent(self):
        c = self.review(self.claim(), 3)
        ref = self.source(c)
        self.store.grant([ref], service_info(self.external)["id"])
        self.assertTrue(alignment.allowed(ref, self.external, self.onto, self.convs, "global"))
        self.onto.add_evidence(c["id"], [{"kind": "user_edit", "quote": "另一条合成依据"}])
        self.assertEqual(self.onto.get_claim(c["id"])["selfAlignment"]["level"], 3)
        self.assertFalse(alignment.allowed(ref, self.external, self.onto, self.convs, "global"))

    def test_no_authorization_external_request_has_no_profile_or_derived_history(self):
        self.review(self.claim(), 0)
        assembled = self.assemble(self.external)
        self.assertNotIn("合成隐私", assembled.system)
        self.assertEqual(assembled.provenance["alignmentSources"], [])
        list(run_turn(self.conv, "星桥项目工作", provider=self.external, ontology=self.onto, conv_store=self.convs))
        self.assertFalse(self.external.requests)
        self.assertTrue(self.local.requests)
        # An unrelated later request is still local: it carries derivative history.
        list(run_turn(self.conv, "换一个话题，聊聊天气", provider=self.external, ontology=self.onto, conv_store=self.convs))
        self.assertFalse(self.external.requests)
        self.assertEqual(len(self.local.requests), 2)

    def test_authorized_service_can_use_profile_and_receipt_survives_refresh(self):
        c = self.review(self.claim(), 2)
        ref = self.source(c)
        self.store.grant([ref], service_info(self.external)["id"])
        list(run_turn(self.conv, "星桥项目工作", provider=self.external, ontology=self.onto, conv_store=self.convs))
        self.assertIn("合成隐私", self.external.requests[-1].system)
        detail = self.client.get(f"/api/mindos/conversations/{self.conv}").json()
        reply = next(m for m in detail["messages"] if m["role"] == "assistant")
        self.assertEqual(reply["provenance"]["alignmentSources"][0]["revision"], 1)

    def test_change_service_revoke_or_retract_routes_history_local(self):
        c = self.review(self.claim(), 4)
        ref = self.source(c)
        self.store.grant([ref], service_info(self.external)["id"])
        self.convs.append_message(self.conv, "assistant", "基于私密画像的衍生内容", meta={"alignmentSources": [ref]})
        other = RecordingProvider(True, "external", "http://another.test/v1")
        self.assertIs(alignment.select_provider(self.conv, "天气", other, self.onto, self.convs), self.local)
        self.store.revoke(c["id"])
        self.assertIs(alignment.select_provider(self.conv, "天气", self.external, self.onto, self.convs), self.local)
        self.store.grant([ref], service_info(self.external)["id"])
        self.onto.transition(c["id"], "retract", surface="ontology_page")
        self.assertIs(alignment.select_provider(self.conv, "天气", self.external, self.onto, self.convs), self.local)

    def test_history_versions_need_explicit_reauthorization(self):
        c = self.review(self.claim(), 1, conversationId=self.conv)
        first = self.source(c)
        c = self.review(c, 3, conversationId=self.conv)
        current = self.source(c)
        url = f"/api/mindos/ontology/alignment/conversations/{self.conv}"
        body = {"serviceId": service_info(self.external)["id"], "refs": [{"claimId": c["id"], "fingerprint": current["fingerprint"]}]}
        self.assertEqual(self.client.post(url + "/consent", json=body).status_code, 200)
        self.assertIs(alignment.select_provider(self.conv, "天气", self.external, self.onto, self.convs), self.local)
        body["refs"].append({"claimId": c["id"], "fingerprint": first["fingerprint"]})
        self.assertEqual(self.client.post(url + "/consent", json=body).status_code, 200)
        self.assertIs(alignment.select_provider(self.conv, "天气", self.external, self.onto, self.convs), self.external)

    def evidence_claim(self):
        c = self.claim()
        for text in ("我负责星桥项目但不是自愿的", "我仍然想自己选择项目而非被安排"):
            msg = self.convs.append_message(self.conv, "user", text)
            self.onto.add_evidence(c["id"], [{"kind": "conversation_turn", "conversation_id": self.conv, "message_id": msg["id"], "quote": text}])
        assistant = self.convs.append_message(self.conv, "assistant", "合成回复")
        return self.onto.get_claim(c["id"]), assistant

    def test_model_only_proposes_user_must_calibrate(self):
        c, msg = self.evidence_claim()
        self.local.json_result = {"level": 4, "framing": "long_term", "reason": "合成提议：这符合你吗？", "evidenceIds": [e["id"] for e in c["evidence"]]}
        result = alignment.propose(c["id"], conversation_id=self.conv, message_id=msg["id"], ontology=self.onto, conversations=self.convs, provider=self.local)
        self.assertEqual(result["state"], "ready")
        proposed = result["claim"]
        self.assertIsNone(proposed["selfAlignment"]["level"])
        self.assertEqual(proposed["selfAlignment"]["proposal"]["level"], 4)
        c = self.review(proposed, 0, proposalId=proposed["selfAlignment"]["proposal"]["id"])
        self.assertEqual(c["selfAlignment"]["level"], 0)

    def test_deferring_without_new_evidence_does_not_repeat(self):
        c, msg = self.evidence_claim()
        a = c["selfAlignment"]
        c = self.store.propose(c["id"], expected_revision=0, version=a["claimVersion"], level=2,
            framing="long_term", reason="合成", evidence_ids=[e["id"] for e in c["evidence"]],
            conversation_id=self.conv, message_id=msg["id"], evidence_digest=a["evidenceVersion"])
        self.review(c, action="defer")
        result = alignment.run_job({"conversationId": self.conv, "messageId": msg["id"], "query": "星桥项目"}, self.onto, self.convs)
        self.assertEqual(result["state"], "skipped")
        self.assertFalse(self.local.requests)

    def test_single_behavior_and_assistant_text_not_enough_evidence(self):
        c = self.claim()
        msg = self.convs.append_message(self.conv, "assistant", "你其实非常喜欢星桥项目")
        self.onto.add_evidence(c["id"], [{"kind": "conversation_turn", "conversation_id": self.conv, "message_id": msg["id"], "quote": msg["content"]}])
        result = alignment.propose(c["id"], conversation_id=self.conv, message_id=msg["id"], ontology=self.onto, conversations=self.convs, provider=self.local)
        self.assertEqual(result["state"], "skipped")
        self.assertFalse(self.local.requests)

    def test_stale_model_proposal_cannot_overwrite_user(self):
        c, msg = self.evidence_claim()
        a = c["selfAlignment"]
        self.review(c, 0)
        with self.assertRaises(ontology_store.OntologyConflictError):
            self.store.propose(c["id"], expected_revision=0, version=a["claimVersion"], level=4,
                framing="long_term", reason="旧提议", evidence_ids=[e["id"] for e in c["evidence"]],
                conversation_id=self.conv, message_id=msg["id"], evidence_digest=a["evidenceVersion"])
        self.assertEqual(self.onto.get_claim(c["id"])["selfAlignment"]["level"], 0)

    def test_private_background_jobs_do_not_leak(self):
        self.review(self.claim(), 0, conversationId=self.conv)
        with patch("mindos.zhijun.jobs.build_provider", side_effect=AssertionError("must not call external")):
            for kind in ("summarize_conversation", "extract_turn", "draft_turn", "first_observation"):
                result = jobs.run_job({"kind": kind, "payload": {"conversationId": self.conv}}, store=self.onto, conv_store=self.convs)
                self.assertEqual(result["reason"], "private_profile_requires_explicit_action")

    def test_local_model_failure_allows_manual_calibration(self):
        c, msg = self.evidence_claim()
        self.local.json_result = ProviderError("local unavailable")
        result = alignment.propose(c["id"], conversation_id=self.conv, message_id=msg["id"], ontology=self.onto, conversations=self.convs, provider=self.local)
        self.assertEqual(result["state"], "paused")
        self.assertEqual(self.review(c, 2)["selfAlignment"]["level"], 2)
        self.assertFalse(self.external.requests)

    def test_no_deep_profile_in_general_context_pack_or_user_md(self):
        c = self.review(self.claim(export_allowed=True), 4)
        pack = context_pack.build_pack(purpose="合成测试", store=self.onto)
        user_md = projection.render(self.onto)[1]
        self.assertNotIn("合成隐私", json.dumps(pack, ensure_ascii=False))
        self.assertNotIn("selfAlignment", json.dumps(pack))
        self.assertNotIn("合成隐私", user_md)
        self.assertIn(c["content"], user_md)

    def test_cross_scope_calibration_denied(self):
        c, _ = self.evidence_claim()
        with patch("mindos.alignment_routes._device_scope_of", return_value="other-device"):
            result = self.client.post(f"/api/mindos/ontology/claims/{c['id']}/alignment", json=self.payload(c))
            self.assertEqual(result.status_code, 404)

    def test_irrelevant_high_alignment_does_not_inject_anchor(self):
        self.review(self.claim("我喜欢摄影", section="ways", predicate="prefers"), 4)
        result = self.assemble(self.local, text="如何给服务器配置端口", turn_mode="deliberate")
        self.assertEqual(result.provenance["anchorClaimIds"], [])
        self.assertEqual(result.provenance["alignmentSources"], [])

    def test_new_contextual_calibration_updates_scope_without_losing_fact(self):
        c = self.review(self.claim(export_allowed=True), 4, framing="context_only", conversationId=self.conv)
        self.assertEqual(c["scope"], "context_only")
        self.assertEqual(c["contextRef"], self.conv)
        self.assertEqual(c["selfAlignment"]["level"], 4)
        self.assertNotIn(c["content"], projection.render(self.onto)[1])
        self.assertEqual(c["trustState"], "confirmed")

    def test_aspirational_calibration_changes_layer_not_achievement(self):
        c = self.review(self.claim(), 4, framing="aspirational")
        self.assertEqual(c["layer"], "aspirational")
        self.assertEqual(c["selfAlignment"]["level"], 4)

    def test_profile_grant_does_not_grant_file_permission(self):
        c = self.claim()
        self.onto.add_evidence(c["id"], [{"kind": "material_span", "material_id": "synthetic-file", "quote": "这是文件中的合成证据"}])
        record = {"versionNumber": 1}
        snapshot = {"snapshot_id": "snapshot-1"}
        with patch("mindos.chat_imports.require_material", return_value=record), \
             patch("mindos.chat_imports.read_ref", return_value=(record, snapshot, "这是文件中的合成证据")), \
             patch("mindos.stores.chat_import_store.ChatImportStore.allowed", return_value=False) as files_allowed:
            c = self.review(self.onto.get_claim(c["id"]), 3)
            ref = self.source(c)
            self.store.grant([ref], service_info(self.external)["id"])
            self.assertFalse(alignment.allowed(ref, self.external, self.onto, self.convs, "global"))
            files_allowed.return_value = True
            self.assertTrue(alignment.allowed(ref, self.external, self.onto, self.convs, "global"))
            snapshot["snapshot_id"] = "snapshot-2"
            self.assertFalse(alignment.allowed(ref, self.external, self.onto, self.convs, "global"))
        self.assertFalse(alignment.allowed(ref, self.external, self.onto, self.convs, "global"))

    def test_continued_dialogue_can_propose_with_new_user_evidence(self):
        c = self.review(self.claim(), 0)
        msg = self.convs.append_message(self.conv, "user", "我现在认同负责星桥项目，我希望负责星桥项目")
        answer = self.convs.append_message(self.conv, "assistant", "合成回复")
        self.local.json_result = {"level": 3, "framing": "long_term", "reason": "你似乎改变了对项目的想法，对吗？",
                                  "evidenceIds": [c["evidence"][0]["id"], "turn:" + msg["id"]]}
        result = alignment.run_job({"conversationId": self.conv, "messageId": answer["id"], "query": msg["content"]}, self.onto, self.convs)
        self.assertEqual(result["state"], "ready")
        c = self.onto.get_claim(c["id"])
        self.assertEqual(c["selfAlignment"]["level"], 0)
        self.assertEqual(c["selfAlignment"]["proposal"]["level"], 3)
        self.assertTrue(any(e.get("messageId") == msg["id"] for e in c["evidence"]))

    def test_restart_preserves_calibration_and_pauses_pending_status(self):
        c = self.review(self.claim(), 3)
        self.store.status(self.conv, status="queued")
        reopened = ontology_store.OntologyStore(self.onto.db_path)
        self.assertEqual(reopened.get_claim(c["id"])["selfAlignment"]["level"], 3)
        self.assertEqual(AlignmentStore(reopened).status(self.conv)["status"], "paused")

    def test_old_schema_migrates_without_scoring_claims(self):
        path = self.root / "old.db"
        with sqlite3.connect(path) as db:
            db.executescript(ontology_store._SCHEMA)
        reopened = ontology_store.OntologyStore(path)
        with reopened._connect() as db:
            self.assertIn("self_alignment_json", {r[1] for r in db.execute("PRAGMA table_info(claims)")})


if __name__ == "__main__":
    unittest.main()
