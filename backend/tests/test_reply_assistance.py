"""Synthetic payload and persistence checks; no real user data or network."""
import json
import unittest
from unittest.mock import patch

from tests import test_task_routing as harness
from mindos.stores.reply_assist_store import ReplyAssistStore
from mindos.zhijun import alignment, extract, jobs
from mindos.zhijun.provider import ProviderError
from mindos.zhijun.routing import Router, PURPOSES, service_info
from mindos.zhijun.reply_assistance import CONTROLS, FORMAT_VERSION, build_request, candidate_texts


class ReplyAssistanceTests(unittest.TestCase):
    setUp = harness.RoutingTests.setUp
    tearDown = harness.RoutingTests.tearDown
    enable = harness.RoutingTests.enable
    claim = harness.RoutingTests.claim
    grant = harness.RoutingTests.grant
    preview = harness.RoutingTests.preview
    send = harness.RoutingTests.send

    def seed(self, protected=False):
        self.local.result = self.online.result = {"candidates": [
            {"text": "我更在意时间够不够，预算可以先控制在小范围。"}, {"text": "我更想先确定要达到什么效果，再决定投入多少。"}]}
        meta = {"routingSources": []}
        if protected:
            c = self.claim()
            meta["routingSources"] = [Router(self.onto, self.convs, self.cid).resolve({"kind": "claim", "id": c["id"]})[0]["ref"]]
        self.target = self.convs.append_message(self.cid, "assistant", "合成问题：目前这件事最需要先澄清什么？", meta=meta)

    def generate(self, **extra):
        body = {"messageId": self.target["id"], "requestId": "synthetic-reply-request", **extra}
        res = self.client.post(self.url + "/reply-assistance", json=body)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def origin(self, batch):
        return {"messageId": batch["messageId"], "selections": [{"batchId": batch["id"], "candidateId": batch["candidates"][0]["id"]}]}

    def test_only_explicit_generation_calls_model_and_reuses_snapshot(self):
        self.seed()
        self.assertIsNone(self.client.get(self.url + "/reply-assistance").json()["batch"])
        preview = self.generate(previewOnly=True)["routePreview"]
        self.assertFalse(self.local.requests)
        batch = self.generate(routeRevision=preview["revision"])["batch"]
        self.assertEqual(len(self.local.requests), 1)
        self.assertEqual(self.generate()["batch"], batch)
        self.assertEqual(ReplyAssistStore(self.convs).get(batch["id"]), batch)
        self.assertEqual(self.client.get(self.url + "/reply-assistance").json()["batch"], batch)
        self.assertEqual(len(self.convs.list_messages(self.cid)), 1)
        self.assertEqual(self.onto.list_claims(), [])
        self.assertEqual(len(self.local.requests), 1)

    def test_inline_expression_is_edited_and_idempotent_on_send(self):
        self.seed()
        batch = self.generate()["batch"]
        content = "这是我自己先写的。\n" + batch["candidates"][0]["text"] + "只针对当前这件事。"
        body, preview = self.preview(content, replyAssistance=self.origin(batch))
        res = self.send(body, preview, requestId="assisted-send-id")
        self.assertIn("event: message_done", res.text)
        user = [m for m in self.convs.list_messages(self.cid) if m["role"] == "user"][0]
        self.assertTrue(user["meta"]["replyAssistance"]["edited"])
        self.assertEqual(user["meta"]["replyAssistance"]["kind"], "assisted")
        self.assertEqual(user["content"], content)
        self.assertEqual(user["meta"]["routingSources"][0]["kind"], "reply_assist")
        self.send(body, preview, requestId="assisted-send-id")
        self.assertEqual(len(self.convs.list_messages(self.cid)), 3)

    def test_new_messages_or_mutated_sources_invalidate_candidates(self):
        self.seed()
        batch = self.generate()["batch"]
        self.convs.append_message(self.cid, "user", "新的话题")
        self.assertIsNone(self.client.get(self.url + "/reply-assistance").json()["batch"])
        res = self.client.post(self.url + "/routing/preview", json={"content": "我选这个", "replyAssistance": self.origin(batch)})
        self.assertEqual(res.status_code, 409)

    def test_context_changed_during_generation_does_not_save(self):
        self.seed()
        original = self.local.complete_json
        def changed(req):
            self.convs.append_message(self.cid, "user", "继续自由输入的新内容")
            return original(req)
        with patch.object(self.local, "complete_json", changed):
            res = self.client.post(self.url + "/reply-assistance", json={"messageId": self.target["id"], "requestId": "generation-race"})
        self.assertEqual(res.status_code, 409)
        self.assertIsNone(ReplyAssistStore(self.convs).latest(self.cid))

    def test_deleted_and_cross_device_batch_cannot_be_used(self):
        self.seed()
        batch = self.generate()["batch"]
        other = self.convs.create_conversation(device_scope="another-device")["id"]
        self.assertEqual(self.client.post(f"/api/mindos/conversations/{other}/reply-assistance", json={"messageId": self.target["id"], "requestId": "cross-device"}).status_code, 404)
        cid = self.convs.create_conversation()["id"]
        target = self.convs.append_message(cid, "assistant", "另一个问题")
        bad = {**self.origin(batch), "messageId": target["id"]}
        self.assertEqual(self.client.post(f"/api/mindos/conversations/{cid}/routing/preview", json={"content": "我选这个", "replyAssistance": bad}).status_code, 404)
        self.client.delete(self.url)
        self.assertIsNone(ReplyAssistStore(self.convs).get(batch["id"]))

    def test_new_purpose_does_not_expand_existing_default_grants(self):
        self.seed(protected=True)
        self.enable(True)
        self.store.set_policy("global", enabled=True, service=service_info(self.online)["id"], service_name="synthetic", include_files=False,
                              purposes=[p for p in PURPOSES if p != "reply_assistance"], expected_revision=0)
        preview = self.generate(previewOnly=True)["routePreview"]
        self.assertTrue(preview["missing"])
        res = self.client.post(self.url + "/reply-assistance", json={"messageId": self.target["id"], "requestId": "not-approved-purpose", "routeRevision": preview["revision"]})
        self.assertEqual(res.status_code, 409)
        self.assertFalse(self.online.requests)

    def test_online_generation_exact_payload_and_revocation_before_call(self):
        self.seed(protected=True)
        self.enable(True)
        preview = self.generate(previewOnly=True)["routePreview"]
        self.grant(preview)
        preview = self.generate(previewOnly=True)["routePreview"]
        self.store.revoke("global")
        res = self.client.post(self.url + "/reply-assistance", json={"messageId": self.target["id"], "requestId": "revoked-before-call", "routeRevision": preview["revision"]})
        self.assertEqual(res.status_code, 409)
        self.assertFalse(self.online.requests)
        preview = self.generate(previewOnly=True)["routePreview"]
        self.grant(preview)
        preview = self.generate(previewOnly=True)["routePreview"]
        batch = self.generate(routeRevision=preview["revision"])["batch"]
        self.assertEqual(self.online.requests[-1].messages, preview["request"]["messages"])
        self.assertTrue(batch["external"])

    def test_edited_text_and_omit_do_not_launder_local_candidate_sources(self):
        self.seed(protected=True)
        self.enable(True)
        batch = self.generate(localOnly=True)["batch"]
        body, preview = self.preview("改几个字后的候选答案", replyAssistance=self.origin(batch), omitSources=True)
        self.assertIn("reply_assist:" + batch["id"], preview["missing"])
        self.assertTrue(any(k.startswith("claim:") for k in preview["missing"]))
        self.assertEqual(self.send(body, preview).status_code, 409)
        self.assertFalse(self.online.requests)

    def test_revocation_blocks_history_summary_and_background_derivatives(self):
        self.seed(protected=True)
        self.enable(True)
        batch = self.generate(localOnly=True)["batch"]
        body, preview = self.preview(batch["candidates"][0]["text"], replyAssistance=self.origin(batch), localOnly=True)
        self.send(body, preview)
        self.store.revoke("global")
        body, preview = self.preview("继续刚才那件事")
        self.assertTrue(preview["missing"])
        self.assertEqual(self.send(body, preview).status_code, 409)
        job = {"kind": "summarize_conversation", "payload": {"conversationId": self.cid}}
        result = jobs.run_job(job, store=self.onto, conv_store=self.convs)
        self.assertEqual(result["state"], "paused")
        self.assertFalse(self.online.requests)

    def test_source_version_changed_and_service_changed(self):
        self.seed(protected=True)
        batch = self.generate()["batch"]
        claim_id = next(s["id"] for s in batch["sources"] if s["kind"] == "claim")
        self.onto.add_evidence(claim_id, [{"kind": "user_edit", "quote": "新的证据"}])
        res = self.client.post(self.url + "/routing/preview", json={"content": "选择候选", "replyAssistance": self.origin(batch)})
        self.assertEqual(res.status_code, 409)
        self.assertIsNone(self.client.get(self.url + "/reply-assistance").json()["batch"])

    def test_invalid_or_empty_candidates_do_not_invent_choices(self):
        self.seed()
        cases = [[], [{"text": "a"}], [{"text": "重复"}, {"text": "重复"}], [{"text": "最推荐这个"}, {"text": "不确定"}], [{"text": "字" * 61}, {"text": "不确定"}]]
        for i, candidates in enumerate(cases):
            self.local.result = {"candidates": candidates}
            res = self.client.post(self.url + "/reply-assistance", json={"messageId": self.target["id"], "requestId": f"invalid-test-{i}"})
            self.assertEqual(res.status_code, 200 if not candidates else 502, res.text)
        self.assertEqual(len(self.convs.list_messages(self.cid)), 1)

    def test_generation_failure_never_switches_model(self):
        self.seed()
        self.local.error = ProviderError("synthetic timeout")
        res = self.client.post(self.url + "/reply-assistance", json={"messageId": self.target["id"], "requestId": "failure-test"})
        self.assertEqual(res.status_code, 503)
        self.assertFalse(self.online.requests)
        self.assertIsNone(ReplyAssistStore(self.convs).latest(self.cid))

    def test_request_explicitly_drafts_user_answer_not_assistant_continuation(self):
        req = build_request([{"role": "user", "content": "我是小林，一名独立开发者。"},
                             {"role": "assistant", "content": "好，先用这一句介绍你。"}])
        self.assertEqual(len(req.messages), 1)
        self.assertEqual(req.messages[-1]["role"], "user")
        self.assertIn('"说话人": "知君"', req.messages[0]["content"])
        self.assertIn("不要替知君继续提问", req.messages[0]["content"])
        self.assertIn("每个候选里的「我」都是用户", req.system)

    def test_concrete_options_not_replaced_by_canned_uncertainty(self):
        self.seed()
        expected = [c["text"] for c in self.local.result["candidates"]]
        batch = self.generate()["batch"]
        self.assertEqual([c["text"] for c in batch["candidates"]], expected)
        self.assertEqual(batch["formatVersion"], FORMAT_VERSION)

    def test_assistant_voice_and_rephrased_questions_are_rejected(self):
        bad = ["那这一栏我先填上这句。你希望它只保留这一句，还是以后可以再加别的？",
               "明白了。要不要我帮你把这句整理成更正式一点的版本？",
               "我帮你整理成三种方向。", "我想先把这件事的限制条件说清楚。", "你可以先从工作身份说起。"]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                candidate_texts({"candidates": [{"text": text}, {"text": "这句只说明我的工作，还不能完整概括我。"}]})
        self.seed()
        self.local.result = {"candidates": [{"text": bad[1]}, {"text": "先保留这一句就好。"}]}
        response = self.client.post(self.url + "/reply-assistance", json={"messageId": self.target["id"], "requestId": "wrong-speaker"})
        self.assertEqual(response.status_code, 502)
        self.assertIsNone(ReplyAssistStore(self.convs).latest(self.cid))

    def test_old_format_hidden_but_lineage_and_drafts_are_not_deleted(self):
        self.seed()
        batch = self.generate()["batch"]
        batch.pop("formatVersion")
        with self.convs._connect() as db:
            db.execute("UPDATE reply_assist_batches SET payload_json=? WHERE id=?", (json.dumps(batch), batch["id"]))
        self.assertIsNone(self.client.get(self.url + "/reply-assistance").json()["batch"])
        self.assertIsNotNone(ReplyAssistStore(self.convs).get(batch["id"]))
        body, preview = self.preview(batch["candidates"][0]["text"], replyAssistance=self.origin(batch))
        self.assertEqual(self.send(body, preview).status_code, 200)

    def test_controls_are_not_extracted_or_used_for_calibration(self):
        self.seed()
        origin = {"messageId": self.target["id"], "selections": [], "control": "pause"}
        body, preview = self.preview(CONTROLS["pause"], replyAssistance=origin)
        with patch("mindos.zhijun.jobs.extraction_enabled", return_value=True):
            res = self.send(body, preview)
        self.assertNotIn('"state": "queued"', res.text)
        user = [m for m in self.convs.list_messages(self.cid) if m["role"] == "user"][0]
        result = jobs._run_job({"kind": "extract_turn", "payload": {"conversationId": self.cid, "messageId": user["id"]}}, store=self.onto, conv_store=self.convs, managed=True)
        self.assertEqual(result["reason"], "conversation_control")
        self.assertEqual(jobs._extractive_summary([user]), ("", []))

    def test_assisted_claim_never_auto_confirms_or_adds_repeated_evidence(self):
        c = extract.ValidatedClaim(section="ways", layer="self_declared", predicate="prefers", subject="me", object=None,
            content="我喜欢先梳理事实", quote="我喜欢先梳理事实", confidence=.99, scope="long_term", privacy_level="private")
        origin = {"kind": "assisted", "evidenceKeys": ["same-option"], "edited": True}
        first = extract.persist([c], [], store=self.onto, conversation_id=self.cid, message_id="synthetic-input", input_origin=origin)
        claim = self.onto.get_claim(first["created"][0])
        self.assertEqual(claim["trustState"], "working")
        self.assertIsNone(claim["selfAlignment"]["level"])
        self.assertTrue(claim["evidence"][0]["locator"]["replyAssistance"]["edited"])
        second = extract.persist([c], [], store=self.onto, conversation_id=self.cid, message_id="repeated-input", input_origin=origin)
        self.assertFalse(second["promoted"])
        self.assertFalse(second["reaffirmed"])
        self.assertEqual(len(self.onto.get_claim(claim["id"])["evidence"]), 1)
        user = self.convs.append_message(self.cid, "user", c.quote, message_id="synthetic-input", meta={"replyAssistance": origin})
        self.assertEqual(alignment.evidence_for(self.onto.get_claim(claim["id"]), self.convs, "global"), [])
        self.assertTrue(user)

    def test_import_preserves_assisted_ancestry_without_sending(self):
        from mindos.stores.chat_import_store import ChatImportStore
        self.seed()
        batch = self.generate()["batch"]
        router = Router(self.onto, self.convs, self.cid)
        from mindos.zhijun.reply_assistance import resolve_input
        origin, refs = resolve_input(router, self.origin(batch), batch["candidates"][0]["text"])
        imports = ChatImportStore(self.convs)
        imported = imports.create(self.cid, "synthetic-import-id", "选择的文字", [{"id": "synthetic-file-id", "name": "演示.txt", "size": 20}], input_meta={"replyAssistance": origin, "routingSources": refs})
        message = self.convs.get_message(imported["message_id"])
        self.assertEqual(message["meta"]["routingSources"], refs)
        self.assertEqual(message["meta"]["replyAssistance"]["kind"], "assisted")

    def test_onboarding_controls_do_not_count_as_answers(self):
        from mindos.zhijun import persona
        from mindos.zhijun.turn import _onboarding_turn_number
        from mindos.zhijun.routing import prepare_chat
        cid = self.convs.create_conversation(mode="onboarding")["id"]
        self.convs.append_message(cid, "assistant", "怎么称呼你？", meta={"kind": "onboarding_open"})
        self.convs.append_message(cid, "user", CONTROLS["rephrase"], meta={"replyAssistance": {"kind": "control"}})
        target = self.convs.append_message(cid, "assistant", "你想让我叫你什么？")
        self.assertEqual(persona.onboarding_answer_count(self.convs.list_messages(cid)), 0)
        self.assertEqual(_onboarding_turn_number(self.convs, cid, 1), 1)
        plan = prepare_chat(Router(self.onto, self.convs, cid, provider=self.local), CONTROLS["rephrase"],
                            reply_assistance={"messageId": target["id"], "selections": [], "control": "rephrase"})
        self.assertIn("对话操作，不是回答", plan.preview["request"]["system"])
        self.assertNotIn("本轮先用一句话确认你听到了什么", plan.preview["request"]["system"])

    def test_retry_after_online_failure_preserves_origin_and_rechecks_permissions(self):
        self.seed(protected=True)
        self.enable(True)
        batch = self.generate(localOnly=True)["batch"]
        body, preview = self.preview(batch["candidates"][0]["text"], replyAssistance=self.origin(batch))
        self.grant(preview)
        body, preview = self.preview(body["content"], replyAssistance=self.origin(batch))
        self.online.error = ProviderError("synthetic failure")
        response = self.send(body, preview, requestId="assisted-failure")
        self.assertIn("event: error", response.text)
        user = [m for m in self.convs.list_messages(self.cid) if m["role"] == "user"][0]
        self.store.revoke("global")
        self.online.error = None
        count = len(self.online.requests)
        body, preview = self.preview(user["content"], retryUserMessageId=user["id"], omitSources=True)
        self.assertTrue(preview["missing"])
        self.assertEqual(self.send(body, preview).status_code, 409)
        self.assertEqual(len(self.online.requests), count)
        self.assertEqual(self.convs.get_message(user["id"])["meta"]["replyAssistance"]["kind"], "assisted")

    def test_actual_http_boundary_rechecks_revoked_consent(self):
        from mindos.zhijun.routing import EGRESS_PERMIT
        from fastapi import HTTPException
        self.seed(protected=True)
        self.enable(True)
        preview = self.generate(previewOnly=True)["routePreview"]
        self.grant(preview)
        preview = self.generate(previewOnly=True)["routePreview"]
        def network_boundary(req):
            self.store.revoke("global")
            permit = EGRESS_PERMIT.get()
            self.assertTrue(callable(permit))
            with self.assertRaises(HTTPException):
                permit()
            raise HTTPException(409, {"code": "REVOKED_AT_HTTP", "detail": "revoked"})
        with patch.object(self.online, "complete_json", network_boundary):
            response = self.client.post(self.url + "/reply-assistance", json={"messageId": self.target["id"], "requestId": "at-http-boundary", "routeRevision": preview["revision"]})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.online.requests)
        self.assertIsNone(ReplyAssistStore(self.convs).latest(self.cid))

    def test_generation_does_not_hold_conversation_lock(self):
        from mindos.zhijun.gate import conversation_locks
        self.seed()
        original = self.local.complete_json
        def generation(req):
            self.assertTrue(conversation_locks.acquire(self.cid))
            conversation_locks.release(self.cid)
            return original(req)
        with patch.object(self.local, "complete_json", generation):
            self.generate()

    def test_local_mode_change_during_generation_discards_old_result(self):
        self.seed()
        original = self.local.complete_json
        def generation(req):
            self.store.set_mode(self.cid, "local", "")
            return original(req)
        with patch.object(self.local, "complete_json", generation):
            res = self.client.post(self.url + "/reply-assistance", json={"messageId": self.target["id"], "requestId": "changed-mode"})
        self.assertEqual(res.status_code, 409)
        self.assertIsNone(ReplyAssistStore(self.convs).latest(self.cid))
